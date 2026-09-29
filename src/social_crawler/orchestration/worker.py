import asyncio
import os
import time
from pathlib import Path

from social_crawler.adapters.targets import resolve_target
from social_crawler.adapters.xhs.sites import is_xhs_api_adapter, is_xhs_platform
from social_crawler.domain.models import CollectionError, RunConfig, TaskRequest
from social_crawler.domain.targets import valid_token
from social_crawler.environments.managed_browser import is_managed_browser_provider
from social_crawler.environments.network import observe_dual_exit, observe_http_transport_exit
from social_crawler.orchestration.budget import RequestBudget
from social_crawler.orchestration.media import MediaDownloader

DEFAULT_CLEANUP_TIMEOUT = 15.0


async def _close_with_timeout(resource, *, timeout: float):
    """Bound cleanup without letting a transport keep a worker forever."""
    task = asyncio.create_task(resource.close())
    done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
    if task not in done:
        task.cancel()
        # Give ordinary asyncio/Playwright waits one loop turn to observe the
        # cancellation. Do not await indefinitely: terminal run state and the
        # worker slot must not depend on a broken transport teardown.
        await asyncio.wait({task}, timeout=0.1)
        return "cleanup_timeout", {"timeout_seconds": timeout}
    try:
        task.result()
    except asyncio.CancelledError:
        return "cleanup_failed", {"error_type": "CancelledError"}
    except Exception as exc:
        return "cleanup_failed", {"error_type": type(exc).__name__}
    return None


async def run_worker(
    store,
    run_id,
    adapter_factory,
    *,
    session=None,
    resume=False,
    reset_contexts=False,
    extra_requests=0,
    extra_seconds=0,
    preserve_cancel=False,
    network_observer=None,
    artifact_root=None,
    media_downloader=None,
    cleanup_timeout=DEFAULT_CLEANUP_TIMEOUT,
):
    epoch = store.start(
        run_id,
        binding=session.binding if session else "offline",
        resume=resume,
        reset_contexts=reset_contexts,
        extra_requests=extra_requests,
        extra_seconds=extra_seconds,
        preserve_cancel=preserve_cancel,
    )
    run = store.get_run(run_id)
    config = RunConfig.model_validate(run["config"])
    budget = RequestBudget(store, run_id, epoch, config, session)
    adapter = None
    started = time.monotonic()
    reason, outcome = None, None
    task = None
    preferred_content_id = None
    reuse_note_page = is_xhs_platform(config.platform) and config.adapter == "browser"
    try:
        if session is not None and store.get_run(run_id)["mode"] == "online":
            observer = network_observer or (
                observe_http_transport_exit
                if is_xhs_api_adapter(config.adapter)
                and not is_managed_browser_provider(session.config.browser_provider)
                else observe_dual_exit
            )
            observation = await observer(session)
            store.verify_run_network(run_id, observation)
        adapter = adapter_factory(budget)
        if config.download_media and run["mode"] == "online" and media_downloader is None:
            root = artifact_root or os.environ.get("CRAWLER_ARTIFACTS_DIR", "artifacts")
            browser_context = None
            if (
                session is not None
                and is_managed_browser_provider(session.config.browser_provider)
                and not is_xhs_api_adapter(config.adapter)
            ):
                # The adapter opens the profile lazily on its first request. Resolve
                # the context at download time so media uses the exact same browser
                # proxy and never falls back to the container's direct network.
                def browser_context():
                    return getattr(adapter, "context", None) or getattr(
                        getattr(adapter, "security", None), "context", None
                    )
            media_downloader = MediaDownloader(
                Path(root),
                config.platform,
                session,
                timeout=config.request_timeout,
                browser_context=browser_context,
            )
        while task := store.next_task(
            run_id, epoch, preferred_content_id=preferred_content_id,
            defer_search=reuse_note_page,
        ):
            budget.check()
            request = TaskRequest(
                operation=task["operation"],
                keyword=task["keyword"],
                sort=config.sort,
                content_id=task["content_id"],
                root_id=task["root_id"],
                context=task["state"]["context"],
                input=task["state"]["input"],
            )
            try:
                for attempt in range(config.network_retries + 1):
                    operation_started = time.time()
                    operation_clock = time.monotonic()
                    try:
                        remaining = max(0.01, budget.deadline - time.monotonic())
                        async with asyncio.timeout(remaining):
                            if request.operation == "resolve_target":
                                result = await resolve_target(
                                    request, config.platform, budget, session,
                                    offline=store.get_run(run_id)["mode"] == "offline",
                                    adapter=config.adapter,
                                )
                            else:
                                if (config.source_type == "posts" and is_xhs_platform(config.platform)
                                        and request.operation == "detail"
                                        and not valid_token(request.input.get("xsec_token"))):
                                    request.input.update(store.post_context(run_id, request.content_id))
                                    if (is_xhs_api_adapter(config.adapter)
                                            and store.get_run(run_id)["mode"] == "online"
                                            and not valid_token(request.input.get("xsec_token"))):
                                        raise CollectionError(
                                            "missing_access_context",
                                            "缺少 XHS / RedNote 访问参数，请提供完整分享链接或使用浏览器模式",
                                        )
                                result = await adapter.fetch(request)
                        budget.check()
                        store.record_operation(
                            run_id,
                            task["id"],
                            operation=str(request.operation),
                            attempt=attempt + 1,
                            started_at=operation_started,
                            duration_ms=(time.monotonic() - operation_clock) * 1000,
                            outcome="success",
                            item_count=len(result.items),
                            response_has_more=result.response_has_more,
                        )
                        break
                    except TimeoutError as exc:
                        store.record_operation(
                            run_id,
                            task["id"],
                            operation=str(request.operation),
                            attempt=attempt + 1,
                            started_at=operation_started,
                            duration_ms=(time.monotonic() - operation_clock) * 1000,
                            outcome="error",
                            error_kind="deadline",
                        )
                        raise CollectionError("deadline") from exc
                    except CollectionError as exc:
                        store.record_operation(
                            run_id,
                            task["id"],
                            operation=str(request.operation),
                            attempt=attempt + 1,
                            started_at=operation_started,
                            duration_ms=(time.monotonic() - operation_clock) * 1000,
                            outcome="error",
                            error_kind=exc.kind,
                        )
                        if exc.kind != "network_failure" or attempt == config.network_retries:
                            raise
                        store.event(
                            run_id, "network_retry", {"task_id": task["id"], "attempt": attempt + 1}
                        )
                        await asyncio.sleep(
                            min(2**attempt, max(0, budget.deadline - time.monotonic()))
                        )
                if media_downloader is not None and request.operation == "detail":
                    for item in result.items:
                        if item.kind != "content":
                            continue
                        downloads = await media_downloader.download(
                            item.id, item.data.get("media") or []
                        )
                        if downloads:
                            item.data["media_downloads"] = downloads
                            store.event(
                                run_id,
                                "media_downloaded",
                                {
                                    "content_id": item.id,
                                    "downloaded": sum(
                                        row["status"] == "downloaded" for row in downloads
                                    ),
                                    "failed": sum(row["status"] == "failed" for row in downloads),
                                    "bytes": sum(
                                        row.get("bytes", 0)
                                        for row in downloads
                                        if row["status"] == "downloaded"
                                    ),
                                },
                            )
                saved = store.commit_page(run_id, task["id"], epoch, result)
                if reuse_note_page:
                    preferred_content_id = request.content_id or None
                if saved["state"]["stop_reason"] in {
                    "no_progress",
                    "missing_cursor",
                    "missing_search_context",
                }:
                    # A broken pagination contract pauses this platform run for inspection.
                    raise CollectionError(saved["state"]["stop_reason"])
            except CollectionError as exc:
                store.fail_task(run_id, task["id"], epoch, exc)
                if exc.blocks_run or exc.kind in {
                    "request_budget",
                    "deadline",
                    "canceled",
                    "stale_worker",
                }:
                    raise
    except CollectionError as exc:
        reason, outcome = exc.kind, exc.outcome
        budget.halt(exc)
        if exc.blocks_run:
            store.record_risk(
                run_id,
                operation=task["operation"] if task else None,
                error=exc,
            )
        store.event(run_id, "run_stopped", {"kind": exc.kind, "retry_after": exc.retry_after})
    except (KeyboardInterrupt, asyncio.CancelledError):
        reason = "interrupted"
        budget.halt(CollectionError(reason))
    except Exception as exc:
        reason, outcome = "internal_error", "failed"
        budget.halt(CollectionError(reason))
        # No raw exception text: transports can embed Cookie-bearing request URLs.
        store.event(run_id, "internal_error", {"type": type(exc).__name__})
        raise
    finally:
        budget.halt(CollectionError(reason or "finished"))
        try:
            # Persist the terminal state before touching external transports.
            # Browser/CDP cleanup can block independently of collection state;
            # it must never leave a run looking active forever.
            if store.get_run(run_id)["epoch"] == epoch:
                store.finish(
                    run_id,
                    epoch,
                    elapsed=time.monotonic() - started,
                    reason=reason,
                    outcome=outcome,
                )
        finally:
            for name, resource in (
                ("adapter", adapter),
                ("media_downloader", media_downloader),
            ):
                if resource is None:
                    continue
                diagnostic = await _close_with_timeout(
                    resource, timeout=cleanup_timeout
                )
                if diagnostic is None:
                    continue
                kind, data = diagnostic
                try:
                    store.event(run_id, kind, {"resource": name} | data)
                except Exception:
                    # Cleanup diagnostics are best effort. A database/logging
                    # failure here must not undo the already-persisted outcome.
                    pass
    return store.get_run(run_id)
