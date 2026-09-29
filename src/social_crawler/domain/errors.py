from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .models import CollectionError


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            )
        except (ValueError, TypeError):
            return None


def check_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if status == 429:
        raise CollectionError(
            "rate_limit", retry_after=retry_after_seconds(headers.get("retry-after"))
        )
    if status == 401:
        raise CollectionError("auth_expired")
    if status in {461, 471}:
        raise CollectionError("verification_required")
    if status in {403, 406}:
        raise CollectionError("access_denied")
    if status == 404:
        raise CollectionError("content_unavailable")
    if status >= 500:
        raise CollectionError("network_failure", f"HTTP {status}")
    if status >= 300:
        raise CollectionError("access_denied", f"Unexpected HTTP {status}")
    if not isinstance(payload, dict):
        text = str(payload).lower()
        kind = (
            "verification_required"
            if any(t in text for t in ("captcha", "验证码", "安全验证"))
            else "abnormal_empty"
        )
        raise CollectionError(kind, "Expected a JSON object")
    code = str(payload.get("code", payload.get("status_code", "0")))
    message = str(payload.get("msg", payload.get("message", payload.get("status_msg", "")))).lower()
    if code in {"-100", "100", "1001"}:
        raise CollectionError("auth_expired", f"Platform code {code}")
    if code in {"300012", "300013"}:
        raise CollectionError("access_denied", f"Platform code {code}")
    if any(s in message for s in ("验证码", "安全验证", "captcha", "verify")):
        raise CollectionError("verification_required", f"Platform code {code}")
    if any(s in message for s in ("登录", "login")):
        raise CollectionError("auth_expired", f"Platform code {code}")
    if any(s in message for s in ("频繁", "限流", "rate limit")):
        raise CollectionError("rate_limit", f"Platform code {code}")
    search_nil = payload.get("search_nil_info") or {}
    if isinstance(search_nil, dict) and any(
        search_nil.get(key) == "verify_check"
        for key in ("search_nil_type", "search_nil_item")
    ):
        raise CollectionError("verification_required", "Platform search verification check")
    if payload.get("success") is False or code not in {"0", "200"}:
        raise CollectionError("access_denied", f"Unrecognized platform error {code}")
