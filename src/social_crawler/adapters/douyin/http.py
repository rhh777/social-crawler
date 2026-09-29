from urllib.parse import quote, urlencode

from curl_cffi.requests import AsyncSession

from social_crawler.domain.errors import check_response
from social_crawler.domain.models import CollectionError, Item, Operation, PageResult
from social_crawler.domain.quality import assess
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    browser_identity_headers,
    http_environment_observation,
)
from social_crawler.environments.session import BROWSER_VIEWPORT

from .security import COMPAT_A_BOGUS, DouyinBrowserSecurity

HOST = "https://www.douyin.com"
ENDPOINTS = {
    # Collection only consumes aweme results.  The dedicated video-search
    # route avoids the mixed user/live/card payload returned by general search
    # and uses the same route as Douyin's "视频" search tab.
    Operation.SEARCH: "/aweme/v1/web/search/item/",
    Operation.DETAIL: "/aweme/v1/web/aweme/detail/",
    Operation.COMMENTS: "/aweme/v1/web/comment/list/",
    Operation.REPLIES: "/aweme/v1/web/comment/list/reply/",
}


def content_item(raw: dict, *, detail=False) -> Item:
    content_id = str(raw.get("aweme_id") or "")
    if not content_id:
        raise CollectionError("schema_changed", "Missing aweme_id")
    author = raw.get("author") or {}
    media = []
    for image in raw.get("images") or []:
        urls = image.get("url_list") or []
        if urls:
            media.append(urls[0])
    video = raw.get("video") or {}
    if (video.get("play_addr") or {}).get("url_list"):
        media.append(video["play_addr"]["url_list"][0])
    return assess(
        Item(
            id=content_id,
            kind="content",
            content_id=content_id,
            data={
                "title": raw.get("desc"),
                "text": raw.get("desc"),
                "author_id": author.get("uid"),
                "author_name": author.get("nickname"),
                "published_at": raw.get("create_time"),
                "metrics": raw.get("statistics", {}),
                "media": media,
                "url": HOST + "/video/" + content_id,
                "detail_complete": detail,
            },
        ),
        detail=detail,
    )


def comment_item(raw, content_id, root_id=None):
    cid = str(raw.get("cid") or "")
    if not cid:
        raise CollectionError("schema_changed", "Missing comment ID")
    user = raw.get("user") or {}
    children = [comment_item(c, content_id, cid) for c in raw.get("reply_comment") or []]
    try:
        reply_count = int(raw.get("reply_comment_total") or 0)
    except (TypeError, ValueError):
        reply_count = None
    parent_id = next(
        (
            str(raw[key])
            for key in ("reply_to_reply_id", "reply_id")
            if raw.get(key) not in (None, "", 0, "0")
        ),
        root_id,
    )
    return assess(
        Item(
            id=cid,
            kind="comment",
            content_id=content_id,
            root_id=root_id,
            parent_id=parent_id,
            data={
                "text": raw.get("text"),
                "author_id": user.get("uid"),
                "author_name": user.get("nickname"),
                "published_at": raw.get("create_time"),
                "like_count": raw.get("digg_count"),
                "reply_count": reply_count,
            },
            children=children,
        )
    )


def parse_page(request, payload) -> PageResult:
    check_response(200, payload)
    skipped = []
    if request.operation == Operation.DETAIL:
        raw = payload.get("aweme_detail")
        if not isinstance(raw, dict) or not raw:
            raise CollectionError("content_unavailable", "Detail is not visible")
        item = content_item(raw, detail=True)
        if item.id != request.content_id:
            raise CollectionError("schema_changed", "Wrong detail ID")
        return PageResult(
            items=[item],
            response_has_more=False,
            stop_reason=None if item.data["detail_complete"] else "incomplete_fields",
        )
    if request.operation == Operation.SEARCH:
        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            raise CollectionError("schema_changed", "Missing search data array")
        items = []
        for index, value in enumerate(raw_items):
            raw = value.get("aweme_info") if isinstance(value, dict) else None
            if isinstance(raw, dict) and raw.get("aweme_id"):
                items.append(content_item(raw))
            else:
                skipped.append({"id": str(index), "reason": "non_content_result"})
    else:
        raw_items = payload.get("comments")
        # Douyin uses JSON null for a valid empty comment page. The accompanying
        # terminal pagination fields distinguish that from a missing schema field.
        if raw_items is None and payload.get("has_more") in (0, False) and payload.get(
            "total"
        ) in (None, 0, "0"):
            raw_items = []
        if not isinstance(raw_items, list):
            raise CollectionError("schema_changed", "Missing comments array")
        items = [
            comment_item(
                c,
                request.content_id,
                request.root_id if request.operation == Operation.REPLIES else None,
            )
            for c in raw_items
        ]
    more = payload.get("has_more")
    if not isinstance(more, (int, bool)) or more not in (0, 1):
        raise CollectionError("schema_changed", "Missing has_more")
    cursor = payload.get("cursor")
    context = {"cursor": str(cursor)} if cursor is not None else {}
    if request.operation == Operation.SEARCH:
        search_id = (payload.get("log_pb") or {}).get("impr_id") or (
            payload.get("extra") or {}
        ).get("logid")
        search_id = search_id or request.context.get("search_id")
        if search_id and context:
            context["search_id"] = str(search_id)
        if more and not search_id:
            return PageResult(
                items=items,
                response_has_more=True,
                stop_reason="missing_search_context",
                skipped=skipped,
            )
    return PageResult(
        items=items, response_has_more=bool(more), next_context=context, skipped=skipped
    )


class DouyinHTTP:
    version = "douyin-http-v4/adspower-websign-curl-cffi-environment-snapshot"

    def __init__(self, session, budget, samples, *, transport=None, security=None):
        self.session, self.budget, self.samples = session, budget, samples
        self.transport = transport
        self.client = None
        self.security = security
        self.environment_checked = False

    def parameters(self, request):
        user_agent = self.session.effective_user_agent
        browser_version = user_agent.split("Chrome/")[-1].split(" ")[0]
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "pc_client_type": "1",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": browser_version,
            "browser_online": "true",
            "engine_name": "Blink",
            "os_name": "Windows",
            "os_version": "10",
            "platform": "PC",
            "screen_width": str(BROWSER_VIEWPORT["width"]),
            "screen_height": str(BROWSER_VIEWPORT["height"]),
            "update_version_code": "170400",
            "engine_version": browser_version,
            "cpu_core_num": "16",
            "device_memory": "8",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
        }
        if token := self.session.cookie_value("msToken"):
            params["msToken"] = token
        if webid := self.session.config.douyin_webid:
            params["webid"] = webid
        if verify_fp := self.session.cookie_value("s_v_web_id"):
            params.update(verifyFp=verify_fp, fp=verify_fp)
        cursor = request.context.get("cursor", "0")
        if request.operation == Operation.SEARCH:
            params.update(
                keyword=request.keyword,
                search_channel="aweme_video_web",
                search_source="switch_tab",
                offset=cursor,
                count="10",
                search_id=request.context.get("search_id", ""),
                query_correct_type="1",
                enable_history="1",
                is_filter_search="0",
                from_group_id="",
                disable_rs="0",
                need_filter_settings="0",
                list_type="single",
                pc_search_top_1_params='{"enable_ai_search_top_1":1}',
                version_code="170400",
                version_name="17.4.0",
            )
            if request.sort == "latest":
                params.update(
                    is_filter_search="1",
                    sort_type="2",
                )
        elif request.operation == Operation.DETAIL:
            params["aweme_id"] = request.content_id
        elif request.operation == Operation.COMMENTS:
            params.update(aweme_id=request.content_id, cursor=cursor, count="20", item_type="0")
        else:
            params.update(
                item_id=request.content_id,
                comment_id=request.root_id,
                cursor=cursor,
                count="20",
                item_type="0",
            )
        return params

    async def fetch(self, request):
        if (
            not self.environment_checked
            and self.session.config.consistency_policy == "strict"
            and self.session.environment_snapshot is None
        ):
            audit_environment(
                self.session,
                http_environment_observation(
                    self.session.effective_user_agent,
                    transport="curl_cffi" if not self.transport else "injected",
                    impersonate=self.session.config.impersonate if not self.transport else None,
                ),
                budget=self.budget,
                stage="douyin_http_preflight",
            )
        await self.budget.admit(str(request.operation))
        params = self.parameters(request)
        headers = {
            "User-Agent": self.session.effective_user_agent,
            "Cookie": self.session.cookie_header,
            "Accept": "application/json",
            "Referer": (
                HOST + "/root/search/" + quote(request.keyword) + "?type=video"
                if request.operation == Operation.SEARCH
                else HOST + "/video/" + request.content_id
            ),
        }
        headers.update(browser_identity_headers(self.session.environment_snapshot))
        try:
            if self.transport:
                encoded = urlencode(params)
                url = (
                    HOST
                    + ENDPOINTS[request.operation]
                    + "?"
                    + encoded
                    + "&a_bogus="
                    + quote(COMPAT_A_BOGUS, safe="")
                )
                status, payload, response_headers = await self.transport(url, headers)
            else:
                if self.security is None:
                    self.security = DouyinBrowserSecurity(
                        self.session,
                        self.budget.config.request_timeout,
                        budget=self.budget,
                    )
                signed = await self.security.sign(ENDPOINTS[request.operation], params)
                url = signed.url
                headers.update(signed.headers)
                headers["Cookie"] = signed.cookie_header
                if self.client is None:
                    self.client = AsyncSession(
                        impersonate=self.session.config.impersonate,
                        proxy=self.session.proxy or None,
                        trust_env=False,
                    )
                response = await self.client.get(
                    url,
                    headers=headers,
                    timeout=self.budget.config.request_timeout,
                    allow_redirects=False,
                    verify=True,
                )
                status, response_headers = response.status_code, dict(response.headers)
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text[:4096]
            if not self.environment_checked:
                if self.session.config.consistency_policy != "off":
                    audit_environment(
                        self.session,
                        http_environment_observation(
                            headers["User-Agent"],
                            transport="curl_cffi" if not self.transport else "injected",
                            impersonate=(
                                self.session.config.impersonate if not self.transport else None
                            ),
                        ),
                        budget=self.budget,
                        stage="douyin_http",
                    )
                self.environment_checked = True
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("network_failure", type(exc).__name__) from exc
        sample = self.samples.write(
            {
                "operation": str(request.operation),
                "path": ENDPOINTS[request.operation],
                "params": params,
            },
            status,
            payload,
        )
        try:
            # A blank HTTP 200 is normally a dropped/empty transport response, not
            # evidence that the platform JSON schema changed. Let the worker retry it.
            if status == 200 and isinstance(payload, str) and not payload.strip():
                raise CollectionError("network_failure", "Empty response body")
            check_response(status, payload, response_headers)
            result = parse_page(request, payload)
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
        try:
            if self.client is not None:
                await self.client.close()
        finally:
            if self.security is not None:
                await self.security.close()
