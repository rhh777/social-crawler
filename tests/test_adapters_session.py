import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from social_crawler.adapters.douyin.http import DouyinHTTP
from social_crawler.adapters.douyin.http import parse_page as parse_douyin
from social_crawler.adapters.douyin.security import DouyinBrowserSecurity
from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.parsing import Exchange, matches
from social_crawler.adapters.xhs.parsing import parse_page as parse_xhs
from social_crawler.domain.errors import check_response
from social_crawler.domain.models import CollectionError, TaskRequest
from social_crawler.domain.redaction import redact
from social_crawler.environments.session import (
    EnvironmentConfig,
    Session,
    exclusive,
    load_cookies,
    load_proxy,
    load_storage_origins,
)


@pytest.mark.parametrize(
    "value",
    [
        r"socks5://user:password\@proxy.example:1080",
        "socks5://user:password@proxy.example:1080&nbsp;",
    ],
)
def test_proxy_rejects_escaped_or_html_encoded_input(tmp_path, value):
    proxy_file = tmp_path / "proxy.url"
    proxy_file.write_text(value)
    with pytest.raises(ValueError):
        load_proxy(proxy_file=str(proxy_file))


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (429, {}, "rate_limit"),
        (401, {}, "auth_expired"),
        (461, {}, "verification_required"),
        (403, {}, "access_denied"),
        (503, {}, "network_failure"),
        (200, "", "abnormal_empty"),
        (200, "<html>captcha</html>", "verification_required"),
        (200, {"success": False, "code": -100}, "auth_expired"),
        (200, {"status_code": 9, "status_msg": "请求频繁"}, "rate_limit"),
        (
            200,
            {
                "status_code": 0,
                "data": [],
                "search_nil_info": {"search_nil_type": "verify_check"},
            },
            "verification_required",
        ),
    ],
)
def test_errors_classified_without_retrying_risk(status, payload, kind):
    with pytest.raises(CollectionError) as found:
        check_response(status, payload, {"Retry-After": "12"})
    assert found.value.kind == kind
    if status == 429:
        assert found.value.retry_after == 12


def test_user_content_cannot_trigger_auth_or_captcha_detection():
    check_response(200, {"status_code": 0, "data": [{"text": "login 验证码"}]})


def test_xhs_response_correlation():
    request = TaskRequest(operation="search", keyword="猫", sort="latest", context={"page": 2})
    good = {"keyword": "猫", "sort": "time_descending", "page": 2}
    url = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
    assert matches(request, Exchange(url, "POST", good, {}))
    for field, wrong in [("keyword", "狗"), ("sort", "general"), ("page", 1)]:
        assert not matches(request, Exchange(url, "POST", good | {field: wrong}, {}))
    assert not matches(
        request, Exchange(url.replace("edith.xiaohongshu.com", "invalid.example"), "POST", good, {})
    )
    current = {
        "keyword": "猫",
        "sort": "general",
        "page": 2,
        "filters": [{"type": "sort_type", "tags": ["time_descending"]}],
    }
    current_url = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
    assert matches(request, Exchange(current_url, "POST", current, {}))
    assert not matches(request, Exchange(current_url, "POST", current | {"filters": []}, {}))
    general = request.model_copy(update={"sort": "general"})
    assert matches(general, Exchange(current_url, "POST", current | {"filters": []}, {}))
    assert not matches(general, Exchange(current_url, "POST", current, {}))
    replies = TaskRequest(
        operation="replies", content_id="note", root_id="root", context={"cursor": "next"}
    )
    url = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/sub/page?note_id=note&root_comment_id=root&cursor=next"
    assert matches(replies, Exchange(url, "GET", {}, {}))
    hinted = replies.model_copy(
        update={"context": {}, "input": {"root_comment": {"sub_cursor": "next"}}}
    )
    assert matches(hinted, Exchange(url, "GET", {}, {}))
    assert not matches(
        replies,
        Exchange(url.replace("root_comment_id=root", "root_comment_id=other"), "GET", {}, {}),
    )


def test_rednote_response_correlation_and_canonical_url():
    request = TaskRequest(operation="search", keyword="coffee", sort="general")
    body = {"keyword": "coffee", "sort": "general", "page": 1}
    rednote_url = "https://webapi.rednote.com/api/sns/web/v1/search/notes"
    assert matches(request, Exchange(rednote_url, "POST", body, {}), platform="rednote")
    assert not matches(
        request,
        Exchange(rednote_url.replace("rednote.com", "xiaohongshu.com"), "POST", body, {}),
        platform="rednote",
    )
    page = parse_xhs(
        request,
        {
            "data": {
                "items": [
                    {
                        "id": "note",
                        "model_type": "note",
                        "xsec_token": "token",
                        "note_card": {"display_title": "result"},
                    }
                ],
                "has_more": False,
            }
        },
        platform="rednote",
    )
    assert page.items[0].data["url"] == "https://www.rednote.com/explore/note"


@pytest.mark.parametrize("platform", ["xhs", "douyin"])
def test_empty_page_requires_explicit_has_more(platform):
    request = TaskRequest(operation="comments", content_id="n")
    parse = parse_xhs if platform == "xhs" else parse_douyin
    body = {"comments": [], "has_more": False, "cursor": ""}
    payload = {"data": body} if platform == "xhs" else body
    assert parse(request, payload).response_has_more is False
    del body["has_more"]
    with pytest.raises(CollectionError, match="has_more"):
        parse(request, payload)


def test_douyin_null_comments_with_terminal_pagination_is_empty_page():
    request = TaskRequest(operation="comments", content_id="n")
    result = parse_douyin(
        request,
        {"status_code": 0, "comments": None, "has_more": 0, "cursor": 20, "total": 0},
    )
    assert result.items == []
    assert result.response_has_more is False

    with pytest.raises(CollectionError, match="comments"):
        parse_douyin(request, {"status_code": 0, "comments": None, "has_more": 1})


def test_xhs_root_comment_preserves_embedded_reply_cursor():
    page = parse_xhs(
        TaskRequest(operation="comments", content_id="n"),
        {
            "data": {
                "comments": [
                    {
                        "id": "root",
                        "content": "root",
                        "sub_comment_cursor": "embedded-reply",
                        "sub_comment_has_more": True,
                    }
                ],
                "has_more": False,
            }
        },
    )
    assert page.items[0].data["sub_cursor"] == "embedded-reply"


def test_detail_cannot_invent_identity():
    request = TaskRequest(operation="detail", content_id="wanted")
    with pytest.raises(CollectionError):
        parse_xhs(request, {"data": {"items": [{"note_card": {"desc": "wrong"}}]}})
    with pytest.raises(CollectionError):
        parse_douyin(request, {"aweme_detail": {"aweme_id": "wrong"}})


def test_xhs_video_references_and_detail_quality():
    request = TaskRequest(operation="detail", content_id="n")
    raw = {
        "noteId": "n",
        "desc": "",
        "time": 1700000000000,
        "user": {"userId": "author"},
        "video": {
            "media": {"stream": {"h264": [{"masterUrl": "https://video.example/video.mp4"}]}}
        },
    }
    page = parse_xhs(request, {"data": {"items": [{"id": "n", "note_card": raw}]}})
    assert page.items[0].data["media"] == ["https://video.example/video.mp4"]
    assert page.items[0].data["detail_complete"] is True  # explicitly empty text is allowed
    assert page.items[0].data["published_at"] == 1700000000


async def test_http_client_initializes_and_closes_without_requests():
    from curl_cffi.requests import AsyncSession

    client = AsyncSession(impersonate="chrome150", proxy=None, trust_env=False)
    await client.close()


def test_douyin_reply_preserves_root_and_direct_parent():
    page = parse_douyin(
        TaskRequest(operation="replies", content_id="n", root_id="root"),
        {
            "comments": [
                {"cid": "reply", "reply_id": "root", "reply_to_reply_id": "another-reply"}
            ],
            "has_more": 0,
            "cursor": 0,
        },
    )
    assert page.items[0].root_id == "root"
    assert page.items[0].parent_id == "another-reply"


def test_douyin_requires_both_cursor_and_search_context():
    request = TaskRequest(operation="search", keyword="猫")
    result = parse_douyin(request, {"data": [], "has_more": 1, "cursor": 10})
    assert result.stop_reason == "missing_search_context"
    result = parse_douyin(request, {"data": [], "has_more": 1, "log_pb": {"impr_id": "s"}})
    assert result.next_context == {}  # search_id alone cannot advance offset


def test_cookie_formats_domain_expiration_and_binding(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "web_session", "value": "synthetic", "domain": ".xiaohongshu.com"},
                    {"name": "expired", "value": "discard", "expires": str(time.time() - 1)},
                    {"name": "unrelated", "value": "discard", "domain": ".example.com"},
                    {"name": "future", "value": "keep", "expires": str(time.time() + 1000)},
                ]
            }
        )
    )
    assert {c["name"] for c in load_cookies(path, "xhs")} == {"web_session", "future"}
    session = Session(EnvironmentConfig(cookie_file=str(path)), "xhs")
    assert "synthetic" in session.cookie_header
    path.write_text("web_session=replaced; a1=synthetic-a1")
    with pytest.raises(CollectionError, match="changed"):
        session.check_unchanged()
    replacement = Session(EnvironmentConfig(cookie_file=str(path)), "xhs")
    assert replacement.binding != session.binding
    path.unlink()
    with pytest.raises(CollectionError, match="changed"):
        replacement.check_unchanged()


def test_managed_profile_auth_does_not_require_or_watch_a_cookie_file(tmp_path):
    missing = tmp_path / "not-created.json"
    session = Session(
        EnvironmentConfig(
            browser_provider="kameleo",
            kameleo_profile_id="11111111-1111-4111-8111-111111111111",
            cookie_file=str(missing),
        ),
        "xhs",
        profile_auth=True,
    )

    assert session.cookies == []
    assert session.cookie_header == ""
    assert session.cookie_digest is None
    session.check_unchanged()


def test_douyin_storage_state_preserves_only_platform_local_storage(tmp_path):
    path = tmp_path / "storage-state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "sessionid", "value": "login", "domain": ".douyin.com"}],
                "origins": [
                    {
                        "origin": "https://www.douyin.com",
                        "localStorage": [
                            {"name": "xmst", "value": "synthetic-xmst"},
                            {"name": "preference", "value": "one"},
                        ],
                    },
                    {
                        "origin": "https://example.com",
                        "localStorage": [{"name": "secret", "value": "discard"}],
                    },
                ],
            }
        )
    )

    origins = load_storage_origins(path, "douyin")
    assert origins == [
        {
            "origin": "https://www.douyin.com",
            "localStorage": [
                {"name": "xmst", "value": "synthetic-xmst"},
                {"name": "preference", "value": "one"},
            ],
        }
    ]


def test_environment_exclusion_and_proxy_fail_closed(tmp_path, monkeypatch):
    with exclusive(["same-profile"], tmp_path):
        with pytest.raises(CollectionError, match="owns"):
            with exclusive(["same-profile"], tmp_path):
                pytest.fail("Lock was not exclusive")
    with exclusive(["same-profile"], tmp_path):
        pass
    monkeypatch.delenv("SYNTHETIC_PROXY", raising=False)
    with pytest.raises(ValueError, match="fallback"):
        Session(EnvironmentConfig(proxy_env="SYNTHETIC_PROXY"), "xhs", offline=True)
    proxy_file = tmp_path / "proxy.url"
    proxy_file.write_text("socks5://user:password@127.0.0.1:1080")
    session = Session(EnvironmentConfig(proxy_file=str(proxy_file)), "xhs", offline=True)
    assert session.proxy.startswith("socks5://")
    with pytest.raises(ValueError, match="only one"):
        Session(
            EnvironmentConfig(
                proxy_env="SYNTHETIC_PROXY",
                proxy_file=str(proxy_file),
            ),
            "xhs",
            offline=True,
        )


def test_proxy_host_relay_rewrites_transport_without_changing_saved_value(
    tmp_path, monkeypatch
):
    proxy_file = tmp_path / "proxy.url"
    original = "socks5://user:password@192.0.2.10:1080"
    proxy_file.write_text(original)
    monkeypatch.setenv("CRAWLER_PROXY_RELAY_HOST", "host.docker.internal")
    monkeypatch.setenv("CRAWLER_PROXY_RELAY_MAP", "192.0.2.10:1080=41080")

    assert load_proxy(proxy_file=str(proxy_file)) == (
        "socks5h://user:password@host.docker.internal:41080"
    )
    assert load_proxy(proxy_file=str(proxy_file), apply_relay=False) == original


def test_redaction_of_nested_headers_query_and_proxy():
    value = {
        "headers": {"Cookie": "secret-cookie", "Authorization": "secret-bearer"},
        "data": [{"xsec_token": "secret-token"}],
        "url": "https://user:secret-password@example.com/read?msToken=secret-ms&keyword=cat",
    }
    encoded = json.dumps(redact(value))
    assert "secret-" not in encoded
    assert "keyword=cat" in encoded


def test_redaction_covers_douyin_security_material():
    value = {
        "uifid": "secret-uifid",
        "verifyFp": "secret-fp",
        "x-secsdk-web-signature": "secret-signature",
        "proxy": "socks5h://user:password@example.com:1080",
    }
    encoded = json.dumps(redact(value))
    assert "secret-" not in encoded
    assert "user:password" not in encoded


class Budget:
    def __init__(self):
        self.calls = []

    async def admit(self, operation):
        self.calls.append(operation)


async def test_douyin_browser_security_adds_both_signing_stages():
    class Context:
        async def cookies(self, urls):
            assert urls
            return [
                {"name": "sessionid", "value": "synthetic-login"},
                {"name": "s_v_web_id", "value": "synthetic-fp"},
            ]

    class Page:
        signed_input = None

        async def evaluate(self, script, value):
            self.signed_input = value
            return {"url": value[0] + "&timestamp=1", "signature": "synthetic-signature"}

    security = DouyinBrowserSecurity(object(), 1)
    security.context = Context()
    security.page = Page()
    security.ms_token = "synthetic-ms"
    security.uifid = "synthetic-uifid"
    signed = await security.sign("/aweme/v1/web/general/search/single/", {"keyword": "猫"})
    query = parse_qs(urlsplit(signed.url).query)
    assert query["msToken"] == ["synthetic-ms"]
    assert query["uifid"] == ["synthetic-uifid"]
    assert query["verifyFp"] == ["synthetic-fp"]
    assert len(query["a_bogus"][0]) > 50
    assert signed.headers["x-secsdk-web-signature"] == "synthetic-signature"
    assert signed.headers["User-Agent"]
    assert "synthetic-login" in signed.cookie_header
    assert query["browser_version"] == [security.browser_version]


async def test_douyin_browser_security_exports_portable_platform_state():
    class Context:
        async def storage_state(self):
            return {
                "cookies": [
                    {"name": "sessionid", "value": "login", "domain": ".douyin.com"},
                    {"name": "other", "value": "discard", "domain": ".example.com"},
                ],
                "origins": [
                    {
                        "origin": "https://www.douyin.com",
                        "localStorage": [{"name": "xmst", "value": "synthetic-xmst"}],
                    },
                    {
                        "origin": "https://example.com",
                        "localStorage": [{"name": "secret", "value": "discard"}],
                    },
                ],
            }

    security = DouyinBrowserSecurity(object(), 1)
    security.context = Context()

    state = await security.storage_state()

    assert [cookie["name"] for cookie in state["cookies"]] == ["sessionid"]
    assert state["origins"] == [
        {
            "origin": "https://www.douyin.com",
            "localStorage": [{"name": "xmst", "value": "synthetic-xmst"}],
        }
    ]


async def test_douyin_bound_profile_keeps_fresher_device_cookies():
    class Context:
        cleared = False
        added = False

        async def cookies(self, urls):
            assert urls
            return [
                {"name": "sessionid", "value": "same-login"},
                {"name": "__ac_signature", "value": "new-device-value"},
            ]

        async def clear_cookies(self):
            self.cleared = True

        async def add_cookies(self, cookies):
            self.added = bool(cookies)

    session = type(
        "Session",
        (),
        {
            "config": type("Config", (), {"user_agent": None})(),
            "cookies": [
                {"name": "sessionid", "value": "same-login"},
                {"name": "__ac_signature", "value": "stale-device-value"},
            ],
        },
    )()
    security = DouyinBrowserSecurity(session, 1)
    security.context = Context()

    await security._bind_session_cookies()

    assert security.context.cleared is False
    assert security.context.added is False


async def test_douyin_bound_profile_imports_missing_security_identity():
    class Context:
        added = []

        async def cookies(self, urls):
            assert urls
            return [{"name": "sessionid", "value": "same-login"}]

        async def clear_cookies(self):
            raise AssertionError("matching login must not clear the profile")

        async def add_cookies(self, cookies):
            self.added = cookies

    session = type(
        "Session",
        (),
        {
            "config": type("Config", (), {"user_agent": None})(),
            "cookies": [
                {"name": "sessionid", "value": "same-login"},
                {"name": "UIFID", "value": "bound-uifid"},
                {"name": "s_v_web_id", "value": "bound-fp"},
                {"name": "__ac_signature", "value": "stale-device-value"},
            ],
        },
    )()
    security = DouyinBrowserSecurity(session, 1)
    security.context = Context()

    await security._bind_session_cookies()

    assert [cookie["name"] for cookie in security.context.added] == [
        "UIFID",
        "s_v_web_id",
    ]


@pytest.mark.parametrize(
    ("policy", "blocked"),
    [("warn", False), ("strict", True)],
)
async def test_douyin_environment_probe_failure_respects_policy(policy, blocked):
    class Page:
        async def evaluate(self, _script):
            raise TypeError("synthetic probe failure")

    session = type(
        "Session",
        (),
        {
            "config": type(
                "Config",
                (),
                {
                    "user_agent": None,
                    "consistency_policy": policy,
                    "headless": True,
                    "browser_channel": None,
                },
            )(),
            "environment_snapshot": None,
        },
    )()
    security = DouyinBrowserSecurity(session, 1)
    security.page = Page()
    security.context = type("Context", (), {"browser": None})()

    if blocked:
        with pytest.raises(CollectionError) as found:
            await security._audit_environment()
        assert found.value.kind == "environment_mismatch"
    else:
        await security._audit_environment()


@pytest.mark.parametrize("operation", ["search", "detail", "comments", "replies"])
async def test_douyin_signing_transport_and_sample_redaction(tmp_path, operation):
    path = tmp_path / "cookie.txt"
    path.write_text("sessionid=synthetic-login; msToken=synthetic-ms; s_v_web_id=synthetic-fp")
    session = Session(EnvironmentConfig(cookie_file=str(path)), "douyin")
    budget = Budget()
    observed = []

    async def transport(url, headers):
        observed.append((url, headers))
        if operation == "detail":
            payload = {"aweme_detail": {"aweme_id": "n", "desc": "synthetic"}}
        elif operation == "search":
            payload = {"data": [], "has_more": 0, "cursor": 20, "log_pb": {"impr_id": "search-id"}}
        else:
            payload = {"comments": [], "has_more": 0, "cursor": 0}
        return 200, {"status_code": 0} | payload, {}

    adapter = DouyinHTTP(session, budget, Samples(tmp_path / "output"), transport=transport)
    request = TaskRequest(
        operation=operation,
        keyword="猫",
        content_id="n",
        root_id="root",
        sort="latest",
        context={"cursor": "10", "search_id": "prior-id"},
    )
    result = await adapter.fetch(request)
    await adapter.close()
    assert result.response_has_more is False
    assert budget.calls == [operation]
    query = parse_qs(urlsplit(observed[0][0]).query)
    assert len(query["a_bogus"][0]) > 50
    assert query["msToken"] == ["synthetic-ms"]
    assert query["verifyFp"] == ["synthetic-fp"]
    assert "synthetic-login" in observed[0][1]["Cookie"]
    if operation == "replies":
        assert query["comment_id"] == ["root"] and query["item_id"] == ["n"]
    if operation == "search":
        assert urlsplit(observed[0][0]).path == "/aweme/v1/web/search/item/"
        assert query["search_channel"] == ["aweme_video_web"]
        assert query["sort_type"] == ["2"]
        assert "filter_selected" not in query
        assert observed[0][1]["Referer"].endswith("?type=video")
    if operation == "search":
        assert query["search_id"] == ["prior-id"] and query["offset"] == ["10"]
    assert "synthetic-ms" not in "".join(
        p.read_text() for p in (tmp_path / "output").rglob("*.json")
    )


async def test_douyin_blank_success_response_is_retryable_network_failure(tmp_path):
    path = tmp_path / "cookie.txt"
    path.write_text("sessionid=synthetic-login")

    async def transport(_url, _headers):
        return 200, "", {}

    adapter = DouyinHTTP(
        Session(EnvironmentConfig(cookie_file=str(path)), "douyin"),
        Budget(),
        Samples(tmp_path / "output"),
        transport=transport,
    )
    with pytest.raises(CollectionError) as found:
        await adapter.fetch(TaskRequest(operation="replies", content_id="n", root_id="root"))
    assert found.value.kind == "network_failure"
