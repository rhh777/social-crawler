from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

from social_crawler.domain.models import CollectionError
from social_crawler.environments.adspower import AdsPowerClient, adspower_resource_key
from social_crawler.environments.kameleo import KameleoClient, kameleo_resource_key

MANAGED_BROWSER_PROVIDERS = frozenset({"adspower", "kameleo"})


def is_managed_browser_provider(value: str) -> bool:
    return value in MANAGED_BROWSER_PROVIDERS


def managed_browser_supports_platform(provider: str, platform: str) -> bool:
    """Keep provider/platform support explicit as hybrid adapters are added."""

    if provider == "adspower":
        return platform in {"xhs", "rednote", "douyin"}
    if provider == "kameleo":
        return platform in {"xhs", "rednote"}
    return False


@dataclass(frozen=True)
class ManagedBrowserSpec:
    provider: str
    profile_id: str
    timeout: int
    display_name: str


def managed_browser_spec(config) -> ManagedBrowserSpec:
    if config.browser_provider == "adspower":
        return ManagedBrowserSpec(
            provider="adspower",
            profile_id=config.adspower_profile_id,
            timeout=getattr(config, "adspower_start_timeout", 90),
            display_name="AdsPower",
        )
    if config.browser_provider == "kameleo":
        return ManagedBrowserSpec(
            provider="kameleo",
            profile_id=config.kameleo_profile_id,
            timeout=getattr(config, "kameleo_start_timeout", 90),
            display_name="Kameleo",
        )
    raise ValueError("Browser provider does not manage an external profile")


def managed_browser_client(config, *, timeout: float | None = None):
    spec = managed_browser_spec(config)
    effective_timeout = timeout if timeout is not None else spec.timeout
    if spec.provider == "adspower":
        return AdsPowerClient(config.adspower_api_url, timeout=effective_timeout)
    return KameleoClient(config.kameleo_api_url, timeout=effective_timeout)


def managed_browser_resource_key(config) -> str:
    spec = managed_browser_spec(config)
    if spec.provider == "adspower":
        return adspower_resource_key(spec.profile_id)
    return kameleo_resource_key(spec.profile_id)


def managed_browser_profile_fingerprint(config) -> str:
    spec = managed_browser_spec(config)
    return hashlib.sha256(f"{spec.provider}:{spec.profile_id}".encode()).hexdigest()[:16]


def managed_browser_transport(config) -> str:
    return managed_browser_spec(config).provider + "_profile"


@dataclass
class ManagedBrowserConnection:
    runtime: Any
    browser: Any
    context: Any
    spec: ManagedBrowserSpec

    async def close(self, *, suppress_stop_errors: bool = False) -> None:
        error: Exception | None = None
        try:
            await self.browser.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        try:
            await self.runtime.stop_profile(self.spec.profile_id)
        except asyncio.CancelledError:
            raise
        except CollectionError as exc:
            if not suppress_stop_errors and error is None:
                error = exc
        finally:
            try:
                await self.runtime.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error


async def connect_managed_browser(
    playwright,
    config,
    *,
    headless: bool,
    display: str | None = None,
    timeout: float | None = None,
) -> ManagedBrowserConnection:
    spec = managed_browser_spec(config)
    effective_timeout = max(spec.timeout, timeout or 0)
    runtime = managed_browser_client(config, timeout=effective_timeout)
    browser = None
    started = False
    try:
        await runtime.check_status()
        endpoint = await runtime.start_profile(
            spec.profile_id,
            headless=headless,
            display=display,
        )
        started = True
        browser = await playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=effective_timeout * 1000,
        )
        if not browser.contexts:
            raise CollectionError(
                "network_failure", f"{spec.display_name} browser has no default context"
            )
        return ManagedBrowserConnection(runtime, browser, browser.contexts[0], spec)
    except BaseException:
        if browser is not None:
            try:
                await browser.close()
            except BaseException:
                pass
        if started:
            try:
                await runtime.stop_profile(spec.profile_id)
            except CollectionError:
                pass
        await runtime.close()
        raise
