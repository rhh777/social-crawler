import asyncio
import json
import stat

import pytest

from social_crawler.environments.cookie_capture import (
    PLATFORMS,
    _platform_cookies,
    initial_login_cookie_values,
    wait_for_douyin_security_material,
    wait_for_login_cookie,
    wait_for_rednote_page_identity,
    write_cookie_file,
)
from social_crawler.environments.proxy import (
    AuthenticatedSocks5Bridge,
    direct_playwright_proxy,
    prepare_playwright_proxy,
)


class CookieContext:
    def __init__(self, responses):
        self.responses = list(responses)

    async def cookies(self, urls):
        assert urls
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def test_platform_cookie_filter_excludes_unrelated_domains():
    cookies = [
        {"name": "web_session", "value": "secret", "domain": ".xiaohongshu.com"},
        {"name": "other", "value": "secret", "domain": "www.example.com"},
    ]
    assert [cookie["name"] for cookie in _platform_cookies(cookies, PLATFORMS["xhs"])] == [
        "web_session"
    ]


def test_rednote_cookie_filter_never_imports_domestic_cookies():
    cookies = [
        {"name": "a1", "value": "international", "domain": ".rednote.com"},
        {"name": "a1", "value": "domestic", "domain": ".xiaohongshu.com"},
    ]
    assert _platform_cookies(cookies, PLATFORMS["rednote"]) == [cookies[0]]


@pytest.mark.asyncio
async def test_rednote_login_uses_hydrated_page_identity():
    class Page:
        async def evaluate(self, _script):
            return {"user_id": "international-user", "nickname": "Red"}

    identity = await wait_for_rednote_page_identity(Page(), timeout=1)
    assert identity == {"user_id": "international-user", "nickname": "Red"}


def test_playwright_proxy_separates_credentials_from_server():
    proxy = direct_playwright_proxy("http://user:password@127.0.0.1:1080")
    assert proxy == {
        "server": "http://127.0.0.1:1080",
        "username": "user",
        "password": "password",
    }


def test_playwright_normalizes_curl_socks5h_spelling():
    assert direct_playwright_proxy("socks5h://127.0.0.1:1080") == {
        "server": "socks5://127.0.0.1:1080"
    }


@pytest.mark.asyncio
async def test_authenticated_socks_uses_loopback_bridge():
    proxy, bridge = await prepare_playwright_proxy(
        "socks5://user:password@127.0.0.1:1080"
    )
    try:
        assert proxy["server"].startswith("socks5://127.0.0.1:")
        assert "username" not in proxy
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_douyin_security_material_requires_uifid_and_xmst():
    class Page:
        calls = 0

        async def evaluate(self, script):
            self.calls += 1
            return "synthetic-ms" if self.calls > 1 else ""

    context = CookieContext(
        [
            [{"name": "sessionid", "value": "login", "domain": ".douyin.com"}],
            [
                {"name": "sessionid", "value": "login", "domain": ".douyin.com"},
                {"name": "UIFID", "value": "synthetic-uifid", "domain": ".douyin.com"},
            ],
        ]
    )
    await wait_for_douyin_security_material(context, Page(), timeout=1)


@pytest.mark.asyncio
async def test_authenticated_socks_bridge_relays_bytes():
    async def fake_upstream(reader, writer):
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x02"
            writer.write(b"\x05\x02")
            await writer.drain()
            version, user_length = await reader.readexactly(2)
            assert version == 1
            assert await reader.readexactly(user_length) == b"user"
            password_length = (await reader.readexactly(1))[0]
            assert await reader.readexactly(password_length) == b"password"
            writer.write(b"\x01\x00")
            await writer.drain()
            head = await reader.readexactly(4)
            assert head == b"\x05\x01\x00\x03"
            host_length = (await reader.readexactly(1))[0]
            await reader.readexactly(host_length + 2)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            payload = await reader.readexactly(4)
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    upstream = await asyncio.start_server(fake_upstream, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    bridge = AuthenticatedSocks5Bridge(
        f"socks5://user:password@127.0.0.1:{upstream_port}"
    )
    await bridge.start()
    bridge_port = bridge.server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge_port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        host = b"example.com"
        writer.write(
            b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big")
        )
        await writer.drain()
        assert (await reader.readexactly(10))[:2] == b"\x05\x00"
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
    finally:
        writer.close()
        await writer.wait_closed()
        await bridge.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_wait_for_login_cookie_polls_until_required_cookie_exists():
    context = CookieContext(
        [
            [{"name": "ttwid", "value": "first", "domain": ".douyin.com"}],
            [
                {"name": "ttwid", "value": "first", "domain": ".douyin.com"},
                {"name": "sessionid_ss", "value": "login", "domain": ".douyin.com"},
            ],
        ]
    )
    cookies = await wait_for_login_cookie(
        context, PLATFORMS["douyin"], timeout=1, poll_interval=0
    )
    assert {cookie["name"] for cookie in cookies} == {"ttwid", "sessionid_ss"}


@pytest.mark.asyncio
async def test_xhs_visitor_session_must_change_before_login_is_accepted():
    visitor = {"name": "web_session", "value": "visitor", "domain": ".xiaohongshu.com"}
    login = {"name": "web_session", "value": "login", "domain": ".xiaohongshu.com"}
    context = CookieContext([[visitor], [visitor], [login]])

    initial = await initial_login_cookie_values(context, PLATFORMS["xhs"], timeout=1)
    cookies = await wait_for_login_cookie(
        context,
        PLATFORMS["xhs"],
        timeout=1,
        poll_interval=0,
        initial_values=initial,
    )

    assert cookies[0]["value"] == "login"


def test_write_cookie_file_is_valid_json_and_private(tmp_path):
    path = tmp_path / "cookies" / "xhs.json"
    cookies = [{"name": "web_session", "value": "secret", "domain": ".xiaohongshu.com"}]
    write_cookie_file(path, cookies)

    assert json.loads(path.read_text()) == cookies
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
