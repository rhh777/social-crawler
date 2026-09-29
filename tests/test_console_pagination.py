"""SQL pagination covers full history; tabbed UI keeps editing independent of lists."""

import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import insert
from test_web import console as console

from social_crawler.domain.models import RunConfig
from social_crawler.interfaces.web import Handler
from social_crawler.storage.store import network_observations, risk_events


def seed(store, count=137):
    account = store.create_account("xhs", "分页账号", {"account_ref": "paged"})
    now = time.time()
    with store.engine.begin() as conn:
        conn.execute(insert(network_observations), [{
            "id": f"network-{i:04}", "account_id": account["id"], "platform": "xhs",
            "proxy_ref": "direct", "source": "test", "observed_at": now,
            "outcome": "success", "egress_ip": f"192.0.2.{i % 250 + 1}", "data": {},
        } for i in range(count)])
        conn.execute(insert(risk_events), [{
            "account_id": account["id"], "platform": "xhs", "operation": "search",
            "kind": "auth_expired", "action": "require_login", "at": now, "data": {},
        } for _ in range(count)])
    return account, now


def test_full_history_pages_counts_filters_and_stable_order(store):
    account, now = seed(store)
    for section in ["network_observations", "risk_events"]:
        ids = []
        for page in range(1, 15):
            result = store.risk_page(section, since=now - 1, page=page, page_size=10,
                                     account_id=account["id"], platform="xhs")
            assert result["total"] == 137 and result["pages"] == 14
            ids += [item["id"] for item in result["items"]]
        assert len(ids) == len(set(ids)) == 137
        assert store.risk_page(section, since=now - 1, page=999)["page"] == 14
        assert store.risk_page(section, since=now + 1)["total"] == 0
        empty = store.risk_page(section, since=0, platform="douyin", page=99)
        assert empty["page"] == 1 and empty["pages"] == 1 and not empty["items"]
        assert store.risk_page(section, since=0, page_size=999)["page_size"] == 50
    assert store.risk_page("risk_events", since=0, operation="comments")["total"] == 0
    with pytest.raises(ValueError, match="未知"):
        store.risk_page("invalid", since=0)


def test_metrics_aggregate_and_quota_page_match_existing_semantics(store):
    account = store.create_account("xhs", "分页账号", {"account_ref": "paged"})
    run = store.create_run(RunConfig(platform="xhs", keywords=["咖啡"]), mode="offline", account_id=account["id"])
    task = store.snapshot(run)["tasks"][0]["id"]
    for i, (outcome, duration, items) in enumerate([("success", 100, 3), ("error", 200, 0), ("success", 300, 5)]):
        store.record_operation(run, task, operation="search", attempt=i + 1,
                               started_at=time.time(), duration_ms=duration, outcome=outcome,
                               item_count=items)
    result = store.risk_page("groups", since=0)
    group = result["items"][0]
    assert result["total"] == 1
    assert (group["attempts"], group["successes"], group["failures"], group["items"], group["avg_duration_ms"]) == (3, 2, 1, 8, 200)
    assert group["success_rate"] == 0.6667
    assert store.risk_summary(since=0)["groups"] == [group]
    for i in range(25):
        store.create_quota_policy("ip_group", f"group-{i:02}", window_seconds=900, request_limit=10)
    store.create_quota_policy("account", account["id"], window_seconds=900, request_limit=12)
    store.create_quota_policy("account", account["id"], window_seconds=900, request_limit=24)
    page = store.risk_page("quotas", since=0, page=3, dimension="ip_group")
    assert page["total"] == 25 and len(page["items"]) == 5
    quota = store.risk_page("quotas", since=0, q="分页账号")["items"][0]
    assert quota["version"] == 2 and quota["remaining"] == 24
    assert store.risk_page("quotas", since=0, q="%")["total"] == 0
    assert len(store.quota_summary()) == 26


@pytest.fixture
def paged_app(console):
    with console.store() as store:
        seed(store)
        for i in range(25):
            store.create_quota_policy("ip_group", f"group-{i:02}", window_seconds=900, request_limit=10)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.browser
async def test_tabs_pagination_filters_and_unsaved_form_survive(paged_app, tmp_path):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(paged_app + "/#automation")
            panel = page.locator('#risk-section-host')
            await expect(panel.locator('tbody tr')).to_have_count(10)
            await expect(panel.locator('.console-pagination')).to_contain_text("共 137 条")
            assert await page.evaluate('document.documentElement.scrollHeight') < 1800
            await page.get_by_role('tab', name='出口观测', exact=True).click()
            await expect(panel.locator('tbody tr')).to_have_count(10)
            await panel.get_by_role('button', name='末页', exact=True).click()
            await expect(panel.locator('.console-pagination')).to_contain_text('第 14 / 14 页')
            await expect(panel.locator('tbody tr')).to_have_count(7)
            await expect(panel.get_by_role('button', name='下一页', exact=True)).to_be_disabled()
            await page.get_by_role('tab', name='风险事件', exact=True).click()
            await expect(panel.locator('.console-pagination')).to_contain_text('第 1 / 14 页')
            await page.get_by_role('tab', name='出口观测', exact=True).click()
            await expect(panel.locator('.console-pagination')).to_contain_text('第 14 / 14 页')
            await panel.locator('[name="page_size"]').select_option('20')
            await expect(panel.locator('tbody tr')).to_have_count(20)
            await expect(panel.locator('.console-pagination')).to_contain_text('第 1 / 7 页')
            await panel.locator('[name="platform"]').select_option('douyin')
            await panel.get_by_role('button', name='应用筛选', exact=True).click()
            await expect(panel).to_contain_text('没有匹配记录')
            await expect(panel.locator('.console-pagination')).to_contain_text('共 0 条')
            await panel.get_by_role('button', name='重置', exact=True).click()
            await expect(panel.locator('tbody tr')).to_have_count(10)
            await page.screenshot(path=str(tmp_path / 'risk-paged.png'), full_page=True)

            await page.locator('[data-page="settings"]').click()
            await expect(page.locator('#system-database-form')).to_be_visible()
            await page.locator('[name="database_url"]').fill('draft-not-saved')
            await page.get_by_role('tab', name='风控策略', exact=True).click()
            await expect(page.locator('#system-database-form')).not_to_be_visible()
            await page.locator('.policy-editor summary').click()
            await page.locator('#quota-policy-form [name="request_limit"]').fill('123')
            quota = page.locator('#quota-list')
            await expect(quota.locator('tbody tr')).to_have_count(10)
            await quota.get_by_role('button', name='下一页', exact=True).click()
            await expect(quota.locator('.console-pagination')).to_contain_text('第 2 / 3 页')
            await expect(page.locator('#quota-policy-form [name="request_limit"]')).to_have_value('123')
            await page.get_by_role('tab', name='采集与运行', exact=True).click()
            await expect(page.locator('[name="database_url"]')).to_have_value('draft-not-saved')
            await page.get_by_role('tab', name='风控策略', exact=True).click()
            await expect(quota.locator('.console-pagination')).to_contain_text('第 2 / 3 页')
            await quota.locator('[name="q"]').fill('group-24')
            await quota.get_by_role('button', name='筛选策略', exact=True).click()
            await expect(quota.locator('tbody tr')).to_have_count(1)
            await expect(quota.locator('.console-pagination')).to_contain_text('共 1 条')
            await page.screenshot(path=str(tmp_path / 'settings-paged.png'), full_page=True)
            await page.get_by_role('tab', name='Agent 模型', exact=True).click()
            await expect(page.locator('#agent-settings-form')).to_be_visible()
            assert not errors
        finally:
            await browser.close()
