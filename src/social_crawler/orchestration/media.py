import asyncio
import base64
import hashlib
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from social_crawler.adapters.xhs.sites import is_xhs_platform, site_for

MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
HOST_SUFFIXES = {
    "xhs": ("xhscdn.com",),
    "rednote": ("rednotecdn.com",),
    "douyin": (
        "douyinvod.com",
        "douyinpic.com",
        "douyinstatic.com",
        "douyincdn.com",
        "byteimg.com",
        "byteimg-h.com",
        "amemv.com",
    ),
}
CONTENT_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _httpx_proxy(value: str) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "socks5h":
        return urlunsplit(("socks5", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def media_kind(url: str) -> str:
    value = url.lower()
    path = urlsplit(value).path
    if any(marker in value for marker in ("mime_type=video", "sns-video")) or path.endswith(
        (".mp4", ".webm", ".m3u8")
    ):
        return "video"
    if path.endswith((".mp3", ".m4a", ".aac")) or "music" in value:
        return "audio"
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")) or any(
        marker in value for marker in ("douyinpic", "webpic", "image")
    ):
        return "image"
    return "media"


def selected_media(media: list[str]) -> list[tuple[int, str, str]]:
    """Download every image/audio and only the first encoded video variant."""
    selected = []
    video_selected = False
    for index, url in enumerate(media):
        if not isinstance(url, str) or not url:
            continue
        kind = media_kind(url)
        if kind == "video":
            if video_selected:
                continue
            video_selected = True
        selected.append((index, kind, url))
    return selected


def safe_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return clean[:120] or hashlib.sha256(value.encode()).hexdigest()[:24]


def extension_for(content_type: str, url: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in CONTENT_EXTENSIONS:
        return CONTENT_EXTENSIONS[media_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {
        ".aac",
        ".avif",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mp3",
        ".mp4",
        ".png",
        ".webm",
        ".webp",
    }:
        return suffix
    return mimetypes.guess_extension(media_type) or ".bin"


class MediaDownloader:
    def __init__(
        self,
        artifact_root,
        platform: str,
        session,
        *,
        client=None,
        allowed_hosts: set[str] | None = None,
        max_bytes: int = MAX_MEDIA_BYTES,
        timeout: float = 30,
        browser_context=None,
    ):
        self.artifact_root = Path(artifact_root).resolve()
        self.media_root = self.artifact_root / "media"
        self.platform = platform
        self.session = session
        self.client = client
        self.owns_client = client is None
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.browser_context = browser_context

    def _host_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.lower()
        allowed = self.allowed_hosts or set(HOST_SUFFIXES.get(self.platform, ()))
        return any(hostname == suffix or hostname.endswith("." + suffix) for suffix in allowed)

    def _client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                proxy=_httpx_proxy(self.session.proxy),
                trust_env=False,
                verify=True,
                follow_redirects=False,
                timeout=self.timeout,
                headers={
                    "Accept": "image/avif,image/webp,image/*,video/*,audio/*,*/*;q=0.8",
                    "User-Agent": self.session.effective_user_agent,
                    "Referer": (
                        site_for(self.platform).web_origin + "/"
                        if is_xhs_platform(self.platform)
                        else "https://www.douyin.com/"
                    ),
                },
            )
        return self.client

    async def download(self, content_id: str, media: list[str]) -> list[dict]:
        results = []
        for index, kind, url in selected_media(media):
            try:
                async with asyncio.timeout(self.timeout):
                    result = await self._download_one(content_id, index, kind, url)
            except TimeoutError:
                result = {
                    "media_index": index,
                    "kind": kind,
                    "status": "failed",
                    "error": "download_timeout",
                }
            results.append(result)
        return results

    async def _download_one(self, content_id: str, index: int, kind: str, url: str) -> dict:
        base = {"media_index": index, "kind": kind}
        if not self._host_allowed(url):
            return base | {"status": "failed", "error": "host_not_allowed"}
        directory = self.media_root / self.platform / safe_component(content_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = f"{kind}-{index}"
        existing = next(
            (
                path
                for path in directory.glob(stem + ".*")
                if path.is_file() and path.suffix != ".part"
            ),
            None,
        )
        if existing:
            digest = hashlib.sha256()
            with existing.open("rb") as source:
                for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                    digest.update(chunk)
            return base | {
                "status": "downloaded",
                "path": existing.relative_to(self.artifact_root).as_posix(),
                "bytes": existing.stat().st_size,
                "sha256": digest.hexdigest(),
                "content_type": mimetypes.guess_type(existing.name)[0] or "application/octet-stream",
                "reused": True,
            }
        temporary = directory / (stem + ".part")
        temporary.unlink(missing_ok=True)
        try:
            if self.browser_context is not None:
                content_type, size, digest = await self._stream_browser_to(temporary, url)
            else:
                content_type, size, digest = await self._stream_http_to(temporary, url)
            if not size:
                raise ValueError("empty_file")
            final = directory / (stem + extension_for(content_type, url))
            os.replace(temporary, final)
            return base | {
                "status": "downloaded",
                "path": final.relative_to(self.artifact_root).as_posix(),
                "bytes": size,
                "sha256": digest,
                "content_type": content_type.split(";", 1)[0].strip().lower(),
                "reused": False,
            }
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return base | {"status": "failed", "error": error[:80]}
        finally:
            temporary.unlink(missing_ok=True)

    async def _stream_http_to(self, temporary: Path, url: str) -> tuple[str, int, str]:
        digest = hashlib.sha256()
        size = 0
        async with self._client().stream("GET", url) as response:
            if response.status_code not in {200, 206}:
                raise ValueError(f"http_{response.status_code}")
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > self.max_bytes:
                raise ValueError("file_too_large")
            with temporary.open("wb") as output:
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("file_too_large")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        return content_type, size, digest.hexdigest()

    async def _stream_browser_to(self, temporary: Path, url: str) -> tuple[str, int, str]:
        """Stream through Chrome's network service, inheriting its managed profile proxy."""
        context = self.browser_context()
        if context is None or not context.pages:
            raise ValueError("browser_context_unavailable")
        session = await context.new_cdp_session(context.pages[0])
        handle = None
        try:
            frame_tree = await session.send("Page.getFrameTree")
            frame_id = frame_tree["frameTree"]["frame"]["id"]
            loaded = await session.send(
                "Network.loadNetworkResource",
                {
                    "frameId": frame_id,
                    "url": url,
                    "options": {"disableCache": True, "includeCredentials": True},
                },
            )
            resource = loaded.get("resource") or {}
            if resource.get("success") is not True:
                raise ValueError("browser_fetch_failed")
            status = int(resource.get("httpStatusCode") or 0)
            if status not in {200, 206}:
                raise ValueError(f"http_{status}")
            headers = {
                str(key).lower(): str(value)
                for key, value in (resource.get("headers") or {}).items()
            }
            content_type = headers.get("content-type", "application/octet-stream")
            declared = int(headers.get("content-length", "0") or 0)
            if declared > self.max_bytes:
                raise ValueError("file_too_large")
            handle = resource.get("stream")
            if not isinstance(handle, str) or not handle:
                raise ValueError("browser_stream_unavailable")

            digest = hashlib.sha256()
            size = 0
            with temporary.open("wb") as output:
                while True:
                    part = await session.send("IO.read", {"handle": handle, "size": CHUNK_BYTES})
                    raw = part.get("data", "")
                    chunk = (
                        base64.b64decode(raw, validate=True)
                        if part.get("base64Encoded")
                        else raw.encode()
                    )
                    if not chunk and not part.get("eof"):
                        raise ValueError("browser_stream_stalled")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("file_too_large")
                    output.write(chunk)
                    digest.update(chunk)
                    if part.get("eof"):
                        break
                output.flush()
                os.fsync(output.fileno())
            return content_type, size, digest.hexdigest()
        finally:
            if handle:
                try:
                    await session.send("IO.close", {"handle": handle})
                except Exception:
                    pass
            await session.detach()

    async def close(self):
        if self.client is not None and self.owns_client:
            await self.client.aclose()
