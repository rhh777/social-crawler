import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_KEYS = {
    "cookie",
    "cookies",
    "authorization",
    "set_cookie",
    "web_session",
    "websession",
    "search_id",
    "session_id",
    "sessionid",
    "sessionid_ss",
    "mstoken",
    "ms_token",
    "a_bogus",
    "x_bogus",
    "uifid",
    "verifyfp",
    "fp",
    "x_secsdk_web_signature",
    "a1",
    "xsec_token",
    "token",
    "password",
    "proxy_password",
    "credentials",
    "storage_state",
}


def is_secret(key: str) -> bool:
    normalized = re.sub(r"[-\s]", "_", key).lower()
    return normalized in SECRET_KEYS or normalized.endswith(("_token", "_password", "_secret"))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[redacted]" if is_secret(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "socks5://", "socks5h://")):
            try:
                u = urlsplit(value)
                host = u.netloc.rsplit("@", 1)[-1]
                query = urlencode(
                    [
                        (k, "[redacted]" if is_secret(k) else v)
                        for k, v in parse_qsl(u.query, keep_blank_values=True)
                    ]
                )
                return urlunsplit((u.scheme, host, u.path, query, ""))
            except ValueError:
                return "[invalid URL]"
        return re.sub(
            r"(?i)\b(cookie|authorization|web_session|sessionid|msToken|xsec_token|a_bogus)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            value,
        )
    return value
