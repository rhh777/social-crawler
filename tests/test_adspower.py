import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from social_crawler.domain.models import CollectionError, RunConfig
from social_crawler.environments.adspower import (
    AdsPowerClient,
    adspower_resource_key,
    validate_adspower_api_url,
)
from social_crawler.environments.session import EnvironmentConfig


@pytest.mark.asyncio
async def test_xhs_browser_uses_adspower_cdp_and_stops_profile(monkeypatch):
    from social_crawler.adapters.xhs import browser as browser_module

    page = SimpleNamespace(on=Mock(), set_default_timeout=Mock())
    context = SimpleNamespace(
        pages=[page],
        route=AsyncMock(),
        route_web_socket=AsyncMock(),
        on=Mock(),
        browser=SimpleNamespace(version="151.0"),
    )
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
    managed = SimpleNamespace(
        browser=browser,
        context=context,
        close=AsyncMock(),
    )
    monkeypatch.setattr(browser_module, "async_playwright", lambda: starter)
    connect = AsyncMock(return_value=managed)
    monkeypatch.setattr(browser_module, "connect_managed_browser", connect)
    bind_profile = AsyncMock()
    monkeypatch.setattr(browser_module, "bind_profile", bind_profile)

    config = EnvironmentConfig(
        browser_provider="adspower",
        adspower_profile_id="profile-a",
        headless=True,
    )
    session = SimpleNamespace(platform="xhs", config=config)
    budget = SimpleNamespace(
        config=RunConfig(platform="xhs", keywords=["test"], request_timeout=7)
    )
    adapter = browser_module.XHSBrowser(session, budget, samples=None, display=":123")

    await adapter._start()
    await adapter.close()

    connect.assert_awaited_once_with(playwright, config, headless=True, display=":123")
    bind_profile.assert_awaited_once_with(context, session)
    managed.close.assert_awaited_once_with(suppress_stop_errors=True)
    playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_douyin_websign_uses_adspower_profile_and_stops_it(monkeypatch):
    from social_crawler.adapters.douyin import security as security_module

    page = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "userAgent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36"
                ),
                "platform": "Win32",
                "cores": 8,
                "memory": 8,
                "width": 1440,
                "height": 900,
            }
        ),
        goto=AsyncMock(),
        set_default_timeout=Mock(),
    )
    context = SimpleNamespace(
        pages=[page],
        browser=SimpleNamespace(version="151.0.0.0"),
    )
    playwright = SimpleNamespace(stop=AsyncMock())
    starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
    managed = SimpleNamespace(context=context, close=AsyncMock())
    connect = AsyncMock(return_value=managed)
    bind_profile = AsyncMock(return_value=False)
    monkeypatch.setattr(security_module, "async_playwright", lambda: starter)
    monkeypatch.setattr(security_module, "connect_managed_browser", connect)
    monkeypatch.setattr(security_module, "bind_profile", bind_profile)

    config = EnvironmentConfig(
        browser_provider="adspower",
        adspower_profile_id="profile-a",
        headless=True,
        consistency_policy="off",
    )
    session = SimpleNamespace(
        platform="douyin",
        config=config,
        profile_auth=True,
        cookies=[],
        proxy="http://proxy.example:8080",
        environment_snapshot=None,
        effective_user_agent=None,
    )
    security = security_module.DouyinBrowserSecurity(session, 7)
    security._wait_for_material = AsyncMock()

    await security._start()
    await security.close()

    connect.assert_awaited_once_with(
        playwright,
        config,
        headless=True,
        timeout=7,
    )
    bound = bind_profile.await_args.args[1]
    assert bound.platform == "douyin"
    assert bound.profile_auth is True
    assert security.user_agent.endswith("Chrome/151.0.0.0 Safari/537.36")
    managed.close.assert_awaited_once_with(suppress_stop_errors=True)
    playwright.stop.assert_awaited_once()


def test_adspower_configuration_is_opt_in_and_requires_a_profile():
    assert EnvironmentConfig().browser_provider == "chromium"

    with pytest.raises(ValidationError):
        EnvironmentConfig(browser_provider="adspower")

    config = EnvironmentConfig(
        browser_provider="adspower",
        adspower_profile_id="profile-a",
    )
    assert config.adspower_profile_id == "profile-a"
    assert adspower_resource_key(config.adspower_profile_id) == (
        "adspower-profile:profile-a"
    )


def test_adspower_session_supports_douyin_but_kameleo_does_not():
    from social_crawler.environments.session import Session

    adspower = EnvironmentConfig(
        browser_provider="adspower",
        adspower_profile_id="profile-a",
    )
    session = Session(adspower, "douyin", profile_auth=True)
    assert session.profile_auth is True

    kameleo = EnvironmentConfig(
        browser_provider="kameleo",
        kameleo_profile_id="11111111-1111-4111-8111-111111111111",
    )
    with pytest.raises(ValueError, match="does not support douyin"):
        Session(kameleo, "douyin", profile_auth=True)


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:50325",
        "http://adspower:50325",
        "http://user:secret@127.0.0.1:50325",
        "http://127.0.0.1",
        "http://127.0.0.1:50325/api",
    ],
)
def test_adspower_api_is_loopback_only(value):
    with pytest.raises(ValueError):
        validate_adspower_api_url(value)


@pytest.mark.asyncio
async def test_adspower_profile_lifecycle_returns_safe_cdp_endpoint():
    requests = []

    async def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/status":
            return httpx.Response(200, json={"code": 0, "msg": "success"})
        if request.url.path.endswith("/start"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "ws": {
                            "puppeteer": "ws://127.0.0.1:49151/devtools/browser/test"
                        }
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:50325", transport=transport
    ) as http:
        client = AdsPowerClient(client=http)
        await client.check_status()
        endpoint = await client.start_profile("profile-a", headless=True)
        await client.stop_profile("profile-a")

    assert endpoint == "ws://127.0.0.1:49151/devtools/browser/test"
    assert requests == [
        ("GET", "/status", None),
        ("POST", "/api/v1/browser/cloud-active", {"user_ids": "profile-a"}),
        ("GET", "/api/v2/browser-profile/active", None),
        (
            "POST",
            "/api/v2/browser-profile/start",
            {
                "profile_id": "profile-a",
                "headless": "1",
                "last_opened_tabs": "0",
                "launch_args": ["--use-fake-device-for-media-stream"],
            },
        ),
        (
            "POST",
            "/api/v2/browser-profile/stop",
            {"profile_id": "profile-a"},
        ),
    ]


@pytest.mark.asyncio
async def test_adspower_lists_every_profile_page_and_returns_only_safe_metadata():
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        page = body["page"]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "profile_id": f"profile-{page}",
                            "name": f"Environment {page}",
                            "profile_no": str(page),
                            "group_name": "XHS",
                            "password": "must-not-leak",
                        }
                    ],
                    "total_pages": 2,
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:50325", transport=httpx.MockTransport(handler)
    ) as http:
        profiles = await AdsPowerClient(client=http).list_profiles()

    assert requests == [{"page": 1, "limit": 200}, {"page": 2, "limit": 200}]
    assert profiles == [
        {
            "id": "profile-1",
            "name": "Environment 1",
            "state": "",
            "detail": "No. 1 · XHS",
            "compatible": True,
        },
        {
            "id": "profile-2",
            "name": "Environment 2",
            "state": "",
            "detail": "No. 2 · XHS",
            "compatible": True,
        },
    ]
    assert "password" not in json.dumps(profiles)


@pytest.mark.asyncio
async def test_adspower_rejects_remote_cdp_and_redacts_api_errors():
    async def remote_cdp(_request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "ws": {
                        "puppeteer": "ws://browser.example:49151/devtools/browser/secret"
                    }
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:50325", transport=httpx.MockTransport(remote_cdp)
    ) as http:
        client = AdsPowerClient(client=http)
        with pytest.raises(CollectionError) as found:
            await client.start_profile("profile-a", headless=True)
    assert found.value.kind == "network_failure"
    assert "browser.example" not in found.value.message

    async def rejected(_request):
        return httpx.Response(
            200,
            json={"code": -1, "msg": "proxy password super-secret"},
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:50325", transport=httpx.MockTransport(rejected)
    ) as http:
        client = AdsPowerClient(client=http)
        with pytest.raises(CollectionError) as found:
            await client.check_status()
    assert "super-secret" not in found.value.message


def _runtime(*, cloud=None, local="Inactive", start=None):
    requests = []

    async def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/v1/browser/cloud-active":
            return httpx.Response(200, json={"code": 0, "data": cloud or []})
        if request.url.path == "/api/v2/browser-profile/active":
            return httpx.Response(200, json={"code": 0, "data": {"status": local}})
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json=start or {
                "code": 0,
                "data": {"ws": {"puppeteer": "ws://127.0.0.1:49151/devtools/browser/test"}},
            })
        return httpx.Response(200, json={"code": 0, "data": {}})

    return requests, httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_adspower_refuses_profile_open_on_another_device():
    # Observed shape: an entry per occupied profile, naming the owner, no status.
    requests, transport = _runtime(cloud=[{"user_id": "profile-a", "account": "owner@example.com"}])
    async with httpx.AsyncClient(base_url="http://127.0.0.1:50325", transport=transport) as http:
        with pytest.raises(CollectionError) as found:
            await AdsPowerClient(client=http).start_profile("profile-a", headless=True)
    assert found.value.kind == "environment_in_use"
    assert found.value.blocks_run
    assert "owner@example.com" not in found.value.message
    assert not any(path.endswith("/start") for _, path, _ in requests)


@pytest.mark.asyncio
async def test_adspower_start_rejection_for_foreign_owner_is_in_use_without_leaking_owner():
    _, transport = _runtime(start={
        "code": -1,
        "msg": "[profile-a] is being used by [owner@example.com] and is not allowed to open",
    })
    async with httpx.AsyncClient(base_url="http://127.0.0.1:50325", transport=transport) as http:
        with pytest.raises(CollectionError) as found:
            await AdsPowerClient(client=http).start_profile("profile-a", headless=True)
    assert found.value.kind == "environment_in_use"
    assert "owner@example.com" not in found.value.message


@pytest.mark.asyncio
async def test_adspower_stops_stale_local_profile_before_start():
    requests, transport = _runtime(local="Active")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:50325", transport=transport) as http:
        await AdsPowerClient(client=http).start_profile("profile-a", headless=True)
    paths = [path for _, path, _ in requests]
    assert paths.index("/api/v2/browser-profile/stop") < paths.index("/api/v2/browser-profile/start")


@pytest.mark.asyncio
async def test_adspower_headful_start_enables_software_webgl_and_screen_sized_window():
    requests, transport = _runtime()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:50325", transport=transport) as http:
        await AdsPowerClient(client=http).start_profile("profile-a", headless=False)
        await AdsPowerClient(client=http).start_profile("profile-a", headless=True)
    headful, headless = [body for _, path, body in requests if path.endswith("/start")]
    assert headful["headless"] == "0"
    assert "--use-angle=swiftshader" in headful["launch_args"]
    assert "--window-size=1440,900" in headful["launch_args"]
    assert "--use-fake-device-for-media-stream" in headful["launch_args"]
    assert headless["launch_args"] == ["--use-fake-device-for-media-stream"]


@pytest.mark.asyncio
async def test_adspower_account_desktop_is_explicit_and_validated():
    requests, transport = _runtime()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:50325", transport=transport) as http:
        client = AdsPowerClient(client=http)
        await client.start_profile("profile-a", headless=False, display=":102")
        with pytest.raises(ValueError):
            await client.start_profile("profile-a", headless=False, display="invalid")
    starts = [body for _, path, body in requests if path.endswith("/start")]
    assert len(starts) == 1
    assert "--display=:102" in starts[0]["launch_args"]
    assert "--start-maximized" in starts[0]["launch_args"]
