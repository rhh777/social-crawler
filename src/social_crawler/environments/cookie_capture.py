import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from playwright.async_api import async_playwright

from social_crawler.domain.models import CollectionError
from social_crawler.environments.adspower import DEFAULT_ADSPOWER_API_URL
from social_crawler.environments.browser_runtime import playwright_launch_options
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    capture_browser_environment,
    write_environment_snapshot,
)
from social_crawler.environments.kameleo import DEFAULT_KAMELEO_API_URL
from social_crawler.environments.managed_browser import (
    connect_managed_browser,
    is_managed_browser_provider,
    managed_browser_supports_platform,
)
from social_crawler.environments.proxy import prepare_playwright_proxy
from social_crawler.environments.session import (
    BROWSER_VIEWPORT,
    load_cookies,
    load_proxy,
)
from social_crawler.environments.session_recovery import (
    clear_platform_auth,
    file_digest,
    read_recovery_receipt,
    verified_storage_state,
    verify_profile,
)


@dataclass(frozen=True)
class PlatformLogin:
    name: str
    display_name: str
    url: str
    domain: str
    required_cookies: frozenset[str]
    default_user_agent: str | None = None
    default_locale: str | None = None
    default_timezone: str | None = None


PLATFORMS = {
    "xhs": PlatformLogin(
        name="xhs",
        display_name="小红书",
        url="https://www.xiaohongshu.com/explore",
        domain="xiaohongshu.com",
        required_cookies=frozenset({"web_session"}),
        default_locale="zh-CN",
        default_timezone="Asia/Shanghai",
    ),
    "rednote": PlatformLogin(
        name="rednote",
        display_name="RedNote",
        url="https://www.rednote.com/explore",
        domain="rednote.com",
        required_cookies=frozenset({"a1"}),
    ),
    "douyin": PlatformLogin(
        name="douyin",
        display_name="抖音",
        url="https://www.douyin.com/",
        domain="douyin.com",
        required_cookies=frozenset({"sessionid", "sessionid_ss"}),
    ),
}


def _platform_cookies(cookies: list[dict], platform: PlatformLogin) -> list[dict]:
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain == platform.domain or domain.endswith("." + platform.domain):
            result.append(cookie)
    return result


def _has_new_login_cookie(
    cookies: list[dict], platform: PlatformLogin, initial_values: dict[str, str]
) -> bool:
    values = {cookie.get("name"): cookie.get("value") for cookie in cookies}
    return any(
        values.get(name) and values.get(name) != initial_values.get(name)
        for name in platform.required_cookies
    )


async def wait_for_login_cookie(
    context,
    platform: PlatformLogin,
    *,
    timeout: float,
    poll_interval: float = 1.0,
    initial_values: dict[str, str] | None = None,
) -> list[dict]:
    initial_values = initial_values or {}
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        cookies = _platform_cookies(await context.cookies([platform.url]), platform)
        if _has_new_login_cookie(cookies, platform, initial_values):
            return cookies
        if asyncio.get_running_loop().time() >= deadline:
            required = " / ".join(sorted(platform.required_cookies))
            raise TimeoutError(
                f"等待 {platform.display_name} 登录超时，未检测到 {required} Cookie"
            )
        await asyncio.sleep(poll_interval)


async def initial_login_cookie_values(
    context,
    platform: PlatformLogin,
    *,
    timeout: float = 30,
    poll_interval: float = 0.25,
) -> dict[str, str]:
    """Capture XHS's anonymous session so it cannot be mistaken for QR login."""
    if platform.name not in {"xhs", "rednote"}:
        return {}
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        cookies = _platform_cookies(await context.cookies([platform.url]), platform)
        values = {
            cookie["name"]: cookie["value"]
            for cookie in cookies
            if cookie.get("name") in platform.required_cookies and cookie.get("value")
        }
        if values:
            return values
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("小红书页面未建立访客会话；拒绝把未确认的 Cookie 当作登录态")
        await asyncio.sleep(poll_interval)


async def wait_for_douyin_security_material(context, page, *, timeout: float = 30) -> None:
    """Ensure the persistent login profile contains the material used by WebSign."""
    deadline = asyncio.get_running_loop().time() + timeout
    search_bootstrapped = False
    while True:
        cookies = _platform_cookies(
            await context.cookies([PLATFORMS["douyin"].url]), PLATFORMS["douyin"]
        )
        names = {cookie.get("name") for cookie in cookies}
        ms_token = await page.evaluate("() => localStorage.getItem('xmst') || ''")
        if "UIFID" in names and ms_token:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= timeout / 2 and not search_bootstrapped and not ms_token:
            await page.goto(
                "https://www.douyin.com/search/%E6%8A%96%E9%9F%B3?type=general",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            search_bootstrapped = True
        if remaining <= 0:
            raise RuntimeError("抖音安全会话尚未生成 UIFID / xmst；未覆盖原 Cookie 文件")
        await asyncio.sleep(0.5)


async def wait_for_rednote_page_identity(page, *, timeout: float) -> dict[str, str]:
    """Wait for RedNote's hydrated user state without guessing a login-cookie name."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        identity = await page.evaluate(
            """() => {
                const state = window.__INITIAL_STATE__;
                const user = state && state.user;
                const logged = user && user.loggedIn;
                const loggedIn = logged && typeof logged === 'object' && '_value' in logged
                    ? logged._value : logged;
                const raw = user && user.userInfo;
                const info = raw && typeof raw === 'object' && '_value' in raw
                    ? raw._value : raw;
                const userId = info && String(info.userId || info.user_id || info.id || '');
                return loggedIn === true && userId
                    ? {user_id: userId, nickname: String(info.nickname || info.name || '')}
                    : null;
            }"""
        )
        if isinstance(identity, dict) and identity.get("user_id"):
            return identity
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("RedNote 登录超时，页面未确认本人身份")
        await asyncio.sleep(1)


def write_cookie_file(path: Path, cookies: list[dict] | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(cookies, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


async def save_browser_login(context, page, platform, *, output, profile,
                             expected_user_id=None, headless=False, browser_channel=None,
                             source="browser", admit=None, environment_session=None,
                             previous=None, old_digest=None):
    """Commit login only after verified completion; failed verification changes no files."""
    previous = read_recovery_receipt(profile) if previous is None else previous
    old_digest = file_digest(output) if old_digest is None else old_digest
    expected = expected_user_id or previous.get("user_id")
    identity = await verify_profile(
        context, page, platform.name, expected_user_id=expected, admit=admit,
    )
    persisted = await verified_storage_state(context, platform.name)
    snapshot = await capture_browser_environment(
        page,
        browser_version=context.browser.version if context.browser else None,
        headless=headless,
        browser_channel=(
            browser_channel
        ),
        source="session_" + source,
    )
    if environment_session is not None:
        audit_environment(
            environment_session, snapshot,
            budget=getattr(admit, "__self__", None), stage="session_recovery",
        )
    # Verify once more immediately before commit, pinned to the first identity.
    await verify_profile(
        context, page, platform.name,
        expected_user_id=identity["user_id"], admit=admit,
    )
    persisted = await verified_storage_state(context, platform.name)
    if file_digest(output) != old_digest:
        raise CollectionError("session_changed")
    # Separate files cannot be atomically replaced as a group. The receipt is
    # the commit marker; any failure leaves the account unavailable.
    write_environment_snapshot(profile, snapshot)
    write_cookie_file(output, persisted)
    load_cookies(output, platform.name)
    write_cookie_file(profile / "session-recovery.json", previous | {
        "user_id": identity["user_id"], "verified_at": time.time(),
        "old_digest": old_digest, "new_digest": file_digest(output),
        "source": source, "outcome": "recovered",
    })
    return len(persisted["cookies"])

async def capture_platform_cookies(
    platform: PlatformLogin,
    *,
    output: Path,
    profile: Path,
    timeout: float,
    channel: str | None = None,
    proxy_env: str | None = None,
    proxy_file: str | None = None,
    mode: Literal["recover", "reauth"] = "recover",
    expected_user_id: str | None = None,
    user_agent: str | None = None,
    headless: bool = False,
    browser_provider: Literal["chromium", "adspower", "kameleo"] = "chromium",
    adspower_profile_id: str | None = None,
    adspower_api_url: str = DEFAULT_ADSPOWER_API_URL,
    adspower_start_timeout: int = 90,
    kameleo_profile_id: str | None = None,
    kameleo_api_url: str = DEFAULT_KAMELEO_API_URL,
    kameleo_start_timeout: int = 90,
    admit=None,
    environment_session=None,
) -> int:
    """Capture verified identity; caller must hold account/profile/proxy locks."""
    if mode not in {"recover", "reauth"}:
        raise ValueError("Invalid capture mode")
    if browser_provider == "adspower" and not adspower_profile_id:
        raise ValueError("AdsPower provider requires adspower_profile_id")
    if browser_provider == "kameleo" and not kameleo_profile_id:
        raise ValueError("Kameleo provider requires kameleo_profile_id")
    if is_managed_browser_provider(browser_provider) and not managed_browser_supports_platform(
        browser_provider, platform.name
    ):
        raise ValueError(f"{browser_provider} provider does not support {platform.name}")
    deadline = asyncio.get_running_loop().time() + timeout
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    previous = read_recovery_receipt(profile)
    expected = expected_user_id or previous.get("user_id")
    old_digest = file_digest(output)
    launch_options = {
        "headless": headless if mode == "recover" else False,
        "viewport": BROWSER_VIEWPORT,
    }
    snapshot_navigator = (getattr(environment_session, "environment_snapshot", None) or {}).get(
        "navigator"
    ) or {}
    locale = (
        getattr(getattr(environment_session, "config", None), "locale", None)
        or snapshot_navigator.get("language")
        or platform.default_locale
    )
    timezone_id = (
        getattr(getattr(environment_session, "config", None), "timezone_id", None)
        or (getattr(environment_session, "environment_snapshot", None) or {}).get("timezone")
        or platform.default_timezone
    )
    if locale:
        launch_options["locale"] = locale
    if timezone_id:
        launch_options["timezone_id"] = timezone_id
    launch_options.update(playwright_launch_options(channel))
    reference = getattr(environment_session, "environment_snapshot", None) or {}
    recovery_ua = (reference.get("navigator") or {}).get("user_agent") if mode == "recover" else None
    if user_agent or recovery_ua or platform.default_user_agent:
        launch_options["user_agent"] = user_agent or recovery_ua or platform.default_user_agent
    proxy_bridge = None
    if browser_provider == "chromium":
        proxy_url = load_proxy(proxy_env=proxy_env, proxy_file=proxy_file)
        proxy, proxy_bridge = await prepare_playwright_proxy(proxy_url)
        if proxy:
            launch_options["proxy"] = proxy
    try:
        async with asyncio.timeout(timeout):
            if admit:
                await admit("search")
            async with async_playwright() as playwright:
                managed = context = None
                try:
                    if is_managed_browser_provider(browser_provider):
                        managed_config = SimpleNamespace(
                            browser_provider=browser_provider,
                            adspower_profile_id=adspower_profile_id,
                            adspower_api_url=adspower_api_url,
                            adspower_start_timeout=adspower_start_timeout,
                            kameleo_profile_id=kameleo_profile_id,
                            kameleo_api_url=kameleo_api_url,
                            kameleo_start_timeout=kameleo_start_timeout,
                        )
                        managed = await connect_managed_browser(
                            playwright,
                            managed_config,
                            headless=launch_options["headless"],
                        )
                        context = managed.context
                    else:
                        context = await playwright.chromium.launch_persistent_context(
                            str(profile.resolve()), **launch_options
                        )
                    if mode == "reauth":
                        await clear_platform_auth(context, platform.name)
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(platform.url, wait_until="domcontentloaded", timeout=60_000)
                    if mode == "reauth":
                        if platform.name == "rednote":
                            await wait_for_rednote_page_identity(page, timeout=timeout)
                        else:
                            initial = await initial_login_cookie_values(
                                context, platform, timeout=min(timeout, 30)
                            )
                            await wait_for_login_cookie(
                                context, platform, timeout=timeout, initial_values=initial,
                            )
                        await asyncio.sleep(2)
                    return await save_browser_login(
                        context, page, platform, output=output, profile=profile,
                        expected_user_id=expected, headless=launch_options["headless"],
                        browser_channel=(
                            browser_provider
                            if is_managed_browser_provider(browser_provider)
                            else channel
                        ),
                        source=mode, admit=admit, environment_session=environment_session,
                        previous=previous, old_digest=old_digest,
                    )
                except CollectionError as exc:
                    if exc.kind in {"verification_required", "access_denied", "rate_limit"}:
                        # Keep the visible page available for a bounded manual inspection.
                        # No reloads or identity retries while platform intervention is shown.
                        if not launch_options["headless"]:
                            print(f"[{platform.display_name}] 需要人工处理，窗口保留 30 秒。")
                            remaining = deadline - asyncio.get_running_loop().time() - 2
                            await asyncio.sleep(max(0, min(30, remaining)))
                    raise
                finally:
                    if managed:
                        await managed.close(suppress_stop_errors=True)
                    elif context:
                        await context.close()
    finally:
        if proxy_bridge:
            await proxy_bridge.close()
