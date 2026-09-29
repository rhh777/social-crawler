"""Task organization and read-only configuration disclosures."""
import pytest
from test_ui_localization import ui_app as ui_app


@pytest.mark.browser
async def test_workbench_schedule_tab_edit_and_creation_defaults(ui_app):
    from playwright.async_api import async_playwright, expect
    _, base, _, _, schedule = ui_app
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(base + '/#automation')
            await expect(page.get_by_role('tab', name='定时作业', exact=True)).to_have_count(0)
            await page.locator('[data-page="workbench"]').click()
            await page.get_by_role('tab', name='定时作业', exact=True).click()
            await expect(page.locator('#workbench-section-runs')).to_be_hidden()
            await page.get_by_role('button', name='新建定时作业', exact=True).click()
            await expect(page.locator('#collect-form [name=trigger_type]')).to_have_value('schedule')
            await expect(page.locator('[data-schedule-fields]')).to_be_visible()
            await page.locator('#close-modal').click()
            await page.locator(f'[data-schedule-edit="{schedule["id"]}"]').click()
            await page.locator('#schedule-edit-form [name=name]').fill('工作台定时作业')
            await page.get_by_role('button', name='保存修改', exact=True).click()
            await expect(page.get_by_role('tab', name='定时作业', exact=True)).to_have_attribute('aria-selected', 'true')
            await expect(page.locator('#workbench-section-schedules')).to_contain_text('工作台定时作业')
            await page.get_by_role('tab', name='采集任务', exact=True).click()
            await expect(page.locator('#workbench-section-runs')).to_be_visible()
            await expect(page.locator('#workbench-section-schedules')).to_be_hidden()
            await expect(page.locator('#new-collection')).to_have_text('新建采集')
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize('adapter,label', [('browser', '浏览器网络响应'), ('httpx', 'HTTP 接口 · httpx')])
async def test_run_configuration_is_collapsed_and_preserved_on_refresh(ui_app, adapter, label):
    from playwright.async_api import async_playwright, expect
    _, base, _, run_id, _ = ui_app
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        async def detail(route):
            response = await route.fetch()
            data = await response.json()
            data['run']['mode'] = 'online'
            data['run']['config'].update(adapter=adapter, source_type='posts', keywords=[],
                post_targets=['https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8'],
                min_interval=6.5, comment_limit=20, reply_parents=3, reply_limit=8)
            await route.fulfill(json=data)
        await page.route(f'**/api/runs/{run_id}', detail)
        try:
            await page.goto(base + '/#workbench')
            await page.locator(f'[data-run="{run_id}"]').click()
            content = page.locator('#run-config-content')
            await expect(content).to_be_hidden()
            await page.locator('#run-configuration summary').click()
            await expect(content).to_contain_text(label)
            await expect(content).to_contain_text('6.5 秒')
            await expect(content).to_contain_text('20 条')
            await expect(content).to_contain_text('66fad51c000000001b0224b8')
            await page.evaluate('(id) => refreshRun(id)', run_id)
            await expect(content).to_be_visible()
            await page.set_viewport_size({'width': 390, 'height': 800})
            assert not await page.locator('#modal').evaluate('el => el.scrollWidth > el.clientWidth')
        finally:
            await browser.close()
