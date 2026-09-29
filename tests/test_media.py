import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest

from social_crawler.domain.models import RunConfig
from social_crawler.orchestration.media import MediaDownloader, selected_media


def session():
    return SimpleNamespace(
        proxy="",
        effective_user_agent="test-agent",
    )


def test_media_download_is_disabled_by_default_and_variants_are_bounded():
    assert RunConfig(platform="xhs", keywords=["test"]).download_media is False
    media = [
        "https://media.example/cover.webp",
        "https://media.example/first.mp4",
        "https://media.example/second.mp4",
    ]
    assert selected_media(media) == [
        (0, "image", media[0]),
        (1, "video", media[1]),
    ]


def test_rednote_media_allowlist_is_separate_from_xhs():
    downloader = MediaDownloader("/tmp", "rednote", session())
    assert downloader._host_allowed("https://sns-img-a.rednotecdn.com/image.webp")
    assert not downloader._host_allowed("https://sns-img-qc.xhscdn.com/image.webp")


@pytest.mark.asyncio
async def test_downloads_media_atomically_and_reuses_local_file(tmp_path):
    payloads = {
        "/cover.webp": ("image/webp", b"RIFF-test-image"),
        "/first.mp4": ("video/mp4", b"\x00\x00\x00\x18ftyp-test-video"),
    }

    async def handle(request):
        content_type, body = payloads[request.url.path]
        return httpx.Response(
            200,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            content=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        downloader = MediaDownloader(
            tmp_path,
            "xhs",
            session(),
            client=client,
            allowed_hosts={"media.example"},
        )
        media = [
            "https://media.example/cover.webp",
            "https://media.example/first.mp4",
            "https://media.example/second.mp4",
        ]
        first = await downloader.download("note/unsafe", media)
        second = await downloader.download("note/unsafe", media)

    assert [row["status"] for row in first] == ["downloaded", "downloaded"]
    assert [row["kind"] for row in first] == ["image", "video"]
    assert all(not row["reused"] for row in first)
    assert all(row["reused"] for row in second)
    assert all((tmp_path / row["path"]).is_file() for row in first)
    assert not list(tmp_path.rglob("*.part"))
    assert (tmp_path / first[1]["path"]).read_bytes()[4:8] == b"ftyp"


@pytest.mark.asyncio
async def test_rejects_untrusted_hosts_and_oversized_files(tmp_path):
    async def handle(_request):
        return httpx.Response(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": "100"},
            content=b"x" * 100,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        downloader = MediaDownloader(
            tmp_path,
            "xhs",
            session(),
            client=client,
            allowed_hosts={"media.example"},
            max_bytes=10,
        )
        untrusted = await downloader.download("one", ["https://evil.example/video.mp4"])
        oversized = await downloader.download("two", ["https://media.example/video.mp4"])

    assert untrusted[0]["error"] == "host_not_allowed"
    assert oversized[0]["error"] == "file_too_large"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_total_download_timeout_removes_partial_file(tmp_path):
    async def handle(_request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=b"video")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        downloader = MediaDownloader(
            tmp_path,
            "xhs",
            session(),
            client=client,
            allowed_hosts={"media.example"},
            timeout=0.01,
        )
        result = await downloader.download("note", ["https://media.example/video.mp4"])

    assert result[0]["error"] == "download_timeout"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_adspower_media_streams_through_browser_network(tmp_path):
    payload = b"RIFF-browser-proxied-image"

    class CDP:
        def __init__(self):
            self.read = False
            self.commands = []
            self.detached = False

        async def send(self, method, params=None):
            self.commands.append((method, params))
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "frame-a"}}}
            if method == "Network.loadNetworkResource":
                return {
                    "resource": {
                        "success": True,
                        "httpStatusCode": 200,
                        "headers": {
                            "Content-Type": "image/webp",
                            "Content-Length": str(len(payload)),
                        },
                        "stream": "stream-a",
                    }
                }
            if method == "IO.read":
                assert not self.read
                self.read = True
                return {
                    "data": base64.b64encode(payload).decode(),
                    "base64Encoded": True,
                    "eof": True,
                }
            if method == "IO.close":
                return {}
            raise AssertionError(method)

        async def detach(self):
            self.detached = True

    cdp = CDP()

    class Context:
        pages = [object()]

        async def new_cdp_session(self, _page):
            return cdp

    managed_session = SimpleNamespace(
        proxy="",
        effective_user_agent="test-agent",
        config=SimpleNamespace(browser_provider="adspower"),
    )
    downloader = MediaDownloader(
        tmp_path,
        "xhs",
        managed_session,
        allowed_hosts={"media.example"},
        browser_context=lambda: Context(),
    )

    result = await downloader.download("note", ["https://media.example/cover.webp"])

    assert result[0]["status"] == "downloaded"
    assert (tmp_path / result[0]["path"]).read_bytes() == payload
    load = next(params for method, params in cdp.commands if method == "Network.loadNetworkResource")
    assert load["options"] == {"disableCache": True, "includeCredentials": True}
    assert cdp.detached is True
    assert downloader.client is None
