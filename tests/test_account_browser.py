"""Local real Chromium coverage; no platform traffic or user credentials."""

import asyncio
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from test_web import console as console  # noqa: F401

from social_crawler.domain.models import CollectionError
from social_crawler.environments import account_browser
from social_crawler.environments.account_browser import (
    AccountBrowser,
    CollectionBrowserViewer,
    browser_open_error,
    prepare_login_page,
)
from social_crawler.environments.cookie_capture import PLATFORMS
from social_crawler.interfaces import web


@pytest.fixture
def local_site(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'<html><body><input style="position:absolute;left:20px;top:20px;width:200px" id="entry"><div id="state"></div><script>document.querySelector("#state").textContent=localStorage.getItem("identity")||"guest"</script></body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    monkeypatch.setitem(PLATFORMS, 'xhs', replace(PLATFORMS['xhs'], url=url, domain='127.0.0.1'))
    yield url
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def ready(session):
    deadline = time.monotonic() + 20
    while session.state == 'opening' and time.monotonic() < deadline:
        time.sleep(.05)
    assert session.state == 'open', session.public()


def test_adspower_douyin_block_has_actionable_safe_error():
    error = RuntimeError(
        'Page.goto: net::ERR_BLOCKED_BY_ADMINISTRATOR at https://www.douyin.com/'
        ' proxy-password=must-not-leak'
    )

    message = browser_open_error(error, provider='adspower', platform='douyin')

    assert '绑定中国大陆手机号' in message
    assert 'KYC' in message
    assert 'must-not-leak' not in message


def test_other_browser_open_errors_keep_generic_safe_message():
    message = browser_open_error(
        RuntimeError('connection failed with secret launch arguments'),
        provider='adspower',
        platform='douyin',
    )

    assert '浏览器服务已安装' in message
    assert 'secret' not in message


@pytest.mark.asyncio
async def test_managed_browser_confirms_login_without_exporting_cookies(
    tmp_path, monkeypatch
):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text('[{"name":"web_session","value":"stale"}]')
    page = SimpleNamespace(evaluate=AsyncMock(return_value="visible"))
    saved = []
    env = SimpleNamespace(
        browser_provider="kameleo",
        cookie_file=str(cookie_file),
        profile_dir=str(tmp_path / "profile"),
        expected_user_id=None,
        browser_channel=None,
    )
    session = AccountBrowser(
        {"id": "managed", "name": "Managed", "platform": "xhs"},
        env,
        [],
        on_saved=lambda: saved.append(True),
    )
    session.context = SimpleNamespace()
    session._platform_pages = lambda _platform: [page]
    verify = AsyncMock(return_value={"user_id": "user-a"})
    monkeypatch.setattr(account_browser, "verify_profile", verify)
    export = AsyncMock(side_effect=AssertionError("managed auth must not export cookies"))
    monkeypatch.setattr(account_browser, "save_browser_login", export)

    result = await session._save_login(source="browser")

    assert result == {"cookie_count": 0}
    assert json.loads(cookie_file.read_text())[0]["value"] == "stale"
    verify.assert_awaited_once()
    export.assert_not_awaited()
    assert saved == [True]


def test_collection_viewers_use_independent_read_only_desktops(monkeypatch):
    created = []

    class Desktop:
        def __init__(self):
            self.display = f':{100 + len(created)}'
            self.port = 6100 + len(created)
            self.authorization = 'Basic test'
            self.closed = False
            created.append(self)

        def start(self):
            return self

        def close(self):
            self.closed = True

    monkeypatch.setattr(account_browser.KasmDesktop, 'available', staticmethod(lambda: True))
    account = {'id': 'account-a', 'name': 'A'}
    first = CollectionBrowserViewer('run-a', account, desktop_factory=Desktop).start()
    second = CollectionBrowserViewer('run-b', account, desktop_factory=Desktop).start()

    assert first.display == ':100'
    assert second.display == ':101'
    assert first.public()['read_only'] is True
    assert 'view_only=1' in first.public()['viewer_url']
    with pytest.raises(ValueError, match='仅供观看'):
        first.request({'kind': 'save'})

    first.close()
    second.close()
    assert all(desktop.closed for desktop in created)


def test_running_collection_viewer_is_reused_instead_of_opening_profile(console):
    account = console.add_account({'platform': 'xhs', 'name': '正在采集'})
    viewer = CollectionBrowserViewer('run-a', account)
    viewer.state = 'open'
    console.collection_browsers[viewer.run_id] = viewer

    result = console.open_account_browser(account['id'])

    assert result['id'] == viewer.id
    assert result['read_only'] is True
    assert console.account_browser_command(account['id'], {
        'session_id': viewer.id, 'kind': 'status'
    })['state'] == 'open'
    with pytest.raises(web.ConsoleError, match='仅供观看'):
        console.account_browser_command(account['id'], {
            'session_id': viewer.id, 'kind': 'close'
        })
    with pytest.raises(web.ConsoleError, match='停止任务'):
        console.open_account_browser(account['id'], auto_save=True)

    viewer.close()


@pytest.mark.asyncio
async def test_login_page_reuses_one_startup_blank_and_closes_only_redundant_blanks():
    restored = AsyncMock()
    restored.url = 'https://example.com/restored'
    restored.is_closed = lambda: False
    first_blank = AsyncMock()
    first_blank.url = 'about:blank'
    first_blank.is_closed = lambda: False
    extra_blank = AsyncMock()
    extra_blank.url = 'about:blank'
    extra_blank.is_closed = lambda: False
    context = AsyncMock()
    context.pages = [restored, first_blank, extra_blank]

    page = await prepare_login_page(context, 'https://example.com/login')

    assert page is first_blank
    first_blank.goto.assert_awaited_once_with(
        'https://example.com/login', wait_until='domcontentloaded', timeout=20000
    )
    first_blank.bring_to_front.assert_awaited_once()
    extra_blank.close.assert_awaited_once()
    restored.close.assert_not_awaited()
    context.new_page.assert_not_awaited()


@pytest.mark.browser
def test_browser_preserves_profile_and_credentials_and_scopes_locks(console, monkeypatch, local_site):
    a = console.add_account({'platform':'xhs', 'name':'A'})
    b = console.add_account({'platform':'xhs', 'name':'B'})
    env = console.environment('xhs', a['id'])

    async def seed():
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(env.profile_dir, headless=True)
            await context.add_cookies([{'name':'session', 'value':'original', 'url':local_site, 'expires':time.time()+3600}])
            page = await context.new_page()
            await page.goto(local_site)
            await page.evaluate("localStorage.setItem('identity','existing-user')")
            await context.close()

    asyncio.run(seed())
    Path(env.cookie_file).write_text('web_session=export-unchanged')
    before = Path(env.cookie_file).read_bytes()
    with console.store() as store:
        account_before = store.get_account(a['id'])
    monkeypatch.setattr(web, 'AccountBrowser', lambda *args, **kwargs: AccountBrowser(*args, headless=True, **kwargs))
    info = console.open_account_browser(a['id'])
    session = console.account_browsers[a['id']]
    try:
        ready(session)
        assert all(page.url != 'about:blank' for page in session.context.pages)
        assert console.open_account_browser(a['id'])['id'] == info['id']
        assert console.resources_busy(console.account_resource_keys('xhs', a['id']))
        assert not console.resources_busy(console.account_resource_keys('xhs', b['id']))
        with pytest.raises(web.ConsoleError, match='该账号或浏览器环境'):
            console.update_account(a['id'], {'name':'cannot-edit'})
        with pytest.raises(web.ConsoleError):
            console.account_browser_command(b['id'], {'session_id':info['id'], 'kind':'frame'})
        assert session.request({'kind':'status'})['state'] == 'open'
        with pytest.raises(ValueError):
            session.request({'kind':'frame'})
    finally:
        console.close_account_browsers()
    assert not session.active
    assert not console.resources_busy(console.account_resource_keys('xhs', a['id']))
    assert Path(env.cookie_file).read_bytes() == before
    with console.store() as store:
        assert store.get_account(a['id']) == account_before

    async def inspect():
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(env.profile_dir, headless=True)
            assert next(c for c in await context.cookies(local_site) if c['name']=='session')['value']=='original'
            page = await context.new_page()
            await page.goto(local_site)
            assert await page.locator('#state').inner_text() == 'existing-user'
            await context.close()

    asyncio.run(inspect())


@pytest.mark.browser
async def test_account_menu_toggle_and_browser_viewer(console, monkeypatch, local_site):
    from playwright.async_api import expect

    account = console.add_account({'platform':'xhs', 'name':'浏览器测试'})
    monkeypatch.setattr(web, 'AccountBrowser', lambda *args, **kwargs: AccountBrowser(*args, headless=True, **kwargs))
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'http://127.0.0.1:{server.server_port}/#accounts')
            toggle = page.locator(f'[data-account-toggle="{account["id"]}"]')
            await toggle.click()
            await expect(toggle).to_have_attribute('aria-checked','false')
            await toggle.click()
            await expect(toggle).to_have_attribute('aria-checked','true')
            row = page.get_by_role('row').filter(has_text='浏览器测试')
            before = await row.bounding_box()
            await page.locator(f'[data-account-menu="{account["id"]}"]').click()
            await expect(page.get_by_role('button',name='账号设置',exact=True)).to_be_visible()
            after = await row.bounding_box()
            assert before['height'] == after['height']
            for removed_item in ('编辑资料', '浏览器环境', '设备指纹', '设置代理'):
                await expect(page.get_by_role('button', name=removed_item, exact=True)).to_have_count(0)
            await page.get_by_role('button',name='账号设置',exact=True).click()
            await expect(page.locator('#account-detail-form').get_by_role('combobox', name='使用代理')).to_be_visible()
            await page.locator('#close-account-detail').click()
            async with page.expect_popup() as popup:
                await row.get_by_role('button',name='打开浏览器',exact=True).first.click()
            viewer = await popup.value
            viewer.on('pageerror', lambda error: errors.append(str(error)))
            await expect(viewer.locator('#message')).to_contain_text('已打开本机浏览器', timeout=20000)
            await expect(viewer.locator('#title')).to_contain_text('浏览器测试')
            await viewer.locator('#close-browser').click()
            await expect(viewer.locator('#message')).to_contain_text('浏览器已关闭')
            assert not console.account_browsers[account['id']].active
            assert not errors
            await browser.close()
    finally:
        console.close_account_browsers()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
async def test_account_pool_attaches_to_running_collection_viewer(console):
    from playwright.async_api import expect

    account = console.add_account({'platform': 'xhs', 'name': '采集中的账号'})
    viewer = CollectionBrowserViewer('run-a', account)
    viewer.state = 'open'
    console.collection_browsers[viewer.run_id] = viewer
    server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f'http://127.0.0.1:{server.server_port}/#accounts')
            row = page.get_by_role('row').filter(has_text='采集中的账号')
            async with page.expect_popup() as popup:
                await row.get_by_role('button', name='观看采集', exact=True).click()
            watched = await popup.value
            await expect(watched.locator('#title')).to_contain_text('采集画面')
            await expect(watched.locator('#message')).to_contain_text('本机浏览器窗口')
            await expect(watched.locator('#save-login')).to_be_hidden()
            await expect(watched.locator('#close-browser')).to_be_hidden()
            await browser.close()
    finally:
        viewer.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
def test_unused_viewer_releases_resources(console, monkeypatch, local_site):
    account = console.add_account({'platform':'xhs', 'name':'idle'})
    monkeypatch.setattr(web, 'AccountBrowser', lambda *args, **kwargs: AccountBrowser(*args, headless=True, idle_seconds=.3, **kwargs))
    console.open_account_browser(account['id'])
    session = console.account_browsers[account['id']]
    ready(session)
    session.thread.join(timeout=5)
    assert not session.active and session.state == 'closed'
    assert not console.resources_busy(console.account_resource_keys('xhs', account['id']))


@pytest.mark.browser
def test_login_mode_auto_saves_after_verified_login(console, monkeypatch, local_site):
    account = console.add_account({'platform':'xhs', 'name':'auto-save'})
    commit = AsyncMock(return_value=3)
    monkeypatch.setattr(account_browser, 'save_browser_login', commit)
    monkeypatch.setattr(
        AccountBrowser, '_auto_login_candidate',
        AsyncMock(return_value=(('web_session', 'logged-in'),)),
    )
    monkeypatch.setattr(
        web, 'AccountBrowser',
        lambda *args, **kwargs: AccountBrowser(
            *args, headless=True, auto_check_seconds=.05, **kwargs
        ),
    )

    console.open_account_browser(account['id'], auto_save=True)
    session = console.account_browsers[account['id']]
    session.thread.join(timeout=10)

    assert not session.active
    assert session.saved and session.state == 'closed'
    assert commit.await_args.kwargs['source'] == 'browser_auto'
    with console.store() as store:
        assert store.get_account(account['id'])['status'] == 'ready'


def test_browser_rejects_busy_and_deleted_account(console):
    account = console.add_account({'platform':'xhs','name':'busy'})
    console.jobs['busy'] = {'id':'busy','kind':'collection','status':'running'}
    console.job_resources['busy'] = frozenset(console.account_resource_keys('xhs',account['id']))
    with pytest.raises(web.ConsoleError, match='该账号或浏览器环境'):
        console.open_account_browser(account['id'])

    assert not console.account_browsers
    console.jobs['busy']['status'] = 'completed'
    console.delete_account(account['id'])
    with pytest.raises(web.ConsoleError, match='账号已删除'):
        console.open_account_browser(account['id'])


@pytest.mark.browser
@pytest.mark.parametrize('disabled', [False, True])
def test_explicit_save_keeps_failed_login_open_and_preserves_manual_disable(console, monkeypatch, local_site, disabled):
    account = console.add_account({'platform':'xhs', 'name':'save-login'})
    if disabled:
        console.set_account_state(account['id'], {'status':'disabled'})
    env = console.environment('xhs', account['id'])
    before_cookie = Path(env.cookie_file).read_bytes()
    with console.store() as store:
        before_account = store.get_account(account['id'])
    commit = AsyncMock(side_effect=CollectionError('verification_required'))
    monkeypatch.setattr(account_browser, 'save_browser_login', commit)
    monkeypatch.setattr(web, 'AccountBrowser', lambda *args, **kwargs: AccountBrowser(*args, headless=True, **kwargs))
    console.open_account_browser(account['id'])
    session = console.account_browsers[account['id']]
    try:
        ready(session)
        result = session.request({'kind':'save'})
        assert result['state'] == 'open' and '验证' in result['error']
        assert session.active
        assert Path(env.cookie_file).read_bytes() == before_cookie
        with console.store() as store:
            assert store.get_account(account['id']) == before_account
        commit.side_effect = None
        commit.return_value = 3
        result = session.request({'kind':'save'})
        assert result['saved'] and result['cookie_count'] == 3
        session.thread.join(timeout=10)
        assert not session.active
        assert not console.resources_busy(session.keys)
        with console.store() as store:
            assert store.get_account(account['id'])['status'] == ('disabled' if disabled else 'ready')
        assert commit.await_count == 2
    finally:
        console.close_account_browsers()
