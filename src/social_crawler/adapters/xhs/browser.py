import asyncio
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from social_crawler.domain.models import CollectionError, Operation, PageResult
from social_crawler.environments.browser_runtime import playwright_launch_options
from social_crawler.environments.environment_snapshot import (
    audit_environment,
    capture_browser_environment,
)
from social_crawler.environments.managed_browser import (
    connect_managed_browser,
    is_managed_browser_provider,
)
from social_crawler.environments.proxy import prepare_playwright_proxy
from social_crawler.environments.session_recovery import bind_profile, identity_from_payload

from .asset_cache import PublicAssetCache
from .errors import check_response
from .parsing import (
    Exchange,
    content_item,
    latest_filter_applied,
    matches,
    operation_for_path,
    parse_page,
)
from .sites import hostname_matches, site_for

REPLY_EXPAND_TEXT = re.compile(r"(?:展开|查看|更多).{0,12}(?:回复|评论)")
REPLY_CONTROL_SELECTOR = (
    ".show-more, .reply-more, [class*='show-more'], [class*='reply-more'], "
    "button, a, [role='button']"
)
LATEST_SORT_TEXT = re.compile(r"^\s*(?:最新|最新发布|按时间)\s*$")
SORT_MENU_TEXT = re.compile(r"^\s*(?:综合|综合排序|默认排序|排序|筛选)\s*$")
LATEST_SORT_SELECTOR = (
    "[data-sort='time_descending'], [data-value='time_descending'], "
    "[value='time_descending']"
)


# XHS pages abandon a read left waiting (~6s), and opening a note overlay
# issues detail and comment reads together. Pace user actions (click, scroll,
# expand) by min_interval instead, and admit the few reads each action issues
# promptly; they still spend the request budget.
# A headful overlay can take >10s to issue its detail read after the click.
ACTION_BURST_SECONDS = 15.0
FOLLOW_UP_SECONDS = 5.0
OVERLAY_BURST_REQUESTS = 3
MAX_SEARCH_RELOADS = 2
SEARCH_BOX_SECONDS = 120
# Notes are processed in result order, so the next card is just below the
# last one. Scanning to the end of the list triggers another result page.
FORWARD_SCAN_STEPS = 4
EMPTY_LIST_SECONDS = 15
# List reads the page may issue again for the same page before rendering
# (re-ranking, default filters, retries); the last answer is the rendered one.
RERENDERED_LIST_OPERATIONS = {Operation.SEARCH}
LIST_SETTLE_SECONDS = 2.5
# On a reloaded "latest" list, newer posts push earlier results down.
RELOAD_GROW_PAGES = 4
SEARCH_SUBMITS = 4
ACTION_BURST_REQUESTS = 2


# Directly loaded search URLs are challenged with a captcha while the home
# page search box works; results then render in an inner scroll container.
SEARCH_BOX_SELECTOR = "#search-input-in-feeds, textarea#search-input, input#search-input"
RESULT_PATHS = {"/search_result", "/search_result_ai"}
RESULTS_SCROLL_JS = """(delta) => {
    const pick = () => {
        const wrapper = document.querySelector('.search-layout-wrapper');
        if (wrapper && wrapper.scrollHeight > wrapper.clientHeight) return wrapper;
        const card = document.querySelector('section.note-item');
        for (let el = card && card.parentElement; el && el !== document.body; el = el.parentElement) {
            if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight) return el;
        }
        return document.scrollingElement || document.documentElement;
    };
    const el = pick();
    if (delta === 'top') el.scrollTo(0, 0);
    else if (delta === 'page') el.scrollBy(0, Math.max(400, el.clientHeight * 0.8));
    return [Math.round(el.scrollTop), Math.round(el.scrollTop + el.clientHeight), el.scrollHeight];
}"""


class XHSBrowser:
    version = "xhs-browser-v7-priority/startup/public-assets"

    def __init__(self, session, budget, samples, *, context_factory=None, display=None):
        self.session, self.budget, self.samples = session, budget, samples
        self.site = site_for(session.platform)
        self.context_factory = context_factory
        self.display = display
        self.playwright = self.browser = self.context = self.page = None
        self.managed_browser = None
        self.proxy_bridge = None
        self.exchanges = []
        self.response_tasks = set()
        self.page_key = None
        self.current_request = None
        self.total_requests = self.blocked_requests = 0
        self.business_slot = asyncio.Semaphore(1)
        self.inflight = set()
        self.identity_verified = False
        self.identity_profile = {}
        self.environment_checked = False
        self.generation = 0
        self.request_generations = {}
        self.pending_business = {}
        self.pending_captures = {}
        self.capture_errors = []
        self.canceled_exchanges = []
        self.identity_error = None
        self.network_events = deque(maxlen=40)
        self.business_events = deque(maxlen=100)
        self.request_started = {}
        self.document_ready = False
        self.search_key = None
        self.overlay_note = None
        self.burst = None
        self.unpaced = set()
        self.result_generations = set()
        self.last_list_capture = 0.0
        self.search_reloads = 0
        self.asset_cache = PublicAssetCache()
        self.closing = False

    async def _start(self):
        if self.page:
            return
        if self.context_factory:
            self.context = await self.context_factory()
        else:
            self.playwright = await async_playwright().start()
            if is_managed_browser_provider(self.session.config.browser_provider):
                try:
                    self.managed_browser = await connect_managed_browser(
                        self.playwright,
                        self.session.config,
                        headless=self.session.config.headless,
                        display=self.display,
                    )
                    self.browser = self.managed_browser.browser
                    self.context = self.managed_browser.context
                except BaseException:
                    self.managed_browser = None
                    await self.playwright.stop()
                    self.playwright = None
                    raise
            else:
                profile = Path(self.session.config.profile_dir).resolve()
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                proxy, self.proxy_bridge = await prepare_playwright_proxy(self.session.proxy)
                try:
                    launch_options = {
                        "headless": self.session.config.headless,
                        "user_agent": self.session.config.user_agent,
                        "viewport": {"width": 1440, "height": 1000},
                        "proxy": proxy,
                        "service_workers": "block",
                    }
                    launch_options.update(
                        playwright_launch_options(self.session.config.browser_channel)
                    )
                    if self.session.browser_locale:
                        launch_options["locale"] = self.session.browser_locale
                    if self.session.browser_timezone:
                        launch_options["timezone_id"] = self.session.browser_timezone
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        str(profile), **launch_options
                    )
                except BaseException:
                    if self.proxy_bridge:
                        await self.proxy_bridge.close()
                        self.proxy_bridge = None
                    raise
        await bind_profile(self.context, self.session)
        # Attach interception BEFORE the first navigation. Worker-only profile;
        # no unrelated browser state, and no write-operation endpoints admitted.
        await self.context.route("**/*", self._route)
        await self.context.route_web_socket("**/*", lambda socket: socket.close())
        self.context.on("requestfinished", self._release_request)
        self.context.on("requestfailed", self._request_failed)
        self.context.on("response", self._on_response)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("domcontentloaded", self._document_loaded)
        self.page.set_default_timeout(self.budget.config.request_timeout * 1000)
        if hasattr(self.budget, "store"):
            self.budget.store.event(
                self.budget.run_id,
                "browser_started",
                {
                    "browser_version": self.context.browser.version
                    if self.context.browser
                    else "unknown",
                    "user_agent": await self.page.evaluate("navigator.userAgent"),
                    "account_ref": self.session.config.account_ref,
                    "session_version": self.session.config.session_version,
                    "binding_version": self.session.config.binding_version,
                    "browser_provider": self.session.config.browser_provider,
                },
            )

    async def _audit_environment(self):
        if self.environment_checked or self.session.config.consistency_policy == "off":
            self.environment_checked = True
            return
        try:
            observed = await capture_browser_environment(
                self.page,
                browser_version=(
                    self.context.browser.version if self.context.browser is not None else None
                ),
                headless=self.session.config.headless,
                browser_channel=self.session.config.browser_channel,
                source="xhs_browser_runtime",
            )
        except (PlaywrightError, TypeError, AttributeError) as exc:
            report = {
                "policy": self.session.config.consistency_policy,
                "status": "probe_failed",
                "allowed": self.session.config.consistency_policy != "strict",
                "stage": "xhs_browser",
                "error_type": type(exc).__name__,
            }
            if hasattr(self.budget, "store"):
                self.budget.store.event(
                    self.budget.run_id, "environment_consistency_checked", report
                )
            if not report["allowed"]:
                raise CollectionError(
                    "environment_mismatch",
                    "Strict environment consistency could not inspect the browser runtime",
                ) from exc
            self.environment_checked = True
            return
        audit_environment(
            self.session,
            observed,
            budget=self.budget,
            stage="xhs_browser",
        )
        self.environment_checked = True

    async def _route(self, route):
        request = route.request
        generation = self.generation
        self.request_generations[request] = generation
        self.total_requests += 1
        u = urlsplit(request.url)
        host = u.hostname or ""
        if not hostname_matches(host, self.site.request_domains):
            self.blocked_requests += 1
            await route.abort()
            return
        operation = operation_for_path(u.path)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and operation not in {
            Operation.SEARCH,
            Operation.DETAIL,
        }:
            self.blocked_requests += 1
            await route.abort()
            return
        try:
            self.budget.check()
            if cached := await self.asset_cache.get(request):
                await route.fulfill(**cached)
                return
            # Page startup emits many read-only API requests. They must pass the
            # allowlist, but only collection endpoints consume the request budget
            # and global pacing slot; otherwise startup traffic can starve the
            # search response past request_timeout.
            if operation:
                self.pending_business[request] = generation
                self.request_started[request] = time.monotonic()
                self._business_event(request, "queued")
                if not await self._acquire_business(request, generation):
                    self._release_request(request)
                    await route.abort()
                    return
                self.inflight.add(request)
                if (generation != self.generation or self.closing
                        or self.pending_business.get(request) != generation):
                    self._release_request(request)
                    await route.abort()
                    return
                if request in self.unpaced:
                    self.unpaced.discard(request)
                    await self.budget.admit(str(operation), paced=False)
                else:
                    await self.budget.admit(str(operation))
                # Pacing can yield while fetch() moves to another document.
                if (generation != self.generation or self.closing
                        or self.pending_business.get(request) != generation):
                    self._release_request(request)
                    await route.abort()
                    return
                self._business_event(request, "admitted")
            await route.fallback()
        except CollectionError as exc:
            self.budget.halt(exc)
            self.blocked_requests += 1
            self._release_request(request)
            await route.abort()
        except PlaywrightError:
            self._release_request(request)

    def _business_priority(self, request):
        target = self.current_request
        if target is None:
            return 2
        exchange = self._exchange(request)
        if matches(target, exchange, self.session.platform):
            return 0
        if target.operation == Operation.SEARCH:
            initial = target.model_copy(update={"sort": "general", "context": {"page": 1}})
            if matches(initial, exchange, self.session.platform):
                return 1
        return 2

    async def _acquire_business(self, request, generation):
        # Select AFTER pacing, so speculative page 2 cannot reserve the next
        # slot while the requested page 1 arrives. Equal priorities stay FIFO.
        while (not self.closing and generation == self.generation
               and self.pending_business.get(request) == generation):
            self.budget.check()
            ready_at = getattr(self.budget, "last_request", 0) + self.budget.config.min_interval
            target = self._business_priority(request) == 0
            burst = self._in_burst(generation, request)
            # Within an action's burst the target read goes first; the rest of
            # the burst (e.g. overlay comments) follows it without pacing.
            if burst and (target or self.burst["target_admitted"]):
                ready_at = 0
            elif burst or self._awaiting_target(generation):
                # Incidental reads (a result page loaded by scrolling the card
                # into view) wait until the action's target read is admitted.
                ready_at = float("inf")
            pending = [r for r, g in self.pending_business.items() if g == generation]
            if (not self.business_slot.locked() and time.monotonic() >= ready_at
                    and min(pending, key=self._business_priority) is request):
                await self.business_slot.acquire()
                if burst and ready_at == 0:
                    self.burst["left"] -= 1
                    if not self.burst["target_admitted"]:
                        self.burst["target_admitted"] = True
                        self.burst["until"] = time.monotonic() + FOLLOW_UP_SECONDS
                    self.unpaced.add(request)
                return True
            await asyncio.sleep(0.05)
        return False

    def _business_event(self, request, outcome, status=None):
        u = urlsplit(request.url)
        operation = operation_for_path(u.path)
        if not operation:
            return
        params = self._exchange(request).params
        # Keep correlation and timing, never tokens, full URLs or request bodies.
        self.business_events.append({
            "at": time.time(), "operation": str(operation), "outcome": outcome,
            "generation": self.request_generations.get(request), "status": status,
            "elapsed_ms": round(1000 * (time.monotonic() - self.request_started[request]))
            if request in self.request_started else None,
            "page": params.get("page") if isinstance(params.get("page"), int) else None,
            "latest": params.get("sort") == "time_descending"
            or latest_filter_applied(params.get("filters")),
            "target_match": bool(self.current_request and matches(
                self.current_request, self._exchange(request), self.session.platform,
            )),
            "failure": request.failure if request.failure == "net::ERR_ABORTED" else None,
        })

    def _release_request(self, request, *, forget_generation=True):
        self.unpaced.discard(request)
        if forget_generation:
            self.request_generations.pop(request, None)
            self.request_started.pop(request, None)
        self.pending_business.pop(request, None)
        if request in self.inflight:
            self.inflight.remove(request)
            self.business_slot.release()

    def _request_failed(self, request):
        generation = self.request_generations.get(request)
        self._business_event(request, "failed")
        self._network_event(request, "failed")
        if generation == self.generation and not self.closing:
            operation = operation_for_path(urlsplit(request.url).path)
            if operation:
                if request.failure == "net::ERR_ABORTED":
                    # Search filter changes cancel superseded requests in the
                    # same document. Wait for their replacement before retrying.
                    self.canceled_exchanges.append(self._exchange(request))
                else:
                    self.capture_errors.append((
                        self._exchange(request),
                        CollectionError("network_failure", "Collection request failed"),
                    ))
        self._release_request(request)

    @staticmethod
    def _exchange(request, payload=None, status=0, sample=None):
        try:
            params = json.loads(request.post_data) if request.post_data else {}
        except (ValueError, PlaywrightError):
            params = {}
        return Exchange(request.url, request.method, params, payload, status, sample)

    def _network_event(self, request, outcome, status=None):
        u = urlsplit(request.url)
        if request.resource_type in {"document", "script", "xhr", "fetch"}:
            failure = request.failure if outcome == "failed" else None
            self.network_events.append({
                "at": time.time(),
                "host": u.hostname, "path": u.path,
                "resource": request.resource_type, "outcome": outcome,
                "status": status,
                # Chromium error codes are useful; raw transport messages may
                # contain URLs, so retain only the symbolic code.
                "failure": failure if failure and re.fullmatch(r"net::[A-Z_]+", failure) else None,
            })

    def _on_response(self, response):
        generation = self.request_generations.get(response.request, self.generation)
        self._business_event(response.request, "response", response.status)
        # The server has answered; a large body (a result page) can stream for
        # seconds and must not hold the single slot the next read waits for.
        if response.request in self.inflight:
            self._release_request(response.request, forget_generation=False)
        self._network_event(response.request, "response", response.status)
        if self.asset_cache.eligible(response.request):
            cached = asyncio.create_task(self.asset_cache.capture(response))
            self.response_tasks.add(cached)
            cached.add_done_callback(self.response_tasks.discard)
        task = asyncio.create_task(self._capture(response, generation))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)
        if operation_for_path(urlsplit(response.url).path):
            self.pending_captures[task] = generation
            task.add_done_callback(lambda finished: self.pending_captures.pop(finished, None))

    async def _capture(self, response, generation=None):
        generation = self.generation if generation is None else generation
        sample = None
        u = urlsplit(response.url)
        operation = operation_for_path(u.path)
        if not operation and not u.path.endswith("/user/me"):
            if response.request.resource_type == "document" and response.status in {
                401,
                403,
                429,
                461,
                471,
            }:
                try:
                    check_response(response.status, {})
                except CollectionError as exc:
                    self.budget.halt(exc)
            return
        try:
            try:
                payload = await response.json()
            except (ValueError, PlaywrightError):
                payload = (await response.text())[:4096]
            params = {}
            if response.request.post_data:
                try:
                    params = json.loads(response.request.post_data)
                except ValueError:
                    params = {}
            sample = self.samples.write(
                {"url": response.url, "method": response.request.method, "params": params},
                response.status,
                payload,
            )
            try:
                check_response(response.status, payload, await response.all_headers())
            except CollectionError as exc:
                # Deliver note-local errors through the correlated exchange. Halting
                # the shared budget here would also stop every remaining note.
                if exc.kind != "content_unavailable" or operation not in {
                    Operation.DETAIL, Operation.COMMENTS, Operation.REPLIES,
                }:
                    raise
            if u.path.endswith("/user/me"):
                self.identity_profile = identity_from_payload(
                    "xhs", payload, self.session.config.expected_user_id,
                )
                self.identity_verified = True
                self.identity_error = None
            elif operation and not self.closing and (
                generation == self.generation
                or (operation == Operation.SEARCH and generation in self.result_generations)
            ):
                if operation in RERENDERED_LIST_OPERATIONS:
                    self.last_list_capture = time.monotonic()
                self.exchanges.append(
                    Exchange(
                        response.url,
                        response.request.method,
                        params,
                        payload,
                        response.status,
                        sample,
                    )
                )
        except CollectionError as exc:
            exc.sample_ref = sample
            self.budget.halt(exc)
        except PlaywrightError:
            # Navigation cancels old document body reads. Only an active,
            # correlated failure belongs to the task; it must remain retryable.
            if generation == self.generation and not self.closing:
                error = CollectionError("network_failure", "Response body unavailable")
                if operation:
                    if response.request.failure == "net::ERR_ABORTED":
                        self.canceled_exchanges.append(self._exchange(response.request))
                    else:
                        self.capture_errors.append((self._exchange(response.request), error))
                elif not self.identity_verified:
                    self.identity_error = error
        except Exception:
            if generation == self.generation and not self.closing:
                self.budget.halt(CollectionError("schema_changed", "Response capture failed"))
        finally:
            # Body-read failures can arrive without requestfinished. Never leave
            # the single business slot held across a navigation/retry.
            if operation:
                self._release_request(response.request)

    def _take(self, request, *, settle=True):
        if self.session.config.expected_user_id and not self.identity_verified:
            if self.identity_error:
                raise self.identity_error
            return None
        exchange = self._select_exchange(request, settle=settle)
        if exchange is not None:
            self.exchanges[:] = [e for e in self.exchanges if e is not exchange]
            try:
                check_response(exchange.status, exchange.payload)
                if request.operation == Operation.DETAIL:
                    # A direct page visit may supply the token omitted by the user.
                    for key in ("xsec_token", "xsec_source"):
                        if exchange.params.get(key):
                            request.input[key] = exchange.params[key]
                result = parse_page(
                    request, exchange.payload, platform=self.session.platform
                )
            except CollectionError as exc:
                exc.sample_ref = exchange.sample_ref
                raise
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                error = CollectionError("schema_changed", "Unexpected response field type")
                error.sample_ref = exchange.sample_ref
                raise error from exc
            result.sample_ref = exchange.sample_ref
            return result
        for index, (exchange, error) in enumerate(self.capture_errors):
            if matches(request, exchange, self.session.platform):
                self.capture_errors.pop(index)
                raise error
        return None

    def _select_exchange(self, request, *, settle=True):
        """The captured answer to use for request, or None while it may change."""
        matching = [e for e in self.exchanges if matches(request, e, self.session.platform)]
        if not matching or request.operation not in RERENDERED_LIST_OPERATIONS:
            return matching[0] if matching else None
        # The page can request the same list page again (re-ranked, default
        # filters added, retried) and renders its last answer; the cards to
        # click come from that one. Wait until answers settle, use the last,
        # and drop the superseded ones.
        if settle and time.monotonic() - self.last_list_capture < LIST_SETTLE_SECONDS:
            return None
        superseded = matching[:-1]
        self.exchanges[:] = [e for e in self.exchanges if not any(e is x for x in superseded)]
        return matching[-1]

    def _list_answer_settling(self, request):
        return request.operation in RERENDERED_LIST_OPERATIONS and any(
            matches(request, e, self.session.platform) for e in self.exchanges
        )

    def _security_error_code(self):
        u = urlsplit(self.page.url)
        path = u.path.rstrip("/")
        if not path.startswith("/website-login/"):
            return None
        if path == "/website-login/error":
            code = (parse_qs(u.query).get("error_code") or [""])[0]
            return code if re.fullmatch(r"[0-9-]{1,12}", code) else "unknown"
        # Captcha and login pages have no platform code; name them instead.
        return "captcha" if path.startswith("/website-login/captcha") else "login"

    async def _page_signals(self):
        # XHS redirects throttled or challenged documents to static risk pages.
        # They never emit a business response, so they must not look like a
        # timeout or keep the run retrying into more challenges.
        if code := self._security_error_code():
            if code == "captcha":
                raise CollectionError("verification_required", "Security page: captcha")
            if code == "login":
                raise CollectionError("auth_expired", "Security page: login")
            try:
                check_response(200, {"code": code, "success": False})
            except CollectionError as exc:
                raise CollectionError(
                    exc.kind, f"Security error page, platform code {code}"
                ) from None
        for selector, kind in [
            (".captcha-container", "verification_required"),
            (".login-modal", "auth_expired"),
            (".login-container", "auth_expired"),
        ]:
            if await self.page.locator(selector).first.is_visible():
                raise CollectionError(kind)
        error = self.page.locator(".error-page, .access-error, .note-not-found").first
        if await error.is_visible():
            raise CollectionError("content_unavailable")

    async def _first_visible(self, *locators):
        for locator in locators:
            for index in range(min(await locator.count(), 30)):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        return None

    async def _latest_sort_control(self):
        return await self._first_visible(
            self.page.locator(LATEST_SORT_SELECTOR),
            self.page.get_by_text(LATEST_SORT_TEXT),
        )

    async def _wait_for_latest_sort_control(self, timeout):
        stop_at = min(self.budget.deadline, time.monotonic() + max(0, timeout))
        while time.monotonic() < stop_at:
            self.budget.check()
            await self._page_signals()
            if newest := await self._latest_sort_control():
                return newest
            await asyncio.sleep(0.2)
        return None

    async def _select_latest_sort(self, request):
        # Visible SSR controls may not have handlers yet, and the initial
        # general-sort prefetch can reset/cancel a concurrent filter change.
        # Wait for its response and queued business reads before applying latest.
        first = request.model_copy(update={"context": {"page": 1}})
        general = first.model_copy(update={"sort": "general"})
        ready_by = min(self.budget.deadline, time.monotonic() + self.budget.config.request_timeout)
        settle_at = time.monotonic() + 8
        while time.monotonic() < ready_by:
            self.budget.check()
            await self._page_signals()
            if any(matches(first, e, self.session.platform) for e in self.exchanges):
                return
            if any(matches(general, e, self.session.platform) for e in self.exchanges):
                break
            # A full page load can render the first results server-side and
            # issue no general read; the filter still works once cards show.
            if (time.monotonic() >= settle_at
                    and await self.page.locator("section.note-item").count()):
                break
            await asyncio.sleep(0.1)
        else:
            self.budget.check()
            raise CollectionError("network_failure", "Initial search response did not become ready")
        # Wait before opening the popover: it closes on its own, so waiting
        # between finding "最新" and clicking it would click nothing.
        await self._wait_request_interval()
        if newest := await self._latest_sort_control():
            self._start_burst(1, {Operation.SEARCH})
            await newest.click(force=True)
            return

        # The v2 search page puts recency under a popover. Depending on the
        # rollout it opens on hover or click and can be an icon-only container.
        trigger_stop = min(
            self.budget.deadline, time.monotonic() + self.budget.config.request_timeout
        )
        while time.monotonic() < trigger_stop:
            self.budget.check()
            await self._page_signals()
            if newest := await self._latest_sort_control():
                self._start_burst(1, {Operation.SEARCH})
                await newest.click(force=True)
                return
            if await self._first_visible(
                self.page.locator(
                    ".filter.ai-chat-filter, [class~='filter'], "
                    "[class*='filter'], [class*='sort']"
                ),
                self.page.get_by_text(SORT_MENU_TEXT),
            ):
                break
            await asyncio.sleep(0.2)

        filter_triggers = self.page.locator(
            ".filter.ai-chat-filter, [class~='filter'], "
            "[class*='filter'], [class*='sort']"
        )
        # Text such as "综合" also labels category chips that narrow the
        # results; fall back to text triggers only without a filter control.
        trigger_groups = (
            (filter_triggers,) if await self._first_visible(filter_triggers)
            else (self.page.get_by_text(SORT_MENU_TEXT),)
        )
        # An SSR trigger is visible before hydration binds its handlers, so a
        # single pass can miss the popover; retry within the request window.
        while True:
            attempted = 0
            for triggers in trigger_groups:
                for index in range(min(await triggers.count(), 20)):
                    trigger = triggers.nth(index)
                    if not await trigger.is_visible():
                        continue
                    attempted += 1
                    try:
                        await trigger.hover(timeout=1_500)
                        if newest := await self._wait_for_latest_sort_control(0.8):
                            self._start_burst(1, {Operation.SEARCH})
                            await newest.click(force=True)
                            return
                        await trigger.click(force=True, timeout=1_500)
                        if newest := await self._wait_for_latest_sort_control(1.2):
                            self._start_burst(1, {Operation.SEARCH})
                            await newest.click(force=True)
                            return
                    except PlaywrightError:
                        continue
                    if attempted >= 8:
                        break
                if attempted >= 8:
                    break
            if time.monotonic() >= trigger_stop:
                break
            # Reset hover state so the next pass re-enters the trigger.
            await self.page.mouse.move(0, 0)
            await asyncio.sleep(1)
            self.budget.check()

        controls = self.page.locator("button, a, [role='button']")
        visible = []
        for index in range(min(await controls.count(), 40)):
            control = controls.nth(index)
            if await control.is_visible():
                text = (await control.inner_text()).strip()
                if text:
                    visible.append(text[:80])
        sort_controls = await self.page.evaluate(
            """() => Array.from(document.querySelectorAll('[class*="filter"], [class*="sort"]'))
                .slice(0, 40).map(el => ({
                    tag: String(el.tagName || '').toLowerCase(),
                    class_name: String(el.className || '').slice(0, 120),
                    text: String(el.innerText || '').trim().slice(0, 40),
                    visible: Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                }))"""
        )
        # Retryable: the popover can fail to open in one page state; a fresh
        # page retries it, and the diagnostic below still shows a real change.
        error = CollectionError("network_failure", "Latest-sort control not found")
        error.sample_ref = self.samples.write(
            {
                "operation": "search",
                "source": "page_dom_diagnostic",
                "keyword": request.keyword,
            },
            0,
            {
                "requested_sort": "latest",
                "page_path": urlsplit(self.page.url).path,
                "query_keys": sorted(parse_qs(urlsplit(self.page.url).query)),
                "visible_controls": visible[:30],
                "sort_controls": sort_controls,
            },
        )
        raise error

    def _search_request(self, request, keyword):
        return request.model_copy(update={
            "operation": Operation.SEARCH, "keyword": keyword,
            "content_id": "", "root_id": "", "context": {},
        })

    def _begin_generation(self, *, keep_results=False):
        self.generation += 1
        self.page_key = None
        # Generations sharing one result document (its note overlays). A
        # result page the list requested in any of them is still current.
        if keep_results:
            self.result_generations.add(self.generation)
        else:
            self.result_generations = {self.generation}
        # Opening a note overlay keeps the result document: result pages the
        # list already loaded (prefetched page 2) will not be requested again.
        kept = [e for e in self.exchanges
                if keep_results and operation_for_path(urlsplit(e.url).path) == Operation.SEARCH]
        self.exchanges.clear()
        self.exchanges.extend(kept)
        self.capture_errors.clear()
        self.canceled_exchanges.clear()
        self.identity_error = None

    def _retire_stale_business(self):
        # Chromium may not emit completion for a paused fetch from a replaced
        # document or closed note overlay. Retire its slot; retain its
        # generation so late response callbacks cannot become current.
        for pending, generation in list(self.pending_business.items()):
            if generation != self.generation:
                self._release_request(pending, forget_generation=False)

    async def _navigate(self, request):
        if request.operation == Operation.SEARCH:
            key = ("search", request.keyword, request.sort)
            if self.page_key == key:
                return False
            if self.overlay_note and self.search_key == key:
                await self._close_overlay()
                if self.search_key == key:
                    self.page_key = key
                    return False
            return await self._search_from_home(request, key)
        else:
            key = ("note", request.content_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", request.content_id):
                raise CollectionError("invalid_input", "Invalid content ID")
            if self.page_key == key:
                return False
            # XHS throttles directly loaded note documents (300013) long before
            # the in-page note overlay; open notes from their search results.
            if await self._open_from_search(request):
                return True
            if request.input.get("search_keyword") and not request.input.get("targeted_post"):
                # A direct note document is what XHS throttles (300013) and it
                # stops the whole run; skip only this note instead.
                self._note_event("note_card_missing", request)
                raise CollectionError(
                    "note_card_missing", "Note is no longer in its search results"
                )
            params = {"xsec_source": request.input.get("xsec_source") or "pc_search"}
            if token := request.input.get("xsec_token"):
                params["xsec_token"] = token
            url = self.site.web_origin + "/explore/" + request.content_id + "?" + urlencode(params)
            self._note_event("note_direct_navigation", request)
        await self._load_document(url)
        self.page_key = key
        return True

    async def _load_document(self, url):
        self._begin_generation()
        self.search_key = self.overlay_note = None
        self.document_ready = False
        # SSR data can be usable while deferred scripts still delay DOMContentLoaded.
        # The fetch loop owns data/identity/risk readiness, not the document event.
        await self.page.goto(url, wait_until="commit")
        self._retire_stale_business()
        await self._audit_environment()

    async def _wait_for_app(self, stop_at):
        # Vue mounts on #app once scripts ran; until then Enter does nothing.
        while time.monotonic() < stop_at:
            self.budget.check()
            await self._page_signals()
            if await self.page.evaluate("""() => document.readyState === 'complete'
                && (!document.querySelector('#app') || !!document.querySelector('#app').__vue_app__)"""):
                return
            await asyncio.sleep(0.3)

    async def _search_from_home(self, request, key):
        # Type into the home page search box like a user; its in-page route
        # change is not challenged the way a loaded search URL is.
        await self._load_document(self.site.web_origin + "/explore")
        box = self.page.locator(SEARCH_BOX_SELECTOR)
        # A cold browser downloads and renders the app through the proxy; give
        # the home page longer than one request window to become interactive.
        stop_at = min(
            self.budget.deadline,
            time.monotonic() + max(SEARCH_BOX_SECONDS, self.budget.config.request_timeout),
        )
        field = None
        while field is None:
            self.budget.check()
            await self._page_signals()
            field = await self._first_visible(box)
            if field is None:
                if time.monotonic() >= stop_at:
                    raise CollectionError("schema_changed", "Home search box not found")
                await asyncio.sleep(0.2)
        await self._wait_for_app(stop_at)
        await self._wait_request_interval()
        self._begin_generation()
        # The SSR box is visible before hydration binds Enter; resubmit
        # rather than waiting out the whole window on one ignored Enter.
        for attempt in range(SEARCH_SUBMITS):
            self._start_burst(2, {Operation.SEARCH}, target_first=False)
            await field.click()
            await field.fill("")
            await field.type(request.keyword, delay=120)
            await self.page.keyboard.press("Enter")
            submit_by = time.monotonic() + 8
            while urlsplit(self.page.url).path.rstrip("/") not in RESULT_PATHS:
                self.budget.check()
                await self._page_signals()
                if time.monotonic() >= min(stop_at, submit_by):
                    break
                await asyncio.sleep(0.2)
            else:
                break
            if time.monotonic() >= stop_at or attempt == SEARCH_SUBMITS - 1:
                raise CollectionError("network_failure", "Search results did not open")
        self._retire_stale_business()
        self.page_key = self.search_key = key
        if request.sort == "latest":
            await self._select_latest_sort(request)
        return True

    def _note_event(self, kind, request, **data):
        if hasattr(self.budget, "store"):
            self.budget.store.event(
                self.budget.run_id, kind, {"content_id": request.content_id, **data}
            )

    async def _open_from_search(self, request):
        keyword = request.input.get("search_keyword")
        if request.input.get("targeted_post") or not (self.search_key or keyword):
            return False
        for reload in (False, True):
            if reload or (keyword and (not self.search_key or self.search_key[1] != keyword)):
                # Each reload is a search document plus search reads; repeated
                # reloads escalated to a captcha in testing, so cap them.
                if not keyword or self.search_reloads >= MAX_SEARCH_RELOADS:
                    return False
                self.search_reloads += 1
                search = self._search_request(request, keyword)
                # Force a fresh result document rather than reusing this one.
                self.page_key = self.search_key = self.overlay_note = None
                await self._navigate(search)
                await self._wait_for_results(search)
            await self._close_overlay()
            await self._page_signals()
            await self._ensure_on_results()
            card = None
            if self.search_key:
                card = await self._find_card(request.content_id, grow_pages=RELOAD_GROW_PAGES if reload else 0)
                if card is None:
                    self._note_event(
                        "note_card_scan_failed", request, reloaded=reload,
                        page=await self._list_snapshot(),
                    )
            if card:
                await self._wait_request_interval()
                self._begin_generation(keep_results=True)
                self._start_burst(OVERLAY_BURST_REQUESTS, {Operation.DETAIL, Operation.COMMENTS})
                await card.click()
                self._retire_stale_business()
                self.page_key = ("note", request.content_id)
                self.overlay_note = request.content_id
                self._note_event("note_opened_from_search", request, reloaded=reload)
                return True
        return False

    async def _wait_for_results(self, search):
        # Cards of the requested sort render only after its first page returns.
        first = search.model_copy(update={"context": {"page": 1}})
        stop_at = min(self.budget.deadline, time.monotonic() + self.budget.config.request_timeout)
        while time.monotonic() < stop_at:
            self.budget.check()
            await self._page_signals()
            if any(matches(first, e, self.session.platform) for e in self.exchanges):
                await asyncio.sleep(1)
                return
            await asyncio.sleep(0.2)
        self.budget.check()

    def _awaiting_target(self, generation):
        burst = self.burst
        return bool(
            burst and burst["generation"] == generation == self.generation
            and not burst["target_admitted"] and time.monotonic() <= burst["until"]
        )

    def _in_burst(self, generation, request=None):
        burst = self.burst
        operation = operation_for_path(urlsplit(request.url).path) if request else None
        return bool(
            burst and burst["generation"] == generation == self.generation
            and burst["left"] > 0 and time.monotonic() <= burst["until"]
            and (not burst["operations"] or operation in burst["operations"])
        )

    def _start_burst(self, requests, operations=None, *, target_first=True):
        target = self.current_request
        self.burst = {
            "generation": self.generation,
            "until": time.monotonic() + ACTION_BURST_SECONDS,
            "left": requests,
            # Only reads this action is expected to issue; incidental ones (a
            # result page loaded by scrolling a card into view) stay paced.
            "operations": set(operations or ([target.operation] if target else [])),
            # Without a current target (or when the action's first read is a
            # prerequisite of the target, like the general search before the
            # latest filter) every read of the action is equal.
            "target_admitted": target is None or not target_first,
        }

    async def _paced_action(self, requests=ACTION_BURST_REQUESTS):
        await self._wait_request_interval()
        self._start_burst(requests)

    async def _wait_request_interval(self):
        ready_at = getattr(self.budget, "last_request", 0) + self.budget.config.min_interval
        while time.monotonic() < ready_at:
            self.budget.check()
            await asyncio.sleep(min(0.1, ready_at - time.monotonic()))

    async def _find_card(self, content_id, *, grow_pages=0):
        # Cards carry their note ID; links cover older and other card layouts.
        cards = self.page.locator(
            f'section.note-item[data-note-id="{content_id}"] a.cover, '
            f'section.note-item a.cover[href*="/{content_id}"], '
            f'a[href*="/search_result/{content_id}"], a[href*="/explore/{content_id}"]'
        )

        async def visible_card():
            for index in range(min(await cards.count(), 5)):
                if await cards.nth(index).is_visible():
                    return cards.nth(index)
            return None

        async def step():
            # Returns [scroll offset, bottom of the viewport, list height].
            return await self.page.evaluate(RESULTS_SCROLL_JS, "page")

        # The result list is virtual: scan what is already loaded, from here
        # to its end and then from the top back to here. Reaching the end
        # loads another result page (a business read), so only a freshly
        # reloaded list may extend the scan, by a few pages.
        if card := await visible_card():
            return card
        # Closing a heavy overlay can clear the list while the page reloads
        # its results; an empty list is not a missing card.
        items = self.page.locator("section.note-item")
        for _ in range(int(EMPTY_LIST_SECONDS / 0.3)):
            if await items.count():
                break
            self.budget.check()
            await self._page_signals()
            await asyncio.sleep(0.3)
        if card := await visible_card():
            return card
        origin, _, limit = await self.page.evaluate(RESULTS_SCROLL_JS, None)
        for _ in range(60 if grow_pages else FORWARD_SCAN_STEPS):
            self.budget.check()
            _, bottom, height = await step()
            await asyncio.sleep(0.3)
            if card := await visible_card():
                return card
            if bottom >= limit - 5:
                if grow_pages <= 0:
                    break
                grow_pages -= 1
                for _ in range(10):
                    if height > limit:
                        break
                    await asyncio.sleep(0.3)
                    height = (await self.page.evaluate(RESULTS_SCROLL_JS, None))[2]
                if height <= limit:
                    break
                limit = height
        await self.page.evaluate(RESULTS_SCROLL_JS, "top")
        await asyncio.sleep(0.3)
        for _ in range(60):
            self.budget.check()
            if card := await visible_card():
                return card
            position, bottom, _ = await step()
            await asyncio.sleep(0.3)
            if position >= origin or bottom >= limit - 5:
                return await visible_card()
        return None

    async def _list_snapshot(self):
        try:
            return await self.page.evaluate("""() => {
                const ids = [...document.querySelectorAll('section.note-item')].map(s => {
                    const a = s.querySelector('a.cover, a[href*="/search_result/"], a[href*="/explore/"]');
                    return a ? (a.getAttribute('href') || '').split('?')[0].split('/').pop() : null;
                });
                return {path: location.pathname, scroll_y: Math.round(window.scrollY),
                        height: document.documentElement.scrollHeight, cards: ids.length,
                        ids: ids.filter(Boolean)};
            }""")
        except PlaywrightError:
            return {"unavailable": True}

    async def _ensure_on_results(self):
        # A late router back can still move the page after a close looked
        # done; let it settle, then step forward from the home page.
        if not self.search_key:
            return
        await asyncio.sleep(1.5)
        path = urlsplit(self.page.url).path.rstrip("/")
        if path in RESULT_PATHS:
            return
        if path == "/explore":
            await self.page.go_forward()
            for _ in range(30):
                if urlsplit(self.page.url).path.rstrip("/") in RESULT_PATHS:
                    return
                await asyncio.sleep(0.1)
        self.search_key = None
        if hasattr(self.budget, "store"):
            self.budget.store.event(self.budget.run_id, "result_page_lost", {
                "page_path": urlsplit(self.page.url).path,
            })

    async def _close_overlay(self):
        note, self.overlay_note = self.overlay_note, None
        if not note:
            return

        def path():
            return urlsplit(self.page.url).path.rstrip("/")

        async def settle(seconds):
            for _ in range(int(seconds * 10)):
                if path() in RESULT_PATHS:
                    return True
                await asyncio.sleep(0.1)
            return False

        # The result page binds Escape (and history.back()) to "back" even
        # without an overlay; only close what is actually open, and step
        # forward again if it overshot to the home page.
        if path().endswith("/" + note):
            await self.page.keyboard.press("Escape")
            # A heavy overlay can take seconds to run Escape's router back;
            # a second back from here would leave the result page.
            if await settle(12):
                return
            if path().endswith("/" + note):
                await self.page.go_back()
                if await settle(3):
                    return
        elif path() in RESULT_PATHS:
            return
        if path() == "/explore":
            await self.page.go_forward()
            if await settle(3):
                return
        # The result document is no longer trustworthy; reload it next time.
        self.search_key = None
        if hasattr(self.budget, "store"):
            self.budget.store.event(self.budget.run_id, "note_overlay_close_failed", {
                "content_id": note, "page_path": urlsplit(self.page.url).path,
            })

    def _document_loaded(self, *_):
        self.document_ready = True

    async def _wait_for_note_startup(self, request):
        # SSR detail can return long before the comment application starts.
        # Give startup and the subsequent response separate bounded windows.
        stop_at = min(self.budget.deadline, time.monotonic() + self.budget.config.request_timeout)
        while not self.document_ready:
            self.budget.check()
            await self._page_signals()
            candidates = self.exchanges + [e for e, _ in self.capture_errors]
            candidates += [self._exchange(r) for r, g in self.pending_business.items()
                           if g == self.generation]
            if any(matches(request, e, self.session.platform) for e in candidates):
                return
            if time.monotonic() >= stop_at:
                self.budget.check()
                raise CollectionError("network_failure", "Note application startup timed out")
            await asyncio.sleep(0.1)

    async def _scroll_comments(self):
        await self.page.evaluate("""() => {
            const el = document.querySelector('.note-scroller') || document.querySelector('.comments-container');
            // The next comment page loads near the end of the list; step
            // there directly so each paced action can trigger it.
            if (el) el.scrollTop = Math.max(el.scrollTop + 600, el.scrollHeight - el.clientHeight * 1.2);
            else window.scrollBy(0, 700);
        }""")

    async def _reply_parent(self, request):
        selectors = (
            f'[id="comment-{request.root_id}"]',
            f'[data-comment-id="{request.root_id}"]',
            f'[data-id="{request.root_id}"]',
            f'[id="{request.root_id}"]',
        )
        for selector in selectors:
            found = self.page.locator(selector).first
            if await found.count():
                block = found.locator(
                    "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' parent-comment ')][1]"
                )
                return block if await block.count() else found

        hint = request.input.get("root_comment") or {}
        root_text = str(hint.get("text") or "").strip()
        if not root_text:
            return None
        matches = self.page.get_by_text(root_text, exact=True)
        for index in range(min(await matches.count(), 5)):
            found = matches.nth(index)
            block = found.locator(
                "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' parent-comment ')][1]"
            )
            if await block.count():
                return block
        return None

    async def _reply_control(self, parent):
        controls = parent.locator(REPLY_CONTROL_SELECTOR).filter(has_text=REPLY_EXPAND_TEXT)
        for index in range(min(await controls.count(), 20)):
            control = controls.nth(index)
            if await control.is_visible():
                return control
        return None

    async def _reply_failure(self, request):
        hint = request.input.get("root_comment") or {}
        controls = self.page.locator(REPLY_CONTROL_SELECTOR).filter(has_text=REPLY_EXPAND_TEXT)
        visible = []
        for index in range(min(await controls.count(), 20)):
            control = controls.nth(index)
            if await control.is_visible():
                visible.append((await control.inner_text()).strip()[:120])
        error = CollectionError(
            "unsupported_operation", "Reply expansion is not available for this root"
        )
        error.sample_ref = self.samples.write(
            {
                "operation": "replies",
                "source": "page_dom_diagnostic",
                "content_id": request.content_id,
                "root_id": request.root_id,
            },
            0,
            {
                "root_text_available": bool(str(hint.get("text") or "").strip()),
                "visible_reply_controls": visible,
            },
        )
        return error

    async def _action(self, request):
        self.budget.check()
        if request.operation == Operation.SEARCH:
            await self.page.evaluate(RESULTS_SCROLL_JS, "page")
        elif request.operation == Operation.COMMENTS:
            await self._scroll_comments()
        elif request.operation == Operation.REPLIES:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", request.root_id):
                raise CollectionError("invalid_input", "Invalid root comment ID")
            parent = await self._reply_parent(request)
            if parent is None:
                await self._scroll_comments()
                return False
            await parent.scroll_into_view_if_needed()
            expand = await self._reply_control(parent)
            if expand is None:
                # Current XHS renders the control after the root row and may use
                # .reply-more instead of .show-more. Give bounded rendering and
                # scrolling retries a chance before declaring the scope unsupported.
                await self._scroll_comments()
                return False
            await expand.click()
            return True
        return True

    def _business_pending(self):
        return (
            self.generation in self.pending_business.values()
            or self.generation in self.pending_captures.values()
        )

    async def _detail_state(self, request):
        u, expected = urlsplit(self.page.url), urlsplit(self.site.web_origin)
        if (u.scheme, u.netloc) != (expected.scheme, expected.netloc):
            return None
        return await self.page.evaluate(
            """id => {
                const unwrap = value => {
                    for (let i = 0; value && i < 3; i++) {
                        if (value._rawValue !== undefined) value = value._rawValue;
                        else if (value._value !== undefined) value = value._value;
                        else if (value.__v_isRef && value.value !== undefined) value = value.value;
                        else break;
                    }
                    return value;
                };
                const map = unwrap(window.__INITIAL_STATE__?.note?.noteDetailMap);
                const entry = unwrap(map?.[id]);
                const note = unwrap(entry?.note || entry?.noteCard || entry);
                const noteId = note?.noteId || note?.note_id || note?.id;
                return noteId === id ? JSON.parse(JSON.stringify(note)) : null;
            }""",
            request.content_id,
        )

    async def _record_failure(self, request, error):
        if not self.page or (
            error.kind not in {"network_failure", "abnormal_empty", "schema_changed"}
            and not self._security_error_code()
        ):
            return
        u = urlsplit(self.page.url)
        diagnostic = {
            "error_kind": error.kind, "host": u.hostname, "page_path": u.path,
            "platform_error_code": self._security_error_code(),
            "query_keys": sorted(parse_qs(u.query)), "generation": self.generation,
            "business_pending": self._business_pending(),
            "identity_verified": self.identity_verified,
            "network_events": list(self.network_events),
            "business_events": list(self.business_events),
            "document_ready": self.document_ready,
            "source_sample_ref": error.sample_ref,
        }
        try:
            async with asyncio.timeout(2):
                diagnostic["page_state"] = await self.page.evaluate(
                    """() => ({ready: document.readyState,
                        state_keys: Object.keys(window.__INITIAL_STATE__ || {}),
                        note_ids: Object.keys(window.__INITIAL_STATE__?.note?.noteDetailMap || {})
                    })"""
                )
        except (PlaywrightError, TimeoutError):
            diagnostic["page_state"] = {"unavailable": True}
        error.sample_ref = self.samples.write(
            {"operation": str(request.operation), "source": "page_failure_diagnostic",
             "content_id": request.content_id, "keyword": request.keyword},
            0, diagnostic,
        )
        try:
            async with asyncio.timeout(2):
                screenshot = self.samples.directory / (Path(error.sample_ref).stem + ".png")
                await self.page.screenshot(path=str(screenshot), timeout=1500)
                screenshot.chmod(0o600)
        except (PlaywrightError, TimeoutError, OSError):
            pass

    def _detail_result(self, request, raw, item):
        sample = self.samples.write(
            {"operation": "detail", "source": "page_state", "id": request.content_id},
            200, raw,
        )
        return PageResult(
            items=[item], response_has_more=False, sample_ref=sample,
            stop_reason=None if item.data["detail_complete"] else "incomplete_fields",
        )

    async def fetch(self, request):
        await self._start()
        self.current_request = request
        try:
            self.budget.check()
            if self.page_key and (result := self._take(request)):
                await self._page_signals()
                self.budget.check()
                return result
            navigated = await self._navigate(request)
            if request.operation in {Operation.COMMENTS, Operation.REPLIES}:
                await self._wait_for_note_startup(request)
            stop_at = min(
                self.budget.deadline, time.monotonic() + self.budget.config.request_timeout
            )
            next_action = time.monotonic() + (1 if navigated else 0)
            actions = 0
            reply_action_succeeded = False
            incomplete_detail = None
            while time.monotonic() < stop_at:
                self.budget.check()
                await self._page_signals()
                self.budget.check()
                if result := self._take(request):
                    return result
                if request.operation == Operation.DETAIL and (
                    not self.session.config.expected_user_id or self.identity_verified
                ):
                    raw = await self._detail_state(request)
                    self.budget.check()
                    if (
                        isinstance(raw, dict)
                        and (raw.get("noteId") or raw.get("note_id") or raw.get("id"))
                        == request.content_id
                    ):
                        item = content_item(
                            raw, detail=True, token=request.input.get("xsec_token"),
                            source=request.input.get("xsec_source"),
                            platform=self.session.platform,
                        )
                        if item.data["detail_complete"]:
                            return self._detail_result(request, raw, item)
                        # A newly committed document can contain only the SSR
                        # shell. Give hydration/the feed response its full window.
                        incomplete_detail = (raw, item)
                elif (
                    time.monotonic() >= next_action and actions < 8
                    and not self._business_pending()
                    and not self._list_answer_settling(request)
                    # The first search page is driven by navigation/sort selection.
                    # Scrolling before it arrives queues unrelated pages and sorts.
                    and (request.operation != Operation.SEARCH
                         or int(request.context.get("page", 1)) > 1)
                ):
                    await self._paced_action()
                    action_succeeded = await self._action(request)
                    actions += 1
                    reply_action_succeeded = reply_action_succeeded or bool(action_succeeded)
                    if (
                        request.operation == Operation.REPLIES
                        and actions >= 8
                        and not reply_action_succeeded
                    ):
                        raise await self._reply_failure(request)
                    next_action = time.monotonic() + max(1, self.budget.config.min_interval)
                await asyncio.sleep(0.1)
            self.budget.check()
            # Settling only prefers a later answer; at the end of the window
            # an answer that arrived is still better than none.
            if result := self._take(request, settle=False):
                return result
            if incomplete_detail is not None:
                return self._detail_result(request, *incomplete_detail)
            if any(matches(request, e, self.session.platform) for e in self.canceled_exchanges):
                raise CollectionError("network_failure", "Page canceled the matching request without replacement")
            if self.session.config.expected_user_id and not self.identity_verified:
                raise CollectionError(
                    "identity_unverified", "No matching user/me identity was observed"
                )
            if (request.operation == Operation.DETAIL and request.input.get("targeted_post")
                    and not request.input.get("xsec_token")):
                raise CollectionError(
                    "missing_access_context",
                    f"页面未能补齐访问参数，请提供完整{self.site.display_name}分享链接",
                )
            if (request.operation in {Operation.COMMENTS, Operation.REPLIES}
                    and request.context.get("cursor")):
                # A later comment page that never loads ends this note's
                # comments; captcha/throttle pages are detected separately.
                raise CollectionError("page_stalled", "Next comment page did not load")
            raise CollectionError(
                "abnormal_empty", "No matching response in bounded page observation"
            )
        except PlaywrightError as exc:
            self.budget.check()
            error = CollectionError("network_failure", type(exc).__name__)
            await self._record_failure(request, error)
            self.page_key = None
            raise error from exc
        except CollectionError as exc:
            await self._record_failure(request, exc)
            if exc.kind == "network_failure":
                self.page_key = None
            raise

    async def close(self):
        self.closing = True
        self.generation += 1
        if self.context and not self.managed_browser:
            await self.context.close()
        if self.response_tasks:
            # Response body reads are no longer useful once collection stops.
            # Cancel them first so a stalled streaming response cannot hold the
            # browser, account resource, and worker slot indefinitely.
            response_tasks = list(self.response_tasks)
            for task in response_tasks:
                task.cancel()
            await asyncio.gather(*response_tasks, return_exceptions=True)
        if self.managed_browser:
            await self.managed_browser.close(suppress_stop_errors=True)
        if self.playwright:
            await self.playwright.stop()
        if self.proxy_bridge:
            await self.proxy_bridge.close()
        if hasattr(self.budget, "store"):
            self.budget.store.event(
                self.budget.run_id,
                "browser_network_summary",
                {
                    "requests_observed": self.total_requests,
                    "requests_blocked": self.blocked_requests,
                    "public_asset_cache_hits": self.asset_cache.hits,
                    "public_asset_cache_bytes": self.asset_cache.size,
                    "business_events": list(self.business_events),
                    "service_workers": "blocked",
                    "websockets": "blocked",
                    "business_requests": "collection endpoints only; persisted on admission",
                },
            )
        self.asset_cache.clear()
