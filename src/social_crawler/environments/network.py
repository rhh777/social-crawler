"""Cookie-free exit observation for HTTP and browser transport paths."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time

from curl_cffi.requests import Session as HTTPSession
from playwright.async_api import async_playwright

from social_crawler.environments.browser_runtime import playwright_launch_options
from social_crawler.environments.managed_browser import (
    connect_managed_browser,
    is_managed_browser_provider,
    managed_browser_profile_fingerprint,
    managed_browser_transport,
)
from social_crawler.environments.proxy import prepare_playwright_proxy

IP_OBSERVATION_URL = "https://ipwho.is/"
IP_FIELDS = "success,ip,country,region,city,connection.asn,connection.isp"


def proxy_fingerprint(value: str) -> str:
    if not value:
        raise ValueError("Online exit observation requires a configured proxy")
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def adspower_profile_fingerprint(profile_id: str) -> str:
    """Identify profile-managed egress without reading or persisting proxy secrets."""
    if not profile_id:
        raise ValueError("AdsPower exit observation requires a profile ID")
    return hashlib.sha256(f"adspower:{profile_id}".encode()).hexdigest()[:16]


def _normalized(data: dict) -> dict:
    if data.get("success") is not True:
        raise ValueError("Exit observation service did not return a successful result")
    address = str(ipaddress.ip_address(data["ip"]))
    connection = data.get("connection") or {}
    return {
        "egress_ip": address,
        "ip_group": address,
        "country": data.get("country"),
        "region": data.get("region"),
        "city": data.get("city"),
        "asn": str(connection.get("asn")) if connection.get("asn") is not None else None,
        "isp": connection.get("isp"),
    }


def observe_http_exit(proxy: str, impersonate: str, *, timeout=20) -> dict:
    started = time.monotonic()
    with HTTPSession(trust_env=False) as http:
        response = http.get(
            IP_OBSERVATION_URL,
            proxy=proxy,
            timeout=timeout,
            impersonate=impersonate,
            params={"fields": IP_FIELDS},
        )
        response.raise_for_status()
        result = _normalized(response.json())
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


async def observe_browser_exit(session, *, timeout=20) -> dict:
    started = time.monotonic()
    if is_managed_browser_provider(session.config.browser_provider):
        connection = page = None
        try:
            async with async_playwright() as playwright:
                connection = await connect_managed_browser(
                    playwright,
                    session.config,
                    headless=True,
                    timeout=timeout,
                )
                page = await connection.context.new_page()
                url = IP_OBSERVATION_URL + "?fields=" + IP_FIELDS
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )
                if response is None or not response.ok:
                    raise ConnectionError("Browser exit observation request failed")
                text = await page.locator("body").inner_text()
                result = _normalized(json.loads(text))
                await page.close()
                page = None
                await connection.close(suppress_stop_errors=True)
                connection = None
        finally:
            if page:
                await page.close()
            if connection:
                await connection.close(suppress_stop_errors=True)
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        return result

    proxy, bridge = await prepare_playwright_proxy(session.proxy)
    launch = {"headless": True, "proxy": proxy}
    launch.update(playwright_launch_options(session.config.browser_channel))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch)
            try:
                # A fresh context deliberately carries no platform cookies.
                context = await browser.new_context()
                page = await context.new_page()
                url = IP_OBSERVATION_URL + "?fields=" + IP_FIELDS
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )
                if response is None or not response.ok:
                    raise ConnectionError("Browser exit observation request failed")
                text = await page.locator("body").inner_text()
                result = _normalized(json.loads(text))
                await context.close()
            finally:
                await browser.close()
    finally:
        if bridge:
            await bridge.close()
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


async def observe_dual_exit(session, *, timeout=20, attempts=2, retry_delay=0.5) -> dict:
    """Observe the account's configured egress without bypassing its proxy owner.

    Browser-only managed adapters are checked through their profile. Hybrid and
    managed HTTP modes compare the browser with the HTTP client's project proxy
    or direct connection before any platform request is admitted.
    """
    started = time.monotonic()
    managed = is_managed_browser_provider(
        getattr(session.config, "browser_provider", "chromium")
    )
    profile_only = (
        managed
        and getattr(session, "platform", None) != "douyin"
        and getattr(session, "profile_auth", True)
    )
    if profile_only:
        proxy_ref = managed_browser_profile_fingerprint(session.config)
        transport = managed_browser_transport(session.config)
        attempts = max(1, int(attempts))
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                result = await observe_browser_exit(session, timeout=timeout)
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(max(0, retry_delay))
                continue
            browser_ip = result["egress_ip"]
            return result | {
                "proxy_ref": proxy_ref,
                "http_egress_ip": None,
                "browser_egress_ip": browser_ip,
                "source": "ipwho.is",
                "observed_at": time.time(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "outcome": "success",
                "error_kind": None,
                "data": {
                    "transport": transport,
                    "transport_match": None,
                    "attempts": attempt,
                },
            }
        return {
            "proxy_ref": proxy_ref,
            "source": "ipwho.is",
            "observed_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "outcome": "error",
            "error_kind": "proxy_unavailable",
            "data": {
                "transport": transport,
                "error_type": type(last_error).__name__ if last_error else "UnknownError",
                "attempts": attempts,
            },
        }

    configured_proxy = session.configured_proxy
    proxy_ref = (
        managed_browser_profile_fingerprint(session.config)
        if managed
        else hashlib.sha256(configured_proxy.encode()).hexdigest()[:16]
        if configured_proxy
        else "unconfigured"
    )
    attempts = max(1, int(attempts))
    last_error = None
    # A managed browser can deliberately use no proxy. Hybrid browser/HTTP
    # flows may then use direct access on both sides and are safe once their
    # observed exits match. Unmanaged sessions keep requiring an explicit
    # proxy so a missing secret never silently degrades to direct access.
    if configured_proxy or not managed:
        try:
            proxy_fingerprint(configured_proxy)
        except Exception as exc:
            last_error = exc
            attempts = 0

    for attempt in range(1, attempts + 1):
        # return_exceptions waits for both transports to clean up their browser
        # and proxy resources even when the other one fails.
        http_result, browser_result = await asyncio.gather(
            asyncio.to_thread(
                observe_http_exit, session.proxy, session.config.impersonate, timeout=timeout
            ),
            observe_browser_exit(session, timeout=timeout),
            return_exceptions=True,
        )
        last_error = next(
            (result for result in (http_result, browser_result) if isinstance(result, Exception)),
            None,
        )
        if last_error is not None:
            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))
            continue

        http_ip = http_result["egress_ip"]
        browser_ip = browser_result["egress_ip"]
        if http_ip != browser_ip:
            return {
                "proxy_ref": proxy_ref,
                "http_egress_ip": http_ip,
                "browser_egress_ip": browser_ip,
                "source": "ipwho.is",
                "observed_at": time.time(),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "outcome": "error",
                "error_kind": "egress_mismatch",
                "data": {
                    "transport_match": False,
                    "attempts": attempt,
                    **(
                        {"transport": managed_browser_transport(session.config)}
                        if managed
                        else {}
                    ),
                },
            }
        return http_result | {
            "proxy_ref": proxy_ref,
            "http_egress_ip": http_ip,
            "browser_egress_ip": browser_ip,
            "source": "ipwho.is",
            "observed_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "outcome": "success",
            "error_kind": None,
            "data": {
                "transport_match": True,
                "attempts": attempt,
                **(
                    {"transport": managed_browser_transport(session.config)}
                    if managed
                    else {}
                ),
            },
        }

    return {
        "proxy_ref": proxy_ref,
        "source": "ipwho.is",
        "observed_at": time.time(),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "outcome": "error",
        "error_kind": "proxy_unavailable",
        # Exception messages may embed proxy credentials or target URLs.
        "data": {
            "error_type": type(last_error).__name__ if last_error else "UnknownError",
            "attempts": max(1, attempts),
        },
    }


async def observe_http_transport_exit(
    session, *, timeout=20, attempts=2, retry_delay=0.5
) -> dict:
    """Observe an HTTP adapter through the project's explicitly configured proxy."""
    started = time.monotonic()
    configured_proxy = session.configured_proxy
    profile_managed = is_managed_browser_provider(
        getattr(session.config, "browser_provider", "chromium")
    )
    account_proxy_ref = (
        managed_browser_profile_fingerprint(session.config)
        if profile_managed
        else "unconfigured"
    )
    try:
        project_proxy_ref = proxy_fingerprint(configured_proxy)
    except Exception as exc:
        return {
            "proxy_ref": account_proxy_ref,
            "source": "ipwho.is",
            "observed_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "outcome": "error",
            "error_kind": "proxy_unavailable",
            "data": {"transport": "http", "error_type": type(exc).__name__, "attempts": 0},
        }
    proxy_ref = account_proxy_ref if profile_managed else project_proxy_ref

    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.to_thread(
                observe_http_exit,
                session.proxy,
                session.config.impersonate,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(max(0, retry_delay))
            continue
        http_ip = result["egress_ip"]
        return result | {
            "proxy_ref": proxy_ref,
            "http_egress_ip": http_ip,
            "browser_egress_ip": None,
            "source": "ipwho.is",
            "observed_at": time.time(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "outcome": "success",
            "error_kind": None,
            "data": {
                "transport": "http",
                "project_proxy_ref": project_proxy_ref,
                "attempts": attempt,
            },
        }
    return {
        "proxy_ref": proxy_ref,
        "source": "ipwho.is",
        "observed_at": time.time(),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "outcome": "error",
        "error_kind": "proxy_unavailable",
        "data": {
            "transport": "http",
            "error_type": type(last_error).__name__ if last_error else "UnknownError",
            "attempts": attempts,
        },
    }
