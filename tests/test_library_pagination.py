"""Pagination interactions with a large result set and changing query results."""
import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from test_ui_localization import ui_app as ui_app


@pytest.mark.browser
async def test_library_page_navigation_jump_and_responsive_layout(ui_app, tmp_path):
    from playwright.async_api import async_playwright, expect

    _, base, *_ = ui_app
    requested = []
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))

        async def results(route):
            query = parse_qs(urlsplit(route.request.url).query)
            current = int(query["page"][0])
            requested.append(current)
            await asyncio.sleep(.15)
            total = 1 if query.get("q") else 4645
            await route.fulfill(json={"page": current, "page_size": 24, "total": total,
                "items": [{"id": f"{current}-{i}", "platform": "xhs", "data": {
                    "title": f"第 {current} 页内容 {i}", "author_name": "测试作者"}}
                    for i in range(min(24, max(0, total - (current - 1) * 24)))]})

        await page.route("**/api/results?*", results)
        try:
            await page.goto(base + "/#library")
            pager = page.get_by_role("navigation", name="内容库分页")
            await expect(pager).to_contain_text("第 1 / 194 页")
            await expect(page.locator("#prev-page")).to_be_disabled()
            await expect(pager.locator('[aria-current="page"]')).to_have_text("1")
            # Paging stays in the viewport even at the start of a long card grid.
            rect = await pager.bounding_box()
            assert rect["y"] >= 0 and rect["y"] + rect["height"] <= 900
            await page.locator("#next-page").click()
            await expect(page.locator("#next-page")).to_be_disabled()
            await expect(pager).to_contain_text("第 2 / 194 页")
            await expect(page.locator(".result-count")).to_be_focused()
            rect = await page.locator(".result-count").bounding_box()
            assert 0 <= rect["y"] <= 30
            jump = page.get_by_role("spinbutton", name="目标页码")
            for invalid in ("", "0", "195", "1.5"):
                await jump.fill(invalid)
                await page.get_by_role("button", name="跳转", exact=True).click()
                assert not await jump.evaluate("el => el.checkValidity()")
                assert requested == [1, 2]
            await jump.fill("194")
            await jump.press("Enter")
            await expect(pager).to_contain_text("第 194 / 194 页")
            await expect(page.locator("#next-page")).to_be_disabled()
            await expect(page.locator(".content-card")).to_have_count(13)
            await pager.get_by_role("button", name="第 1 页", exact=True).click()
            await expect(pager).to_contain_text("第 1 / 194 页")
            await jump.fill("88")
            await page.get_by_role("button", name="跳转", exact=True).click()
            await expect(pager).to_contain_text("第 88 / 194 页")
            await expect(pager.get_by_role("button", name="第 87 页", exact=True)).to_be_visible()
            for width in (1920, 1024, 390):
                await page.set_viewport_size({"width": width, "height": 900})
                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
                await expect(jump).to_be_visible()
                await page.screenshot(path=str(tmp_path / f"pagination-{width}.png"))
            await page.locator('#library-search input').fill("唯一内容")
            await page.locator('#library-search').get_by_role("button").click()
            await expect(pager).to_contain_text("第 1 / 1 页")
            await expect(page.locator("#prev-page")).to_be_disabled()
            await expect(page.locator("#next-page")).to_be_disabled()
            assert requested == [1, 2, 194, 1, 88, 1]
            assert not errors
        finally:
            await browser.close()
