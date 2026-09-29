from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from social_crawler.domain.models import CollectionError

SNAPSHOT_FILENAME = ".social-crawler-environment.json"
SNAPSHOT_SCHEMA_VERSION = 1


def environment_snapshot_path(profile_dir: str | Path) -> Path:
    return Path(profile_dir) / SNAPSHOT_FILENAME


def write_environment_snapshot(profile_dir: str | Path, snapshot: dict[str, Any]) -> Path:
    path = environment_snapshot_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return path


def load_environment_snapshot(profile_dir: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    path = environment_snapshot_path(profile_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeError):
        return None, "invalid"
    if not isinstance(value, dict) or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None, "unsupported_schema"
    return value, None


def browser_major(value: str | None) -> int | None:
    found = re.search(r"(?:Chrome|Chromium|HeadlessChrome)/(\d+)", value or "")
    if found:
        return int(found.group(1))
    found = re.search(r"(?:chrome|chromium|edge)(\d+)", value or "", re.IGNORECASE)
    if found:
        return int(found.group(1))
    found = re.match(r"\s*(\d+)", value or "")
    return int(found.group(1)) if found else None


async def capture_browser_environment(
    page,
    *,
    browser_version: str | None,
    headless: bool,
    browser_channel: str | None,
    source: str,
) -> dict[str, Any]:
    observed = await page.evaluate(
        """async () => {
            let uaData = null;
            try {
                if (navigator.userAgentData) {
                    uaData = await navigator.userAgentData.getHighEntropyValues([
                        'architecture', 'bitness', 'fullVersionList', 'model',
                        'platformVersion', 'wow64'
                    ]);
                }
            } catch (_) {}
            let webgl = null;
            try {
                const canvas = document.createElement('canvas');
                const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                const extension = gl && gl.getExtension('WEBGL_debug_renderer_info');
                if (extension) {
                    webgl = {
                        vendor: String(gl.getParameter(extension.UNMASKED_VENDOR_WEBGL) || ''),
                        renderer: String(gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) || '')
                    };
                }
            } catch (_) {}
            const resolved = Intl.DateTimeFormat().resolvedOptions();
            return {
                navigator: {
                    user_agent: navigator.userAgent || '',
                    platform: navigator.platform || '',
                    language: navigator.language || '',
                    languages: Array.from(navigator.languages || []),
                    hardware_concurrency: navigator.hardwareConcurrency || null,
                    device_memory: navigator.deviceMemory || null,
                    webdriver: navigator.webdriver,
                    user_agent_data: uaData
                },
                timezone: resolved.timeZone || '',
                screen: {
                    width: screen.width,
                    height: screen.height,
                    avail_width: screen.availWidth,
                    avail_height: screen.availHeight,
                    color_depth: screen.colorDepth,
                    pixel_depth: screen.pixelDepth,
                    device_pixel_ratio: window.devicePixelRatio
                },
                webgl
            };
        }"""
    )
    version = str(browser_version or "")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": time.time(),
        "source": source,
        "browser": {
            "version": version,
            "major": browser_major(version) or browser_major(
                (observed.get("navigator") or {}).get("user_agent")
            ),
            "channel": browser_channel or "chromium",
            "headless": bool(headless),
        },
        **observed,
    }


def effective_user_agent(configured: str | None, snapshot: dict[str, Any] | None, fallback: str):
    if configured:
        return configured
    navigator = (snapshot or {}).get("navigator") or {}
    return str(navigator.get("user_agent") or fallback)


def browser_identity_headers(snapshot: dict[str, Any] | None) -> dict[str, str]:
    navigator = (snapshot or {}).get("navigator") or {}
    data = navigator.get("user_agent_data") or {}
    headers: dict[str, str] = {}
    brands = data.get("brands")
    if isinstance(brands, list):
        rendered = []
        for brand in brands:
            if not isinstance(brand, dict) or not brand.get("brand") or not brand.get("version"):
                continue
            name = str(brand["brand"]).replace('"', "")
            version = str(brand["version"]).replace('"', "")
            rendered.append(f'"{name}";v="{version}"')
        if rendered:
            headers["sec-ch-ua"] = ", ".join(rendered)
    if platform := data.get("platform"):
        headers["sec-ch-ua-platform"] = f'"{str(platform).replace(chr(34), "")}"'
    mobile = data.get("mobile")
    if isinstance(mobile, bool):
        headers["sec-ch-ua-mobile"] = "?1" if mobile else "?0"
    languages = navigator.get("languages")
    if isinstance(languages, list) and languages:
        clean_languages = [str(value).strip() for value in languages if str(value).strip()]
        if clean_languages:
            headers["accept-language"] = ",".join(
                value if index == 0 else f"{value};q={max(0.1, 1 - index / 10):.1f}"
                for index, value in enumerate(clean_languages[:9])
            )
    return headers


def _platform_from_user_agent(user_agent: str) -> tuple[str | None, str | None]:
    if "Windows" in user_agent:
        return "Win32", "Windows"
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "MacIntel", "macOS"
    if "Linux" in user_agent:
        return "Linux x86_64", "Linux"
    return None, None


def http_environment_observation(
    user_agent: str,
    *,
    transport: str,
    impersonate: str | None = None,
) -> dict[str, Any]:
    platform, ua_platform = _platform_from_user_agent(user_agent)
    major = browser_major(user_agent)
    transport_data: dict[str, Any] = {
        "name": transport,
        "coverage": "request_identity_only",
    }
    if impersonate:
        target = impersonate
        try:
            from curl_cffi.requests.impersonate import resolve_latest_browser_type

            target = str(resolve_latest_browser_type(impersonate))
        except (ImportError, TypeError, ValueError):
            pass
        transport_data.update(
            impersonate=impersonate,
            resolved_impersonate=target,
            impersonated_browser_major=browser_major(target),
        )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": time.time(),
        "source": "http_runtime",
        "browser": {"major": major},
        "navigator": {
            "user_agent": user_agent,
            "platform": platform,
            "user_agent_data": {"platform": ua_platform} if ua_platform else {},
        },
        "transport": transport_data,
    }


def _nested(value: dict[str, Any], path: str):
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


COMPARISON_FIELDS = {
    "browser.major": "medium",
    "browser.channel": "medium",
    "browser.headless": "medium",
    "navigator.user_agent": "medium",
    "navigator.platform": "high",
    "navigator.language": "medium",
    "navigator.webdriver": "medium",
    "navigator.user_agent_data.platform": "high",
    "navigator.user_agent_data.architecture": "high",
    "navigator.user_agent_data.bitness": "high",
    "timezone": "medium",
    "screen.width": "low",
    "screen.height": "low",
    "screen.device_pixel_ratio": "low",
    "navigator.hardware_concurrency": "low",
    "navigator.device_memory": "low",
    "webgl.vendor": "medium",
    "webgl.renderer": "medium",
}
SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def evaluate_environment_consistency(
    reference: dict[str, Any] | None,
    observed: dict[str, Any],
    *,
    policy: str,
) -> dict[str, Any]:
    if policy == "off":
        return {
            "policy": policy,
            "status": "off",
            "allowed": True,
            "max_severity": "none",
            "differences": [],
        }
    if reference is None:
        return {
            "policy": policy,
            "status": "missing_reference",
            "allowed": policy != "strict",
            "max_severity": "high",
            "differences": [],
        }
    differences = []
    max_severity = "none"
    for path, severity in COMPARISON_FIELDS.items():
        expected = _nested(reference, path)
        actual = _nested(observed, path)
        # HTTP observations intentionally contain only request-visible identity.
        if expected is None or actual is None or expected == actual:
            continue
        differences.append(
            {"field": path, "severity": severity, "expected": expected, "observed": actual}
        )
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[max_severity]:
            max_severity = severity

    transport = observed.get("transport") or {}
    impersonated_major = transport.get("impersonated_browser_major")
    reported_major = _nested(observed, "browser.major")
    if impersonated_major and reported_major and impersonated_major != reported_major:
        differences.append(
            {
                "field": "transport.impersonated_browser_major",
                "severity": "medium",
                "expected": reported_major,
                "observed": impersonated_major,
            }
        )
        if SEVERITY_ORDER["medium"] > SEVERITY_ORDER[max_severity]:
            max_severity = "medium"

    return {
        "policy": policy,
        "status": "drift" if differences else "match",
        "allowed": not (policy == "strict" and max_severity == "high"),
        "max_severity": max_severity,
        "differences": differences,
    }


def audit_environment(session, observed: dict[str, Any], *, budget=None, stage: str):
    report = evaluate_environment_consistency(
        session.environment_snapshot,
        observed,
        policy=session.config.consistency_policy,
    )
    report.update(
        stage=stage,
        reference_source=(session.environment_snapshot or {}).get("source"),
        reference_captured_at=(session.environment_snapshot or {}).get("captured_at"),
        observed_source=observed.get("source"),
        transport=(observed.get("transport") or {}).get("name"),
    )
    if budget is not None and hasattr(budget, "store"):
        budget.store.event(budget.run_id, "environment_consistency_checked", report)
    if report["allowed"]:
        return report
    if report["status"] == "missing_reference":
        raise CollectionError(
            "environment_snapshot_missing",
            "Strict environment consistency requires a login snapshot",
        )
    raise CollectionError(
        "environment_mismatch",
        "Strict environment consistency rejected a high-severity identity drift",
    )
