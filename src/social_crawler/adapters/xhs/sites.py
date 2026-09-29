from dataclasses import dataclass


@dataclass(frozen=True)
class XHSSite:
    platform: str
    display_name: str
    web_origin: str
    api_origin: str
    cookie_domain: str
    request_domains: tuple[str, ...]
    media_domains: tuple[str, ...]
    local_session_cookies: frozenset[str]


SITES = {
    "xhs": XHSSite(
        platform="xhs",
        display_name="小红书",
        web_origin="https://www.xiaohongshu.com",
        api_origin="https://edith.xiaohongshu.com",
        cookie_domain="xiaohongshu.com",
        request_domains=("xiaohongshu.com", "xhscdn.com", "xhslink.com"),
        media_domains=("xhscdn.com",),
        local_session_cookies=frozenset({"web_session"}),
    ),
    "rednote": XHSSite(
        platform="rednote",
        display_name="RedNote",
        web_origin="https://www.rednote.com",
        api_origin="https://webapi.rednote.com",
        cookie_domain="rednote.com",
        request_domains=("rednote.com", "rednotecdn.com"),
        media_domains=("rednotecdn.com",),
        # RedNote session-cookie names have changed across frontend versions.
        # a1 is only a local signing-material check; online identity is verified
        # separately through user/me or observed page state.
        local_session_cookies=frozenset({"a1"}),
    ),
}

XHS_PLATFORMS = frozenset(SITES)
XHS_API_ADAPTERS = frozenset({"httpx", "curl_cffi"})


def is_xhs_platform(platform: str) -> bool:
    return platform in XHS_PLATFORMS


def is_xhs_api_adapter(adapter: str | None) -> bool:
    return adapter in XHS_API_ADAPTERS


def site_for(platform: str) -> XHSSite:
    try:
        return SITES[platform]
    except KeyError:
        raise ValueError(f"Unsupported XHS-family platform: {platform}") from None


def hostname_matches(hostname: str, suffixes: tuple[str, ...]) -> bool:
    value = hostname.lower().rstrip(".")
    return any(value == suffix or value.endswith("." + suffix) for suffix in suffixes)
