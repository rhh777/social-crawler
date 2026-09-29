"""Run the bounded Web-console smoke matrix against real platform accounts.

This script is deliberately separate from the default pytest suite. It creates
real online runs and therefore requires an explicit confirmation flag.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokeCase:
    name: str
    platform: str
    account_role: str
    adapter: str | None = None
    headless: bool | None = None
    comments: bool = False


SMOKE_CASES = (
    SmokeCase("douyin-headed", "douyin", "douyin", headless=False),
    SmokeCase(
        "douyin-headless-comments",
        "douyin",
        "douyin",
        headless=True,
        comments=True,
    ),
    SmokeCase("xhs-httpx", "xhs", "xhs_http", adapter="httpx"),
    SmokeCase("xhs-curl-cffi", "xhs", "xhs_http", adapter="curl_cffi"),
    SmokeCase(
        "xhs-chromium-headed",
        "xhs",
        "xhs_chromium",
        adapter="browser",
        headless=False,
    ),
    SmokeCase(
        "xhs-chromium-headless-comments",
        "xhs",
        "xhs_chromium",
        adapter="browser",
        headless=True,
        comments=True,
    ),
    SmokeCase(
        "xhs-adspower-headed",
        "xhs",
        "xhs_adspower",
        adapter="browser",
        headless=False,
    ),
    SmokeCase(
        "xhs-adspower-headless",
        "xhs",
        "xhs_adspower",
        adapter="browser",
        headless=True,
    ),
)


class ConsoleClient:
    def __init__(self, base_url: str, timeout: float = 30):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SmokeFailure("Smoke test only accepts a loopback HTTP console URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            if not self.token:
                raise SmokeFailure("Console token has not been loaded")
            headers["X-Console-Token"] = self.token
        request = Request(
            self.base_url + path,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", "request failed")
            except (json.JSONDecodeError, AttributeError):
                detail = "request failed"
            raise SmokeFailure(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise SmokeFailure(f"{method} {path}: console unavailable: {exc}") from exc

    def bootstrap(self):
        result = self.request("GET", "/api/bootstrap")
        self.token = result["token"]
        return result


def build_config(case: SmokeCase, keyword: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "platform": case.platform,
        "source_type": "keyword",
        "keywords": [keyword],
        "post_targets": [],
        "sort": "general" if case.comments else "latest",
        "content_limit": 1,
        "comment_limit": 1 if case.comments else 0,
        "reply_parents": 0,
        "reply_limit": 0,
        "max_requests": 10 if case.comments else 8,
        "max_seconds": 240,
        "max_pages": 2,
        "min_interval": 1,
        "request_timeout": 30,
        "network_retries": 0,
        "download_media": False,
    }
    if case.adapter is not None:
        config["adapter"] = case.adapter
    if case.headless is not None:
        config["headless"] = case.headless
    return config


def _select_account(
    accounts: list[dict[str, Any]],
    *,
    platform: str,
    provider: str,
    requested_id: str | None,
) -> dict[str, Any]:
    candidates = [
        account
        for account in accounts
        if account.get("platform") == platform
        and account.get("status") == "ready"
        and account.get("browser_provider", "chromium") == provider
    ]
    if requested_id:
        candidates = [account for account in candidates if account.get("id") == requested_id]
    if not candidates:
        requested = f" id={requested_id}" if requested_id else ""
        raise SmokeFailure(f"No ready {platform}/{provider} account{requested}")
    return candidates[0]


def select_accounts(settings: dict[str, Any], overrides: dict[str, str | None]) -> dict[str, str]:
    accounts = settings.get("account_pool") or []
    selected = {
        "douyin": _select_account(
            accounts,
            platform="douyin",
            provider="chromium",
            requested_id=overrides.get("douyin"),
        )["id"],
        "xhs_chromium": _select_account(
            accounts,
            platform="xhs",
            provider="chromium",
            requested_id=overrides.get("xhs_chromium"),
        )["id"],
        "xhs_adspower": _select_account(
            accounts,
            platform="xhs",
            provider="adspower",
            requested_id=overrides.get("xhs_adspower"),
        )["id"],
    }
    selected["xhs_http"] = overrides.get("xhs_http") or selected["xhs_chromium"]
    if overrides.get("xhs_http"):
        _select_account(
            accounts,
            platform="xhs",
            provider="chromium",
            requested_id=selected["xhs_http"],
        )
    return selected


def validate_network_observations(case: SmokeCase, observations: list[dict[str, Any]]) -> str:
    if not observations or any(row.get("outcome") != "success" for row in observations):
        raise SmokeFailure(f"{case.name}: successful egress observation was not recorded")

    if case.account_role == "xhs_adspower":
        if any(
            not row.get("browser_egress_ip")
            or row.get("http_egress_ip") is not None
            or (row.get("data") or {}).get("transport") != "adspower_profile"
            for row in observations
        ):
            raise SmokeFailure(f"{case.name}: AdsPower profile browser egress was not confirmed")
        return "adspower_profile"

    if case.adapter in {"httpx", "curl_cffi"}:
        if any(
            not row.get("http_egress_ip")
            or row.get("browser_egress_ip") is not None
            or (row.get("data") or {}).get("transport") != "http"
            for row in observations
        ):
            raise SmokeFailure(f"{case.name}: HTTP adapter egress was not confirmed")
        return "http"

    if any(
        not row.get("http_egress_ip")
        or not row.get("browser_egress_ip")
        or row.get("http_egress_ip") != row.get("browser_egress_ip")
        for row in observations
    ):
        raise SmokeFailure(f"{case.name}: HTTP/browser egress was not confirmed")
    return "dual"


def validate_run(case: SmokeCase, snapshot: dict[str, Any]) -> dict[str, Any]:
    run = snapshot["run"]
    if run["status"] != "completed":
        raise SmokeFailure(f"{case.name}: run ended as {run['status']}")
    if not 0 < run["requests"] <= build_config(case, "smoke")["max_requests"]:
        raise SmokeFailure(f"{case.name}: request count is outside the smoke budget")
    tasks = {task["operation"]: task for task in snapshot.get("tasks", [])}
    for operation in ("search", "detail", "comments"):
        if operation not in tasks or tasks[operation]["status"] != "completed":
            raise SmokeFailure(f"{case.name}: {operation} task did not complete")
    if tasks["search"]["count"] < 1 or tasks["detail"]["count"] < 1:
        raise SmokeFailure(f"{case.name}: search/detail did not persist an item")
    if case.comments and tasks["comments"].get("stop_reason") not in {"limit", "exhausted"}:
        raise SmokeFailure(f"{case.name}: comment request did not reach a valid terminal state")
    if not case.comments and tasks["comments"].get("stop_reason") != "not_requested":
        raise SmokeFailure(f"{case.name}: comments exceeded the configured scope")
    if snapshot.get("risk_events"):
        raise SmokeFailure(f"{case.name}: risk events were recorded")
    observations = snapshot.get("network_observations") or []
    network_mode = validate_network_observations(case, observations)
    return {
        "name": case.name,
        "run_id": run["id"],
        "status": run["status"],
        "requests": run["requests"],
        "elapsed_seconds": round(run["elapsed"], 3),
        "content_count": tasks["detail"]["count"],
        "comment_count": tasks["comments"]["count"],
        "network_mode": network_mode,
    }


def wait_for_run(
    client: ConsoleClient,
    case: SmokeCase,
    run_id: str,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        snapshot = client.request("GET", f"/api/runs/{run_id}")
        run = snapshot["run"]
        state = (run["status"], run["requests"])
        if state != previous:
            print(f"[{case.name}] status={state[0]} requests={state[1]}", flush=True)
            previous = state
        if run["status"] not in {"pending", "running"}:
            return snapshot
        time.sleep(poll_interval)
    try:
        client.request("POST", f"/api/runs/{run_id}/cancel", {})
    except SmokeFailure:
        pass
    raise SmokeFailure(f"{case.name}: timed out after {timeout:g} seconds; cancel requested")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Run the bounded real-platform smoke matrix through the local Web console"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--keyword", default="咖啡")
    parser.add_argument("--timeout-per-case", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--output", default="data/live-smoke-latest.json")
    parser.add_argument("--douyin-account-id")
    parser.add_argument("--xhs-chromium-account-id")
    parser.add_argument("--xhs-adspower-account-id")
    parser.add_argument("--xhs-http-account-id")
    parser.add_argument(
        "--confirm-online",
        action="store_true",
        help="Required acknowledgement that eight bounded runs will access real platforms",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.confirm_online:
        print("Refusing real platform traffic without --confirm-online", file=sys.stderr)
        return 2
    if args.timeout_per_case <= 0 or args.poll_interval <= 0:
        print("Timeout and poll interval must be positive", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "cases": [],
        "status": "running",
    }
    output = Path(args.output)
    try:
        client = ConsoleClient(args.base_url)
        health = client.request("GET", "/api/health")
        if health.get("status") != "ok":
            raise SmokeFailure("Web console health check did not return ok")
        if health.get("queued") or health.get("running"):
            raise SmokeFailure("Web console must be idle before the smoke matrix starts")
        bootstrap = client.bootstrap()
        selected = select_accounts(
            bootstrap["settings"],
            {
                "douyin": args.douyin_account_id,
                "xhs_chromium": args.xhs_chromium_account_id,
                "xhs_adspower": args.xhs_adspower_account_id,
                "xhs_http": args.xhs_http_account_id,
            },
        )
        print(f"Running {len(SMOKE_CASES)} bounded online smoke cases", flush=True)
        for case in SMOKE_CASES:
            body = {
                "account_id": selected[case.account_role],
                "config": build_config(case, args.keyword),
            }
            created = client.request("POST", "/api/runs", body)
            run_id = created["run_id"]
            print(f"[{case.name}] created run_id={run_id}", flush=True)
            snapshot = wait_for_run(
                client,
                case,
                run_id,
                args.timeout_per_case,
                args.poll_interval,
            )
            report["cases"].append(validate_run(case, snapshot))
        final_health = client.request("GET", "/api/health")
        if final_health.get("queued") or final_health.get("running"):
            raise SmokeFailure("Web console was not idle after the smoke matrix")
        report["status"] = "passed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_report(output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Report: {output.resolve()}")
        return 0
    except (KeyError, TypeError, SmokeFailure) as exc:
        report["status"] = "failed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["error"] = str(exc)
        write_report(output, report)
        print(f"Smoke failed: {exc}", file=sys.stderr)
        print(f"Report: {output.resolve()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
