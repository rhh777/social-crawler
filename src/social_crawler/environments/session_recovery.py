"""Profile-first coordination. Callers own account/profile/proxy resource locks.

Collection never exports or replaces a conflicting login. Recovery is a separate,
bounded maintenance operation and writes a receipt only after authoritative checks.
"""

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from social_crawler.adapters.xhs.sites import is_xhs_platform, site_for
from social_crawler.domain.errors import check_response
from social_crawler.domain.models import CollectionError, TaskRequest
from social_crawler.environments.session import filter_storage_origins

DOMAINS = {
    "xhs": "xiaohongshu.com",
    "rednote": "rednote.com",
    "douyin": "douyin.com",
}
LOGIN_COOKIES = {
    "xhs": frozenset({"web_session", "web_session_sec"}),
    "rednote": frozenset({"web_session", "web_session_sec", "id_token"}),
    "douyin": frozenset({"sessionid", "sessionid_ss"}),
}
AUTH_COOKIES = {
    "xhs": LOGIN_COOKIES["xhs"] | {"id_token"},
    "rednote": LOGIN_COOKIES["rednote"],
    "douyin": LOGIN_COOKIES["douyin"]
    | {
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
        "sid_guard",
        "sid_ucp_v1",
        "ssid_ucp_v1",
        "passport_auth_status",
        "passport_auth_status_ss",
    },
}


def file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"


def recovery_key(config):
    # Browser cache writes are deliberately excluded: they must not re-arm retries.
    material = [
        file_digest(config.cookie_file),
        config.session_version,
        config.binding_version,
        str(Path(config.profile_dir).resolve()),
        config.browser_provider,
        config.adspower_profile_id,
        config.kameleo_profile_id,
    ]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def read_recovery_receipt(profile):
    try:
        value = json.loads((Path(profile) / "session-recovery.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


async def clear_platform_auth(context, platform):
    domain = re.compile(r"(^|\.)" + re.escape(DOMAINS[platform]) + r"$")
    for name in sorted(AUTH_COOKIES[platform]):
        await context.clear_cookies(name=name, domain=domain)


async def bind_profile(context, session):
    """Seed an empty profile; otherwise preserve login and device state.

    Empty-profile import supports explicitly supplied Cookie files. It conveys no
    identity proof; normal platform checks still apply before a successful task.
    A conflict is delegated to maintenance, never resolved by overwriting state.
    """
    platform = session.platform
    existing = await context.cookies(["https://www." + DOMAINS[platform] + "/"])
    current = {c["name"]: c["value"] for c in existing if c.get("value")}
    names = LOGIN_COOKIES[platform]
    current_auth = {n: current[n] for n in names if n in current}
    if getattr(session, "profile_auth", False):
        if not current_auth:
            raise CollectionError("auth_expired", "Managed browser profile is not logged in")
        return False
    supplied = {c["name"]: c["value"] for c in session.cookies if c.get("value")}
    supplied_auth = {n: supplied[n] for n in names if n in supplied}
    if current_auth and current_auth != supplied_auth:
        raise CollectionError("auth_expired", "Profile session differs; run session recovery")
    if not existing:
        await context.add_cookies(session.cookies)
        return True
    if not current_auth:
        raise CollectionError("auth_expired", "Existing profile needs identity recovery")
    allowed = {
        "xhs": {"a1"},
        "rednote": {"a1", "webId", "xsecappid"},
        "douyin": {"UIFID", "s_v_web_id"},
    }[platform]
    missing = [
        c
        for c in session.cookies
        if c["name"] in allowed and c.get("value") and c["name"] not in current
    ]
    if missing:
        await context.add_cookies(missing)
    return False


def identity_from_payload(platform, payload, expected_user_id=None):
    check_response(200, payload)
    if is_xhs_platform(platform):
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise CollectionError("identity_unverified")
        user = data.get("user_info") or data.get("userInfo") or data.get("user") or data
        if data.get("guest") is True or (isinstance(user, dict) and user.get("guest") is True):
            raise CollectionError("auth_expired")
        if not isinstance(user, dict):
            raise CollectionError("identity_unverified")
        actual = str(user.get("user_id") or user.get("userId") or user.get("id") or "")
    else:
        if payload.get("status_code") != 0:
            raise CollectionError("identity_unverified")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        user = next(
            (
                u
                for u in (
                    payload.get("user"),
                    payload.get("user_info"),
                    data.get("user"),
                    data.get("user_info"),
                )
                if isinstance(u, dict) and u.get("sec_uid") and u.get("uid")
            ),
            {},
        )
        actual = str(user.get("uid") or "")
    if actual in {"", "0"}:
        raise CollectionError("identity_unverified")
    if expected_user_id and actual != expected_user_id:
        raise CollectionError("identity_mismatch")
    result = {"user_id": actual}
    if platform == "douyin":
        result["sec_uid"] = str(user["sec_uid"])
    nickname = next(
        (
            str(user[k]).strip()
            for k in ("nickname", "nick_name", "nickName", "name")
            if user.get(k)
        ),
        "",
    )
    if nickname:
        result["nickname"] = nickname
    return result


async def check_page_signals(page, platform):
    title = await page.title()
    if any(s in title for s in ("验证码", "安全验证", "设备验证")) or any(
        "verifycenter/captcha" in frame.url for frame in page.frames
    ):
        raise CollectionError("verification_required")
    for selector in (".captcha-container", "#captcha_container", ".verify-dialog"):
        if await page.locator(selector).first.is_visible():
            raise CollectionError("verification_required")
    if "login" in page.url.split("?", 1)[0].lower():
        raise CollectionError("auth_expired")
    if is_xhs_platform(platform):
        for selector in (".login-modal", ".login-container"):
            if await page.locator(selector).first.is_visible():
                raise CollectionError("auth_expired")


async def verify_profile(context, page, platform, *, expected_user_id=None, admit=None):
    """One signed self-identity GET, using only the open Profile's credentials."""
    await check_page_signals(page, platform)
    if admit:
        await admit("search")
    cookies = await context.cookies(["https://www." + DOMAINS[platform] + "/"])
    values = {c["name"]: c["value"] for c in cookies if c.get("value")}
    required = (
        site_for(platform).local_session_cookies
        if is_xhs_platform(platform)
        else LOGIN_COOKIES[platform]
    )
    if not required.intersection(values):
        raise CollectionError("auth_expired")
    if is_xhs_platform(platform):
        from social_crawler.adapters.xhs.http import XHSSigner

        uri = "/api/sns/web/v2/user/me"
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        site = site_for(platform)
        headers = XHSSigner(site.api_origin).headers("GET", uri, cookie_header, {})
        url = site.api_origin + uri
    else:
        from social_crawler.adapters.douyin.http import DouyinHTTP
        from social_crawler.adapters.douyin.security import DouyinBrowserSecurity

        user_agent = await page.evaluate("navigator.userAgent")
        session = SimpleNamespace(
            config=SimpleNamespace(user_agent=user_agent, douyin_webid=""),
            effective_user_agent=user_agent,
            cookie_value=lambda name: values.get(name, ""),
        )
        security = DouyinBrowserSecurity(session, 15)
        # Reuse this context; sign() must never launch or bind another profile.
        security.context, security.page = context, page
        security.user_agent = await page.evaluate("navigator.userAgent")
        await security._wait_for_material()
        params = DouyinHTTP(session, None, None).parameters(
            TaskRequest(operation="search", keyword="咖啡")
        )
        signed = await security.sign("/aweme/v1/web/user/profile/self/", params)
        url, headers = signed.url, signed.headers
    headers = headers | {
        "Referer": (
            site_for(platform).web_origin + "/"
            if is_xhs_platform(platform)
            else "https://www." + DOMAINS[platform] + "/"
        ),
        "User-Agent": await page.evaluate("navigator.userAgent"),
    }
    response = await context.request.get(url, headers=headers, timeout=15_000, max_redirects=0)
    try:
        try:
            payload = await response.json()
        except ValueError:
            payload = (await response.text())[:4096]
        check_response(response.status, payload)
        identity = identity_from_payload(platform, payload, expected_user_id)
    finally:
        await response.dispose()
    await check_page_signals(page, platform)
    return identity


async def verified_storage_state(context, platform):
    state = await context.storage_state()
    domain = DOMAINS[platform]
    cookies = [
        c
        for c in state["cookies"]
        if (host := str(c.get("domain", "")).lstrip(".").lower()) == domain
        or host.endswith("." + domain)
    ]
    values = {c["name"]: c["value"] for c in cookies if c.get("value")}
    required = (
        site_for(platform).local_session_cookies
        if is_xhs_platform(platform)
        else LOGIN_COOKIES[platform]
    )
    if not required.intersection(values):
        raise CollectionError("auth_expired")
    origins = filter_storage_origins(state.get("origins"), platform)
    if platform == "douyin" and (
        not values.get("UIFID")
        or not any(
            item["name"] == "xmst" and item["value"]
            for origin in origins
            for item in origin["localStorage"]
        )
    ):
        raise CollectionError("identity_unverified", "Missing UIFID / xmst")
    return {"cookies": cookies, "origins": origins}
