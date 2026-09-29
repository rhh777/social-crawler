from types import SimpleNamespace

import pytest

from social_crawler.environments import network


def session():
    return SimpleNamespace(
        configured_proxy="socks5://name:secret@127.0.0.1:1080",
        proxy="socks5://name:secret@127.0.0.1:1080",
        config=SimpleNamespace(impersonate="chrome150", browser_channel=None),
    )


def adspower_session():
    return SimpleNamespace(
        configured_proxy="",
        proxy="",
        config=SimpleNamespace(
            browser_provider="adspower",
            adspower_profile_id="profile-a",
            impersonate="chrome150",
        ),
    )


def kameleo_session():
    return SimpleNamespace(
        configured_proxy="",
        proxy="",
        config=SimpleNamespace(
            browser_provider="kameleo",
            kameleo_profile_id="11111111-1111-4111-8111-111111111111",
            impersonate="chrome150",
        ),
    )


@pytest.mark.asyncio
async def test_dual_exit_retries_transient_transport_failure(monkeypatch):
    calls = {"http": 0, "browser": 0}

    def http(*_args, **_kwargs):
        calls["http"] += 1
        if calls["http"] == 1:
            raise OSError("synthetic TLS failure")
        return {"egress_ip": "203.0.113.10", "ip_group": "203.0.113.10"}

    async def browser(*_args, **_kwargs):
        calls["browser"] += 1
        return {"egress_ip": "203.0.113.10", "ip_group": "203.0.113.10"}

    monkeypatch.setattr(network, "observe_http_exit", http)
    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(session(), retry_delay=0)

    assert result["outcome"] == "success"
    assert result["data"] == {"transport_match": True, "attempts": 2}
    assert calls == {"http": 2, "browser": 2}


@pytest.mark.asyncio
async def test_dual_exit_does_not_retry_real_egress_mismatch(monkeypatch):
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.10",
            "ip_group": "203.0.113.10",
        },
    )

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.11", "ip_group": "203.0.113.11"}

    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(session(), retry_delay=0)

    assert result["error_kind"] == "egress_mismatch"
    assert result["data"] == {"transport_match": False, "attempts": 1}


@pytest.mark.asyncio
async def test_adspower_exit_uses_profile_browser_without_project_proxy(monkeypatch):
    async def browser(*_args, **_kwargs):
        return {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
            "country": "Test",
        }

    monkeypatch.setattr(network, "observe_browser_exit", browser)
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: pytest.fail("project proxy must not be used"),
    )

    result = await network.observe_dual_exit(adspower_session(), retry_delay=0)

    assert result["outcome"] == "success"
    assert result["http_egress_ip"] is None
    assert result["browser_egress_ip"] == "203.0.113.12"
    assert result["proxy_ref"] == network.adspower_profile_fingerprint("profile-a")
    assert result["data"]["transport"] == "adspower_profile"


@pytest.mark.asyncio
async def test_douyin_adspower_requires_matching_browser_and_curl_exits(monkeypatch):
    current = adspower_session()
    current.platform = "douyin"
    current.configured_proxy = current.proxy = "http://proxy.example:8080"
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
        },
    )

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.12", "ip_group": "203.0.113.12"}

    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "success"
    assert result["http_egress_ip"] == result["browser_egress_ip"]
    assert result["proxy_ref"] == network.adspower_profile_fingerprint("profile-a")
    assert result["data"] == {
        "transport_match": True,
        "attempts": 1,
        "transport": "adspower_profile",
    }


@pytest.mark.asyncio
async def test_douyin_adspower_allows_matching_direct_browser_and_curl_exits(monkeypatch):
    current = adspower_session()
    current.platform = "douyin"
    current.profile_auth = True
    seen = []

    def http(proxy, *_args, **_kwargs):
        seen.append(proxy)
        return {"egress_ip": "203.0.113.12", "ip_group": "203.0.113.12"}

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.12", "ip_group": "203.0.113.12"}

    monkeypatch.setattr(network, "observe_http_exit", http)
    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "success"
    assert result["http_egress_ip"] == result["browser_egress_ip"]
    assert result["proxy_ref"] == network.adspower_profile_fingerprint("profile-a")
    assert seen == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [adspower_session(), kameleo_session()])
async def test_xhs_managed_http_allows_matching_direct_exit(current, monkeypatch):
    current.platform = "xhs"
    current.profile_auth = False
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
        },
    )

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.12", "ip_group": "203.0.113.12"}

    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "success"
    assert result["http_egress_ip"] == result["browser_egress_ip"]
    assert result["data"]["transport_match"] is True


@pytest.mark.asyncio
async def test_douyin_adspower_rejects_mismatched_browser_and_curl_exits(monkeypatch):
    current = adspower_session()
    current.platform = "douyin"
    current.configured_proxy = current.proxy = "http://proxy.example:8080"
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
        },
    )

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.99", "ip_group": "203.0.113.99"}

    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "error"
    assert result["error_kind"] == "egress_mismatch"


@pytest.mark.asyncio
async def test_douyin_adspower_rejects_mismatched_direct_exits(monkeypatch):
    current = adspower_session()
    current.platform = "douyin"
    current.profile_auth = True
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
        },
    )

    async def browser(*_args, **_kwargs):
        return {"egress_ip": "203.0.113.99", "ip_group": "203.0.113.99"}

    monkeypatch.setattr(network, "observe_browser_exit", browser)

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "error"
    assert result["error_kind"] == "egress_mismatch"


@pytest.mark.asyncio
async def test_unmanaged_dual_exit_does_not_fall_back_to_direct(monkeypatch):
    current = session()
    current.configured_proxy = current.proxy = ""
    current.platform = "xhs"
    current.config.browser_provider = "chromium"
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: pytest.fail("unmanaged direct HTTP must not run"),
    )
    monkeypatch.setattr(
        network,
        "observe_browser_exit",
        lambda *_args, **_kwargs: pytest.fail("unmanaged direct browser must not run"),
    )

    result = await network.observe_dual_exit(current, retry_delay=0)

    assert result["outcome"] == "error"
    assert result["error_kind"] == "proxy_unavailable"


@pytest.mark.asyncio
async def test_kameleo_exit_uses_profile_browser_without_project_proxy(monkeypatch):
    async def browser(*_args, **_kwargs):
        return {
            "egress_ip": "203.0.113.13",
            "ip_group": "203.0.113.13",
            "country": "Test",
        }

    monkeypatch.setattr(network, "observe_browser_exit", browser)
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: pytest.fail("project proxy must not be used"),
    )

    result = await network.observe_dual_exit(kameleo_session(), retry_delay=0)

    assert result["outcome"] == "success"
    assert result["http_egress_ip"] is None
    assert result["browser_egress_ip"] == "203.0.113.13"
    assert result["data"]["transport"] == "kameleo_profile"


@pytest.mark.asyncio
async def test_http_transport_requires_project_proxy(monkeypatch):
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: pytest.fail("unconfigured transport must not run"),
    )

    result = await network.observe_http_transport_exit(adspower_session(), retry_delay=0)

    assert result["outcome"] == "error"
    assert result["error_kind"] == "proxy_unavailable"
    assert result["proxy_ref"] == network.adspower_profile_fingerprint("profile-a")
    assert result["data"]["transport"] == "http"


@pytest.mark.asyncio
async def test_adspower_http_transport_keeps_profile_binding_and_checks_project_proxy(monkeypatch):
    current = adspower_session()
    current.configured_proxy = current.proxy = "http://proxy.example:8080"
    monkeypatch.setattr(
        network,
        "observe_http_exit",
        lambda *_args, **_kwargs: {
            "egress_ip": "203.0.113.12",
            "ip_group": "203.0.113.12",
        },
    )

    result = await network.observe_http_transport_exit(current, retry_delay=0)

    assert result["outcome"] == "success"
    assert result["proxy_ref"] == network.adspower_profile_fingerprint("profile-a")
    assert result["http_egress_ip"] == "203.0.113.12"
    assert result["browser_egress_ip"] is None
    assert result["data"]["project_proxy_ref"] == network.proxy_fingerprint(
        current.configured_proxy
    )
