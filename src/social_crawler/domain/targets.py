"""Local, deterministic post target parsing. Never performs network requests."""

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from social_crawler.adapters.xhs.sites import is_xhs_platform, site_for
from social_crawler.domain.models import CollectionError

HOSTS = {
    "douyin": {"www.douyin.com", "douyin.com", "v.douyin.com", "www.iesdouyin.com"},
    "xhs": {"www.xiaohongshu.com", "xiaohongshu.com", "xhslink.com", "www.xhslink.com"},
    "rednote": {"www.rednote.com", "rednote.com"},
}
SHORT_HOSTS = {"v.douyin.com", "xhslink.com", "www.xhslink.com"}
ID_PATTERNS = {
    "douyin": r"\d{10,25}",
    "xhs": r"[0-9a-fA-F]{24}",
    "rednote": r"[0-9a-fA-F]{24}",
}
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、！？（）【】]+")


def valid_token(value):
    return isinstance(value, str) and bool(value) and "[redacted]" not in value


def resolved_post_target(platform, cid, **context):
    """Validate a target already resolved inside the trusted task pipeline."""
    try:
        valid_id = re.fullmatch(ID_PATTERNS[platform], str(cid))
    except KeyError:
        valid_id = None
    if not valid_id:
        raise CollectionError("invalid_target", "链接中没有有效帖子 ID（用户主页暂不支持）")
    if platform == "rednote" and not valid_token(context.get("xsec_token")):
        raise CollectionError("invalid_target", "RedNote 指定帖子需要有效 xsec_token")
    return post_target(platform, str(cid), **context)


def checked_url(platform, url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.hostname not in HOSTS[platform]
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError
    except (ValueError, KeyError):
        raise CollectionError("invalid_target", "链接域名或平台不匹配") from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def parse_target(platform, value):
    value = value.strip()
    if re.fullmatch(ID_PATTERNS[platform], value):
        if platform == "rednote":
            raise CollectionError(
                "invalid_target", "RedNote 请提供带 xsec_token 的完整帖子链接"
            )
        return resolved_post_target(platform, value)
    urls = URL_RE.findall(value)
    if len(urls) != 1:
        raise CollectionError("invalid_target", "每行请提供一个帖子链接、分享文本或帖子 ID")
    url = checked_url(platform, urls[0].rstrip(".,;!?)】"))
    parsed = urlsplit(url)
    if parsed.hostname in SHORT_HOSTS:
        if not parsed.path.strip("/"):
            raise CollectionError("invalid_target", "分享短链缺少路径")
        return {"url": url, "needs_redirect": True}
    query = parse_qs(parsed.query)
    if platform == "douyin":
        match = re.fullmatch(r"/(?:video|note|share/video|share/note)/(\d{10,25})/?", parsed.path)
        cid = match.group(1) if match else (query.get("modal_id") or [""])[0]
    else:
        match = re.fullmatch(
            r"/(?:explore|discovery/item|search_result)/(\w{24})/?", parsed.path
        )
        cid = match.group(1) if match else ""
    if not re.fullmatch(ID_PATTERNS[platform], cid):
        raise CollectionError("invalid_target", "链接中没有有效帖子 ID（用户主页暂不支持）")
    context = {}
    if is_xhs_platform(platform):
        token = (query.get("xsec_token") or [""])[0]
        if valid_token(token):
            context["xsec_token"] = token
        context["xsec_source"] = (query.get("xsec_source") or ["pc_feed"])[0]
        if platform == "rednote" and "xsec_token" not in context:
            raise CollectionError(
                "invalid_target", "RedNote 指定帖子需要带 xsec_token 的完整链接"
            )
    return resolved_post_target(platform, cid, **context)


def post_target(platform, cid, **context):
    if platform == "douyin":
        path = f"https://www.douyin.com/video/{cid}"
    else:
        path = f"{site_for(platform).web_origin}/explore/{cid}"
    return {"id": cid, "url": path, **context}
