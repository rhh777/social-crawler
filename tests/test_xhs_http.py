import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.http import XHSHTTP, XHSSigner, _httpx_proxy
from social_crawler.domain.models import CollectionError, RunConfig, TaskRequest
from social_crawler.environments.environment_snapshot import write_environment_snapshot
from social_crawler.environments.session import EnvironmentConfig, Session


class Budget:
    def __init__(self):
        self.calls = []
        self.config = SimpleNamespace(request_timeout=20)

    async def admit(self, operation):
        self.calls.append(operation)


class Signer:
    def __init__(self, api_origin="https://edith.xiaohongshu.com"):
        self.calls = []
        self.api_origin = api_origin

    def search_id(self):
        return "search-session"

    def headers(self, method, uri, cookies, data):
        self.calls.append((method, uri, cookies, data.copy()))
        return {
            "x-s": "synthetic-signature",
            "x-t": "1700000000000",
            "x-s-common": "synthetic-common",
            "x-b3-traceid": "0123456789abcdef",
        }

    def url(self, uri, params):
        return self.api_origin + uri + "?" + urlencode(params)

    def body(self, payload):
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def online_session(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps(
            {
                "a1": "synthetic-a1",
                "web_session": "synthetic-session",
                "webId": "synthetic-web-id",
            }
        )
    )
    return Session(EnvironmentConfig(cookie_file=str(path)), "xhs")


def rednote_session(tmp_path):
    path = tmp_path / "rednote-cookies.json"
    path.write_text(
        json.dumps(
            [
                {"name": "a1", "value": "synthetic-a1", "domain": ".rednote.com"},
                {"name": "webId", "value": "synthetic-web-id", "domain": ".rednote.com"},
            ]
        )
    )
    return Session(EnvironmentConfig(cookie_file=str(path)), "rednote")


@pytest.mark.parametrize("platform", ["xhs", "rednote"])
async def test_xhs_family_http_four_operations_and_search_context(tmp_path, platform):
    session = online_session(tmp_path) if platform == "xhs" else rednote_session(tmp_path)
    budget = Budget()
    signer = Signer(
        "https://edith.xiaohongshu.com"
        if platform == "xhs"
        else "https://webapi.rednote.com"
    )
    observed = []

    async def transport(method, url, headers, body):
        observed.append((method, url, headers, body))
        path = urlsplit(url).path
        if path.endswith("/search/notes"):
            payload = {
                "success": True,
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "n1",
                            "model_type": "note",
                            "xsec_token": "synthetic-token",
                            "xsec_source": "pc_search",
                            "note_card": {"display_title": "result"},
                        }
                    ],
                    "has_more": True,
                },
            }
        elif path.endswith("/feed"):
            payload = {
                "success": True,
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "n1",
                            "note_card": {
                                "note_id": "n1",
                                "desc": "full text",
                                "time": 1700000000000,
                                "user": {"user_id": "author"},
                            },
                        }
                    ]
                },
            }
        elif path.endswith("/comment/page"):
            payload = {
                "success": True,
                "code": 0,
                "data": {
                    "comments": [
                        {
                            "id": "root",
                            "content": "comment",
                            "sub_comment_count": 1,
                            "sub_comment_has_more": True,
                            "sub_comment_cursor": "embedded-cursor",
                        }
                    ],
                    "has_more": False,
                    "cursor": "",
                },
            }
        else:
            payload = {
                "success": True,
                "code": 0,
                "data": {
                    "comments": [{"id": "reply", "content": "reply"}],
                    "has_more": False,
                    "cursor": "",
                },
            }
        return 200, payload, {}

    adapter = XHSHTTP(
        session,
        budget,
        Samples(tmp_path / "output"),
        transport=transport,
        signer=signer,
    )
    search = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
    assert search.next_context == {"page": 2, "search_id": "search-session"}
    assert search.items[0].data["xsec_token"] == "synthetic-token"
    assert signer.calls[0][3]["sort"] == "time_descending"
    assert search.items[0].data["url"].startswith(
        "https://www.xiaohongshu.com" if platform == "xhs" else "https://www.rednote.com"
    )

    detail_input = search.items[0].data
    detail = await adapter.fetch(
        TaskRequest(operation="detail", content_id="n1", input=detail_input)
    )
    assert detail.items[0].data["text"] == "full text"
    comments = await adapter.fetch(
        TaskRequest(operation="comments", content_id="n1", input=detail.items[0].data)
    )
    root = comments.items[0]
    replies = await adapter.fetch(
        TaskRequest(
            operation="replies",
            content_id="n1",
            root_id="root",
            input=detail.items[0].data | {"root_comment": root.data},
        )
    )
    assert replies.items[0].root_id == "root"
    assert budget.calls == ["search", "detail", "comments", "replies"]
    assert json.loads(observed[0][3])["search_id"] == "search-session"
    assert parse_qs(urlsplit(observed[-1][1]).query)["cursor"] == ["embedded-cursor"]
    assert all(call[2]["x-s"] == "synthetic-signature" for call in observed)
    await adapter.close()

    saved = "".join(path.read_text() for path in (tmp_path / "output").rglob("*.json"))
    assert "search-session" not in saved
    assert "synthetic-token" not in saved
    assert "synthetic-session" not in saved


async def test_xhs_http_risk_is_typed_and_not_retried(tmp_path):
    session = online_session(tmp_path)

    async def transport(_method, _url, _headers, _body):
        return 461, "captcha", {"Verifytype": "1"}

    adapter = XHSHTTP(
        session,
        Budget(),
        Samples(tmp_path / "output"),
        transport=transport,
        signer=Signer(),
    )
    with pytest.raises(CollectionError) as found:
        await adapter.fetch(TaskRequest(operation="search", keyword="猫"))
    assert found.value.kind == "verification_required"
    assert found.value.sample_ref


def test_xhs_http_configuration_and_cookie_requirements(tmp_path):
    assert RunConfig(platform="xhs", keywords=["猫"]).adapter == "browser"
    assert RunConfig(platform="rednote", keywords=["coffee"]).adapter == "browser"
    assert RunConfig(platform="xhs", keywords=["猫"], adapter="httpx").adapter == "httpx"
    assert RunConfig(platform="rednote", keywords=["coffee"], adapter="httpx").adapter == "httpx"
    assert RunConfig(platform="xhs", keywords=["猫"], adapter="curl_cffi").adapter == "curl_cffi"
    assert (
        RunConfig(platform="rednote", keywords=["coffee"], adapter="curl_cffi").adapter
        == "curl_cffi"
    )
    with pytest.raises(ValueError, match="only for XHS"):
        RunConfig(platform="douyin", keywords=["猫"], adapter="httpx")
    with pytest.raises(ValueError, match="only for XHS"):
        RunConfig(platform="douyin", keywords=["猫"], adapter="curl_cffi")

    path = tmp_path / "cookies.txt"
    path.write_text("web_session=synthetic")
    session = Session(EnvironmentConfig(cookie_file=str(path)), "xhs")
    with pytest.raises(CollectionError, match="a1"):
        XHSHTTP(session, Budget(), Samples(tmp_path / "output"), signer=Signer())

    with pytest.raises(ValueError, match="Unsupported XHS HTTP transport"):
        XHSHTTP(
            online_session(tmp_path),
            Budget(),
            Samples(tmp_path / "other-output"),
            signer=Signer(),
            transport_kind="unknown",
        )


async def test_xhs_curl_cffi_transport_preserves_signed_body_and_cookie_source(
    tmp_path, monkeypatch
):
    created = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class CurlSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.requests = []
            self.closed = False
            created.append(self)

        async def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            if urlsplit(url).path.endswith("/search/notes"):
                return Response(
                    {
                        "success": True,
                        "code": 0,
                        "data": {"items": [], "has_more": False},
                    }
                )
            return Response(
                {
                    "success": True,
                    "code": 0,
                    "data": {"comments": [], "has_more": False, "cursor": ""},
                }
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr("social_crawler.adapters.xhs.http.CurlAsyncSession", CurlSession)
    session = online_session(tmp_path)
    session.proxy = "socks5h://proxy.example:1080"
    signer = Signer()
    adapter = XHSHTTP(
        session,
        Budget(),
        Samples(tmp_path / "curl-output"),
        signer=signer,
        transport_kind="curl_cffi",
    )

    await adapter.fetch(TaskRequest(operation="search", keyword="猫"))
    await adapter.fetch(
        TaskRequest(operation="comments", content_id="n1", input={"xsec_token": "token"})
    )
    await adapter.close()

    client = created[0]
    assert client.kwargs == {
        "impersonate": "chrome150",
        "proxy": "socks5h://proxy.example:1080",
        "trust_env": False,
        "default_headers": False,
        "discard_cookies": True,
        "max_clients": 1,
    }
    post_method, _post_url, post = client.requests[0]
    assert post_method == "POST"
    assert post["content"] == signer.body(signer.calls[0][3])
    assert post["headers"]["Cookie"] == session.cookie_header
    assert client.requests[1][0] == "GET" and client.requests[1][2]["content"] is None
    assert all(request[2]["allow_redirects"] is False for request in client.requests)
    assert client.closed


async def test_rednote_http_uses_international_origins_and_schema(tmp_path):
    observed = []

    async def transport(method, url, headers, body):
        observed.append((method, url, headers, body))
        return 200, {
            "success": True,
            "code": 0,
            "data": {
                "items": [
                    {
                        "id": "n1",
                        "model_type": "note",
                        "xsec_token": "token",
                        "note_card": {"display_title": "result"},
                    }
                ],
                "has_more": False,
            },
        }, {}

    adapter = XHSHTTP(
        rednote_session(tmp_path),
        Budget(),
        Samples(tmp_path / "output"),
        transport=transport,
        signer=Signer(),
    )
    page = await adapter.fetch(TaskRequest(operation="search", keyword="coffee"))
    assert observed[0][1].startswith("https://webapi.rednote.com/")
    assert observed[0][2]["origin"] == "https://www.rednote.com"
    assert observed[0][2]["referer"] == "https://www.rednote.com/"
    assert page.items[0].data["url"] == "https://www.rednote.com/explore/n1"


def test_xhshow_signer_and_proxy_normalization():
    signer = XHSSigner()
    cookies = "a1=19abc0000000000000000000000000000000000000000000000000; web_session=test"
    headers = signer.headers(
        "POST",
        "/api/sns/web/v1/search/notes",
        cookies,
        {
            "keyword": "猫",
            "page": 1,
            "page_size": 20,
            "search_id": signer.search_id(),
            "sort": "general",
            "note_type": 0,
        },
    )
    assert {"x-s", "x-t", "x-s-common", "x-b3-traceid", "x-rap-param"} <= headers.keys()
    assert _httpx_proxy("socks5h://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert _httpx_proxy("") is None


async def test_xhs_http_reuses_login_snapshot_identity(tmp_path):
    profile = tmp_path / "profile"
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0.0.0"
    write_environment_snapshot(
        profile,
        {
            "schema_version": 1,
            "captured_at": 1.0,
            "source": "login",
            "browser": {"major": 150},
            "navigator": {
                "user_agent": user_agent,
                "platform": "Linux x86_64",
                "languages": ["zh-CN", "zh"],
                "user_agent_data": {
                    "brands": [{"brand": "Chromium", "version": "150"}],
                    "platform": "Linux",
                    "mobile": False,
                },
            },
        },
    )
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("web_session=synthetic; a1=synthetic-a1")
    session = Session(
        EnvironmentConfig(cookie_file=str(cookie_file), profile_dir=str(profile)), "xhs"
    )
    observed = []

    async def transport(method, url, headers, body):
        observed.append(headers)
        return 200, {"success": True, "code": 0, "data": {"items": [], "has_more": False}}, {}

    adapter = XHSHTTP(
        session,
        Budget(),
        Samples(tmp_path / "output"),
        transport=transport,
        signer=Signer(),
    )
    await adapter.fetch(TaskRequest(operation="search", keyword="猫"))

    assert observed[0]["user-agent"] == user_agent
    assert observed[0]["sec-ch-ua"] == '"Chromium";v="150"'
    assert observed[0]["sec-ch-ua-platform"] == '"Linux"'
    assert observed[0]["accept-language"] == "zh-CN,zh;q=0.9"
