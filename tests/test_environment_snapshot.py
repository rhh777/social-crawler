import json
import stat
from types import SimpleNamespace

import pytest

from social_crawler.domain.models import CollectionError
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    browser_identity_headers,
    browser_major,
    capture_browser_environment,
    effective_user_agent,
    evaluate_environment_consistency,
    http_environment_observation,
    load_environment_snapshot,
    write_environment_snapshot,
)


def snapshot(*, platform="Linux x86_64", major=151):
    return {
        "schema_version": 1,
        "captured_at": 1.0,
        "source": "login",
        "browser": {"major": major, "channel": "chromium", "headless": False},
        "navigator": {
            "user_agent": f"Mozilla/5.0 (X11; Linux x86_64) Chrome/{major}.0.0.0",
            "platform": platform,
            "language": "zh-CN",
            "languages": ["zh-CN", "zh"],
            "webdriver": True,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "user_agent_data": {
                "brands": [
                    {"brand": "Chromium", "version": str(major)},
                    {"brand": "Not_A Brand", "version": "99"},
                ],
                "platform": "Linux",
                "architecture": "x86",
                "bitness": "64",
                "mobile": False,
            },
        },
        "timezone": "Asia/Shanghai",
        "screen": {"width": 1440, "height": 1000, "device_pixel_ratio": 1},
        "webgl": {"vendor": "Google Inc.", "renderer": "SwiftShader"},
    }


def test_snapshot_round_trip_is_private_and_invalid_files_fail_closed(tmp_path):
    profile = tmp_path / "profile"
    path = write_environment_snapshot(profile, snapshot())

    restored, error = load_environment_snapshot(profile)
    assert error is None and restored == snapshot()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700

    path.write_text("not-json")
    assert load_environment_snapshot(profile) == (None, "invalid")


@pytest.mark.asyncio
async def test_browser_capture_records_runtime_identity():
    class BrowserPage:
        async def evaluate(self, script):
            assert "userAgentData" in script
            reference = snapshot()
            return {
                "navigator": reference["navigator"],
                "timezone": reference["timezone"],
                "screen": reference["screen"],
                "webgl": reference["webgl"],
            }

    observed = await capture_browser_environment(
        BrowserPage(),
        browser_version="151.0.7390.0",
        headless=False,
        browser_channel=None,
        source="test",
    )
    assert observed["browser"] == {
        "version": "151.0.7390.0",
        "major": 151,
        "channel": "chromium",
        "headless": False,
    }
    assert observed["navigator"]["platform"] == "Linux x86_64"


def test_http_identity_uses_snapshot_headers_and_configured_ua_precedence():
    reference = snapshot()
    headers = browser_identity_headers(reference)

    assert effective_user_agent(None, reference, "fallback") == reference["navigator"][
        "user_agent"
    ]
    assert effective_user_agent("configured", reference, "fallback") == "configured"
    assert headers == {
        "sec-ch-ua": '"Chromium";v="151", "Not_A Brand";v="99"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-ch-ua-mobile": "?0",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    assert browser_major("chrome150") == 150


def test_consistency_policy_only_blocks_high_drift_in_strict_mode():
    reference = snapshot()
    version_drift = snapshot(major=152)
    version_report = evaluate_environment_consistency(
        reference, version_drift, policy="strict"
    )
    assert version_report["max_severity"] == "medium"
    assert version_report["allowed"] is True

    platform_drift = snapshot(platform="Win32")
    platform_report = evaluate_environment_consistency(
        reference, platform_drift, policy="strict"
    )
    assert platform_report["max_severity"] == "high"
    assert platform_report["allowed"] is False

    assert evaluate_environment_consistency(reference, platform_drift, policy="warn")[
        "allowed"
    ]
    assert evaluate_environment_consistency(reference, platform_drift, policy="off")[
        "status"
    ] == "off"


def test_http_observation_and_audit_event_are_explicit_about_limited_coverage():
    reference = snapshot()
    observed = http_environment_observation(
        reference["navigator"]["user_agent"],
        transport="curl_cffi",
        impersonate="chrome150",
    )
    report = evaluate_environment_consistency(reference, observed, policy="warn")
    assert observed["transport"]["coverage"] == "request_identity_only"
    assert observed["transport"]["impersonated_browser_major"] == 150
    assert any(
        difference["field"] == "transport.impersonated_browser_major"
        for difference in report["differences"]
    )

    events = []
    store = SimpleNamespace(event=lambda *values: events.append(values))
    budget = SimpleNamespace(store=store, run_id="run-1")
    session = SimpleNamespace(
        config=SimpleNamespace(consistency_policy="warn"),
        environment_snapshot=reference,
    )
    audit_environment(session, observed, budget=budget, stage="test")
    assert events[0][0:2] == ("run-1", "environment_consistency_checked")


def test_strict_policy_rejects_missing_snapshot():
    session = SimpleNamespace(
        config=SimpleNamespace(consistency_policy="strict"),
        environment_snapshot=None,
    )
    with pytest.raises(CollectionError) as found:
        audit_environment(
            session,
            json.loads(json.dumps(snapshot())),
            stage="test",
        )
    assert found.value.kind == "environment_snapshot_missing"
