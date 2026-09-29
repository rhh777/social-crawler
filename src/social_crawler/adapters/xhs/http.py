from urllib.parse import urlsplit, urlunsplit

import httpx
from curl_cffi.curl import CurlError
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from xhshow import Xhshow

from social_crawler.domain.models import CollectionError, Operation
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    browser_identity_headers,
    http_environment_observation,
)

from .errors import check_response
from .parsing import parse_page
from .sites import site_for

HOST = "https://edith.xiaohongshu.com"
ENDPOINTS = {
    Operation.SEARCH: ("POST", "/api/sns/web/v1/search/notes"),
    Operation.DETAIL: ("POST", "/api/sns/web/v1/feed"),
    Operation.COMMENTS: ("GET", "/api/sns/web/v2/comment/page"),
    Operation.REPLIES: ("GET", "/api/sns/web/v2/comment/sub/page"),
}


def _httpx_proxy(value: str) -> str | None:
    """HTTPX uses socks5 for remote-DNS SOCKS connections, unlike curl's socks5h alias."""
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "socks5h":
        return urlunsplit(("socks5", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


class XHSSigner:
    """Small boundary around xhshow so signatures can be fixture-tested and versioned."""

    def __init__(self, api_origin: str = HOST):
        self.client = Xhshow()
        self.api_origin = api_origin

    def search_id(self) -> str:
        return self.client.get_search_id()

    def headers(self, method: str, uri: str, cookies: str, data: dict) -> dict[str, str]:
        kwargs = {
            "uri": uri,
            "cookies": cookies,
            "x_rap": uri.endswith(("/search/notes", "/feed")),
        }
        if method == "POST":
            return self.client.sign_headers_post(payload=data, **kwargs)
        return self.client.sign_headers_get(params=data, **kwargs)

    def url(self, uri: str, params: dict) -> str:
        return self.client.build_url(self.api_origin + uri, params)

    def body(self, payload: dict) -> str:
        return self.client.build_json_body(payload)


class XHSHTTP:
    """Read-only XHS-family API adapter with selectable HTTP transports."""

    version = "xhs-api-v4/httpx-or-curl-cffi/rednote-sites/environment-snapshot/xhshow-0.2"

    def __init__(
        self,
        session,
        budget,
        samples,
        *,
        transport=None,
        signer=None,
        transport_kind="httpx",
    ):
        self.session, self.budget, self.samples = session, budget, samples
        self.site = site_for(session.platform)
        self.transport = transport
        if transport_kind not in {"httpx", "curl_cffi"}:
            raise ValueError(f"Unsupported XHS HTTP transport: {transport_kind}")
        self.transport_kind = transport_kind
        self.signer = signer or XHSSigner(self.site.api_origin)
        self.client = None
        self.environment_checked = False
        if not session.cookie_value("a1"):
            raise CollectionError(
                "auth_expired", f"{self.site.display_name} HTTP adapter requires the a1 cookie"
            )

    def request_data(self, request) -> tuple[str, str, dict]:
        method, uri = ENDPOINTS[request.operation]
        cursor = str(request.context.get("cursor", ""))
        token = str(request.input.get("xsec_token") or "")
        if request.operation != Operation.SEARCH and not token:
            raise CollectionError(
                "invalid_input",
                f"{self.site.display_name} HTTP detail and comment requests require xsec_token",
            )
        if request.operation == Operation.SEARCH:
            search_id = str(request.context.get("search_id") or self.signer.search_id())
            data = {
                "keyword": request.keyword,
                "page": int(request.context.get("page", 1)),
                "page_size": 20,
                "search_id": search_id,
                "sort": "time_descending" if request.sort == "latest" else "general",
                "note_type": 0,
            }
        elif request.operation == Operation.DETAIL:
            data = {
                "source_note_id": request.content_id,
                "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": 1},
                "xsec_source": request.input.get("xsec_source") or "pc_search",
                "xsec_token": token,
            }
        elif request.operation == Operation.COMMENTS:
            data = {
                "note_id": request.content_id,
                "cursor": cursor,
                "top_comment_id": "",
                "image_formats": "jpg,webp,avif",
                "xsec_token": token,
            }
        else:
            if not request.root_id:
                raise CollectionError("invalid_input", "Missing root comment ID")
            if not cursor:
                cursor = str((request.input.get("root_comment") or {}).get("sub_cursor") or "")
            data = {
                "note_id": request.content_id,
                "root_comment_id": request.root_id,
                "num": "10",
                "cursor": cursor,
                "image_formats": "jpg,webp,avif",
                "top_comment_id": "",
                "xsec_token": token,
            }
        return method, uri, data

    def _headers(self, method: str, uri: str, data: dict) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "cache-control": "no-cache",
            "content-type": "application/json;charset=UTF-8",
            "origin": self.site.web_origin,
            "pragma": "no-cache",
            "referer": self.site.web_origin + "/",
            "user-agent": self.session.effective_user_agent,
            "Cookie": self.session.cookie_header,
        }
        if locale := self.session.browser_locale:
            headers["accept-language"] = locale
        headers.update(browser_identity_headers(self.session.environment_snapshot))
        headers.update(self.signer.headers(method, uri, self.session.cookie_header, data))
        return headers

    async def fetch(self, request):
        method, uri, data = self.request_data(request)
        if not self.environment_checked:
            if self.session.config.consistency_policy != "off":
                audit_environment(
                    self.session,
                    http_environment_observation(
                        self.session.effective_user_agent,
                        transport="injected" if self.transport else self.transport_kind,
                        impersonate=(
                            self.session.config.impersonate
                            if not self.transport and self.transport_kind == "curl_cffi"
                            else None
                        ),
                    ),
                    budget=self.budget,
                    stage="xhs_http",
                )
            self.environment_checked = True
        await self.budget.admit(str(request.operation))
        try:
            headers = self._headers(method, uri, data)
            body = self.signer.body(data) if method == "POST" else None
            url = self.site.api_origin + uri if method == "POST" else self.signer.url(uri, data)
            if self.transport:
                status, payload, response_headers = await self.transport(method, url, headers, body)
            else:
                if self.client is None and self.transport_kind == "httpx":
                    self.client = httpx.AsyncClient(
                        proxy=_httpx_proxy(self.session.proxy),
                        trust_env=False,
                        verify=True,
                        follow_redirects=False,
                        timeout=self.budget.config.request_timeout,
                    )
                elif self.client is None:
                    # XHS signatures use the persisted Cookie header. Do not let a
                    # transport-owned cookie jar silently diverge from that input.
                    self.client = CurlAsyncSession(
                        impersonate=self.session.config.impersonate,
                        proxy=self.session.proxy or None,
                        trust_env=False,
                        default_headers=False,
                        discard_cookies=True,
                        max_clients=1,
                    )
                if self.transport_kind == "httpx":
                    response = await self.client.request(
                        method, url, headers=headers, content=body
                    )
                else:
                    response = await self.client.request(
                        method,
                        url,
                        headers=headers,
                        content=body,
                        timeout=self.budget.config.request_timeout,
                        allow_redirects=False,
                        verify=True,
                    )
                status, response_headers = response.status_code, dict(response.headers)
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text[:4096]
        except CollectionError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise CollectionError("schema_changed", "XHS request signing failed") from exc
        except (httpx.HTTPError, CurlError, OSError) as exc:
            raise CollectionError("network_failure", type(exc).__name__) from exc
        except Exception as exc:
            raise CollectionError("network_failure", type(exc).__name__) from exc

        sample = self.samples.write(
            {
                "operation": str(request.operation),
                "path": uri,
                "method": method,
                "params": data,
            },
            status,
            payload,
        )
        try:
            if status == 200 and isinstance(payload, str) and not payload.strip():
                raise CollectionError("network_failure", "Empty response body")
            check_response(status, payload, response_headers)
            parse_request = request
            if request.operation == Operation.SEARCH:
                parse_request = request.model_copy(
                    update={"context": request.context | {"search_id": data["search_id"]}}
                )
            result = parse_page(parse_request, payload, platform=self.session.platform)
        except CollectionError as exc:
            exc.sample_ref = sample
            raise
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            error = CollectionError("schema_changed", "Unexpected response field type")
            error.sample_ref = sample
            raise error from exc
        result.sample_ref = sample
        return result

    async def close(self):
        if self.client is not None:
            if self.transport_kind == "httpx":
                await self.client.aclose()
            else:
                await self.client.close()
