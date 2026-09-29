"""Manual account operations remain independent from automatic recovery."""

import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.request import urlopen

import pytest
from test_web import wait_job

from social_crawler.domain.models import CollectionError, RunConfig
from social_crawler.interfaces import web
from social_crawler.interfaces.web import ConsoleError
from social_crawler.storage.store import Store


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")
    app = web.Console(tmp_path)
    yield app
    for thread in app.threads:
        thread.join(timeout=10)
    app.worker_pool.shutdown(wait_seconds=10)


@pytest.mark.parametrize("kind", [
    "rate_limit", "verification_required", "access_denied", "proxy_unavailable",
    "egress_unconfirmed", "egress_changed", "egress_mismatch",
])
def test_default_risk_records_failure_without_cooling(store, kind):
    account = store.create_account("xhs", "manual", {})
    run = store.create_run(RunConfig(platform="xhs", keywords=["test"]),
                           mode="online", account_id=account["id"])
    assert store.automatic_cooldown_enabled() is False
    result = store.record_risk(run, operation="search", error=CollectionError(kind))
    assert result == {"action": "manual_review", "cooldown_until": None}
    current = store.get_account(account["id"])
    assert current["status"] == "ready"
    assert current["last_failure_kind"] == kind
    assert store.risk_summary()["risk_events"][0]["action"] == "manual_review"
    store.record_risk(run, operation="search", error=CollectionError("auth_expired"))
    assert store.get_account(account["id"])["status"] == "login_required"


def test_cooldown_setting_is_shared_persistent_and_can_change_while_busy(console):
    console.jobs["busy"] = {"id": "busy", "kind": "collection", "status": "running"}
    assert console.save({"automatic_cooldown": True})["automatic_cooldown"] is True
    other = Store(console.settings["database_url"])
    try:
        assert other.automatic_cooldown_enabled() is True
        console.save({"automatic_cooldown": False})
        assert other.automatic_cooldown_enabled() is False
    finally:
        other.close()
    with pytest.raises(ConsoleError):
        console.save({"automatic_cooldown": "false"})


@pytest.mark.parametrize("kind", ["login", "recover"])
@pytest.mark.parametrize("automatic", [False, True])
def test_manual_login_and_recovery_go_directly_ready_without_quota(
    console, monkeypatch, kind, automatic,
):
    account = console.add_account({"platform": "xhs", "name": "manual-login"})
    with console.store() as store:
        store.set_automatic_cooldown(automatic)
        store.set_account_status(account["id"], "cooling", cooldown_until=time.time() + 86400)
    capture = AsyncMock(return_value=2)
    monkeypatch.setattr(web, "capture_platform_cookies", capture)
    monkeypatch.setattr(web, "observe_dual_exit", AsyncMock(side_effect=AssertionError("no egress gate")))
    job = wait_job(console, console.check({
        "kind": kind, "platform": "xhs", "account_id": account["id"],
    }))
    assert "直接试跑" in job["result"]["message"]
    assert capture.call_args.kwargs["mode"] == ("reauth" if kind == "login" else "recover")
    assert "admit" not in capture.call_args.kwargs
    with console.store() as store:
        row = store.get_account(account["id"])
        assert row["status"] == "ready" and row["cooldown_until"] is None
        assert store.list_recovery_probes(account_id=account["id"]) == []


def test_edit_reset_delete_only_block_related_resources(console):
    first = console.add_account({"platform": "xhs", "name": "busy"})
    second = console.add_account({"platform": "xhs", "name": "idle"})
    console.jobs["busy"] = {"id": "busy", "kind": "collection", "status": "running",
                            "account_id": first["id"]}
    console.job_resources["busy"] = frozenset(console.account_resource_keys("xhs", first["id"]))
    assert console.update_account(second["id"], {"name": "changed"})["name"] == "changed"
    with console.store() as store:
        store.set_account_status(second["id"], "probe_due")
        probe = store.create_recovery_probe(second["id"])
    console.set_account_state(second["id"], {"status": "ready"})
    with console.store() as store:
        assert store.get_account(second["id"])["status"] == "ready"
        assert store.list_recovery_probes(account_id=second["id"])[0]["active_key"] is None
        with pytest.raises(ValueError):
            store.start_recovery_probe(probe["id"])
    console.delete_account(second["id"])
    for action in (
        lambda: console.update_account(first["id"], {"name": "no"}),
        lambda: console.set_account_state(first["id"], {"status": "ready"}),
        lambda: console.delete_account(first["id"]),
    ):
        with pytest.raises(ConsoleError, match="正在执行"):
            action()


def test_copy_configuration_uses_independent_paths_and_no_login(console):
    source = console.add_account({
        "platform": "xhs", "name": "source", "cookie_text": "web_session=secret; a1=stable",
        "proxy_url": "http://name:pass@127.0.0.1:10000",
        "environment": {"locale": "zh-CN", "expected_user_id": "original-user"},
    })
    copied = console.copy_account(source["id"])
    original = console.environment("xhs", source["id"])
    target = console.environment("xhs", copied["id"])
    assert copied["status"] == "login_required"
    for key in ("cookie_file", "profile_dir", "account_ref"):
        assert getattr(original, key) != getattr(target, key)
    assert target.expected_user_id is None
    assert target.locale == "zh-CN"
    assert json.loads(Path(target.cookie_file).read_text()) == []
    assert target.proxy_id == original.proxy_id
    assert target.proxy_file == original.proxy_file
    assert console.copy_account(source["id"])["name"] != copied["name"]


def test_delete_keeps_history_and_legacy_does_not_reappear(console):
    account_id = "legacy-xhs"
    with console.store() as store:
        run = store.create_run(RunConfig(platform="xhs", keywords=["test"]),
                               mode="offline", account_id=account_id)
    with pytest.raises(ConsoleError, match="未结束"):
        console.delete_account(account_id)
    with console.store() as store:
        store.block_pending_run(run, CollectionError("canceled"))
    console.delete_account(account_id)
    console.sync_legacy_accounts(force=True)
    with console.store() as store:
        assert account_id not in {row["id"] for row in store.list_accounts()}
        assert store.get_account(account_id)["status"] == "deleted"
        assert store.get_run_context(run)["account_id"] == account_id
    with pytest.raises(ConsoleError, match="已删除"):
        console.update_account(account_id, {"status": "ready"})


def test_automatic_recovery_checks_are_idle_by_default(console, monkeypatch):
    with console.store() as store:
        store.set_account_status("legacy-xhs", "probe_due")
    monkeypatch.setattr(console, "recovery_check", AsyncMock(side_effect=AssertionError("unexpected")))
    assert console.dispatch_recovery_checks() is False


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_deleted_account_diagnostic_history_does_not_break_page_apis(console, restart, legacy):
    account_id = "legacy-xhs" if legacy else console.add_account({
        "platform": "xhs", "name": "有检测历史的账号",
    })["id"]
    old = wait_job(console, console.check({
        "kind": "cookie", "platform": "xhs", "account_id": account_id,
    }))
    current = wait_job(console, console.check({
        "kind": "cookie", "platform": "douyin", "account_id": "legacy-douyin",
    }))
    console.delete_account(account_id)
    app = web.Console(console.root) if restart else console
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for endpoint in ("bootstrap", "jobs", "dashboard"):
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/{endpoint}") as response:
                assert response.status == 200
                data = json.load(response)
            jobs = data["settings"]["checks"] if endpoint == "bootstrap" else data["jobs"]
            by_id = {job["id"]: job for job in jobs}
            assert by_id[old["id"]]["current"] is False
            assert by_id[current["id"]]["current"] is True
        with app.store() as store:
            assert store.get_account(account_id)["status"] == "deleted"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if restart:
            app.worker_pool.shutdown(wait_seconds=10)


@pytest.mark.browser
async def test_account_controls_in_browser(console, tmp_path):
    from playwright.async_api import async_playwright, expect

    account = console.add_account({"platform": "xhs", "name": "手动账号"})
    with console.store() as store:
        store.set_account_status(account["id"], "cooling", cooldown_until=time.time() + 3600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                await page.locator('[data-page="settings"]').click()
                await page.get_by_role("tab", name="风控策略", exact=True).click()
                toggle = page.locator('#cooldown-policy-form [name="automatic_cooldown"]')
                await expect(toggle).not_to_be_checked()
                await toggle.check()
                await page.get_by_role("button", name="保存冷却设置").click()
                await expect(page.locator("#toast")).to_contain_text("冷却设置已保存")
                assert console.public_settings()["automatic_cooldown"] is True
                await page.locator('[data-page="accounts"]').click()
                await expect(page.locator('[data-account-reset]')).to_have_count(0)
                row = page.get_by_role("row").filter(has_text="手动账号")
                await expect(row.get_by_role("button", name="打开浏览器", exact=True).first).to_be_visible()
                await expect(row.locator("summary")).to_have_count(0)
                await expect(page.locator('[data-account-maintenance], [data-account-reason]')).to_have_count(0)
                await page.locator(f'[data-account-detail="{account["id"]}"]').click()
                await expect(page.locator("#account-detail-form")).to_be_visible()
                await expect(page.locator('#account-detail-form [name="status"]')).to_have_count(0)
                await page.locator('#account-detail-form [name="name"]').fill("已编辑账号")
                await page.get_by_role("button", name="保存账号配置").click()
                await expect(page.get_by_role("row").filter(has_text="已编辑账号")).to_be_visible()
                with console.store() as store:
                    assert store.get_account(account["id"])["status"] == "cooling"
                # An old diagnostic must not change presentation or start polling.
                wait_job(console, console.check({
                    "kind": "cookie", "platform": "xhs", "account_id": account["id"],
                }))
                requests = []
                page.on("request", lambda request: requests.append(request.url))
                await page.wait_for_timeout(4200)
                assert not [url for url in requests if "/api/" in url]
                await page.locator(f'[data-account-menu="{account["id"]}"]').click()
                await page.locator(f'[data-account-delete="{account["id"]}"]').click()
                await page.get_by_role("button", name="确认删除", exact=True).click()
                await expect(page.locator(f'[data-account-delete="{account["id"]}"]')).to_have_count(0)
                await page.reload()
                await expect(page.locator(f'[data-account-detail="{account["id"]}"]')).to_have_count(0)
                await expect(page.locator(".error-panel")).to_have_count(0)
                await page.screenshot(path=str(tmp_path / "account-controls.png"), full_page=True)
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
async def test_account_login_requests_auto_save_without_diagnostics(console, monkeypatch):
    from playwright.async_api import async_playwright, expect

    account = console.add_account({"platform": "xhs", "name": "直接登录"})
    monkeypatch.setattr(console, "open_account_browser", lambda account_id, **kwargs: {
        "id": "test", "name": "直接登录", "state": "failed", "error": "测试窗口", "transport": "native",
    })
    from types import SimpleNamespace
    console.account_browsers[account['id']] = SimpleNamespace(id='test', access_token='test', active=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                requests = []
                context.on("request", lambda request: requests.append(request))
                await page.goto(f"http://127.0.0.1:{server.server_port}/#accounts")
                await page.locator(f'[data-account-menu="{account["id"]}"]').click()
                async with page.expect_popup() as popup:
                    await page.locator(f'[data-account-login="{account["id"]}"]').click()
                viewer = await popup.value
                await expect(viewer.locator('#message')).to_contain_text('测试窗口')
                assert not any('/api/check' in r.url or '/api/account-maintenance' in r.url for r in requests)
                assert any(r.method == 'POST' and r.post_data_json.get('kind') == 'open' for r in requests)
                assert any(r.method == 'POST' and r.post_data_json.get('auto_save') is True for r in requests)
                await expect(page.locator('.account-table')).not_to_contain_text('检查')
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
