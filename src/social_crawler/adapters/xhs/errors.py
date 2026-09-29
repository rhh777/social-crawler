from typing import Any

from social_crawler.domain.errors import check_response as check_platform_response
from social_crawler.domain.models import CollectionError

# Observed note-level states: only this note is unavailable, the account is
# fine. Both the code and the message must match so that a risk response that
# reuses a code (e.g. "访问受限") still stops the run.
UNAVAILABLE_NOTE = {
    "-510000": "笔记不存在",
    "-510001": "当前内容无法展示",
    "-510002": "笔记正在审核中",
}


def check_response(status: int, payload: Any, headers=None) -> None:
    """Recognize observed unavailable-note responses without relaxing risk checks."""
    try:
        check_platform_response(status, payload, headers)
    except CollectionError as exc:
        if exc.kind == "access_denied" and status == 200 and isinstance(payload, dict):
            expected = UNAVAILABLE_NOTE.get(str(payload.get("code")))
            message = str(payload.get("msg") or "")
            if expected and payload.get("success") is False and message.startswith(expected):
                raise CollectionError("content_unavailable", message) from exc
        raise
