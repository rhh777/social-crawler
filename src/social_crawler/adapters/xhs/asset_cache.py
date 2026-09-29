"""Bounded, per-run reuse of public fingerprinted frontend assets only."""

import re
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError


class PublicAssetCache:
    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_asset_bytes=4 * 1024 * 1024):
        self.entries = OrderedDict()
        self.max_bytes, self.max_asset_bytes = max_bytes, max_asset_bytes
        self.size = self.hits = 0

    @staticmethod
    def eligible(request):
        u = urlsplit(request.url)
        return (
            request.method == "GET" and request.resource_type in {"script", "stylesheet"}
            and u.scheme == "https" and u.netloc == "fe-static.xhscdn.com"
            and not u.query and not u.fragment
            and u.path.startswith("/formula-static/xhs-pc-web/public/resource/")
            and bool(re.search(r"\.[a-f0-9]{8,}\.(?:js|css)$", u.path))
        )

    @staticmethod
    async def anonymous(request):
        headers = await request.all_headers()
        return not any(k in headers for k in ("cookie", "authorization", "range"))

    async def get(self, request):
        if not self.eligible(request) or request.url not in self.entries:
            return None
        if not await self.anonymous(request):
            return None
        expires, headers, body, variant = self.entries[request.url]
        if expires <= time.monotonic():
            self._remove(request.url)
            return None
        request_headers = await request.all_headers()
        if any(request_headers.get(k, "") != v for k, v in variant.items()):
            return None
        self.entries.move_to_end(request.url)
        self.hits += 1
        return {"status": 200, "headers": headers, "body": body}

    async def capture(self, response):
        request = response.request
        if not self.eligible(request) or response.status != 200:
            return
        if request.url in self.entries and self.entries[request.url][0] > time.monotonic():
            return  # A fulfilled cache response must not renew its own TTL.
        try:
            if not await self.anonymous(request):
                return
            headers = await response.all_headers()
            control = headers.get("cache-control", "").lower()
            directives = {part.strip() for part in control.split(",")}
            max_age = re.search(r"(?:^|,)\s*max-age=(\d+)\s*(?:,|$)", control)
            vary = {part.strip().lower() for part in headers.get("vary", "").split(",")}
            mime = headers.get("content-type", "").split(";")[0].strip().lower()
            if (not max_age
                    or any(d.split("=")[0] in {"private", "no-store", "no-cache"} for d in directives)
                    or "set-cookie" in headers or vary - {
                        "", "accept-encoding", "origin", "access-control-request-headers",
                        "access-control-request-method",
                    }
                    or mime not in {"application/javascript", "text/javascript", "text/css"}):
                return
            ttl = min(3600, int(max_age[1]) - int(headers.get("age", "0")))
            if ttl <= 0 or int(headers.get("content-length", "0")) > self.max_asset_bytes:
                return
            body = await response.body()
            if not body or len(body) > min(self.max_asset_bytes, self.max_bytes):
                return
            request_headers = await request.all_headers()
            variant = {k: request_headers.get(k, "") for k in vary - {"", "accept-encoding"}}
            # Playwright returns decoded bytes. Keep policy/CORS headers, but
            # never reuse wire encoding/length or hop-by-hop metadata.
            headers = {k: v for k, v in headers.items() if k not in {
                "content-encoding", "content-length", "transfer-encoding", "connection", "age",
            }}
            self._remove(request.url)
            while self.size + len(body) > self.max_bytes:
                self._remove(next(iter(self.entries)))
            self.entries[request.url] = (time.monotonic() + ttl, headers, body, variant)
            self.size += len(body)
        except (PlaywrightError, ValueError):
            # Optional cache capture must never turn a page read into a failure.
            return

    def _remove(self, url):
        if entry := self.entries.pop(url, None):
            self.size -= len(entry[2])

    def clear(self):
        self.entries.clear()
        self.size = 0
