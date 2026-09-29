import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from social_crawler.domain.models import CollectionError
from social_crawler.environments.kameleo import (
    KameleoClient,
    kameleo_resource_key,
    validate_kameleo_api_url,
)
from social_crawler.environments.managed_browser import connect_managed_browser
from social_crawler.environments.session import EnvironmentConfig

PROFILE_ID = "11111111-1111-4111-8111-111111111111"


def test_kameleo_configuration_is_opt_in_and_requires_a_uuid():
    with pytest.raises(ValidationError):
        EnvironmentConfig(browser_provider="kameleo")
    with pytest.raises(ValidationError):
        EnvironmentConfig(browser_provider="kameleo", kameleo_profile_id="not-a-uuid")

    config = EnvironmentConfig(
        browser_provider="kameleo",
        kameleo_profile_id=PROFILE_ID.upper(),
    )
    assert config.kameleo_profile_id == PROFILE_ID
    assert kameleo_resource_key(config.kameleo_profile_id) == (
        f"kameleo-profile:{PROFILE_ID}"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:5050",
        "http://kameleo:5050",
        "http://user:secret@127.0.0.1:5050",
        "http://127.0.0.1",
        "http://127.0.0.1:5050/api",
    ],
)
def test_kameleo_api_is_loopback_only(value):
    with pytest.raises(ValueError):
        validate_kameleo_api_url(value)


def _runtime(*, state="created", profile=None, start=None, stop=None):
    requests = []

    async def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/general/user-info":
            return httpx.Response(200, json={"email": "test@example.invalid"})
        if request.url.path == f"/profiles/{PROFILE_ID}":
            return httpx.Response(
                200,
                json=profile
                or {"browser": {"product": "chrome"}, "device": "desktop"},
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"lifetimeState": state})
        if request.url.path.endswith("/start"):
            return start or httpx.Response(200, json={"lifetimeState": "running"})
        if request.url.path.endswith("/stop"):
            return stop or httpx.Response(200, json={"lifetimeState": "terminated"})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    return requests, httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_kameleo_profile_lifecycle_returns_playwright_endpoint():
    requests, transport = _runtime()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        client = KameleoClient(client=http)
        await client.check_status()
        endpoint = await client.start_profile(PROFILE_ID, headless=True)
        await client.stop_profile(PROFILE_ID)

    assert endpoint == f"ws://127.0.0.1:5050/playwright/{PROFILE_ID}"
    assert requests == [
        ("GET", "/general/user-info", None),
        ("GET", f"/profiles/{PROFILE_ID}", None),
        ("GET", f"/profiles/{PROFILE_ID}/status", None),
        ("POST", f"/profiles/{PROFILE_ID}/start", {"arguments": ["--headless"]}),
        ("POST", f"/profiles/{PROFILE_ID}/stop", None),
    ]


@pytest.mark.asyncio
async def test_kameleo_lists_profiles_and_marks_only_desktop_chrome_compatible():
    firefox_id = "22222222-2222-4222-8222-222222222222"

    async def handler(request):
        assert request.url.path == "/profiles"
        return httpx.Response(
            200,
            json=[
                {
                    "id": PROFILE_ID,
                    "name": "XHS primary",
                    "fingerprint": {
                        "browser": {"product": "chrome"},
                        "device": {"type": "desktop"},
                    },
                    "status": {"lifetimeState": "terminated"},
                    "storage": "cloud",
                    "proxy": {"password": "must-not-leak"},
                },
                {
                    "id": firefox_id,
                    "name": "Unsupported Firefox",
                    "browser": {"product": "firefox"},
                    "device": "desktop",
                },
            ],
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=httpx.MockTransport(handler)
    ) as http:
        profiles = await KameleoClient(client=http).list_profiles()

    assert profiles[0] == {
        "id": PROFILE_ID,
        "name": "XHS primary",
        "state": "terminated",
        "detail": "Chrome · Desktop · Cloud",
        "compatible": True,
    }
    assert profiles[1]["id"] == firefox_id
    assert profiles[1]["compatible"] is False
    assert "must-not-leak" not in json.dumps(profiles)


@pytest.mark.asyncio
async def test_kameleo_accepts_profile_metadata_nested_under_fingerprint():
    _, transport = _runtime(
        profile={
            "fingerprint": {
                "browser": {"product": "chrome", "major": 153},
                "device": {"type": "desktop", "name": "Mac"},
            },
            "status": {"lifetimeState": "created"},
        }
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        endpoint = await KameleoClient(client=http).start_profile(
            PROFILE_ID, headless=False
        )

    assert endpoint.endswith(f"/playwright/{PROFILE_ID}")


@pytest.mark.asyncio
async def test_kameleo_does_not_take_over_a_running_profile():
    requests, transport = _runtime(state="running")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        with pytest.raises(CollectionError) as found:
            await KameleoClient(client=http).start_profile(PROFILE_ID, headless=False)

    assert found.value.kind == "environment_in_use"
    assert not any(path.endswith(("/start", "/stop")) for _, path, _ in requests)


@pytest.mark.asyncio
async def test_kameleo_refuses_busy_or_unsupported_profiles():
    _, transport = _runtime(state="locked")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        with pytest.raises(CollectionError) as found:
            await KameleoClient(client=http).start_profile(PROFILE_ID, headless=True)
    assert found.value.kind == "environment_in_use"

    _, transport = _runtime(
        profile={"browser": {"product": "firefox"}, "device": "desktop"}
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        with pytest.raises(CollectionError, match="desktop Chrome"):
            await KameleoClient(client=http).start_profile(PROFILE_ID, headless=True)


@pytest.mark.asyncio
async def test_kameleo_maps_and_redacts_api_errors():
    response = httpx.Response(
        409,
        json={
            "errorCode": "profile_locked",
            "message": "profile belongs to secret@example.com",
        },
    )
    _, transport = _runtime(start=response)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        with pytest.raises(CollectionError) as found:
            await KameleoClient(client=http).start_profile(PROFILE_ID, headless=True)
    assert found.value.kind == "environment_in_use"
    assert "secret@example.com" not in found.value.message


@pytest.mark.asyncio
async def test_kameleo_stop_is_idempotent_when_profile_is_not_running():
    response = httpx.Response(
        409,
        json={"errorCode": "profile_not_running", "message": "already stopped"},
    )
    _, transport = _runtime(stop=response)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5050", transport=transport
    ) as http:
        await KameleoClient(client=http).stop_profile(PROFILE_ID)


@pytest.mark.asyncio
async def test_managed_browser_connects_kameleo_over_cdp_and_cleans_up(monkeypatch):
    context = SimpleNamespace()
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser))
    )
    runtime = SimpleNamespace(
        check_status=AsyncMock(),
        start_profile=AsyncMock(
            return_value=f"ws://127.0.0.1:5050/playwright/{PROFILE_ID}"
        ),
        stop_profile=AsyncMock(),
        close=AsyncMock(),
    )
    from social_crawler.environments import managed_browser as module

    monkeypatch.setattr(module, "managed_browser_client", lambda *_args, **_kwargs: runtime)
    config = EnvironmentConfig(
        browser_provider="kameleo",
        kameleo_profile_id=PROFILE_ID,
        kameleo_start_timeout=30,
    )

    connection = await connect_managed_browser(playwright, config, headless=True)
    await connection.close()

    runtime.check_status.assert_awaited_once()
    runtime.start_profile.assert_awaited_once_with(
        PROFILE_ID, headless=True, display=None
    )
    playwright.chromium.connect_over_cdp.assert_awaited_once_with(
        f"ws://127.0.0.1:5050/playwright/{PROFILE_ID}", timeout=30_000
    )
    browser.close.assert_awaited_once()
    runtime.stop_profile.assert_awaited_once_with(PROFILE_ID)
    runtime.close.assert_awaited_once()
