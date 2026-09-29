"""Proxy inventory, shared account bindings and the visible configuration workflow."""

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_account_controls import console as console

from social_crawler.interfaces.web import Console, ConsoleError, Handler
from social_crawler.storage.store import Store


def test_shared_proxy_edit_switch_and_delete(console):
    first = console.save_proxy({"name": "上海出口", "proxy_url": "http://user:secret@localhost:10001"})
    second = console.save_proxy({"name": "备用出口", "proxy_url": "socks5://localhost:10002"})
    accounts = [console.add_account({"platform": platform, "name": platform, "proxy_id": first["id"]})
                for platform in ("xhs", "douyin")]
    with console.store() as store:
        for account in accounts:
            store.confirm_account_network(
                account["id"], proxy_ref="first", egress_ip="203.0.113.10"
            )
    assert len(console.list_proxies()[0]["accounts"]) == 2
    assert "secret" not in json.dumps(console.list_proxies())
    assert "user" not in console.list_proxies()[0]["endpoint"]
    with pytest.raises(ConsoleError, match="仍被账号使用"):
        console.delete_proxy(first["id"])
    console.save_proxy({"name": "上海新出口", "proxy_url": "http://localhost:10003"}, first["id"])
    for account in accounts:
        assert console.account_detail(account["id"])["proxy_url"] == "http://localhost:10003"
        with console.store() as store:
            assert store.get_account_network_binding(account["id"]) is None
        env = console.environment(account["platform"], account["id"])
        assert Path(env.proxy_file).stat().st_mode & 0o777 == 0o600
    with console.store() as store:
        for account in accounts:
            store.confirm_account_network(
                account["id"], proxy_ref="updated", egress_ip="203.0.113.11"
            )
    console.update_account(accounts[0]["id"], {"proxy_id": second["id"]})
    console.update_account(accounts[1]["id"], {"proxy_id": ""})
    with console.store() as store:
        assert store.get_account_network_binding(accounts[0]["id"]) is None
        assert store.get_account_network_binding(accounts[1]["id"]) is None
    direct = console.environment("douyin", accounts[1]["id"])
    assert direct.proxy_id is direct.proxy_file is direct.proxy_env is None
    console.delete_proxy(first["id"])
    assert [p["id"] for p in console.list_proxies()] == [second["id"]]
    assert console.account_detail(accounts[0]["id"])["proxy_url"] == "socks5://localhost:10002"


def test_failed_proxy_save_keeps_existing_connection(console, monkeypatch):
    proxy = console.save_proxy({"name": "original", "proxy_url": "http://localhost:10001"})
    account = console.add_account({"platform": "xhs", "name": "bound", "proxy_id": proxy["id"]})

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(Store, "upsert_proxy", fail)
    with pytest.raises(RuntimeError):
        console.save_proxy({"proxy_url": "http://localhost:10002"}, proxy["id"])
    assert console.account_detail(account["id"])["proxy_url"] == "http://localhost:10001"
    assert console.proxy_detail(proxy["id"])["proxy_url"] == "http://localhost:10001"


def test_proxy_migration_keeps_sources_and_survives_restart(console, monkeypatch):
    path = console.root / "legacy.proxy"
    path.write_text("socks5://test:secret@localhost:10001")
    monkeypatch.setenv("TEST_POOL_PROXY", "http://localhost:10002")
    with console.store() as store:
        for platform, source in (("xhs", {"proxy_file": "legacy.proxy"}),
                                 ("rednote", {"proxy_file": str(path)}),
                                 ("douyin", {"proxy_env": "TEST_POOL_PROXY"})):
            account = store.get_account("legacy-" + platform)
            store.upsert_account(account["id"], platform, account["name"], account["environment"] | source)
    console.migrate_account_proxies()
    rows = console.list_proxies()
    assert len(rows) == 2
    assert sorted(len(row["accounts"]) for row in rows) == [1, 2]
    console.migrate_account_proxies()
    assert console.list_proxies() == rows
    restored = Console(console.root)
    try:
        assert restored.list_proxies() == rows
        assert restored.account_detail("legacy-xhs")["proxy_url"] == path.read_text()
        env = restored.environment("douyin", "legacy-douyin")
        assert env.proxy_env == "TEST_POOL_PROXY"
        restored.save_proxy({"proxy_url": "http://localhost:10003"}, env.proxy_id)
        assert restored.environment("douyin", "legacy-douyin").proxy_env is None
        assert restored.account_detail("legacy-douyin")["proxy_url"] == "http://localhost:10003"
        assert path.read_text() == "socks5://test:secret@localhost:10001"
    finally:
        restored.worker_pool.shutdown(wait_seconds=10)


@pytest.mark.parametrize("owner", ["job", "browser"])
def test_proxy_update_blocks_busy_users_but_allows_renaming(console, owner):
    proxy = console.save_proxy({"name": "shared", "proxy_url": "http://localhost:10001"})
    account = console.add_account({"platform": "xhs", "name": "busy", "proxy_id": proxy["id"]})
    keys = console.account_resource_keys("xhs", account["id"])
    if owner == "job":
        console.jobs["busy"] = {"status": "running"}
        console.job_resources["busy"] = frozenset(keys)
    else:
        console.account_browsers[account["id"]] = SimpleNamespace(active=True, keys=keys)
    console.save_proxy({"name": "renamed"}, proxy["id"])
    with pytest.raises(ConsoleError, match="正在执行"):
        console.save_proxy({"proxy_url": "http://localhost:10002"}, proxy["id"])
    assert console.account_detail(account["id"])["proxy_url"] == "http://localhost:10001"
    with pytest.raises(ConsoleError):
        console.update_account(account["id"], {"proxy_id": ""})


def test_invalid_proxy_and_missing_selection_do_not_change_account(console):
    account = console.add_account({"platform": "xhs", "name": "unchanged"})
    with pytest.raises(ConsoleError):
        console.save_proxy({"name": "invalid", "proxy_url": "ftp://localhost:1234"})
    with pytest.raises(ConsoleError, match="代理不存在"):
        console.update_account(account["id"], {"proxy_id": "missing"})
    assert console.list_proxies() == []
    assert console.environment("xhs", account["id"]).proxy_id is None


def test_legacy_inline_edit_does_not_overwrite_shared_proxy(console):
    original = console.add_account({"platform": "xhs", "name": "original",
                                    "proxy_url": "http://localhost:10001"})
    copied = console.copy_account(original["id"])
    console.update_account(original["id"], {"proxy_url": "http://localhost:10002"})
    assert console.account_detail(copied["id"])["proxy_url"] == "http://localhost:10001"
    assert console.account_detail(original["id"])["proxy_url"] == "http://localhost:10002"


def test_unreadable_migrated_proxy_is_visible_without_direct_fallback(console):
    with console.store() as store:
        account = store.get_account("legacy-xhs")
        store.upsert_account(account["id"], "xhs", account["name"],
                             account["environment"] | {"proxy_file": "missing.proxy"})
    console.migrate_account_proxies()
    proxy = console.list_proxies()[0]
    assert proxy["configured"] is False
    assert proxy["accounts"][0]["id"] == "legacy-xhs"
    assert console.environment("xhs", "legacy-xhs").proxy_file.endswith("missing.proxy")


@pytest.mark.browser
async def test_proxy_ui_account_selection_and_responsive_alignment(console, tmp_path):
    from playwright.async_api import async_playwright, expect

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/#proxies")
                await expect(page.get_by_text("还没有代理", exact=True)).to_be_visible()
                await page.locator("#add-proxy").click()
                form = page.locator("#proxy-form")
                await form.get_by_label("代理名称", exact=True).fill("上海固定出口")
                await form.get_by_label("主机", exact=True).fill("127.0.0.1")
                await form.get_by_label("端口", exact=True).fill("1080")
                await form.get_by_label("代理账号", exact=True).fill("tester")
                await form.get_by_label("代理密码", exact=True).fill("private-password")
                await page.screenshot(animations="disabled", path=str(tmp_path / "proxy-editor.png"))
                await form.get_by_role("button", name="保存代理").click()
                await expect(page.locator(".proxy-table")).to_contain_text("上海固定出口")
                await expect(page.locator(".proxy-table")).not_to_contain_text("private-password")
                proxy_id = console.list_proxies()[0]["id"]
                await page.locator('[data-page="accounts"]').click()
                await page.locator("#add-account").click()
                form = page.locator("#new-account-form")
                await form.get_by_label("账号名称", exact=True).fill("代理测试账号")
                await form.get_by_role("combobox", name="创建后操作").click()
                await page.get_by_role("option", name="稍后登录", exact=True).click()
                await form.get_by_role("combobox", name="使用代理").click()
                await page.get_by_role("option", name="上海固定出口", exact=False).click()
                await form.get_by_role("button", name="创建账号").click()
                row = page.get_by_role("row").filter(has_text="代理测试账号")
                await expect(row).to_contain_text("上海固定出口")
                await row.locator('[data-account-detail]').click()
                form = page.locator("#account-detail-form")
                await expect(form.get_by_role("combobox", name="使用代理")).to_contain_text("上海固定出口")
                await form.locator("summary").click()
                await page.screenshot(animations="disabled", path=str(tmp_path / "account-detail-desktop.png"))
                boxes = [await form.locator(f'[name="{name}"]').bounding_box()
                         for name in ("account_ref", "expected_user_id", "profile_dir", "cookie_file")]
                assert abs(boxes[0]["y"] - boxes[1]["y"]) < 1
                assert abs(boxes[2]["y"] - boxes[3]["y"]) < 1
                assert abs(boxes[0]["width"] - boxes[1]["width"]) < 1
                for width in (768, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    await page.screenshot(animations="disabled", path=str(tmp_path / f"account-detail-{width}.png"))
                    assert await page.locator("#modal").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.locator("#close-account-detail").click()
                await page.locator('[data-page="proxies"]').click()
                await expect(page.locator('.nav-item.active')).to_have_attribute("data-page", "proxies")
                await expect(page.locator('[data-proxy-delete]')).to_be_disabled()
                await page.screenshot(animations="disabled", path=str(tmp_path / "proxy-list.png"))
                await page.locator('[data-proxy-edit]').click()
                await page.locator('#proxy-form [name="name"]').fill("上海备用出口")
                await page.get_by_role("button", name="保存代理").click()
                await expect(page.locator(".proxy-table")).to_contain_text("上海备用出口")
                await page.locator('[data-proxy-account]').click()
                form = page.locator("#account-detail-form")
                await form.get_by_role("combobox", name="使用代理").click()
                await page.get_by_role("option", name="本机直连（不使用代理）", exact=True).click()
                await form.get_by_role("button", name="保存账号配置").click()
                await expect(page.locator('[data-proxy-delete]')).to_be_enabled()
                await page.locator('[data-proxy-delete]').click()
                await page.get_by_role("button", name="确认删除", exact=True).click()
                await expect(page.get_by_text("还没有代理", exact=True)).to_be_visible()
                assert not any(p["id"] == proxy_id for p in console.list_proxies())
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
