"""Bounded share-link resolution in the worker's session and request budget."""

from urllib.parse import urljoin

import httpx
from curl_cffi.requests import AsyncSession

from social_crawler.domain.models import CollectionError, PageResult
from social_crawler.domain.targets import checked_url, parse_target
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    http_environment_observation,
)


async def resolve_target(
    request, platform, budget, session, *, offline=False, transport=None, adapter=None
):
    target = dict(request.input["target"])
    if not target.get("needs_redirect"):
        return PageResult(resolved_target=target, response_has_more=False)
    if offline and transport is None:
        raise CollectionError("invalid_target", "离线演示不展开短链，请使用完整链接或帖子 ID")
    if session is None and transport is None:
        raise CollectionError("invalid_target", "短链解析需要采集会话")
    client = None
    try:
        if transport is None:
            use_curl = platform == "douyin" or adapter == "curl_cffi"
            audit_environment(
                session,
                http_environment_observation(
                    session.effective_user_agent,
                    transport="curl_cffi" if use_curl else "httpx",
                    impersonate=session.config.impersonate if use_curl else None,
                ),
                budget=budget,
                stage="target_resolution",
            )
            if use_curl:
                options = {
                    "impersonate": session.config.impersonate,
                    "proxy": session.proxy or None,
                    "trust_env": False,
                }
                if platform != "douyin":
                    options.update(
                        default_headers=False,
                        discard_cookies=True,
                        max_clients=1,
                    )
                client = AsyncSession(**options)
            else:
                from social_crawler.adapters.xhs.http import _httpx_proxy

                client = httpx.AsyncClient(
                    proxy=_httpx_proxy(session.proxy),
                    trust_env=False,
                    follow_redirects=False,
                )
        visited = set()
        url = target["url"]
        for _ in range(5):
            url = checked_url(platform, url)
            if url in visited:
                raise CollectionError("invalid_target", "分享链接发生循环跳转")
            visited.add(url)
            await budget.admit("resolve_target")
            if transport:
                status, headers = await transport(url)
            elif use_curl:
                response = await client.get(
                    url,
                    headers={"User-Agent": session.effective_user_agent},
                    timeout=budget.config.request_timeout,
                    allow_redirects=False,
                )
                status, headers = response.status_code, response.headers
            else:
                async with client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": session.effective_user_agent},
                    timeout=budget.config.request_timeout,
                ) as response:
                    status, headers = response.status_code, response.headers
            if status in (401, 403, 429):
                raise CollectionError(
                    {401: "auth_expired", 403: "access_denied", 429: "rate_limit"}[status]
                )
            if status >= 500:
                raise CollectionError("network_failure", "分享链接服务暂时不可用")
            location = headers.get("location") or headers.get("Location")
            if status not in (301, 302, 303, 307, 308) or not location:
                raise CollectionError("invalid_target", "分享链接未返回可识别的帖子跳转")
            url = checked_url(platform, urljoin(url, location))
            target = parse_target(platform, url)
            if not target.get("needs_redirect"):
                return PageResult(resolved_target=target, response_has_more=False)
        raise CollectionError("invalid_target", "分享链接超过 5 次跳转上限")
    except CollectionError:
        raise
    except Exception as exc:
        raise CollectionError("network_failure", type(exc).__name__) from exc
    finally:
        if client is not None:
            if use_curl:
                await client.close()
            else:
                await client.aclose()
