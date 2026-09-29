"""Exercise shared controls through their visible UI and real form/API boundaries."""

import pytest
from test_ui_localization import ui_app as ui_app


@pytest.mark.browser
async def test_dropdown_filters_modal_dependencies_and_confirmation(ui_app, tmp_path):
    from playwright.async_api import async_playwright, expect

    _, base, _, _, schedule = ui_app
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/#accounts")
            platform = page.locator("#account-filters").get_by_role("combobox", name="平台", exact=True)
            await platform.click()
            await expect(page.get_by_role("listbox")).to_be_visible()
            await page.screenshot(path=str(tmp_path / "account-dropdown.png"))
            await platform.press("ArrowDown")
            await platform.press("Enter")
            await expect(platform).to_have_text("小红书")
            await expect(page.locator('#account-filters [name="platform"]')).to_have_value("xhs")
            await page.locator("#account-filters").get_by_role("button", name="筛选", exact=True).click()
            await expect(page.locator(".account-table")).to_contain_text("中文演示账号")
            platform = page.locator("#account-filters").get_by_role("combobox", name="平台", exact=True)
            await platform.click()
            await platform.press("Escape")
            await expect(platform).to_be_focused()
            await expect(page.get_by_role("listbox")).to_have_count(0)
            await platform.click()
            await page.get_by_role("heading", name="账号池", exact=True).click()
            await expect(page.get_by_role("listbox")).to_have_count(0)

            await page.locator('[data-page="workbench"]').click()
            await page.locator("#new-collection").click()
            form = page.locator("#collect-form")
            source = form.get_by_role("combobox", name="采集来源", exact=True)
            await source.click()
            await page.get_by_role("option", name="指定帖子", exact=True).click()
            await expect(form.locator('[name="post_targets"]')).to_be_visible()
            await expect(form.locator("#keywords")).not_to_be_visible()
            await source.click()
            await page.screenshot(path=str(tmp_path / "modal-dropdown.png"))
            await source.press("Escape")
            await expect(page.locator("#modal")).to_be_visible()
            await form.get_by_role("combobox", name="触发方式", exact=True).click()
            await page.get_by_role("option", name="增加定时作业").click()
            await form.locator('[name="cron_expression"]').fill("0 9 * * 1-5")
            await expect(form.get_by_role("combobox", name="常用周期")).to_have_text("工作日 09:00")
            timezone = form.get_by_role("combobox", name="时区", exact=True)
            await timezone.fill("Asia/")
            await expect(page.get_by_role("option", name="Asia/Tokyo")).to_be_visible()
            await page.get_by_role("option", name="Asia/Tokyo").click()
            await expect(timezone).to_have_value("Asia/Tokyo")
            await timezone.fill("Europe/London")
            await expect(page.locator(".control-empty")).to_contain_text("可继续输入")
            await timezone.press("Escape")
            await expect(timezone).to_have_value("Europe/London")
            await page.locator("#close-modal").click()

            await page.locator('[data-page="workbench"]').click()
            await page.get_by_role("tab", name="定时作业", exact=True).click()
            delete = page.locator(f'[data-schedule-delete="{schedule["id"]}"]')
            await delete.click()
            await expect(page.get_by_role("dialog", name="确认删除")).to_be_visible()
            await expect(page.get_by_role("button", name="取消", exact=True)).to_be_focused()
            await page.screenshot(path=str(tmp_path / "delete-confirmation.png"))
            await page.keyboard.press("Escape")
            await expect(delete).to_be_focused()
            await delete.click()
            async with page.expect_response(lambda r: r.request.method == "DELETE" and "/api/schedules/" in r.url):
                await page.get_by_role("button", name="确认删除", exact=True).click()
            await expect(delete).to_have_count(0)
            assert not errors
        finally:
            await browser.close()


@pytest.mark.browser
async def test_calendar_keyboard_bounds_clear_and_filter_values(ui_app, tmp_path):
    from playwright.async_api import async_playwright, expect

    _, base, _, _, _ = ui_app
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/#workbench")
            date = page.locator('input[name="date_from"]')
            await date.fill("2024-02-28")
            await page.get_by_role("button", name="选择开始日期", exact=True).click()
            await page.get_by_role("button", name="2024年2月28日", exact=True).press("ArrowRight")
            await expect(page.get_by_role("button", name="2024年2月29日", exact=True)).to_be_focused()
            await page.keyboard.press("Enter")
            await expect(date).to_have_value("2024-02-29")
            await expect(page.get_by_role("button", name="选择开始日期", exact=True)).to_be_focused()
            await page.get_by_role("button", name="选择开始日期", exact=True).click()
            await page.keyboard.press("Tab")
            await expect(page.get_by_role("button", name="清除", exact=True)).to_be_focused()
            await page.keyboard.press("Tab")
            await expect(page.get_by_role("button", name="今天", exact=True)).to_be_focused()
            await page.screenshot(path=str(tmp_path / "date-calendar.png"))
            await page.get_by_role("button", name="下个月", exact=True).click()
            await page.get_by_role("button", name="2024年3月15日", exact=True).click()
            await expect(date).to_have_value("2024-03-15")
            await page.get_by_role("button", name="选择结束日期", exact=True).click()
            await page.get_by_role("button", name="今天", exact=True).click()
            async with page.expect_response(lambda r: "/api/dashboard?" in r.url and "date_from=2024-03-15" in r.url):
                await page.locator("#run-filters").get_by_role("button", name="筛选", exact=True).click()
            date = page.locator('input[name="date_from"]')
            await expect(date).to_have_value("2024-03-15")
            await date.evaluate("el => { el.min = '2024-03-10'; el.max = '2024-03-20'; }")
            await page.get_by_role("button", name="选择开始日期", exact=True).click()
            await expect(page.get_by_role("button", name="2024年3月9日", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="2024年3月21日", exact=True)).to_be_disabled()
            await page.get_by_role("button", name="清除", exact=True).click()
            await expect(date).to_have_value("")
            await page.get_by_role("button", name="选择开始日期", exact=True).click()
            await page.keyboard.press("Escape")
            await expect(page.locator(".date-popup")).to_have_count(0)
            await date.fill("2024-02-30")
            assert not await date.evaluate("el => el.checkValidity()")
            await date.fill("2024-03-16")
            assert await date.evaluate("el => el.checkValidity()")
            # A narrow viewport must keep the calendar inside its visible edges.
            await page.set_viewport_size({"width": 390, "height": 720})
            await page.get_by_role("button", name="选择结束日期", exact=True).click()
            bounds = await page.locator(".date-popup").bounding_box()
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 720
            await page.screenshot(path=str(tmp_path / "mobile-calendar.png"))
            assert not errors
        finally:
            await browser.close()
