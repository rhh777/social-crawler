import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from social_crawler.domain.models import CollectionError, Item, Operation, PageResult
from social_crawler.domain.quality import assess

from .errors import check_response
from .sites import site_for


@dataclass
class Exchange:
    url: str
    method: str
    params: dict[str, Any]
    payload: dict[str, Any]
    status: int = 200
    sample_ref: str | None = None


def operation_for_path(path: str):
    if path.endswith("/search/notes"):
        return Operation.SEARCH
    if path.endswith("/feed"):
        return Operation.DETAIL
    if path.endswith("/comment/sub/page"):
        return Operation.REPLIES
    if path.endswith("/comment/page"):
        return Operation.COMMENTS
    return None


def latest_filter_applied(value: object) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "sort_type"
        and isinstance(item.get("tags"), list)
        and "time_descending" in item["tags"]
        for item in value
    )


def matches(request, exchange: Exchange, platform: str = "xhs") -> bool:
    u = urlsplit(exchange.url)
    domain = site_for(platform).cookie_domain
    hostname = (u.hostname or "").lower()
    if hostname != domain and not hostname.endswith("." + domain):
        return False
    if operation_for_path(u.path) != request.operation:
        return False
    params = {
        k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()
    } | exchange.params
    if request.operation == Operation.SEARCH:
        if exchange.method != "POST" or params.get("keyword") != request.keyword:
            return False
        actual_sort = params.get("sort", "general")
        filtered_latest = latest_filter_applied(params.get("filters"))
        if request.sort == "latest":
            # The current v2 contract keeps the top-level sort as general and
            # expresses recency through filters. Keep the old top-level value
            # for accounts still receiving the earlier page variant.
            if actual_sort != "time_descending" and not (
                actual_sort == "general" and filtered_latest
            ):
                return False
        elif actual_sort != "general" or filtered_latest:
            return False
        return str(params.get("page", 1)) == str(request.context.get("page", 1))
    if request.operation == Operation.DETAIL:
        return (
            exchange.method == "POST"
            and str(params.get("source_note_id", params.get("note_id", ""))) == request.content_id
        )
    if exchange.method != "GET" or str(params.get("note_id", "")) != request.content_id:
        return False
    if (
        request.operation == Operation.REPLIES
        and str(params.get("root_comment_id", "")) != request.root_id
    ):
        return False
    expected_cursor = request.context.get("cursor", "")
    if request.operation == Operation.REPLIES and not expected_cursor:
        expected_cursor = (request.input.get("root_comment") or {}).get("sub_cursor", "")
    return str(params.get("cursor", "")) == str(expected_cursor)


def content_item(
    raw: dict, *, detail=False, token=None, source=None, platform="xhs"
) -> Item:
    cid = str(raw.get("note_id") or raw.get("noteId") or raw.get("id") or "")
    if not cid:
        raise CollectionError("schema_changed", "Missing note ID")
    user = raw.get("user") or {}
    images = raw.get("image_list") or raw.get("imageList") or []
    media = [i.get("url_default") or i.get("urlDefault") or i.get("url") for i in images]
    media = [url for url in media if url]
    streams = ((raw.get("video") or {}).get("media") or {}).get("stream") or {}
    for variants in streams.values():
        for stream in variants or []:
            url = stream.get("masterUrl") or stream.get("master_url")
            if url and url not in media:
                media.append(url)
    return assess(
        Item(
            id=cid,
            kind="content",
            content_id=cid,
            data={
                "title": raw.get("title", raw.get("display_title", "")),
                "text": raw.get("desc"),
                "author_id": user.get("user_id", user.get("userId")),
                "author_name": user.get("nickname"),
                "published_at": raw.get("time"),
                "source_updated_at": raw.get("last_update_time", raw.get("lastUpdateTime")),
                "media": media,
                "metrics": raw.get("interact_info", raw.get("interactInfo", {})),
                "xsec_token": token or raw.get("xsec_token") or raw.get("xsecToken"),
                "xsec_source": source or raw.get("xsec_source") or raw.get("xsecSource") or "pc_search",
                "url": site_for(platform).web_origin + "/explore/" + cid,
                "detail_complete": detail,
            },
        ),
        detail=detail,
    )


def comment_item(raw, content_id, root_id=None):
    cid = str(raw.get("id") or "")
    if not cid:
        raise CollectionError("schema_changed", "Missing comment ID")
    user = raw.get("user_info") or raw.get("userInfo") or {}
    children = [
        comment_item(c, content_id, cid)
        for c in raw.get("sub_comments", raw.get("subComments", [])) or []
    ]
    reply_count = raw.get("sub_comment_count", raw.get("subCommentCount"))
    try:
        reply_count = int(reply_count) if reply_count is not None else None
    except (ValueError, TypeError):
        reply_count = None
    more = raw.get("sub_comment_has_more")
    sub_cursor = raw.get("sub_comment_cursor", raw.get("subCommentCursor"))
    return assess(
        Item(
            id=cid,
            kind="comment",
            content_id=content_id,
            root_id=root_id,
            parent_id=(raw.get("target_comment") or {}).get("id") or root_id,
            data={
                "text": raw.get("content"),
                "author_id": user.get("user_id", user.get("userId")),
                "author_name": user.get("nickname"),
                "published_at": raw.get("create_time", raw.get("createTime")),
                "like_count": raw.get("like_count"),
                "reply_count": reply_count,
                "sub_has_more": more if isinstance(more, bool) else None,
                "sub_cursor": str(sub_cursor) if sub_cursor not in {None, ""} else None,
            },
            children=children,
        )
    )


def parse_page(request, payload, *, platform="xhs") -> PageResult:
    check_response(200, payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectionError("schema_changed", "Missing data object")
    if request.operation == Operation.DETAIL:
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise CollectionError("schema_changed", "Missing detail items")
        items = []
        for value in raw_items:
            raw = value.get("note_card") if isinstance(value, dict) else None
            if not isinstance(raw, dict) or not raw:
                raise CollectionError("schema_changed", "Missing detail card")
            item = content_item(
                raw | {"id": value.get("id")},
                detail=True,
                token=request.input.get("xsec_token"),
                source=request.input.get("xsec_source"),
                platform=platform,
            )
            items.append(item)
        items = [item for item in items if item.id == request.content_id]
        if not items:
            raise CollectionError("content_unavailable")
        return PageResult(
            items=items,
            response_has_more=False,
            stop_reason=None
            if all(i.data["detail_complete"] for i in items)
            else "incomplete_fields",
        )
    skipped = []
    if request.operation == Operation.SEARCH:
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise CollectionError("schema_changed", "Missing search items")
        items = []
        for index, raw in enumerate(raw_items):
            if (
                not isinstance(raw, dict)
                or raw.get("model_type", "note") != "note"
                or not raw.get("note_card")
            ):
                skipped.append({"id": str(index), "reason": "non_note"})
                continue
            items.append(
                content_item(
                    raw["note_card"] | {"id": raw.get("id")},
                    token=raw.get("xsec_token"),
                    source=raw.get("xsec_source"),
                    platform=platform,
                )
            )
        context = {"page": int(request.context.get("page", 1)) + 1}
        if search_id := request.context.get("search_id"):
            context["search_id"] = str(search_id)
    else:
        raw_items = data.get("comments")
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
        context = {"cursor": str(data["cursor"])} if data.get("cursor") not in {None, ""} else {}
    if not isinstance(data.get("has_more"), bool):
        raise CollectionError("schema_changed", "Missing has_more")
    return PageResult(
        items=items, next_context=context, response_has_more=data["has_more"], skipped=skipped
    )
