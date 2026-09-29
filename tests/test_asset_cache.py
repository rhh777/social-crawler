import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from social_crawler.adapters.xhs.asset_cache import PublicAssetCache

URL = 'https://fe-static.xhscdn.com/formula-static/xhs-pc-web/public/resource/js/vendor.1234abcd.js'


def response(*, url=URL, request_headers=None, headers=None, body=b'abc', status=200):
    request = SimpleNamespace(url=url, method='GET', resource_type='script',
                              all_headers=AsyncMock(return_value=request_headers or {}))
    return SimpleNamespace(
        request=request, status=status, body=AsyncMock(return_value=body),
        all_headers=AsyncMock(return_value={
            'cache-control': 'public, max-age=600', 'content-type': 'application/javascript',
            'content-encoding': 'gzip', 'content-length': '2', **(headers or {}),
        }),
    )


async def test_public_assets_reuse_decoded_body_without_renewing_ttl():
    cache = PublicAssetCache()
    r = response()
    await cache.capture(r)
    expiry = cache.entries[URL][0]
    hit = await cache.get(r.request)
    assert hit['body'] == b'abc'
    assert 'content-encoding' not in hit['headers']
    assert 'content-length' not in hit['headers']
    await cache.capture(r)
    assert cache.entries[URL][0] == expiry
    assert cache.hits == 1
    cache.entries[URL] = (time.monotonic() - 1, hit['headers'], hit['body'], {})
    assert await cache.get(r.request) is None
    assert cache.size == 0


@pytest.mark.parametrize('changes', [
    {'request_headers': {'cookie': 'private'}},
    {'request_headers': {'authorization': 'private'}},
    {'request_headers': {'range': 'bytes=0-1'}},
    {'headers': {'set-cookie': 'private'}},
    {'headers': {'cache-control': 'private, max-age=600'}},
    {'headers': {'cache-control': ''}},
    {'headers': {'cache-control': 'public, no-store, max-age=600'}},
    {'headers': {'cache-control': 'public, no-cache, max-age=600'}},
    {'headers': {'age': '600'}},
    {'headers': {'vary': 'Cookie'}},
    {'headers': {'content-type': 'text/html'}},
    {'url': URL + '?token=private'},
    {'url': URL.replace('vendor.1234abcd', 'vendor')},
    {'url': 'https://www.xiaohongshu.com/api/sns/web/v1/feed'},
    {'url': URL.replace('fe-static.xhscdn.com', 'evil.xhscdn.com')},
    {'status': 403},
])
async def test_private_dynamic_and_non_public_responses_are_never_cached(changes):
    cache = PublicAssetCache()
    r = response(**changes)
    await cache.capture(r)
    assert await cache.get(r.request) is None
    assert not cache.entries


async def test_cache_memory_cap_and_credentialed_request_miss():
    cache = PublicAssetCache(max_bytes=5, max_asset_bytes=4)
    first = response()
    second = response(url=URL.replace('1234abcd', 'deadbeef'))
    await cache.capture(first)
    await cache.capture(second)
    assert cache.size == 3
    assert await cache.get(first.request) is None
    second.request.all_headers.return_value = {'cookie': 'private'}
    assert await cache.get(second.request) is None
    await cache.capture(response(body=b'too large'))
    assert cache.size == 3
    cache.clear()
    assert not cache.entries and cache.size == 0


async def test_anonymous_max_age_asset_respects_cors_vary():
    cache = PublicAssetCache()
    r = response(headers={'cache-control': 'max-age=600', 'vary': 'Origin, Accept-Encoding'},
                 request_headers={'origin': 'https://www.xiaohongshu.com'})
    await cache.capture(r)
    assert await cache.get(r.request)
    r.request.all_headers.return_value = {'origin': 'https://different.example'}
    assert await cache.get(r.request) is None
