"""Real Chromium, synthetic pages: every URL is fulfilled locally or aborted."""

import asyncio
import json
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.browser import XHSBrowser
from social_crawler.adapters.xhs.parsing import operation_for_path
from social_crawler.domain.models import CollectionError, RunConfig, TaskRequest
from social_crawler.environments.session import EnvironmentConfig, Session

pytestmark = pytest.mark.browser

HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="height:5000px">
<button onclick="loadSearch(1, 'time_descending')">最新</button>
<div class="note-scroller" style="height:200px;overflow:auto">
<div style="height:2000px"><div id="comment-r1">
<button class="show-more" onclick="loadReplies()">展开更多回复</button>
</div></div></div>
<script>
const api = '/api/sns/web/v1/';
const post = (path, data) => fetch(api+path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
let commentCursor = '', replyCursor = '', searchPage = 1;
function loadSearch(page, sort='general') {return post('search/notes',{keyword:'猫',page,sort});}
function loadComments() {fetch(api+'comment/page?note_id=n1&cursor='+commentCursor);commentCursor='c1';}
function loadReplies() {fetch(api+'comment/sub/page?note_id=n1&root_comment_id=r1&cursor='+replyCursor);replyCursor='c1';}
if (location.pathname.startsWith('/search_result')) {
  post('search/notes',{keyword:'wrong',page:1,sort:'general'});
  loadSearch(1);
  window.addEventListener('scroll',()=>{if(searchPage===1){searchPage=2;loadSearch(2);}});
} else {
  post('feed',{source_note_id:'n1'});
  loadComments();
  document.querySelector('.note-scroller').addEventListener('scroll',()=>{if(commentCursor==='c1'){loadComments();commentCursor='end';}});
}
</script></body></html>"""

STARTUP_API_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<script>
fetch('/api/sns/web/v2/user/me');
fetch('/api/sns/web/v1/system/config');
fetch('/api/sns/web/v2/search/notes', {
  method:'POST', headers:{'Content-Type':'application/json'},
  body:JSON.stringify({keyword:'猫',page:1,sort:'general'})
});
</script></body></html>"""

CURRENT_REPLY_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="note-scroller" style="height:200px;overflow:auto">
  <div style="height:2000px">
    <div class="parent-comment">
      <div class="comment-item"><span>目标根评论</span></div>
      <div class="reply-container" id="reply-slot"></div>
    </div>
  </div>
</div>
<script>
const api = '/api/sns/web/v1/';
function loadComments() {fetch(api+'comment/page?note_id=n1&cursor=');}
function loadReplies() {fetch(api+'comment/sub/page?note_id=n1&root_comment_id=r1&cursor=');}
loadComments();
setTimeout(() => {
  document.querySelector('#reply-slot').innerHTML =
    '<button class="reply-more" onclick="loadReplies()">展开 4 条回复</button>';
}, 1400);
</script></body></html>"""

DELAYED_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<button style="display:none">最新</button>
<button id="sort-menu">综合排序</button>
<div id="options"></div>
<script>
const api = '/api/sns/web/v1/';
const post = (data) => fetch(api+'search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
post({keyword:'猫',page:1,sort:'general'});
document.querySelector('#sort-menu').addEventListener('mouseenter', () => setTimeout(() => {
  document.querySelector('#options').innerHTML = '<button data-sort="time_descending" onclick="post({keyword:\\'猫\\',page:1,sort:\\'time_descending\\'})">最新发布</button>';
}, 500), {once:true});
</script></body></html>"""

CURRENT_FILTER_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="height:5000px">
<button class="filter ai-chat-filter">筛选</button>
<div id="options"></div>
<script>
const api = '/api/sns/web/v2/';
const post = (data) => fetch(api+'search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
let searchPage = 1;
const latest = (page) => post({keyword:'猫',page,page_size:20,sort:'general',filters:[{type:'sort_type',tags:['time_descending']}]});
post({keyword:'猫',page:1,page_size:20,sort:'general'});
document.querySelector('.filter').addEventListener('click', () => {
  document.querySelector('#options').innerHTML = '<button onclick="latest(1)">最新</button>';
}, {once:true});
window.addEventListener('scroll', () => {if (searchPage === 1) {searchPage = 2; latest(2);}});
</script></body></html>"""


# The home page search box; production submits it as an in-page route change.
EXPLORE_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<textarea id="search-input-in-feeds" placeholder="搜索小红书"></textarea>
<script>
fetch('/api/sns/web/v2/user/me');
document.querySelector('#search-input-in-feeds').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    location.href = '/search_result_ai?keyword=' + encodeURIComponent(e.target.value) + '&source=web_explore_feed';
  }
});
</script></body></html>"""


class LocalBudget:
    def __init__(self):
        self.config = RunConfig(platform="xhs", keywords=["猫"], min_interval=0, request_timeout=5)
        self.deadline = time.monotonic() + 30
        self.halted = None
        self.calls = []

    def check(self):
        if self.halted:
            raise self.halted

    async def admit(self, operation, *, paced=True):
        self.check()
        self.calls.append(operation)

    def halt(self, error):
        self.halted = self.halted or error


def response_for(request):
    u = urlsplit(request.url)
    query = parse_qs(u.query, keep_blank_values=True)
    if u.path.endswith("/user/me"):
        return {"guest": False, "user_id": "logged-in", "nickname": "测试昵称"}
    if u.path.endswith("/search/notes"):
        body = request.post_data_json
        page = body["page"]
        cid = "wrong" if body["keyword"] == "wrong" else f"n{page}"
        return {
            "items": [{"id": cid, "note_card": {"title": cid}, "xsec_token": "synthetic-token"}],
            "has_more": page == 1,
        }
    if u.path.endswith("/feed"):
        return {
            "items": [
                {
                    "id": "n1",
                    "note_card": {
                        "desc": "full text",
                        "user": {"user_id": "author"},
                        "time": 1700000000000,
                    },
                }
            ]
        }
    first = query.get("cursor", [""])[0] == ""
    replies = "/sub/" in u.path
    return {
        "comments": [
            {"id": ("reply" if replies else "r") + ("1" if first else "2"), "content": "synthetic"}
        ],
        "has_more": first,
        "cursor": "c1" if first else "end",
    }


async def create_adapter(
    browser, tmp_path, *, risk=None, page_html=HTML, intercept=None, platform="xhs",
):
    observed = []

    async def factory():
        context = await browser.new_context(service_workers="block")

        async def fulfill(route):
            request = route.request
            observed.append(request.url)
            if intercept and await intercept(route):
                return
            if request.resource_type == "document":
                body = EXPLORE_HTML if urlsplit(request.url).path == "/explore" else page_html
                await route.fulfill(status=200, content_type="text/html", body=body)
            elif "/api/" in request.url:
                if risk:
                    await route.fulfill(
                        status=risk,
                        json={"message": "synthetic risk"},
                        headers={"Retry-After": "30"},
                    )
                else:
                    await route.fulfill(
                        status=200, json={"success": True, "code": 0, "data": response_for(request)}
                    )
            else:
                await route.abort()  # Never allow a real transport fallback.

        await context.route("**/*", fulfill)
        return context

    session = Session(EnvironmentConfig(), platform, offline=True)
    budget = LocalBudget()
    return XHSBrowser(session, budget, Samples(tmp_path), context_factory=factory), budget, observed


async def test_browser_actions_capture_correlated_pages_and_reply_scope(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(browser, tmp_path)
        try:
            request = TaskRequest(operation="search", keyword="猫")
            first = await adapter.fetch(request)
            assert [i.id for i in first.items] == ["n1"]  # unrelated request ignored
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert [i.id for i in second.items] == ["n2"]
            detail = await adapter.fetch(
                TaskRequest(
                    operation="detail", content_id="n1", input={"xsec_token": "synthetic-token"}
                )
            )
            assert detail.items[0].data["text"] == "full text"
            request = TaskRequest(operation="comments", content_id="n1")
            first = await adapter.fetch(request)
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert first.items[0].id == "r1" and second.items[0].id == "r2"
            request = TaskRequest(operation="replies", content_id="n1", root_id="r1")
            first = await adapter.fetch(request)
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert first.items[0].root_id == second.items[0].root_id == "r1"
            assert first.response_has_more is True and second.response_has_more is False
            expected = [
                str(operation)
                for url in observed
                if (operation := operation_for_path(urlsplit(url).path))
            ]
            assert Counter(budget.calls) == Counter(expected)
            assert {"search", "detail", "comments", "replies"} <= set(budget.calls)
            assert "page_session" not in budget.calls
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_browser_replies_support_current_parent_structure_and_delayed_control(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(
            browser, tmp_path, page_html=CURRENT_REPLY_HTML
        )
        try:
            result = await adapter.fetch(
                TaskRequest(
                    operation="replies",
                    content_id="n1",
                    root_id="r1",
                    input={"root_comment": {"id": "r1", "text": "目标根评论"}},
                )
            )
            assert result.items[0].id == "reply1"
            assert any("/comment/sub/page" in url for url in observed)
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_browser_latest_sort_and_write_endpoint_block(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(browser, tmp_path)
        try:
            result = await adapter.fetch(
                TaskRequest(operation="search", keyword="猫", sort="latest")
            )
            assert result.items[0].id == "n1"
            denied = await adapter.page.evaluate(
                "fetch('/api/sns/web/v1/comment/post',{method:'POST'}).then(()=>false).catch(()=>true)"
            )
            assert denied is True
            assert not any("/comment/post" in url for url in observed)
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_browser_latest_sort_waits_for_delayed_visible_menu_variant(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, _observed = await create_adapter(
            browser, tmp_path, page_html=DELAYED_SORT_HTML
        )
        try:
            result = await adapter.fetch(
                TaskRequest(operation="search", keyword="猫", sort="latest")
            )
            assert result.items[0].id == "n1"
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_browser_latest_sort_from_home_search_paginates(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(
            browser, tmp_path, page_html=CURRENT_FILTER_SORT_HTML
        )
        try:
            request = TaskRequest(operation="search", keyword="猫", sort="latest")
            first = await adapter.fetch(request)
            second = await adapter.fetch(
                request.model_copy(update={"context": first.next_context})
            )
            assert first.items[0].id == "n1"
            assert second.items[0].id == "n2"
            page_url = urlsplit(adapter.page.url)
            # Reached through the home search box, not a loaded search URL.
            assert page_url.path == "/search_result_ai"
            assert parse_qs(page_url.query) == {
                "keyword": ["猫"],
                "source": ["web_explore_feed"],
            }
            assert any("/api/sns/web/v2/search/notes" in url for url in observed)
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_browser_startup_apis_do_not_consume_collection_budget(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, _observed = await create_adapter(
            browser, tmp_path, page_html=STARTUP_API_HTML
        )
        try:
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫"))
            assert [item.id for item in result.items] == ["n1"]
            assert budget.calls == ["search"]
            assert adapter.identity_profile == {
                "user_id": "logged-in",
                "nickname": "测试昵称",
            }
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


@pytest.mark.parametrize("risk,kind", [(429, "rate_limit"), (461, "verification_required")])
async def test_browser_risk_stops_further_navigation(tmp_path, risk, kind):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(browser, tmp_path, risk=risk)
        try:
            with pytest.raises(CollectionError) as found:
                await adapter.fetch(TaskRequest(operation="search", keyword="猫"))
            assert found.value.kind == kind
            before = len(observed)
            with pytest.raises(CollectionError):
                await adapter.fetch(TaskRequest(operation="detail", content_id="n1"))
            assert len(observed) == before
        finally:
            await adapter.close()
            await browser.close()


async def test_direct_note_browser_recovers_token_from_captured_request(tmp_path):
    html = HTML.replace("post('feed',{source_note_id:'n1'});", "post('feed',{source_note_id:'n1',xsec_token:'page-token',xsec_source:'pc_feed'});")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, observed = await create_adapter(browser, tmp_path, page_html=html)
        try:
            request = TaskRequest(operation="detail", content_id="n1", input={"targeted_post": "1"})
            result = await adapter.fetch(request)
            assert result.items[0].data["xsec_token"] == "page-token"
            assert result.items[0].data["xsec_source"] == "pc_feed"
            assert not any('search/notes' in url for url in observed)
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


@pytest.mark.parametrize("platform", ["xhs", "rednote"])
async def test_detail_reads_verified_ssr_before_slow_deferred_script(tmp_path, platform):
    release_script = asyncio.Event()
    html = '''<html><head><meta charset="utf-8"></head><body><p>正文</p><script>
    window.__INITIAL_STATE__ = {note: {noteDetailMap: {_rawValue: {n1: {_value: {
      noteCard: {noteId:'n1', desc:'完整正文', user:{userId:'author'}, time:1700000000000}
    }}}}}};
    fetch('/api/sns/web/v2/user/me');
    </script><script defer src="/slow.js"></script></body></html>'''

    async def intercept(route):
        if route.request.url.endswith('/slow.js'):
            await release_script.wait()
            await route.fulfill(body='window.slowFinished = true;', content_type='text/javascript')
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, platform=platform, page_html=html, intercept=intercept,
        )
        adapter.session.config.expected_user_id = 'logged-in'
        budget.config.request_timeout = 2
        try:
            async with asyncio.timeout(3):
                result = await adapter.fetch(TaskRequest(operation='detail', content_id='n1'))
            assert result.items[0].data['detail_complete']
            assert result.items[0].data['text'] == '完整正文'
            assert adapter.identity_verified
            assert not await adapter.page.evaluate('Boolean(window.slowFinished)')
        finally:
            release_script.set()
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_slow_first_search_does_not_scroll_ahead(tmp_path):
    html = HTML.replace("  post('search/notes',{keyword:'wrong',page:1,sort:'general'});", '')
    requests = []

    async def intercept(route):
        if route.request.url.endswith('/search/notes'):
            requests.append(route.request.post_data_json)
            await asyncio.sleep(1.4)
            await route.fulfill(json={'success': True, 'code': 0, 'data': response_for(route.request)})
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html, intercept=intercept)
        try:
            result = await adapter.fetch(TaskRequest(operation='search', keyword='猫'))
            assert result.items[0].id == 'n1'
            assert [r['page'] for r in requests] == [1]
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_response_body_failure_retries_with_new_navigation(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, observed = await create_adapter(browser, tmp_path)
        capture = adapter._capture
        failures = 0

        async def fail_once(response, generation=None):
            nonlocal failures
            if response.url.endswith('/feed') and failures == 0:
                failures += 1
                response = SimpleNamespace(
                    url=response.url, request=response.request, status=response.status,
                    json=AsyncMock(side_effect=PlaywrightError('body canceled')),
                    text=AsyncMock(side_effect=PlaywrightError('body canceled')),
                )
            await capture(response, generation)

        adapter._capture = fail_once
        request = TaskRequest(operation='detail', content_id='n1')
        try:
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(request)
            assert failed.value.kind == 'network_failure'
            assert failed.value.sample_ref
            assert budget.halted is None
            result = await adapter.fetch(request)
            assert result.items[0].data['detail_complete']
            assert sum('/explore/n1' in u for u in observed) == 2
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_old_document_body_failure_is_ignored_but_risk_is_not(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path)
        request = Mock(
            url='https://edith.xiaohongshu.com/api/sns/web/v1/feed',
            method='POST', post_data='{"source_note_id":"n1"}', resource_type='fetch',
        )
        try:
            await adapter._capture(SimpleNamespace(
                url=request.url, request=request, status=200,
                json=AsyncMock(side_effect=PlaywrightError('old body canceled')),
                text=AsyncMock(side_effect=PlaywrightError('old body canceled')),
            ), generation=-1)
            assert budget.halted is None
            assert not adapter.capture_errors
            await adapter._capture(SimpleNamespace(
                url=request.url, request=request, status=429,
                json=AsyncMock(return_value={}), all_headers=AsyncMock(return_value={}),
            ), generation=-1)
            assert budget.halted.kind == 'rate_limit'
        finally:
            await adapter.close()
            await browser.close()


@pytest.mark.parametrize('failure', ['wrong_note', 'wrong_identity'])
async def test_ssr_never_accepts_wrong_target_or_identity_and_saves_diagnostic(tmp_path, failure):
    nid = 'other' if failure == 'wrong_note' else 'n1'
    html = f'''<html><body><script>
    window.__INITIAL_STATE__ = {{note:{{noteDetailMap:{{n1:{{note:{{noteId:'{nid}',
      desc:'body',user:{{userId:'author'}},time:1700000000000}}}}}}}}}};
    fetch('/api/sns/web/v2/user/me');</script></body></html>'''
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html)
        adapter.session.config.expected_user_id = 'unexpected' if failure == 'wrong_identity' else 'logged-in'
        budget.config.request_timeout = 1
        try:
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(TaskRequest(
                    operation='detail', content_id='n1', input={'xsec_token': 'private-token'},
                ))
            assert failed.value.kind == ('abnormal_empty' if failure == 'wrong_note' else 'identity_mismatch')
            if failure == 'wrong_note':
                diagnostic = json.loads((tmp_path / failed.value.sample_ref).read_text())
                assert diagnostic['request']['source'] == 'page_failure_diagnostic'
                assert diagnostic['response']['page_state']['note_ids'] == ['n1']
                assert 'private-token' not in json.dumps(diagnostic)
                assert list((tmp_path / 'samples').glob('*.png'))
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_ssr_shell_waits_for_complete_target_data(tmp_path):
    html = '''<html><body><script>
    window.__INITIAL_STATE__ = {note:{noteDetailMap:{n1:{note:{noteId:'n1'}}}}};
    setTimeout(() => Object.assign(window.__INITIAL_STATE__.note.noteDetailMap.n1.note,
      {desc:'hydrated body',user:{userId:'author'},time:1700000000000}), 600);
    </script></body></html>'''
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html)
        try:
            result = await adapter.fetch(TaskRequest(operation='detail', content_id='n1'))
            assert result.items[0].data['detail_complete']
            assert result.items[0].data['text'] == 'hydrated body'
            assert result.stop_reason is None
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_navigation_during_pacing_does_not_send_old_request(tmp_path):
    session = Session(EnvironmentConfig(), 'xhs', offline=True)
    budget = LocalBudget()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def admit(_operation):
        entered.set()
        await release.wait()

    budget.admit = admit
    adapter = XHSBrowser(session, budget, Samples(tmp_path))
    request = Mock(url='https://www.xiaohongshu.com/api/sns/web/v1/feed', method='POST',
                   post_data='{}', failure=None)
    route = SimpleNamespace(request=request, fallback=AsyncMock(), abort=AsyncMock())
    pending = asyncio.create_task(adapter._route(route))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        adapter.generation += 1
        release.set()
        await pending
        route.fallback.assert_not_awaited()
        route.abort.assert_awaited_once()
        assert not adapter.inflight
        assert not adapter.pending_business
        assert budget.halted is None
    finally:
        release.set()
        await pending


@pytest.mark.parametrize('replacement', [True, False])
async def test_canceled_search_waits_for_replacement_or_bounded_retry(tmp_path, replacement):
    html = '''<html><head><meta charset="utf-8"></head><body>
    <button onclick="latest()">最新</button><script>
    function search(sort) {return fetch('/api/sns/web/v1/search/notes', {
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({keyword:'猫',page:1,sort})});}
    search('general');
    function latest() {
      search('time_descending').catch(() => {});
      REPLACEMENT
    }
    </script></body></html>'''.replace(
        'REPLACEMENT', "setTimeout(() => search('time_descending'), 200);" if replacement else '',
    )
    latest_requests = 0

    async def intercept(route):
        nonlocal latest_requests
        if (route.request.url.endswith('/search/notes')
                and route.request.post_data_json['sort'] == 'time_descending'):
            latest_requests += 1
            if latest_requests == 1:
                await route.abort('aborted')
                return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, observed = await create_adapter(
            browser, tmp_path, page_html=html, intercept=intercept,
        )
        budget.config.request_timeout = 1
        try:
            request = TaskRequest(operation='search', keyword='猫', sort='latest')
            started = time.monotonic()
            if replacement:
                result = await adapter.fetch(request)
                assert result.items[0].id == 'n1'
                assert latest_requests == 2
            else:
                with pytest.raises(CollectionError) as failed:
                    await adapter.fetch(request)
                assert failed.value.kind == 'network_failure'
                assert time.monotonic() - started >= 1
                assert adapter.page_key is None
            assert sum(urlsplit(url).path.startswith('/search_result') for url in observed) == 1
            assert budget.halted is None
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_latest_sort_waits_for_initial_response_before_click(tmp_path):
    html = HTML.replace("  post('search/notes',{keyword:'wrong',page:1,sort:'general'});", '')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html)
        capture = adapter._capture
        initial_ready = False
        clicked_early = []

        async def delay_capture(response, generation=None):
            nonlocal initial_ready
            if response.url.endswith('/search/notes'):
                if response.request.post_data_json['sort'] == 'general':
                    await asyncio.sleep(0.6)
                    await capture(response, generation)
                    initial_ready = True
                    return
                clicked_early.append(not initial_ready)
            await capture(response, generation)

        adapter._capture = delay_capture
        try:
            result = await adapter.fetch(TaskRequest(operation='search', keyword='猫', sort='latest'))
            assert result.items[0].id == 'n1'
            assert clicked_early == [False]
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_pacing_selects_target_before_queued_speculative_pages(tmp_path):
    html = '''<html><head><meta charset="utf-8"></head><body>
    <button onclick="latest()">最新</button><script>
    function search(page, sort='general') {return fetch('/api/sns/web/v1/search/notes', {
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({keyword:'猫',page,sort})});}
    search(1).then(() => search(2));
    function latest() {
      search(2, 'time_descending');
      setTimeout(() => search(1, 'time_descending'), 100);
    }
    </script></body></html>'''
    sent = []

    async def intercept(route):
        if route.request.url.endswith('/search/notes'):
            sent.append((route.request.post_data_json, time.monotonic()))
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html, intercept=intercept)
        budget.config.min_interval = 0.6
        budget.last_request = 0
        original_admit = budget.admit

        async def paced_admit(operation, *, paced=True):
            if paced:
                assert time.monotonic() >= budget.last_request + budget.config.min_interval
            await original_admit(operation)
            budget.last_request = time.monotonic()

        budget.admit = paced_admit
        try:
            result = await adapter.fetch(TaskRequest(operation='search', keyword='猫', sort='latest'))
            assert result.items[0].id == 'n1'
            order = [(r['page'], r['sort']) for r, _ in sent]
            # The search box submission admits the page's own general reads;
            # the latest filter's target page still precedes its speculative page 2.
            assert order[:2] == [(1, 'general'), (2, 'general')]
            assert order[2] == (1, 'time_descending')
            assert sent[2][1] - sent[1][1] >= 0.55
            assert any(e['outcome'] == 'queued' and e['page'] == 2 for e in adapter.business_events)
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_comments_get_response_window_after_slow_application_startup(tmp_path):
    html = '''<html><body><script>
    window.__INITIAL_STATE__ = {note:{noteDetailMap:{n1:{note:{noteId:'n1',
      desc:'body',user:{userId:'author'},time:1700000000000}}}}};
    </script><script defer src="/boot.js"></script></body></html>'''

    async def intercept(route):
        if route.request.url.endswith('/boot.js'):
            await asyncio.sleep(0.65)
            await route.fulfill(content_type='text/javascript', body='''setTimeout(() =>
              fetch('/api/sns/web/v1/comment/page?note_id=n1&cursor='), 700);''')
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, observed = await create_adapter(browser, tmp_path, page_html=html, intercept=intercept)
        budget.config.request_timeout = 1
        try:
            detail = await adapter.fetch(TaskRequest(operation='detail', content_id='n1'))
            assert detail.items[0].data['detail_complete']
            assert not adapter.document_ready
            result = await adapter.fetch(TaskRequest(operation='comments', content_id='n1'))
            assert result.items[0].id == 'r1'
            assert sum('/explore/n1' in url for url in observed) == 1
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


@pytest.mark.parametrize('stop', ['deadline', 'verification_required'])
async def test_comment_startup_preserves_deadline_and_risk_stop(tmp_path, stop):
    release = asyncio.Event()
    html = '<html><body>' + ('<div class="captcha-container">captcha</div>'
                            if stop == 'verification_required' else '')
    html += '<script defer src="/boot.js"></script></body></html>'

    async def intercept(route):
        if route.request.url.endswith('/boot.js'):
            await release.wait()
            await route.fulfill(body='', content_type='text/javascript')
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html, intercept=intercept)
        budget.config.request_timeout = 2
        original_check = budget.check

        def check():
            original_check()
            if time.monotonic() >= budget.deadline:
                raise CollectionError('deadline')

        budget.check = check
        budget.deadline = time.monotonic() + 0.5
        try:
            started = time.monotonic()
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(TaskRequest(operation='comments', content_id='n1'))
            assert failed.value.kind == stop
            assert time.monotonic() - started < 1.5
        finally:
            release.set()
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


async def test_public_script_is_reused_across_note_navigation(tmp_path):
    url = 'https://fe-static.xhscdn.com/formula-static/xhs-pc-web/public/resource/js/boot.1234abcd.js'
    html = f'<html><body><script src="{url}"></script></body></html>'
    downloads = 0

    async def intercept(route):
        nonlocal downloads
        if route.request.url == url:
            downloads += 1
            await route.fulfill(content_type='application/javascript',
                                headers={'cache-control': 'public,max-age=600'}, body='''
                const id = location.pathname.split('/').pop();
                window.__INITIAL_STATE__ = {note:{noteDetailMap:{[id]:{note:{noteId:id,
                  desc:'body', user:{userId:'author'},time:1700000000000}}}}};''')
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=html, intercept=intercept)
        try:
            for nid in ['n1', 'n2']:
                result = await adapter.fetch(TaskRequest(operation='detail', content_id=nid))
                assert result.items[0].id == nid and result.items[0].data['detail_complete']
            assert downloads == 1
            assert adapter.asset_cache.hits == 1
            assert budget.calls == []
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()
        assert not adapter.asset_cache.entries


@pytest.mark.parametrize('code, kind', [
    ('300013', 'access_denied'), ('300012', 'access_denied'), ('captcha', 'verification_required'),
])
async def test_detail_redirect_to_security_error_page_is_platform_risk(tmp_path, code, kind):
    target = ('https://www.xiaohongshu.com/website-login/captcha?redirectPath=x&verifyUuid=u'
              if code == 'captcha' else
              'https://www.xiaohongshu.com/website-login/error?error_code=' + code
              + '&error_msg=%E8%AE%BF%E9%97%AE%E9%A2%91%E7%B9%81&redirectPath=x&uuid=u')

    async def intercept(route):
        url = route.request.url
        if '/explore/n1' in url:
            await route.fulfill(status=302, headers={'location': target})
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html='<html><body>安全限制</body></html>', intercept=intercept,
        )
        budget.config.request_timeout = 5
        try:
            started = time.monotonic()
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(TaskRequest(operation='detail', content_id='n1'))
            assert failed.value.kind == kind
            assert failed.value.blocks_run
            assert code in failed.value.message
            assert time.monotonic() - started < 3
            diagnostic = json.loads((tmp_path / failed.value.sample_ref).read_text())
            assert diagnostic['response']['platform_error_code'] == code
        finally:
            budget.halt(CollectionError('finished'))
            await adapter.close()
            await browser.close()


CARD_SEARCH_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="height:4000px">
<div id="list"></div><div id="overlay"></div>
<script>
const api = '/api/sns/web/v1/';
const post = (p, d) => fetch(api+p, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
function card(id, top) {
  return `<section class="note-item" style="margin-top:${top}px"><a class="cover" href="/search_result/${id}?xsec_token=tok-${id}&xsec_source=" onclick="event.preventDefault();openNote('${id}')">${id}</a></section>`;
}
function openNote(id) {
  history.pushState({note:id}, '', '/explore/'+id+'?xsec_token=tok-'+id+'&xsec_source=pc_search');
  document.querySelector('#overlay').innerHTML = '<div class="note-detail-mask"><div class="note-scroller" style="height:200px;overflow:auto"><div style="height:2000px"></div></div></div>';
  post('feed', {source_note_id:id, xsec_token:'tok-'+id});
  fetch(api+'comment/page?note_id='+id+'&cursor=');
}
window.addEventListener('popstate', () => {document.querySelector('#overlay').innerHTML = '';});
document.addEventListener('keydown', e => {if (e.key === 'Escape' && history.state && history.state.note) history.back();});
if (location.pathname.startsWith('/search_result')) {
  post('search/notes', {keyword:'猫',page:1,sort:'general'});
  document.querySelector('#list').innerHTML = card('n1', 10);
  // The second card only renders after the result list is scrolled.
  window.addEventListener('scroll', () => {
    if (scrollY > 300 && !document.querySelector('a[href*="/n2?"]')) document.querySelector('#list').insertAdjacentHTML('beforeend', card('n2', 800));
  });
}
</script></body></html>"""


def _card_page_intercept(documents):
    async def intercept(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.resource_type == "document":
            documents.append(path)
            if path.startswith("/explore/"):
                # Production throttles direct note documents with 300013.
                await route.fulfill(status=302, headers={"location": (
                    "https://www.xiaohongshu.com/website-login/error?error_code=300013")})
                return True
            return False
        if path.endswith("/feed"):
            note = request.post_data_json["source_note_id"]
            await route.fulfill(status=200, json={"success": True, "code": 0, "data": {"items": [{
                "id": note, "note_card": {"desc": "text " + note, "user": {"user_id": "author"},
                                          "time": 1700000000000}}]}})
            return True
        return False
    return intercept


async def test_notes_open_by_clicking_search_cards_without_note_documents(tmp_path):
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CARD_SEARCH_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            search = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            assert [i.id for i in search.items] == ["n1"]
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            detail = await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            assert detail.items[0].data["text"] == "text n1"
            comments = await adapter.fetch(TaskRequest(operation="comments", content_id="n1", input=context))
            assert comments.items[0].id == "r1"
            second = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n2", input={"search_keyword": "猫", "xsec_token": "tok-n2"},
            ))
            assert second.items[0].data["text"] == "text n2"
            assert documents == ["/explore", "/search_result_ai"]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_note_task_reopens_its_search_page_after_the_document_was_lost(tmp_path):
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CARD_SEARCH_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            detail = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", sort="general",
                input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            assert detail.items[0].data["text"] == "text n1"
            assert documents == ["/explore", "/search_result_ai"]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_missing_search_card_reloads_once_then_fails_only_that_note(tmp_path):
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CARD_SEARCH_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            started = time.monotonic()
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(TaskRequest(
                    operation="detail", content_id="n9", sort="general",
                    input={"search_keyword": "猫", "xsec_token": "tok-n9"},
                ))
            # A direct note document is what XHS throttles (300013); never fall back to it.
            assert failed.value.kind == "note_card_missing"
            assert not failed.value.blocks_run
            assert documents == ["/explore", "/search_result_ai"] * 2
            assert time.monotonic() - started < 25
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


LATE_HANDLER_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="filter">筛选<div id="options"></div></div>
<script>
const api = '/api/sns/web/v2/';
const post = (data) => fetch(api+'search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
post({keyword:'猫',page:1,page_size:20,sort:'general'});
// The SSR filter is visible immediately, but hydration binds it later.
setTimeout(() => document.querySelector('.filter').addEventListener('mouseenter', () => {
  document.querySelector('#options').innerHTML = '<span onclick="post({keyword:\\'猫\\',page:1,page_size:20,sort:\\'general\\',filters:[{type:\\'sort_type\\',tags:[\\'time_descending\\']}]})">最新</span>';
}), 3000);
document.querySelector('.filter').addEventListener('mouseleave', () => {document.querySelector('#options').innerHTML = '';});
</script></body></html>"""


async def test_browser_latest_sort_retries_until_the_filter_is_hydrated(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=LATE_HANDLER_SORT_HTML)
        budget.config.request_timeout = 15
        try:
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
            assert result.items[0].id == "n1"
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


OVERLAY_BURST_HTML = CARD_SEARCH_HTML.replace(
    "post('feed', {source_note_id:id, xsec_token:'tok-'+id});\n  fetch(api+'comment/page?note_id='+id+'&cursor=');",
    # The note overlay loads comments and detail together and gives up on a
    # detail request that is not answered promptly (production: ~6s).
    "fetch(api+'comment/page?note_id='+id+'&cursor=');\n"
    "  setTimeout(() => fetch(api+'feed', {method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({source_note_id:id, xsec_token:'tok-'+id}),signal:AbortSignal.timeout(1500)}), 50);",
)


async def test_note_overlay_requests_are_one_paced_action(tmp_path):
    assert OVERLAY_BURST_HTML != CARD_SEARCH_HTML
    documents, admitted = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=OVERLAY_BURST_HTML, intercept=_card_page_intercept(documents),
        )
        budget.config.min_interval = 2
        budget.last_request = 0

        async def paced_admit(operation, *, paced=True):
            now = time.monotonic()
            if paced:
                assert now >= budget.last_request + budget.config.min_interval
            admitted.append((operation, now, paced))
            budget.last_request = now

        budget.admit = paced_admit
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            detail = await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            assert detail.items[0].data["text"] == "text n1"
            assert detail.stop_reason is None
            comments = await adapter.fetch(TaskRequest(operation="comments", content_id="n1", input=context))
            assert comments.items[0].id == "r1"
            (_, search_at, _), (first_op, first_at, _), (second_op, second_at, _) = admitted[:3]
            # The click waits for the interval; the target detail read goes first
            # and the overlay's comment read follows it without pacing.
            assert first_at - search_at >= budget.config.min_interval
            assert (first_op, second_op) == ("detail", "comments")
            assert second_at - first_at < 1.5
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


SCROLL_PAGE_HTML = OVERLAY_BURST_HTML.replace(
    "fetch(api+'comment/page?note_id='+id+'&cursor=');",
    "fetch(api+'comment/page?note_id='+id+'&cursor=');\n"
    "  document.querySelector('.note-scroller').addEventListener('scroll', () => {"
    "if (!window.more) {window.more = 1; fetch(api+'comment/page?note_id='+id+'&cursor=c1',"
    "{signal:AbortSignal.timeout(1500)});}});",
    1,
)


async def test_scroll_actions_are_paced_so_their_reads_are_not_abandoned(tmp_path):
    assert SCROLL_PAGE_HTML != OVERLAY_BURST_HTML
    documents, admitted = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=SCROLL_PAGE_HTML, intercept=_card_page_intercept(documents),
        )
        budget.config.min_interval = 2
        budget.config.request_timeout = 8
        budget.last_request = 0

        async def paced_admit(operation, *, paced=True):
            now = time.monotonic()
            if paced:
                assert now >= budget.last_request + budget.config.min_interval
            admitted.append((operation, now))
            budget.last_request = now

        budget.admit = paced_admit
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            request = TaskRequest(operation="comments", content_id="n1", input=context)
            first = await adapter.fetch(request)
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert [i.id for i in second.items] == ["r2"]
            last_two = [at for _, at in admitted[-2:]]
            assert last_two[1] - last_two[0] >= budget.config.min_interval
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


AUTO_HIDE_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="filter">筛选<div id="options"></div></div>
<script>
const api = '/api/sns/web/v2/';
const post = (data) => fetch(api+'search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
post({keyword:'猫',page:1,page_size:20,sort:'general'});
let hide;
document.querySelector('.filter').addEventListener('mouseenter', () => {
  document.querySelector('#options').innerHTML = '<span onclick="post({keyword:\\'猫\\',page:1,page_size:20,sort:\\'general\\',filters:[{type:\\'sort_type\\',tags:[\\'time_descending\\']}]})">最新</span>';
  clearTimeout(hide);
  // The popover closes on its own shortly after opening.
  hide = setTimeout(() => {document.querySelector('#options').innerHTML = '';}, 1000);
});
</script></body></html>"""


async def test_latest_sort_waits_for_the_interval_before_opening_its_popover(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=AUTO_HIDE_SORT_HTML)
        budget.config.min_interval = 2
        budget.config.request_timeout = 10
        budget.last_request = 0

        async def paced_admit(operation, *, paced=True):
            budget.last_request = time.monotonic()

        budget.admit = paced_admit
        try:
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
            assert result.items[0].id == "n1"
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


SLOW_OVERLAY_HTML = OVERLAY_BURST_HTML.replace(", 50);", ", 4000);", 1)


async def test_overlay_burst_waits_for_a_slow_detail_read(tmp_path):
    assert SLOW_OVERLAY_HTML != OVERLAY_BURST_HTML
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=SLOW_OVERLAY_HTML, intercept=_card_page_intercept(documents),
        )
        budget.config.min_interval = 3
        budget.config.request_timeout = 12
        budget.last_request = 0

        async def paced_admit(operation, *, paced=True):
            if paced:
                assert time.monotonic() >= budget.last_request + budget.config.min_interval
            budget.last_request = time.monotonic()

        budget.admit = paced_admit
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            detail = await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            assert detail.stop_reason is None
            comments = await adapter.fetch(TaskRequest(operation="comments", content_id="n1", input=context))
            assert comments.items[0].id == "r1"
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


ENDLESS_CARD_HTML = CARD_SEARCH_HTML.replace(
    "document.querySelector('#list').innerHTML = card('n1', 10);",
    "document.querySelector('#list').innerHTML = card('n1', 10) + card('n2', 10);\n"
    "  // A virtual list drops cards scrolled far out of view.\n"
    "  const first = document.querySelector('#list').innerHTML;\n"
    "  window.addEventListener('scroll', () => {\n"
    "    const list = document.querySelector('#list');\n"
    "    if (scrollY > 500 && list.innerHTML) list.innerHTML = '';\n"
    "    if (scrollY <= 500 && !list.innerHTML) list.innerHTML = first;\n"
    "  });\n"
    "  let page = 1;\n"
    "  // Reaching the end loads another result page, forever.\n"
    "  window.addEventListener('scroll', () => {\n"
    "    if (innerHeight + scrollY >= document.body.scrollHeight - 5) {\n"
    "      page += 1; post('search/notes', {keyword:'猫',page,sort:'general'});\n"
    "      document.body.style.height = (document.body.scrollHeight + 2000) + 'px';\n"
    "    }\n"
    "  });",
    1,
)


async def test_card_search_scans_loaded_results_instead_of_loading_more(tmp_path):
    assert ENDLESS_CARD_HTML != CARD_SEARCH_HTML
    documents, searches = [], []
    base = _card_page_intercept(documents)

    async def intercept(route):
        if route.request.url.endswith("/search/notes"):
            searches.append(route.request.post_data_json["page"])
        return await base(route)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=ENDLESS_CARD_HTML, intercept=intercept,
        )
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            # Leave the list scrolled past the first card, as after earlier notes.
            await adapter.page.evaluate("window.scrollTo(0, 3000)")
            await asyncio.sleep(0.5)
            loaded = len(searches)
            detail = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            assert detail.items[0].data["text"] == "text n1"
            assert len(searches) - loaded <= 1
            assert documents == ["/explore", "/search_result_ai"]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_search_page_reloads_are_capped_per_run(tmp_path):
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CARD_SEARCH_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            for note in ("n7", "n8", "n9"):
                with pytest.raises(CollectionError, match="search results"):
                    await adapter.fetch(TaskRequest(
                        operation="detail", content_id=note, sort="general",
                        input={"search_keyword": "猫", "xsec_token": "tok-" + note},
                    ))
            # Repeated reloads escalated to a captcha in production.
            assert documents == ["/explore", "/search_result_ai"] * 3
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


CONTAINER_SEARCH_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<div class="search-layout-wrapper" style="height:500px;overflow:auto"><div style="height:3000px"></div></div>
<script>
const post = (data) => fetch('/api/sns/web/v2/search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const keyword = new URLSearchParams(location.search).get('keyword');
post({keyword, page:1, sort:'general'});
let page = 1;
// The AI result page scrolls an inner container; the window never moves.
document.querySelector('.search-layout-wrapper').addEventListener('scroll', () => {
  if (page === 1) {page = 2; post({keyword, page:2, sort:'general'});}
});
</script></body></html>"""


async def test_search_starts_from_the_home_search_box_and_scrolls_its_container(tmp_path):
    documents = []

    async def intercept(route):
        if route.request.resource_type == "document":
            documents.append(urlsplit(route.request.url).path)
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CONTAINER_SEARCH_HTML, intercept=intercept,
        )
        try:
            request = TaskRequest(operation="search", keyword="猫", sort="general")
            first = await adapter.fetch(request)
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert [i.id for i in first.items] == ["n1"] and [i.id for i in second.items] == ["n2"]
            # A directly loaded search URL is throttled with a captcha; only the
            # home page is loaded as a document, the results come from its box.
            assert documents[0] == "/explore"
            assert "/search_result" not in documents
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


LATE_EXPLORE_HTML = EXPLORE_HTML.replace(
    "document.querySelector('#search-input-in-feeds').addEventListener('keydown', e => {",
    "setTimeout(() => document.querySelector('#search-input-in-feeds').addEventListener('keydown', e => {",
).replace("  }\n});\n</script>", "  }\n}), 2500);\n</script>")


async def test_home_search_resubmits_when_the_box_is_not_hydrated_yet(tmp_path):
    assert LATE_EXPLORE_HTML != EXPLORE_HTML

    async def intercept(route):
        if route.request.resource_type == "document" and urlsplit(route.request.url).path == "/explore":
            await route.fulfill(status=200, content_type="text/html", body=LATE_EXPLORE_HTML)
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=CONTAINER_SEARCH_HTML, intercept=intercept,
        )
        budget.config.request_timeout = 30
        try:
            started = time.monotonic()
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            assert result.items[0].id == "n1"
            assert time.monotonic() - started < 20
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


INCIDENTAL_SEARCH_HTML = OVERLAY_BURST_HTML.replace(
    "fetch(api+'comment/page?note_id='+id+'&cursor=');\n",
    # Scrolling the card into view also loads another result page.
    "post('search/notes', {keyword:'猫', page:2, sort:'general'});\n"
    "  fetch(api+'comment/page?note_id='+id+'&cursor=');\n",
    1,
).replace(", 50);", ", 300);", 1)


async def test_overlay_detail_read_is_not_queued_behind_an_incidental_slow_read(tmp_path):
    assert INCIDENTAL_SEARCH_HTML.count("page:2") == 1
    documents = []
    base = _card_page_intercept(documents)

    async def intercept(route):
        body = route.request.post_data_json if route.request.method == "POST" else None
        if body and body.get("page") == 2:
            await asyncio.sleep(3)  # a large result page streams slowly
        return await base(route)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=INCIDENTAL_SEARCH_HTML, intercept=intercept,
        )
        budget.config.min_interval = 0.5
        budget.config.request_timeout = 8
        budget.last_request = 0

        async def paced_admit(operation, *, paced=True):
            budget.last_request = time.monotonic()

        budget.admit = paced_admit
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            detail = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            assert detail.stop_reason is None
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


SSR_RESULTS_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="filter">筛选<div id="options"></div></div>
<section class="note-item"><a class="cover" href="/search_result/x1?xsec_token=t">x1</a></section>
<script>
// Results arrived server-rendered: no general search read is issued.
const post = (data) => fetch('/api/sns/web/v2/search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
document.querySelector('.filter').addEventListener('mouseenter', () => {
  document.querySelector('#options').innerHTML = '<span onclick="post({keyword:\\'猫\\',page:1,page_size:20,sort:\\'general\\',filters:[{type:\\'sort_type\\',tags:[\\'time_descending\\']}]})">最新</span>';
});
</script></body></html>"""


async def test_latest_sort_proceeds_when_results_were_server_rendered(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=SSR_RESULTS_SORT_HTML)
        budget.config.request_timeout = 20
        try:
            started = time.monotonic()
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
            assert result.items[0].id == "n1"
            assert time.monotonic() - started < 15
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


# The AI result page binds Escape to router back even without an overlay.
ESCAPE_BACK_HTML = CARD_SEARCH_HTML.replace(
    "document.addEventListener('keydown', e => {if (e.key === 'Escape' && history.state && history.state.note) history.back();});",
    "document.addEventListener('keydown', e => {if (e.key === 'Escape') history.back();});",
    1,
)


async def test_escape_is_not_pressed_once_the_overlay_already_closed(tmp_path):
    assert ESCAPE_BACK_HTML != CARD_SEARCH_HTML
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=ESCAPE_BACK_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            # The overlay closed on its own (e.g. the page reset it).
            await adapter.page.evaluate("history.back()")
            await asyncio.sleep(0.5)
            second = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n2", input={"search_keyword": "猫", "xsec_token": "tok-n2"},
            ))
            assert second.items[0].data["text"] == "text n2"
            # No reload: an extra Escape would have left for the home page.
            assert documents == ["/explore", "/search_result_ai"]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


PREFETCH_CARD_HTML = CARD_SEARCH_HTML.replace(
    "post('search/notes', {keyword:'猫',page:1,sort:'general'});\n",
    "post('search/notes', {keyword:'猫',page:1,sort:'general'})"
    ".then(() => post('search/notes', {keyword:'猫',page:2,sort:'general'}));\n",
    1,
)


async def test_prefetched_result_page_survives_opening_notes(tmp_path):
    assert PREFETCH_CARD_HTML != CARD_SEARCH_HTML
    documents, searches = [], []
    base = _card_page_intercept(documents)

    async def intercept(route):
        if route.request.url.endswith("/search/notes"):
            searches.append(route.request.post_data_json["page"])
        return await base(route)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=PREFETCH_CARD_HTML, intercept=intercept,
        )
        try:
            request = TaskRequest(operation="search", keyword="猫", sort="general")
            first = await adapter.fetch(request)
            await asyncio.sleep(0.5)
            await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            # Notes of page 1 ran before page 2 (defer_search); the page had
            # already prefetched page 2 and will not request it again.
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert [i.id for i in second.items] == ["n2"]
            assert searches == [1, 2]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


async def test_result_page_arriving_after_a_note_opened_is_kept(tmp_path):
    documents, searches = [], []
    base = _card_page_intercept(documents)

    async def intercept(route):
        if route.request.url.endswith("/search/notes"):
            page = route.request.post_data_json["page"]
            searches.append(page)
            if page == 2:
                await asyncio.sleep(2)  # the prefetch answers after a note opened
        return await base(route)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=PREFETCH_CARD_HTML, intercept=intercept,
        )
        try:
            request = TaskRequest(operation="search", keyword="猫", sort="general")
            first = await adapter.fetch(request)
            await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            await asyncio.sleep(2.5)
            second = await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            assert [i.id for i in second.items] == ["n2"]
            assert searches == [1, 2]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


STALLED_COMMENTS_HTML = CARD_SEARCH_HTML  # its overlay never loads a second comment page


async def test_stalled_comment_pagination_fails_only_that_note(tmp_path):
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=STALLED_COMMENTS_HTML, intercept=_card_page_intercept(documents),
        )
        budget.config.request_timeout = 3
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            context = {"search_keyword": "猫", "xsec_token": "tok-n1"}
            await adapter.fetch(TaskRequest(operation="detail", content_id="n1", input=context))
            request = TaskRequest(operation="comments", content_id="n1", input=context)
            first = await adapter.fetch(request)
            with pytest.raises(CollectionError) as stalled:
                await adapter.fetch(request.model_copy(update={"context": first.next_context}))
            # A later comment page not loading is not a platform risk signal.
            assert stalled.value.kind == "page_stalled"
            assert not stalled.value.blocks_run
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


# A heavy overlay (hundreds of comments) runs a second, late router back after
# Escape, leaving the result page for the home page after the close looked done.
SLOW_ESCAPE_HTML = CARD_SEARCH_HTML.replace(
    "document.addEventListener('keydown', e => {if (e.key === 'Escape' && history.state && history.state.note) history.back();});",
    "document.addEventListener('keydown', e => {if (e.key === 'Escape' && history.state && history.state.note) {history.back(); setTimeout(() => history.back(), 200);}});",
    1,
)


async def test_late_escape_back_does_not_strand_the_result_page(tmp_path):
    assert SLOW_ESCAPE_HTML != CARD_SEARCH_HTML
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=SLOW_ESCAPE_HTML, intercept=_card_page_intercept(documents),
        )
        budget.config.request_timeout = 20
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            second = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n2", input={"search_keyword": "猫", "xsec_token": "tok-n2"},
            ))
            assert second.items[0].data["text"] == "text n2"
            assert adapter.search_reloads == 0
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


# After a long overlay the result list is cleared and re-rendered.
RERENDER_LIST_HTML = CARD_SEARCH_HTML.replace(
    "window.addEventListener('popstate', () => {document.querySelector('#overlay').innerHTML = '';});",
    "window.addEventListener('popstate', () => {document.querySelector('#overlay').innerHTML = '';"
    " const list = document.querySelector('#list'); const kept = list.innerHTML; list.innerHTML = '';"
    " setTimeout(() => {list.innerHTML = kept;}, 6000);});",
    1,
)


async def test_card_scan_waits_for_a_re_rendering_result_list(tmp_path):
    assert RERENDER_LIST_HTML != CARD_SEARCH_HTML
    documents = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(
            browser, tmp_path, page_html=RERENDER_LIST_HTML, intercept=_card_page_intercept(documents),
        )
        try:
            await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            again = await adapter.fetch(TaskRequest(
                operation="comments", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            assert again.items
            # Reopening n1 after its overlay was closed finds the re-rendered card.
            adapter.page_key = None
            await adapter._close_overlay()
            detail = await adapter.fetch(TaskRequest(
                operation="detail", content_id="n1", input={"search_keyword": "猫", "xsec_token": "tok-n1"},
            ))
            assert detail.items[0].data["text"] == "text n1"
            assert adapter.search_reloads == 0
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


RERANKED_SEARCH_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="height:3000px">
<div id="list"></div><div id="overlay"></div>
<script>
const post = (data) => fetch('/api/sns/web/v2/search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
// General results are requested twice; the page renders the second, re-ranked answer.
post({keyword:'猫', page:1, sort:'general'}).then(() => setTimeout(() =>
  post({keyword:'猫', page:1, sort:'general', filters:[{type:'filter_note_type', tags:['不限']}]})
    .then(r => r.json()).then(j => {
      document.querySelector('#list').innerHTML = j.data.items.map(i => `<section class="note-item" data-note-id="${i.id}"><a class="cover" href="/search_result/${i.id}?xsec_token=t">${i.id}</a></section>`).join('');
    }), 800));
</script></body></html>"""


async def test_general_search_records_the_result_page_the_list_renders(tmp_path):
    async def intercept(route):
        if route.request.url.endswith("/search/notes"):
            body = route.request.post_data_json
            note = "rendered" if body.get("filters") else "discarded"
            await route.fulfill(status=200, json={"success": True, "code": 0, "data": {
                "items": [{"id": note, "model_type": "note", "note_card": {"title": note}, "xsec_token": "t"}],
                "has_more": True}})
            return True
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=RERANKED_SEARCH_HTML, intercept=intercept)
        try:
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="general"))
            # Notes are opened by clicking their card; only the rendered list has them.
            assert [i.id for i in result.items] == ["rendered"]
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


CHIP_AND_FILTER_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="tag-chips"><button class="chip">综合</button><button class="chip">资料</button></div>
<div class="filter">筛选<div id="options"></div></div>
<script>
const post = (data) => fetch('/api/sns/web/v2/search/notes', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
post({keyword:'猫',page:1,page_size:20,sort:'general'});
// A category chip narrows the results; it is not the sort menu.
document.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () =>
  post({keyword:'猫',page:1,page_size:20,sort:'general',filters:[{type:'category',tags:[b.textContent]}]})));
document.querySelector('.filter').addEventListener('mouseenter', () => {
  document.querySelector('#options').innerHTML = '<span onclick="post({keyword:\\'猫\\',page:1,page_size:20,sort:\\'general\\',filters:[{type:\\'sort_type\\',tags:[\\'time_descending\\']}]})">最新</span>';
});
</script></body></html>"""


async def test_latest_sort_uses_the_filter_not_a_category_chip(tmp_path):
    categories = []

    async def intercept(route):
        if route.request.url.endswith("/search/notes"):
            body = route.request.post_data_json
            if any(f.get("type") == "category" for f in body.get("filters") or []):
                categories.append(body)
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=CHIP_AND_FILTER_HTML, intercept=intercept)
        budget.config.request_timeout = 15
        try:
            result = await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
            assert result.items[0].id == "n1"
            assert categories == []
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()


NO_SORT_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="filter">筛选</div>
<script>
fetch('/api/sns/web/v2/search/notes', {method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({keyword:'猫',page:1,page_size:20,sort:'general'})});
</script></body></html>"""


async def test_unavailable_latest_sort_is_retryable_and_keeps_the_run(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        adapter, budget, _ = await create_adapter(browser, tmp_path, page_html=NO_SORT_HTML)
        budget.config.request_timeout = 3
        try:
            with pytest.raises(CollectionError) as failed:
                await adapter.fetch(TaskRequest(operation="search", keyword="猫", sort="latest"))
            # One search whose popover never opened is not a changed page
            # contract; retry it from a fresh page and keep other keywords.
            assert failed.value.kind == "network_failure"
            assert not failed.value.blocks_run
            assert failed.value.sample_ref
        finally:
            budget.halt(CollectionError("finished"))
            await adapter.close()
            await browser.close()
