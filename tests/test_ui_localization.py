"""Chinese presentation keeps machine identifiers unchanged at API boundaries."""

import ast
import json
import re
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_web import collect

from social_crawler.domain.models import CollectionError, RunConfig
from social_crawler.interfaces.web import UI_LABELS, Console, Handler, error_message


def test_all_declared_collection_errors_have_chinese_labels():
    root = Path(__file__).parents[1] / "src/social_crawler"
    kinds = set()
    for path in root.rglob("*.py"):
        if "vendor" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CollectionError" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                kinds.add(node.args[0].value)
    assert kinds <= UI_LABELS["reasons"].keys()
    for kind in kinds:
        assert kind not in error_message(CollectionError(kind))
        assert re.search("[\u3400-\u9fff]", error_message(CollectionError(kind)))


def test_validation_and_known_configuration_errors_are_chinese():
    with pytest.raises(ValidationError) as invalid:
        RunConfig(platform="xhs", keywords=["咖啡"], max_requests=-1)
    message = error_message(invalid.value)
    assert "请求上限" in message and "最小值" in message
    assert "max_requests" not in message and "greater_than_equal" not in message
    assert error_message(ValueError("Operation quota requires an operation")) == "动作额度必须选择一个采集动作"
    assert "secret" not in error_message(ValueError("secret private connection"))


@pytest.fixture
def ui_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")
    app = Console(tmp_path)
    account = app.add_account({"platform": "xhs", "name": "中文演示账号"})
    run_id = collect(app)
    schedule = app.create_schedule({
        "name": "中文定时作业", "kind": "cron", "cron_expression": "0 9 * * *",
        "config": {"platform": "xhs", "keywords": ["咖啡"]},
    })
    with app.store() as store:
        for dimension, subject, operation in [
            ("operation", "rednote", "resolve_target"),
            ("platform", "douyin", None),
            ("account", account["id"], None),
            ("ip_group", "203.0.113.7", None),
        ]:
            store.create_quota_policy(dimension, subject, operation=operation,
                                      window_seconds=3600, request_limit=10)
        store.event(run_id, "session_recovery_failed", {"operation": "search", "reason": "identity_mismatch"})
    original = app.risk_report

    def risk_report(query=None):
        report = original(query)
        report["risk_events"] = [{
            "platform": "xhs", "operation": "comments", "kind": "access_denied",
            "action": "manual_review", "at": time.time(),
        }]
        report["network_observations"] = [{
            "platform": "xhs", "account_id": account["id"], "outcome": "success",
            "observed_at": time.time(), "egress_ip": "203.0.113.7", "ip_group": "203.0.113.7",
        }]
        report["recovery_probes"] = [{
            "account_id": account["id"], "created_at": time.time(), "attempt": 1,
            "request_count": 1, "outcome": "config_blocked",
        }]
        section = (query or {}).get("section", [""])[0]
        if section in {"risk_events", "network_observations", "recovery_probes"}:
            report.update(items=report[section], total=len(report[section]), page=1, pages=1)
        return report

    monkeypatch.setattr(app, "risk_report", risk_report)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{server.server_port}", account, run_id, schedule
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    for task in app.threads:
        task.join(timeout=10)
    app.worker_pool.shutdown(wait_seconds=10)


@pytest.mark.browser
async def test_all_pages_modals_and_quota_submission_use_chinese(ui_app, tmp_path):
    from playwright.async_api import async_playwright, expect

    app, base, account, run_id, schedule = ui_app
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base, wait_until="networkidle")
            for route, title in [
                ("overview", "总览大屏"), ("workbench", "采集工作台"),
                ("accounts", "账号池"), ("library", "内容库"),
                ("automation", "风控观测"), ("settings", "系统配置"),
            ]:
                await page.locator(f'[data-page="{route}"]').click()
                await expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                text = await page.locator("body").inner_text()
                assert not re.search(r"\b(?:operation|platform|resolve_target|manual_review|config_blocked|Worker|Canary|LOCAL|WORKSPACE)\b", text), text
                await page.screenshot(path=str(tmp_path / f"{route}.png"), full_page=True)
            await page.get_by_role("tab", name="风控策略", exact=True).click()
            await expect(page.locator("#quota-list tbody")).to_be_visible()
            await expect(page.locator("#main")).to_contain_text("小红书国际版")
            await expect(page.locator("#main")).to_contain_text("解析帖子链接")
            await expect(page.locator("#main")).to_contain_text("3,600 秒")
            await page.locator('.policy-editor summary').click()
            form = page.locator("#quota-policy-form")
            await form.locator('[name="dimension"]').select_option(label="动作")
            await form.locator('[name="subject"]').select_option(label="小红书")
            await form.locator('[name="operation"]').select_option(label="一级评论")
            async with page.expect_response("**/api/quota-policies") as response:
                await form.get_by_role("button", name="新增策略版本").click()
            saved = await (await response.value).json()
            assert (saved["dimension"], saved["subject"], saved["operation"]) == ("operation", "xhs", "comments")
            await expect(page.locator("#toast")).to_contain_text("额度策略版本已创建")
            await page.locator('.policy-editor summary').click()
            await form.locator('[name="dimension"]').select_option(label="账号")
            await form.locator('[name="subject"]').select_option(label="小红书 · 中文演示账号")
            assert await form.locator('[name="subject"]').input_value() == account["id"]
            await form.locator('[name="dimension"]').select_option(label="出口地址组")
            await expect(form.locator('[name="subject"]')).to_have_attribute("data-suggestion-list", "quota-address-groups")
            await expect(page.locator("#quota-address-groups option")).to_have_attribute("value", "203.0.113.7")

            await page.locator('[data-page="automation"]').click()
            await expect(page.locator("#main")).to_contain_text("等待人工处理")
            await page.get_by_role("tab", name="恢复检测", exact=True).click()
            await expect(page.locator("#main")).to_contain_text("配置未通过")
            await page.locator('[data-page="workbench"]').click()
            await page.get_by_role("tab", name="定时作业", exact=True).click()
            await page.locator(f'[data-schedule-edit="{schedule["id"]}"]').click()
            await expect(page.locator("#modal")).to_contain_text("定时表达式")
            await page.locator("#close-modal").click()
            await page.locator('[data-page="workbench"]').click()
            await page.get_by_role("tab", name="采集任务", exact=True).click()
            await page.locator(f'[data-run="{run_id}"]').click()
            await expect(page.locator("#run-progress")).to_contain_text("登录身份与预期账号不一致")
            await expect(page.locator("#run-progress")).not_to_contain_text("session_recovery_failed")
            await page.locator("#close-modal").click()
            await page.locator('#new-collection').click()
            await expect(page.locator("#collect-form")).to_contain_text("模拟浏览器指纹请求（试用）")
            await page.locator("#close-modal").click()
            await page.locator('[data-page="accounts"]').click()
            await page.locator(f'[data-account-detail="{account["id"]}"]').click()
            await expect(page.locator("#modal")).to_contain_text("浏览器身份标识")
            await expect(page.locator("#modal")).to_contain_text("高级配置与登录凭据")
            await expect(page.locator("#modal")).not_to_contain_text("Profile")
            await page.locator("#close-modal").click()
            await page.locator("#add-account").click()
            await expect(page.locator("#modal")).to_contain_text("浏览器类型")
            await page.locator("#close-modal").click()
            await page.locator('[data-page="library"]').click()
            await page.locator('[data-content]').first.click()
            await expect(page.locator("#modal")).to_contain_text("内容预览")
            assert not errors
            (tmp_path / "localization-audit.json").write_text(json.dumps({
                "pages": 6, "modals": 6, "quota_api_values_preserved": True, "browser_errors": errors,
            }, ensure_ascii=False, indent=2))
        finally:
            await browser.close()


@pytest.mark.browser
async def test_account_credentials_load_on_demand_and_closed_modal_stays_closed(ui_app):
    import asyncio

    from playwright.async_api import async_playwright, expect

    app, base, account, _, _ = ui_app
    app.update_account(account["id"], {"cookie_text": "web_session=lazy-load-test"})
    expected_cookie = app.account_detail(account["id"])["cookie_text"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            async with page.expect_response("**/api/bootstrap") as response:
                await page.goto(base + "/#accounts")
            settings = (await (await response.value).json())["settings"]
            assert all("cookie_text" not in a for a in settings["account_pool"])
            detail_url = base + "/api/accounts/" + account["id"]
            assert detail_url not in requests
            await page.locator(f'[data-account-detail="{account["id"]}"]').click()
            await expect(page.locator('#account-detail-form [name="cookie_text"]')).to_have_value(expected_cookie)
            assert detail_url in requests
            # Saving unrelated settings must preserve the fetched credentials.
            await page.locator('#account-detail-form [name="name"]').fill("更新名称")
            await page.get_by_role("button", name="保存账号配置", exact=True).click()
            await expect(page.locator("#modal")).not_to_be_visible()
            assert app.account_detail(account["id"])["cookie_text"] == expected_cookie

            started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def delayed_detail(route):
                response = await route.fetch()
                started.set()
                await release.wait()
                await route.fulfill(response=response)
                finished.set()

            await page.route(detail_url, delayed_detail)
            await page.locator(f'[data-account-detail="{account["id"]}"]').click()
            await asyncio.wait_for(started.wait(), 5)
            await page.locator("#close-modal").click()
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            await expect(page.locator("#modal")).not_to_be_visible()
            await page.locator("#add-account").click()
            await expect(page.locator("#new-account-form")).to_be_visible()
            assert not errors
        finally:
            await browser.close()
