"""Explicit, account-scoped browser inspection, independent of diagnostics."""

import asyncio
import os
import queue
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from social_crawler.domain.models import CollectionError
from social_crawler.environments.browser_runtime import playwright_launch_options
from social_crawler.environments.cookie_capture import (
    PLATFORMS,
    save_browser_login,
    verify_profile,
)
from social_crawler.environments.environment_snapshot import load_environment_snapshot
from social_crawler.environments.kasmvnc import KasmDesktop
from social_crawler.environments.managed_browser import (
    connect_managed_browser,
    is_managed_browser_provider,
)
from social_crawler.environments.proxy import prepare_playwright_proxy
from social_crawler.environments.session import exclusive, load_cookies, load_proxy


def browser_open_error(exc, *, provider, platform):
    """Return a safe, actionable message without exposing connection details."""

    if (
        provider == 'adspower'
        and platform == 'douyin'
        and 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc)
    ):
        return (
            'AdsPower 已限制当前账号访问抖音。请先在 AdsPower「设置 → 我的账户」'
            '绑定中国大陆手机号或完成 KYC 实名认证，关闭该环境后再点“重新打开”。'
        )
    return '浏览器未能打开或已断开，请确认浏览器服务已安装且该账号未被其他进程占用后重试。'


async def prepare_login_page(context, url):
    """Reuse Chromium's startup tab and remove only redundant blank tabs."""

    pages = [page for page in context.pages if not page.is_closed()]
    blank_pages = [page for page in pages if page.url == 'about:blank']
    page = blank_pages[0] if blank_pages else await context.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
    except BrowserTimeout:
        pass  # Keep a slow or challenged page available for the user.
    for candidate in list(context.pages):
        if candidate is not page and not candidate.is_closed() and candidate.url == 'about:blank':
            await candidate.close()
    await page.bring_to_front()
    return page


class CollectionBrowserViewer:
    """Read-only desktop attached to one running headed collection."""

    def __init__(self, run_id, account, *, desktop_factory=KasmDesktop):
        self.id = uuid.uuid4().hex
        self.run_id = run_id
        self.account_id = account['id']
        self.name = account['name']
        self.state = 'opening'
        self.error = None
        self.read_only = True
        self.desktop = None
        self.access_token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        self.last_activity = time.monotonic()
        self.desktop_factory = desktop_factory

    @property
    def active(self):
        return not self.stopped.is_set() and self.state in {'opening', 'open'}

    @property
    def display(self):
        return self.desktop.display if self.desktop else None

    def start(self):
        if KasmDesktop.available():
            try:
                self.desktop = self.desktop_factory().start()
            except Exception:
                # Watching is optional. A desktop failure must not turn an
                # otherwise valid collection into a failed run.
                self.desktop = None
                self.error = '实时画面不可用，采集仍在继续。'
        self.state = 'open'
        return self

    def close(self):
        if self.stopped.is_set():
            return
        self.state = 'closing'
        self.stopped.set()
        if self.desktop:
            self.desktop.close()
        self.state = 'closed'

    def public(self):
        result = {
            'id': self.id,
            'account_id': self.account_id,
            'run_id': self.run_id,
            'name': self.name,
            'state': self.state,
            'error': self.error,
            'saved': False,
            'read_only': True,
            'transport': 'kasmvnc' if self.desktop else 'native',
        }
        if self.desktop and self.state in {'opening', 'open'}:
            result['viewer_url'] = (
                f'/browser/{self.id}/?path=browser/{self.id}/websockify'
                '&resize=remote&enable_ime=0&view_only=1&autoconnect=1'
            )
        return result

    def request(self, command):
        self.last_activity = time.monotonic()
        if command.get('kind') != 'status':
            raise ValueError('采集浏览器仅供观看，不能从查看窗口操作或关闭')
        return self.public()


class AccountBrowser:
    """One owner thread per profile; all page commands execute on that thread."""

    def __init__(self, account, env, keys, *, headless=False, idle_seconds=120,
                 on_saved=None, auto_save=False, auto_check_seconds=1):
        self.id = uuid.uuid4().hex
        self.account_id = account['id']
        self.name = account['name']
        self.platform = account['platform']
        self.env = env
        self.keys = frozenset(keys)
        self.headless = headless
        self.idle_seconds = idle_seconds
        self.state = 'opening'
        self.error = None
        self.desktop = None
        self.access_token = secrets.token_urlsafe(32)
        self.on_saved = on_saved
        self.auto_save = auto_save
        self.auto_check_seconds = auto_check_seconds
        self.auto_initialized = False
        self.auto_baseline = None
        self.auto_require_change = False
        self.auto_last_candidate = None
        self.auto_retry_at = 0.0
        self.auto_next_check = 0.0
        self.saved = False
        self.stopped = threading.Event()
        self.commands = queue.Queue(maxsize=64)
        self.last_activity = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def active(self):
        return self.thread.is_alive()

    def public(self):
        result = {'id': self.id, 'account_id': self.account_id, 'name': self.name,
                  'state': self.state, 'error': self.error, 'saved': self.saved,
                  'transport': 'kasmvnc' if self.desktop else 'native',
                  'profile_auth': is_managed_browser_provider(self.env.browser_provider)}
        if self.desktop and self.state in {'opening', 'open', 'saving'}:
            result['viewer_url'] = (f'/browser/{self.id}/?path=browser/{self.id}/websockify'
                                    '&resize=remote&enable_ime=1&autoconnect=1')
        return result

    def start(self):
        self.thread.start()

    def enable_auto_save(self):
        if not self.auto_save:
            self.auto_initialized = False
            self.auto_next_check = 0.0
        self.auto_save = True

    def close(self):
        if self.state not in {'closed', 'failed'}:
            self.state = 'closing'
        self.stopped.set()
        self.thread.join(timeout=5)
        return self.public()

    def request(self, command):
        self.last_activity = time.monotonic()
        if command.get('kind') == 'status':
            return self.public()
        if command.get('kind') != 'save':
            raise ValueError('无效浏览器操作')
        if self.state != 'open':
            return self.public()
        future = Future()
        self.commands.put_nowait((command, future))
        return future.result(timeout=75)

    def _run(self):
        try:
            with exclusive(list(self.keys)):
                try:
                    if not self.headless:
                        if KasmDesktop.available():
                            desktop = KasmDesktop().start()
                            self.desktop = desktop
                        elif sys.platform == 'linux':
                            raise RuntimeError('KasmVNC is not installed')
                    asyncio.run(self._serve())
                finally:
                    if self.desktop:
                        self.desktop.close()
        except Exception as exc:
            # Never expose proxy credentials, launch arguments, or page contents in errors.
            self.error = browser_open_error(
                exc,
                provider=self.env.browser_provider,
                platform=self.platform,
            )
            self.state = 'failed'
        finally:
            if self.state != 'failed':
                self.state = 'closed'
            while not self.commands.empty():
                _, future = self.commands.get_nowait()
                future.set_result(self.public())

    async def _serve(self):
        env = self.env
        platform = PLATFORMS[self.platform]
        profile = Path(env.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        snapshot, _ = load_environment_snapshot(str(profile))
        snapshot = snapshot or {}
        navigator = snapshot.get('navigator') or {}
        options = {'headless': self.headless, 'args': ['--restore-last-session', '--start-maximized'], 'no_viewport': True,
                   **playwright_launch_options(env.browser_channel)}
        if self.desktop:
            options['env'] = dict(os.environ, DISPLAY=self.desktop.display)
        for key, value in {
            'user_agent': env.user_agent or navigator.get('user_agent') or platform.default_user_agent,
            'locale': env.locale or navigator.get('language') or platform.default_locale,
            'timezone_id': env.timezone_id or snapshot.get('timezone') or platform.default_timezone,
        }.items():
            if value:
                options[key] = value
        bridge = managed = context = None
        try:
            if not is_managed_browser_provider(env.browser_provider):
                proxy, bridge = await prepare_playwright_proxy(load_proxy(
                    proxy_env=env.proxy_env, proxy_file=env.proxy_file))
                if proxy:
                    options['proxy'] = proxy
            async with async_playwright() as playwright:
                try:
                    if is_managed_browser_provider(env.browser_provider):
                        managed = await connect_managed_browser(
                            playwright,
                            env,
                            headless=self.headless,
                            display=self.desktop.display if self.desktop else None,
                        )
                        context = managed.context
                    else:
                        context = await playwright.chromium.launch_persistent_context(str(profile.resolve()), **options)
                    # Seed imported credentials only for an empty site profile. Never clear
                    # auth, replace an existing browser session, or write exported credentials.
                    if (
                        not is_managed_browser_provider(env.browser_provider)
                        and not await context.cookies([platform.url])
                        and Path(env.cookie_file).is_file()
                    ):
                        try:
                            await context.add_cookies(load_cookies(Path(env.cookie_file), self.platform))
                        except ValueError:
                            pass
                    self.context = context
                    self.page = await prepare_login_page(context, platform.url)
                    context.on('page', lambda page: setattr(self, 'page', page))
                    self.state = 'open'
                    if self.desktop and is_managed_browser_provider(env.browser_provider):
                        # Managed browsers can override launch-time window flags. Maximize
                        # the actual window after attachment so desktop resizes
                        # also resize it instead of leaving a large empty border.
                        cdp = await context.new_cdp_session(self.page)
                        try:
                            window = await cdp.send('Browser.getWindowForTarget')
                            await cdp.send('Browser.setWindowBounds', {
                                'windowId': window['windowId'],
                                'bounds': {'windowState': 'maximized'},
                            })
                        finally:
                            await cdp.detach()
                    self.state = 'open'
                    self.last_activity = time.monotonic()
                    while not self.stopped.is_set():
                        if self.desktop and any(p.poll() is not None for p in self.desktop.processes):
                            raise RuntimeError('Browser desktop exited')
                        if time.monotonic() - self.last_activity > self.idle_seconds:
                            break
                        if self.page.is_closed():
                            if not context.pages:
                                break
                            self.page = context.pages[-1]
                        if await self._maybe_auto_save(platform):
                            break
                        try:
                            command, future = self.commands.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(.05)
                            continue
                        try:
                            self.state = 'saving'
                            result = await asyncio.wait_for(
                                self._save_login(source='browser'), timeout=60
                            )
                            future.set_result(self.public() | result)
                        except Exception as exc:
                            self.state = 'open'
                            message = '暂时无法保存登录，浏览器保持打开，请稍后重试。'
                            if isinstance(exc, CollectionError):
                                message = {
                                    'auth_expired': '请先在浏览器中完成登录，再保存。',
                                    'verification_required': '请在浏览器中完成平台验证，再保存。',
                                    'identity_mismatch': '当前登录的账号与配置不一致，请切换为对应账号。',
                                    'identity_unverified': '暂时未能确认登录，请完成登录后重试。',
                                }.get(exc.kind, message)
                            future.set_result(self.public() | {'error': message})
                finally:
                    if managed:
                        await managed.close()
                    elif context:
                        await context.close()
        finally:
            if bridge:
                await bridge.close()

    def _credential_file_configured(self, platform):
        try:
            names = {cookie.get('name') for cookie in load_cookies(
                Path(self.env.cookie_file), self.platform
            )}
        except (OSError, ValueError):
            return False
        return bool(platform.required_cookies.intersection(names))

    def _platform_pages(self, platform):
        return [p for p in self.context.pages if not p.is_closed()
                and ((host := urlsplit(p.url).hostname or '') == platform.domain
                     or host.endswith('.' + platform.domain))]

    async def _auto_login_candidate(self, platform):
        cookies = await self.context.cookies([platform.url])
        values = {cookie.get('name'): cookie.get('value') for cookie in cookies
                  if cookie.get('value')}
        if not platform.required_cookies.intersection(values):
            return None
        names = set(platform.required_cookies)
        if platform.name == 'douyin':
            names.add('UIFID')
        candidate = tuple(sorted((name, values.get(name, '')) for name in names))
        if platform.name == 'douyin':
            pages = self._platform_pages(platform)
            xmst = False
            if pages:
                try:
                    xmst = bool(await pages[-1].evaluate(
                        "() => localStorage.getItem('xmst') || ''"
                    ))
                except Exception:
                    pass
            candidate += (('xmst_ready', str(xmst)),)
        return candidate

    async def _maybe_auto_save(self, platform):
        if not self.auto_save or self.state != 'open':
            return False
        now = time.monotonic()
        if not self.auto_initialized:
            self.auto_baseline = await self._auto_login_candidate(platform)
            self.auto_require_change = (
                not is_managed_browser_provider(self.env.browser_provider)
                and self._credential_file_configured(platform)
            )
            self.auto_initialized = True
            self.auto_next_check = now + self.auto_check_seconds
            return False
        if now < self.auto_next_check:
            return False
        self.auto_next_check = now + self.auto_check_seconds
        candidate = await self._auto_login_candidate(platform)
        if candidate is None:
            return False
        if self.auto_require_change and candidate == self.auto_baseline:
            return False
        if candidate == self.auto_last_candidate and now < self.auto_retry_at:
            return False
        self.auto_last_candidate = candidate
        self.state = 'saving'
        try:
            await asyncio.wait_for(self._save_login(source='browser_auto'), timeout=60)
        except Exception:
            # Login UI, verification and security material can settle at different
            # times. Failed checks never write credentials; retry at a bounded rate.
            self.state = 'open'
            self.auto_retry_at = time.monotonic() + 5
            return False
        return True

    async def _save_login(self, *, source):
        platform = PLATFORMS[self.platform]
        pages = self._platform_pages(platform)
        if not pages:
            raise CollectionError('auth_expired')
        page = pages[-1]
        for candidate in pages:
            if await candidate.evaluate('document.visibilityState') == 'visible':
                page = candidate
                break
        if is_managed_browser_provider(self.env.browser_provider):
            await verify_profile(
                self.context,
                page,
                platform.name,
                expected_user_id=self.env.expected_user_id,
            )
            count = 0
        else:
            count = await save_browser_login(
                self.context, page, platform,
                output=Path(self.env.cookie_file), profile=Path(self.env.profile_dir),
                expected_user_id=self.env.expected_user_id,
                headless=self.headless,
                browser_channel=self.env.browser_channel,
                source=source,
            )
        if self.on_saved:
            self.on_saved()
        self.saved = True
        self.stopped.set()
        self.state = 'closing'
        return {'cookie_count': count}
