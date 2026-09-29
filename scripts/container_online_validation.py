"""Run one bounded, auditable platform validation from the final container image."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from social_crawler.config import load_config as load_environment_config
from social_crawler.domain.models import RunConfig

PLATFORM_URLS = {
    "xhs": "https://www.xiaohongshu.com/",
    "douyin": "https://www.douyin.com/",
}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def parse_args():
    load_environment_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(PLATFORM_URLS), required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--cookies", required=True)
    parser.add_argument(
        "--output-root", default="artifacts/container-online-validation"
    )
    parser.add_argument("--human-review-confirmed", action="store_true")
    return parser.parse_args()


def network_precheck(platform: str) -> dict:
    parsed = urlsplit(PLATFORM_URLS[platform])
    started = time.monotonic()
    addresses = sorted(
        {row[4][0] for row in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    )
    with socket.create_connection((parsed.hostname, 443), timeout=10) as raw:
        with ssl.create_default_context().wrap_socket(
            raw, server_hostname=parsed.hostname
        ) as tls:
            tls_version = tls.version()
            certificate = tls.getpeercert()
    return {
        "dns_addresses": addresses,
        "tcp": "connected",
        "tls_version": tls_version,
        "certificate_not_after": certificate.get("notAfter"),
        "system_utc": datetime.now(UTC).isoformat(),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "business_requests": 0,
    }


def load_config(path: Path) -> RunConfig:
    import tomllib

    data = tomllib.loads(path.read_text())
    config = RunConfig.model_validate(data["run"])
    if (
        config.content_limit > 1
        or config.max_requests > 8
        or config.max_pages > 1
        or config.network_retries
    ):
        raise ValueError(
            "Container canary requires one page, content_limit <= 1, max_requests <= 8, and no retries"
        )
    return config


def canary_readiness(output_root: Path, platform: str) -> dict:
    attempts = []
    for directory in sorted(output_root.glob(f"*/{platform}")):
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
            report = json.loads((directory / "report.json").read_text())
            risks = json.loads((directory / "risk-events.json").read_text())
            observations = json.loads(
                (directory / "network-observation.json").read_text()
            )
            review = json.loads((directory / "human-review.json").read_text())
            successful_network = bool(observations) and all(
                row.get("outcome") == "success"
                and row.get("http_egress_ip") == row.get("browser_egress_ip")
                for row in observations
            )
            passed = (
                report.get("status") == "completed"
                and report.get("automated_scope_gate") is True
                and manifest.get("image_digest") not in {None, "", "unavailable"}
                and not risks
                and successful_network
                and review.get("confirmed") is True
            )
            attempts.append(
                {
                    "directory": str(directory),
                    "at": datetime.fromisoformat(manifest["validated_at"]).timestamp(),
                    "passed": passed,
                }
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            attempts.append({"directory": str(directory), "at": 0, "passed": False})
    latest = attempts[-3:]
    span = latest[-1]["at"] - latest[0]["at"] if len(latest) == 3 else 0
    return {
        "platform": platform,
        "eligible": len(latest) == 3 and all(row["passed"] for row in latest) and span >= 86400,
        "consecutive_passes": sum(row["passed"] for row in latest),
        "observed_span_seconds": max(0, span),
        "required_passes": 3,
        "required_span_seconds": 86400,
        "latest_attempts": latest,
    }


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    cookie_path = Path(args.cookies).resolve()
    config = load_config(config_path)
    if config.platform != args.platform:
        raise ValueError("Config platform does not match --platform")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = Path(args.output_root).resolve() / stamp
    temporary_output = base / ".runs"
    destination = base / args.platform
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    precheck = network_precheck(args.platform)

    command = [
        "social-crawler",
        "--output",
        str(temporary_output),
        "run",
        "--config",
        str(config_path),
        "--cookies",
        str(cookie_path),
        "--online",
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        report = {"status": "failed", "error": "invalid_cli_report"}
    run_id = report.get("run_id")
    source = temporary_output / str(run_id)
    if run_id and source.is_dir():
        for artifact in source.iterdir():
            shutil.move(str(artifact), destination / artifact.name)
    shutil.rmtree(temporary_output, ignore_errors=True)

    manifest = {
        "run_id": run_id,
        "platform": args.platform,
        "validated_at": datetime.now(UTC).isoformat(),
        "image_digest": os.environ.get("CRAWLER_IMAGE_DIGEST", "unavailable"),
        "container_hostname": socket.gethostname(),
        "config": str(config_path),
        "network_precheck": precheck,
        "exit_code": completed.returncode,
    }
    write_json(destination / "manifest.json", manifest)
    write_json(
        destination / "human-review.json",
        {
            "confirmed": bool(args.human_review_confirmed),
            "required_samples": ["content", "comment", "reply"],
        },
    )
    # CLI and transport exception details are deliberately not copied. The
    # structured stderr line contains only run/output/mode; keep a minimal log.
    (destination / "sanitized-log.txt").write_text(
        f"exit_code={completed.returncode}\nrun_id={run_id or 'unknown'}\n"
    )
    (destination / "sanitized-log.txt").chmod(0o600)
    write_json(
        destination / "readiness.json",
        canary_readiness(Path(args.output_root).resolve(), args.platform),
    )
    print(json.dumps({"directory": str(destination), **manifest}, ensure_ascii=False))
    return 0 if completed.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
