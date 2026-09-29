"""Legacy account diagnostics remain read-only after the static list redesign."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_web import console as console  # noqa: F401
from test_web import wait_job

from social_crawler.domain.models import CollectionError
from social_crawler.interfaces import web


@pytest.fixture
def checked_account(console, monkeypatch):
    account = console.add_account({
        "platform": "xhs", "name": "检查账号",
        "cookie_text": "web_session=fixture; a1=stable",
    })
    monkeypatch.setattr(web, "observe_dual_exit", AsyncMock(return_value={
        "outcome": "success", "proxy_ref": "direct", "egress_ip": "203.0.113.8",
        "ip_group": "203.0.113.8",
    }))
    monkeypatch.setattr(console, "probe", AsyncMock(return_value={"login_state": "verified"}))
    return account


def test_unified_check_records_evidence_and_invalidates_changed_credentials(console, checked_account):
    account = checked_account
    job = wait_job(console, console.account_maintenance({"action": "check", "account_id": account["id"]}))
    assert job["kind"] == "account_check"
    row = next(j for j in console.diagnostic_jobs() if j["id"] == job["id"])["result"]["results"][0]
    assert row["current"] is True and row["checked_at"] > 0
    assert "评论和回复未检查" in row["message"]
    assert row["changes"] == []
    assert "fingerprint" not in row
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "ready"
    Path(console.environment("xhs", account["id"]).cookie_file).write_text("web_session=changed; a1=stable")
    row = next(j for j in console.diagnostic_jobs() if j["id"] == job["id"])["result"]["results"][0]
    assert row["current"] is False
    console.delete_account(account["id"])
    assert next(j for j in console.diagnostic_jobs() if j["id"] == job["id"])["result"]["results"][0]["current"] is False


def test_unified_check_skips_busy_and_disabled_accounts(console, checked_account, monkeypatch):
    disabled = console.add_account({"platform": "xhs", "name": "停用账号"})
    console.set_account_state(disabled["id"], {"status": "disabled"})
    busy = checked_account
    console.jobs["busy"] = {"id": "busy", "kind": "collection", "status": "running", "account_id": busy["id"]}
    console.job_resources["busy"] = frozenset(console.account_resource_keys("xhs", busy["id"]))
    completed = wait_job(console, console.account_maintenance({"action": "check"}))
    rows = {row["id"]: row for row in completed["result"]["results"]}
    assert rows[busy["id"]]["outcome"] == rows[disabled["id"]]["outcome"] == "skipped"
    assert console.jobs["busy"]["status"] == "running"
    with console.store() as store:
        assert store.get_account(disabled["id"])["status"] == "disabled"


def test_unified_check_preserves_verification_reason(console, checked_account, monkeypatch):
    monkeypatch.setattr(console, "probe", AsyncMock(side_effect=CollectionError("verification_required")))
    completed = wait_job(console, console.account_maintenance({"action": "check", "account_id": checked_account["id"]}))
    row = completed["result"]["results"][0]
    assert row["outcome"] == "failed"
    assert row["platform_check"] == "verification_required"
    assert row["changes"] == []
    with console.store() as store:
        account = store.get_account(checked_account["id"])
        assert account["status"] == "ready"
        assert not account["last_failure_kind"]


def test_connection_configuration_failure_does_not_request_login(console, checked_account, monkeypatch):
    monkeypatch.setattr(
        web,
        "Session",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid proxy")),
    )
    completed = wait_job(console, console.account_maintenance({"action": "check", "account_id": checked_account["id"]}))
    row = completed["result"]["results"][0]
    assert row["outcome"] == "failed" and row["proxy"] == "proxy_unavailable"
    with console.store() as store:
        assert store.get_account(checked_account["id"])["status"] == "ready"


@pytest.mark.parametrize("failure", [None, "verification_required", "auth_expired"])
def test_disabling_during_check_is_preserved(console, checked_account, monkeypatch, failure):
    async def probe(session):
        console.set_account_state(checked_account["id"], {"status": "disabled"})
        if failure:
            raise CollectionError(failure)
        return {"login_state": "verified"}

    monkeypatch.setattr(console, "probe", probe)
    wait_job(console, console.account_maintenance({"action": "check", "account_id": checked_account["id"]}))
    with console.store() as store:
        assert store.get_account(checked_account["id"])["status"] == "disabled"


def test_network_exception_is_a_persisted_failed_check(console, checked_account, monkeypatch):
    monkeypatch.setattr(web, "observe_dual_exit", AsyncMock(side_effect=CollectionError("network_failure")))
    completed = wait_job(console, console.account_maintenance({"action": "check", "account_id": checked_account["id"]}))
    row = completed["result"]["results"][0]
    assert row["outcome"] == "failed" and row["platform_check"] == "network_failure"
    restarted = web.Console(console.root)
    try:
        row = next(j for j in restarted.diagnostic_jobs() if j["id"] == completed["id"])["result"]["results"][0]
        assert row["current"] is True
    finally:
        restarted.worker_pool.shutdown(wait_seconds=5)


def test_changes_during_check_do_not_certify_new_credentials(console, checked_account, monkeypatch):
    async def probe(session):
        Path(session.config.cookie_file).write_text("web_session=replaced; a1=stable")
        return {"login_state": "verified"}

    monkeypatch.setattr(console, "probe", probe)
    completed = wait_job(console, console.account_maintenance({"action": "check", "account_id": checked_account["id"]}))
    row = next(j for j in console.diagnostic_jobs() if j["id"] == completed["id"])["result"]["results"][0]
    assert row["current"] is False
