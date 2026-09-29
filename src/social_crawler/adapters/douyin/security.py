import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from social_crawler.domain.models import CollectionError
from social_crawler.environments.browser_runtime import CHROME_MAJOR, playwright_launch_options
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    capture_browser_environment,
)
from social_crawler.environments.managed_browser import (
    connect_managed_browser,
    is_managed_browser_provider,
)
from social_crawler.environments.proxy import prepare_playwright_proxy
from social_crawler.environments.session import (
    BROWSER_VIEWPORT,
    DEFAULT_UA,
    filter_storage_origins,
    load_storage_origins,
)
from social_crawler.environments.session_recovery import bind_profile, verify_profile

HOST = "https://www.douyin.com"

# Douyin currently accepts this compatibility value before its WebSecSDK adds
# timestamp and x-secsdk-web-signature. Keep it isolated so an online probe can
# replace it without changing request parsing or orchestration.
COMPAT_A_BOGUS = (
    "d70fDeSixoAbPdKS8cB09l3UKzLArs8yoeTORYFTeOOVyqtG6RPn/OS7boq923qG0YBTiKp7iDeMGdxcp4U0peCkKm"
    "kkSxT6MTV5VU8LgqqgaUksDrDLe0WFKwBFUOkN-QClEAkRXsMxIVnRIqVBld/a95zo5cDgWHB9pZG9tEWXDC8kh93iO"
    "CgpYLiaUlcS"
)


@dataclass(frozen=True)
class SignedRequest:
    url: str
    headers: dict[str, str]
    cookie_header: str


class DouyinBrowserSecurity:
    """Use the logged-in browser's WebSecSDK while data stays on the HTTP path."""

    def __init__(self, session, timeout: float, *, budget=None):
        self.session = session
        self.timeout = timeout
        self.budget = budget
        self.playwright = self.context = self.page = None
        self.managed_browser = None
        self.proxy_bridge = None
        self.ms_token = ""
        self.uifid = ""
        configured_ua = getattr(getattr(session, "config", None), "user_agent", None)
        self.user_agent = (
            getattr(session, "effective_user_agent", None) or configured_ua or DEFAULT_UA
        )
        self.browser_version = self._chrome_version(self.user_agent)
        self.browser_parameters = {
            "browser_version": self.browser_version,
            "engine_version": self.browser_version,
            "screen_width": str(BROWSER_VIEWPORT["width"]),
            "screen_height": str(BROWSER_VIEWPORT["height"]),
        }

    @staticmethod
    def _chrome_version(user_agent: str) -> str:
        found = re.search(r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", user_agent)
        return found.group(1) if found else f"{CHROME_MAJOR}.0.0.0"

    async def _native_user_agent(self) -> str:
        """Match the HTTP identity to the installed browser without exposing HeadlessChrome."""

        browser = await self.playwright.chromium.launch(
            headless=True,
            **playwright_launch_options(self.session.config.browser_channel),
        )
        try:
            page = await browser.new_page()
            user_agent = await page.evaluate("navigator.userAgent")
            return user_agent.replace("HeadlessChrome/", "Chrome/")
        finally:
            await browser.close()

    async def _restore_local_storage(self) -> None:
        origins = load_storage_origins(Path(self.session.config.cookie_file), "douyin")
        if not origins:
            return
        state = json.dumps(origins, ensure_ascii=False)
        await self.context.add_init_script(
            script=(
                f"(() => {{ const origins = {state};"
                "const current = origins.find((entry) => entry.origin === location.origin);"
                "if (current) { for (const item of current.localStorage) "
                "if (localStorage.getItem(item.name) === null) "
                "localStorage.setItem(item.name, item.value); } })();"
            )
        )

    async def _bind_session_cookies(self) -> bool:
        # Older lightweight callers omit platform; the adapter is unambiguously Douyin.
        from types import SimpleNamespace

        bound = SimpleNamespace(
            platform="douyin",
            cookies=self.session.cookies,
            profile_auth=getattr(self.session, "profile_auth", False),
        )
        return await bind_profile(self.context, bound)

    async def _start(self):
        if self.page:
            return
        self.playwright = await async_playwright().start()
        try:
            if is_managed_browser_provider(self.session.config.browser_provider):
                self.managed_browser = await connect_managed_browser(
                    self.playwright,
                    self.session.config,
                    headless=self.session.config.headless,
                    timeout=self.timeout,
                )
                self.context = self.managed_browser.context
            else:
                proxy, self.proxy_bridge = await prepare_playwright_proxy(self.session.proxy)
                profile = Path(self.session.config.profile_dir).resolve()
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not self.session.config.user_agent and not self.session.environment_snapshot:
                    self.user_agent = await self._native_user_agent()
                    self.browser_version = self._chrome_version(self.user_agent)
                launch_options = playwright_launch_options(self.session.config.browser_channel)
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=self.session.config.headless,
                    user_agent=self.user_agent,
                    viewport=BROWSER_VIEWPORT,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    proxy=proxy,
                    args=["--disable-blink-features=AutomationControlled"],
                    **launch_options,
                )
            if await self._bind_session_cookies():
                await self._restore_local_storage()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            observed = await self.page.evaluate(
                "() => ({userAgent: navigator.userAgent, platform: navigator.platform, "
                "cores: navigator.hardwareConcurrency, "
                "memory: navigator.deviceMemory, width: screen.width, height: screen.height})"
            )
            self.user_agent = str(observed.get("userAgent") or self.user_agent).replace(
                "HeadlessChrome/", "Chrome/"
            )
            self.browser_version = self._chrome_version(self.user_agent)
            platform = str(observed.get("platform") or "")
            if platform.startswith("Win"):
                os_name, os_version = "Windows", "10"
            elif platform.startswith("Mac"):
                os_name, os_version = "Mac OS", "10_15_7"
            else:
                os_name, os_version = "Linux", platform or "x86_64"
            self.browser_parameters = {
                "browser_version": self.browser_version,
                "engine_version": self.browser_version,
                "browser_platform": platform,
                "os_name": os_name,
                "os_version": os_version,
                "screen_width": str(observed.get("width") or BROWSER_VIEWPORT["width"]),
                "screen_height": str(observed.get("height") or BROWSER_VIEWPORT["height"]),
                "cpu_core_num": str(observed.get("cores") or 8),
                "device_memory": str(observed.get("memory") or 8),
            }
            self.page.set_default_timeout(self.timeout * 1000)
            await self.page.goto(HOST + "/", wait_until="domcontentloaded")
            await self._wait_for_material()
            await self._audit_environment()
            if self.session.config.expected_user_id:
                await verify_profile(
                    self.context, self.page, "douyin",
                    expected_user_id=self.session.config.expected_user_id,
                    admit=self.budget.admit if self.budget is not None else None,
                )
        except BaseException:
            await self.close()
            raise

    async def _audit_environment(self):
        if self.session.config.consistency_policy == "off":
            return
        try:
            runtime = await capture_browser_environment(
                self.page,
                browser_version=(
                    self.context.browser.version if self.context.browser is not None else None
                ),
                headless=self.session.config.headless,
                browser_channel=self.session.config.browser_channel,
                source="douyin_browser_security_runtime",
            )
        except (PlaywrightError, TypeError, AttributeError) as exc:
            report = {
                "policy": self.session.config.consistency_policy,
                "status": "probe_failed",
                "allowed": self.session.config.consistency_policy != "strict",
                "stage": "douyin_browser_security",
                "error_type": type(exc).__name__,
            }
            if self.budget is not None and hasattr(self.budget, "store"):
                self.budget.store.event(
                    self.budget.run_id, "environment_consistency_checked", report
                )
            if not report["allowed"]:
                raise CollectionError(
                    "environment_mismatch",
                    "Strict environment consistency could not inspect the browser runtime",
                ) from exc
            return
        audit_environment(
            self.session,
            runtime,
            budget=self.budget,
            stage="douyin_browser_security",
        )

    async def _wait_for_material(self):
        # The loop count is derived from the request timeout and keeps startup bounded.
        attempts = max(2, int(self.timeout * 2))
        for attempt in range(attempts):
            title = await self.page.title()
            challenge = "验证码" in title or any(
                "verifycenter/captcha" in frame.url for frame in self.page.frames
            )
            if challenge:
                raise CollectionError(
                    "verification_required",
                    "Douyin returned a slider challenge before WebSign initialization",
                )
            cookies = await self.context.cookies([HOST + "/"])
            values = {cookie["name"]: cookie["value"] for cookie in cookies}
            self.uifid = values.get("UIFID", "")
            self.ms_token = await self.page.evaluate("() => localStorage.getItem('xmst') || ''")
            ready = await self.page.evaluate(
                "() => typeof window.use === 'function' "
                "&& typeof window.use('webSignUrl') === 'function'"
            )
            if self.uifid and self.ms_token and ready:
                return
            if attempt == attempts // 2 and not self.ms_token:
                await self.page.goto(
                    HOST + "/search/%E6%8A%96%E9%9F%B3?type=general",
                    wait_until="domcontentloaded",
                )
            await self.page.wait_for_timeout(500)
        raise CollectionError(
            "verification_required", "Douyin browser security material is unavailable"
        )

    async def sign(self, endpoint: str, params: dict[str, str]) -> SignedRequest:
        await self._start()
        cookies = await self.context.cookies([HOST + "/"])
        values = {cookie["name"]: cookie["value"] for cookie in cookies}
        params = dict(params)
        params.update(self.browser_parameters)
        params.update(msToken=self.ms_token, uifid=self.uifid)
        if verify_fp := values.get("s_v_web_id"):
            params.update(verifyFp=verify_fp, fp=verify_fp)
        unsigned = (
            HOST
            + endpoint
            + "?"
            + urlencode(params)
            + "&a_bogus="
            + quote(COMPAT_A_BOGUS, safe="")
        )
        signed = await self.page.evaluate(
            "([url, uifid]) => {"
            "window.__uifid = String(uifid);"
            "const result = window.use('webSignUrl')(url);"
            "return {url: result.url || url, "
            "signature: (result.headers || {})['x-secsdk-web-signature'] || ''};"
            "}",
            [unsigned, self.uifid],
        )
        if not signed.get("url") or not signed.get("signature"):
            raise CollectionError("verification_required", "Douyin WebSign returned no signature")
        cookies = await self.context.cookies([HOST + "/"])
        return SignedRequest(
            url=str(signed["url"]),
            headers={
                "User-Agent": self.user_agent,
                "uifid": self.uifid,
                "x-secsdk-web-signature": str(signed["signature"]),
            },
            cookie_header="; ".join(f"{c['name']}={c['value']}" for c in cookies),
        )

    async def storage_state(self) -> dict:
        """Export a portable, platform-scoped browser state after a verified request."""

        if self.context is None:
            raise RuntimeError("Douyin browser security context is not running")
        state = await self.context.storage_state()
        cookies = [
            cookie
            for cookie in state.get("cookies", [])
            if (domain := str(cookie.get("domain", "")).lstrip(".").lower())
            == "douyin.com"
            or domain.endswith(".douyin.com")
        ]
        return {
            "cookies": cookies,
            "origins": filter_storage_origins(state.get("origins"), "douyin"),
        }

    async def close(self):
        try:
            if self.managed_browser is not None:
                await self.managed_browser.close(suppress_stop_errors=True)
            elif self.context is not None:
                await self.context.close()
        finally:
            self.managed_browser = None
            self.context = self.page = None
            try:
                if self.playwright is not None:
                    await self.playwright.stop()
            finally:
                self.playwright = None
                if self.proxy_bridge is not None:
                    await self.proxy_bridge.close()
                    self.proxy_bridge = None
