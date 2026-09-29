import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("live_smoke", ROOT / "scripts/live_smoke.py")
live_smoke = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = live_smoke
SPEC.loader.exec_module(live_smoke)


def accounts():
    return {
        "account_pool": [
            {
                "id": "douyin-ready",
                "platform": "douyin",
                "status": "ready",
                "browser_provider": "chromium",
            },
            {
                "id": "xhs-disabled",
                "platform": "xhs",
                "status": "disabled",
                "browser_provider": "chromium",
            },
            {
                "id": "xhs-chromium",
                "platform": "xhs",
                "status": "ready",
                "browser_provider": "chromium",
            },
            {
                "id": "xhs-adspower",
                "platform": "xhs",
                "status": "ready",
                "browser_provider": "adspower",
            },
        ]
    }


def snapshot(case, *, comment_count=0, comment_reason=None, risks=None, observations=None):
    return {
        "run": {
            "id": "run-id",
            "status": "completed",
            "requests": 4,
            "elapsed": 1.25,
        },
        "tasks": [
            {"operation": "search", "status": "completed", "count": 1, "stop_reason": "limit"},
            {
                "operation": "detail",
                "status": "completed",
                "count": 1,
                "stop_reason": "detail_obtained",
            },
            {
                "operation": "comments",
                "status": "completed",
                "count": comment_count,
                "stop_reason": comment_reason
                or ("exhausted" if case.comments else "not_requested"),
            },
        ],
        "risk_events": risks or [],
        "network_observations": observations
        or [
            {
                "outcome": "success",
                "http_egress_ip": "192.0.2.10",
                "browser_egress_ip": "192.0.2.10",
            }
        ],
    }


def observation_for(case):
    if case.account_role == "xhs_adspower":
        return {
            "outcome": "success",
            "http_egress_ip": None,
            "browser_egress_ip": "192.0.2.10",
            "data": {"transport": "adspower_profile"},
        }
    if case.adapter in {"httpx", "curl_cffi"}:
        return {
            "outcome": "success",
            "http_egress_ip": "192.0.2.10",
            "browser_egress_ip": None,
            "data": {"transport": "http"},
        }
    return {
        "outcome": "success",
        "http_egress_ip": "192.0.2.10",
        "browser_egress_ip": "192.0.2.10",
        "data": {"transport": "dual"},
    }


def test_smoke_matrix_covers_requested_modes_with_bounded_configs():
    assert [case.name for case in live_smoke.SMOKE_CASES] == [
        "douyin-headed",
        "douyin-headless-comments",
        "xhs-httpx",
        "xhs-curl-cffi",
        "xhs-chromium-headed",
        "xhs-chromium-headless-comments",
        "xhs-adspower-headed",
        "xhs-adspower-headless",
    ]
    for case in live_smoke.SMOKE_CASES:
        config = live_smoke.build_config(case, "smoke")
        assert config["content_limit"] == 1
        assert config["comment_limit"] <= 1
        assert config["reply_parents"] == config["reply_limit"] == 0
        assert config["max_requests"] <= 10
        assert config["network_retries"] == 0
        assert config["download_media"] is False


def test_account_selection_requires_ready_provider_specific_accounts():
    selected = live_smoke.select_accounts(accounts(), {})
    assert selected == {
        "douyin": "douyin-ready",
        "xhs_chromium": "xhs-chromium",
        "xhs_adspower": "xhs-adspower",
        "xhs_http": "xhs-chromium",
    }
    with pytest.raises(live_smoke.SmokeFailure, match="No ready xhs/adspower"):
        live_smoke.select_accounts({"account_pool": accounts()["account_pool"][:-1]}, {})


def test_run_validation_accepts_empty_but_exhausted_comment_page():
    case = next(case for case in live_smoke.SMOKE_CASES if case.comments)
    result = live_smoke.validate_run(case, snapshot(case))
    assert result["status"] == "completed"
    assert result["comment_count"] == 0


@pytest.mark.parametrize("case", live_smoke.SMOKE_CASES, ids=lambda case: case.name)
def test_run_validation_accepts_each_network_ownership_mode(case):
    result = live_smoke.validate_run(case, snapshot(case, observations=[observation_for(case)]))
    expected = (
        "adspower_profile"
        if case.account_role == "xhs_adspower"
        else "http"
        if case.adapter in {"httpx", "curl_cffi"}
        else "dual"
    )
    assert result["network_mode"] == expected


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"risks": [{"kind": "rate_limit"}]}, "risk events"),
        (
            {
                "observations": [
                    {
                        "outcome": "success",
                        "http_egress_ip": "192.0.2.10",
                        "browser_egress_ip": "192.0.2.11",
                    }
                ]
            },
            "egress",
        ),
    ],
)
def test_run_validation_rejects_risk_or_egress_mismatch(changes, message):
    case = live_smoke.SMOKE_CASES[0]
    with pytest.raises(live_smoke.SmokeFailure, match=message):
        live_smoke.validate_run(case, snapshot(case, **changes))


@pytest.mark.parametrize(
    "case_name,observation,message",
    [
        (
            "xhs-httpx",
            {
                "outcome": "success",
                "http_egress_ip": "192.0.2.10",
                "browser_egress_ip": "192.0.2.10",
                "data": {"transport": "dual"},
            },
            "HTTP adapter egress",
        ),
        (
            "xhs-adspower-headed",
            {
                "outcome": "success",
                "http_egress_ip": "192.0.2.10",
                "browser_egress_ip": "192.0.2.10",
                "data": {"transport": "dual"},
            },
            "AdsPower profile browser egress",
        ),
        (
            "xhs-adspower-headless",
            {
                "outcome": "success",
                "http_egress_ip": None,
                "browser_egress_ip": "192.0.2.10",
                "data": {"transport": "http"},
            },
            "AdsPower profile browser egress",
        ),
    ],
)
def test_run_validation_rejects_wrong_network_ownership(case_name, observation, message):
    case = next(case for case in live_smoke.SMOKE_CASES if case.name == case_name)
    with pytest.raises(live_smoke.SmokeFailure, match=message):
        live_smoke.validate_run(case, snapshot(case, observations=[observation]))
