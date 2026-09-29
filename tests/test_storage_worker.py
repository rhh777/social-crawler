import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fixture_adapter import FixtureAdapter
from sqlalchemy import select, update

from social_crawler.adapters.samples import Samples
from social_crawler.domain.models import CollectionError, Item, PageResult, RunConfig
from social_crawler.orchestration.budget import RequestBudget
from social_crawler.orchestration.report import export_run
from social_crawler.orchestration.worker import run_worker
from social_crawler.storage.store import (
    accounts,
    contents,
    merged,
    quota_buckets,
    quota_policies,
    quota_reservations,
    tasks,
)


def test_new_search_summary_preserves_prior_full_detail():
    result = merged(
        {"text": "full body", "detail_complete": True, "detail_observed": True},
        {
            "text": "excerpt",
            "detail_complete": False,
            "detail_observed": False,
            "metrics": {"likes": 2},
        },
    )
    assert result["text"] == "full body" and result["detail_complete"]
    assert result["metrics"] == {"likes": 2}


def configuration(**overrides):
    return RunConfig.model_validate(
        dict(
            platform="xhs",
            keywords=["one"],
            min_interval=0,
            content_limit=5,
            comment_limit=5,
            reply_limit=5,
        )
        | overrides
    )


def item(cid):
    return Item(id=cid, content_id=cid, kind="content", data={"title": cid})


def started(store, **overrides):
    run_id = store.create_run(configuration(**overrides), mode="offline", binding="original")
    epoch = store.start(run_id, binding="original")
    task = store.next_task(run_id, epoch)
    return run_id, epoch, task


@pytest.mark.parametrize("platform", ["xhs", "douyin"])
async def test_full_flow_deduplicates_and_keeps_keyword_hits(store, tmp_path, platform):
    run_id = store.create_run(
        configuration(platform=platform, keywords=["one", "two"]), mode="offline"
    )
    samples = Samples(tmp_path)
    await run_worker(store, run_id, lambda b: FixtureAdapter(platform, b, samples))
    report = export_run(store, run_id, tmp_path)
    assert report["status"] == "completed"
    assert report["automated_scope_gate"] is True
    assert report["requests"] == 19
    assert (report["contents"], report["comments_including_replies"], report["hits"]) == (3, 18, 6)
    assert report["online_stability"] == "not_validated"
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert all(c["observed_unique"] == 3 for c in coverage if c["operation"] == "replies")
    assert all(c["stop_reason"] == "exhausted" for c in coverage if c["operation"] == "search")
    assert "fixture-token" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


@pytest.mark.parametrize("stage", ["after_items", "before_commit"])
def test_transaction_rollback_includes_derived_tasks_and_checkpoint(store, stage):
    run_id, epoch, task = started(store)

    def crash(at):
        if at == stage:
            raise RuntimeError("simulated process failure")

    with pytest.raises(RuntimeError):
        store.commit_page(
            run_id,
            task["id"],
            epoch,
            PageResult(items=[item("n1")], next_context={"page": 2}, response_has_more=True),
            fault=crash,
        )
    snapshot = store.snapshot(run_id)
    assert not snapshot["items"] and not snapshot["hits"]
    assert len(snapshot["tasks"]) == 1
    assert snapshot["tasks"][0]["state"]["context"] == {}
    with store.engine.begin() as conn:
        assert not conn.execute(select(contents)).all()


def test_committed_batch_survives_crash_and_stale_epoch_cannot_write(store):
    run_id, epoch, task = started(store)
    page = PageResult(items=[item("n1")], next_context={"page": 2}, response_has_more=True)

    def crash(at):
        if at == "after_commit":
            raise RuntimeError("crash after commit")

    with pytest.raises(RuntimeError):
        store.commit_page(run_id, task["id"], epoch, page, fault=crash)
    resumed = store.start(run_id, binding="original", resume=True)
    checkpoint = store.next_task(run_id, resumed)
    assert checkpoint["state"]["context"] == {"page": 2}
    with pytest.raises(CollectionError, match="no longer owns"):
        store.commit_page(run_id, task["id"], epoch, page)
    store.commit_page(
        run_id,
        task["id"],
        resumed,
        PageResult(items=[item("n1"), item("n2")], response_has_more=False),
    )
    snapshot = store.snapshot(run_id)
    assert len(snapshot["items"]) == len(snapshot["hits"]) == 2
    assert len([t for t in snapshot["tasks"] if t["operation"] == "detail"]) == 2


def test_limit_does_not_claim_exhaustion(store):
    run_id, epoch, task = started(store, content_limit=1)
    result = store.commit_page(
        run_id,
        task["id"],
        epoch,
        PageResult(
            items=[item("n1"), item("n2")], next_context={"page": 2}, response_has_more=True
        ),
    )
    assert result["state"]["count"] == 1
    assert result["state"]["stop_reason"] == "limit"
    assert result["state"]["response_has_more"] is True
    assert len(store.snapshot(run_id)["hits"]) == 1


def test_unlimited_comments_continue_until_platform_exhaustion(store):
    run_id, epoch, search = started(
        store,
        content_limit=1,
        comment_limit=1,
        unlimited_comments=True,
    )
    store.commit_page(
        run_id,
        search["id"],
        epoch,
        PageResult(items=[item("n1")], response_has_more=False),
    )
    detail = store.next_task(run_id, epoch)
    store.commit_page(
        run_id,
        detail["id"],
        epoch,
        PageResult(
            items=[
                Item(
                    id="n1",
                    content_id="n1",
                    kind="content",
                    data={"title": "n1", "detail_complete": True},
                )
            ],
            response_has_more=False,
        ),
    )
    comment = store.next_task(run_id, epoch)
    assert comment["target"] == -1
    first = store.commit_page(
        run_id,
        comment["id"],
        epoch,
        PageResult(
            items=[
                Item(id="c1", content_id="n1", kind="comment"),
                Item(id="c2", content_id="n1", kind="comment"),
            ],
            next_context={"cursor": "next"},
            response_has_more=True,
        ),
    )
    assert first["status"] == "pending" and first["state"]["count"] == 2
    final = store.commit_page(
        run_id,
        comment["id"],
        epoch,
        PageResult(
            items=[Item(id="c3", content_id="n1", kind="comment")],
            response_has_more=False,
        ),
    )
    assert final["status"] == "completed"
    assert final["state"]["count"] == 3
    assert final["state"]["stop_reason"] == "exhausted"


def test_changed_binding_replays_duplicates_then_advances(store):
    run_id, epoch, task = started(store, content_limit=20)
    for page in range(1, 4):
        store.commit_page(
            run_id,
            task["id"],
            epoch,
            PageResult(
                items=[item(str(page))], next_context={"page": page + 1}, response_has_more=True
            ),
        )
    store.finish(run_id, epoch, elapsed=1, reason="interrupted")
    new_epoch = store.start(run_id, binding="replacement", resume=True)
    assert store.next_task(run_id, new_epoch)["state"]["context"] == {}
    for page in range(1, 5):
        result = store.commit_page(
            run_id,
            task["id"],
            new_epoch,
            PageResult(
                items=[item(str(page))], next_context={"page": page + 1}, response_has_more=True
            ),
        )
        assert result["status"] == "pending"
    assert result["state"]["count"] == 4


def test_reply_tasks_keep_root_hint_and_legacy_partial_is_enriched_on_resume(store):
    run_id, epoch, search = started(store, content_limit=1, comment_limit=1, reply_limit=3)
    store.commit_page(
        run_id,
        search["id"],
        epoch,
        PageResult(items=[item("n1")], response_has_more=False),
    )
    detail = store.next_task(run_id, epoch)
    # Browser note tasks reopen their search results instead of note documents.
    assert detail["state"]["input"]["search_keyword"] == search["keyword"]
    store.commit_page(
        run_id,
        detail["id"],
        epoch,
        PageResult(
            items=[
                Item(
                    id="n1",
                    content_id="n1",
                    kind="content",
                    data={"text": "note", "detail_complete": True},
                )
            ],
            response_has_more=False,
        ),
    )
    comment = store.next_task(run_id, epoch)
    assert comment["state"]["input"]["search_keyword"] == search["keyword"]
    store.commit_page(
        run_id,
        comment["id"],
        epoch,
        PageResult(
            items=[
                Item(
                    id="r1",
                    content_id="n1",
                    kind="comment",
                    data={
                        "text": "目标根评论",
                        "reply_count": 3,
                        "sub_has_more": True,
                        "sub_cursor": "s1",
                    },
                    children=[
                        Item(
                            id="s1",
                            content_id="n1",
                            root_id="r1",
                            kind="comment",
                            data={"text": "embedded"},
                        )
                    ],
                )
            ],
            response_has_more=False,
        ),
    )
    reply = store.next_task(run_id, epoch)
    assert reply["state"]["input"]["search_keyword"] == search["keyword"]
    assert reply["state"]["input"]["root_comment"] == {
        "id": "r1",
        "text": "目标根评论",
        "reply_count": 3,
        "sub_cursor": "s1",
    }
    assert reply["state"]["context"] == {"cursor": "s1"}
    store.fail_task(run_id, reply["id"], epoch, CollectionError("unsupported_operation"))
    store.finish(run_id, epoch, elapsed=1)

    with store.engine.begin() as conn:
        state = dict(conn.execute(select(tasks.c.state).where(tasks.c.id == reply["id"])).scalar())
        state["input"] = dict(state["input"])
        state["input"].pop("root_comment")
        conn.execute(update(tasks).where(tasks.c.id == reply["id"]).values(state=state))

    resumed = store.start(run_id, binding="original", resume=True)
    enriched = store.next_task(run_id, resumed)
    assert enriched["state"]["input"]["root_comment"] == {
        "id": "r1",
        "text": "目标根评论",
        "reply_count": 3,
        "sub_cursor": "s1",
    }


async def test_budget_stop_preserves_items_and_resume_requires_explicit_increase(store, tmp_path):
    run_id = store.create_run(configuration(max_requests=1), mode="offline")

    def factory(b):
        return FixtureAdapter("xhs", b, Samples(tmp_path))

    result = await run_worker(store, run_id, factory)
    assert result["status"] == "partial" and result["requests"] == 1
    assert len(store.snapshot(run_id)["items"]) == 2
    result = await run_worker(store, run_id, factory, resume=True)
    assert result["requests"] == 1 and result["status"] == "partial"
    result = await run_worker(store, run_id, factory, resume=True, extra_requests=40)
    assert result["status"] == "completed"
    assert result["config"]["content_limit"] == 5


class FaultAdapter:
    def __init__(self, budget, kind):
        self.budget, self.kind = budget, kind
        self.calls = 0
        self.closed = False

    async def fetch(self, _request):
        await self.budget.admit("search")
        self.calls += 1
        if self.calls == 1:
            return PageResult(items=[item("n1")], next_context={"page": 2}, response_has_more=True)
        if self.kind == "no_progress":
            return PageResult(items=[item("n1")], next_context={"page": 2}, response_has_more=True)
        if self.kind == "missing_cursor":
            return PageResult(items=[], response_has_more=True)
        raise CollectionError(self.kind, retry_after=30)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "kind, expected_status",
    [
        ("rate_limit", "blocked"),
        ("auth_expired", "blocked"),
        ("verification_required", "blocked"),
        ("schema_changed", "failed"),
        ("no_progress", "partial"),
        ("missing_cursor", "partial"),
    ],
)
async def test_risk_stops_entire_run_without_losing_prior_batch(
    store, kind, expected_status
):
    # Consecutive search pages; the browser adapter would run page 1's notes
    # in between (defer_search), which is covered by the task order tests.
    run_id = store.create_run(configuration(adapter="httpx"), mode="offline")
    instances = []

    def factory(b):
        adapter = FaultAdapter(b, kind)
        instances.append(adapter)
        return adapter

    result = await run_worker(store, run_id, factory)
    assert result["status"] == expected_status
    assert instances[0].calls == 2 and instances[0].closed
    snapshot = store.snapshot(run_id)
    assert len(snapshot["items"]) == 1
    assert all(t["state"]["stop_reason"] == kind for t in snapshot["tasks"])


async def test_cancel_and_deadline_admit_no_further_requests(store):
    run_id, epoch, _task = started(store)
    budget = RequestBudget(store, run_id, epoch, configuration())
    await budget.admit("search")
    store.request_cancel(run_id)
    with pytest.raises(CollectionError, match="canceled"):
        await budget.admit("search")
    assert store.get_run(run_id)["requests"] == 1
    budget.deadline = 0
    with pytest.raises(CollectionError, match="deadline"):
        await budget.admit("search")


async def test_concurrent_admission_cannot_overspend(store):
    run_id, epoch, _task = started(store, max_requests=2)
    budget = RequestBudget(store, run_id, epoch, configuration(max_requests=2))
    results = await asyncio.gather(
        *(budget.admit("search") for _ in range(8)), return_exceptions=True
    )
    assert sum(isinstance(r, CollectionError) for r in results) == 6
    assert store.get_run(run_id)["requests"] == 2


async def test_unpaced_admission_still_spends_budget_and_resets_interval(store):
    run_id, epoch, _task = started(store, max_requests=3)
    budget = RequestBudget(store, run_id, epoch, configuration(max_requests=3, min_interval=5))
    await budget.admit("search")
    started_at = time.monotonic()
    await budget.admit("detail", paced=False)
    assert time.monotonic() - started_at < 1
    assert store.get_run(run_id)["requests"] == 2
    with pytest.raises(CollectionError, match="request_budget"):
        await budget.admit("comments", paced=False)
        await budget.admit("comments", paced=False)


async def test_factory_failure_finishes_attempt(store):
    run_id = store.create_run(configuration(), mode="offline")

    def factory(_budget):
        raise RuntimeError("broken factory")

    with pytest.raises(RuntimeError):
        await run_worker(store, run_id, factory)
    assert store.get_run(run_id)["status"] == "failed"


async def test_hung_cleanup_cannot_keep_run_or_worker_active(store):
    run_id = store.create_run(configuration(), mode="offline")

    class HangingCleanupAdapter:
        def __init__(self, budget):
            self.budget = budget
            self.close_cancelled = False

        async def fetch(self, _request):
            await self.budget.admit("search")
            raise CollectionError("access_denied")

        async def close(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.close_cancelled = True

    adapters = []

    def factory(budget):
        adapter = HangingCleanupAdapter(budget)
        adapters.append(adapter)
        return adapter

    result = await run_worker(store, run_id, factory, cleanup_timeout=0.01)

    assert result["status"] == "blocked"
    assert adapters[0].close_cancelled
    events = store.snapshot(run_id)["events"]
    assert any(event["kind"] == "attempt_finished" for event in events)
    timeout = next(event for event in events if event["kind"] == "cleanup_timeout")
    assert timeout["data"]["resource"] == "adapter"


def online_run(store, account, *, environment=None, max_requests=10):
    run_id = store.create_run(
        configuration(max_requests=max_requests),
        mode="online",
        binding="online-binding",
        account_id=account["id"],
        environment=environment or {},
    )
    epoch = store.start(run_id, binding="online-binding")
    return run_id, epoch


def test_run_local_budget_stop_does_not_replace_account_cooldown_reason(store):
    store.set_automatic_cooldown(True)
    account = store.create_account("xhs", "cooldown-history", {})
    run_id, epoch = online_run(store, account)
    store.record_risk(
        run_id,
        operation="search",
        error=CollectionError("rate_limit", retry_after=3600),
    )

    store.finish(
        run_id,
        epoch,
        elapsed=1,
        reason="request_budget",
        outcome="failed",
    )

    current = store.get_account(account["id"])
    assert current["status"] == "cooling"
    assert current["last_failure_kind"] == "rate_limit"


def test_network_observation_requires_confirmed_account_exit(store):
    account = store.create_account("xhs", "network-a", {})
    run_id = store.create_run(
        configuration(), mode="online", account_id=account["id"]
    )
    observation = {
        "proxy_ref": "proxy-1",
        "egress_ip": "203.0.113.10",
        "ip_group": "203.0.113.10",
        "http_egress_ip": "203.0.113.10",
        "browser_egress_ip": "203.0.113.10",
        "source": "test",
        "outcome": "success",
    }
    with pytest.raises(CollectionError, match="not been accepted"):
        store.verify_run_network(run_id, observation)
    assert len(store.snapshot(run_id)["network_observations"]) == 1

    store.set_account_status(account["id"], "cooling", cooldown_until=time.time() + 86400)
    with store.engine.begin() as conn:
        conn.execute(
            update(accounts)
            .where(accounts.c.id == account["id"])
            .values(last_failure_kind="egress_unconfirmed")
        )
    store.confirm_account_network(
        account["id"],
        proxy_ref="proxy-1",
        egress_ip="203.0.113.10",
    )
    repaired = store.get_account(account["id"])
    assert repaired["status"] == "ready"
    assert repaired["cooldown_until"] is None
    assert repaired["last_failure_kind"] is None
    recorded = store.verify_run_network(run_id, observation)
    context = store.get_run_context(run_id)
    assert context["environment"]["network_observation_id"] == recorded["id"]
    assert context["environment"]["ip_group"] == "203.0.113.10"

    changed = observation | {
        "egress_ip": "203.0.113.11",
        "ip_group": "203.0.113.11",
        "http_egress_ip": "203.0.113.11",
        "browser_egress_ip": "203.0.113.11",
    }
    with pytest.raises(CollectionError, match="outside the accepted group"):
        store.verify_run_network(run_id, changed)


def test_web_run_initializes_selected_exit_and_default_quotas(store):
    account = store.create_account("xhs", "web-network", {})
    run_id = store.create_run(
        configuration(),
        mode="online",
        account_id=account["id"],
        environment={"auto_bind_network": True, "use_default_quotas": True},
    )
    observation = {
        "proxy_ref": "proxy-selected-by-user",
        "egress_ip": "203.0.113.20",
        "ip_group": "203.0.113.20",
        "http_egress_ip": "203.0.113.20",
        "browser_egress_ip": "203.0.113.20",
        "source": "test",
        "outcome": "success",
    }

    store.verify_run_network(run_id, observation)

    binding = store.get_account_network_binding(account["id"])
    assert binding["proxy_ref"] == "proxy-selected-by-user"
    assert binding["ip_group"] == "203.0.113.20"
    policies = store.quota_summary()
    assert len(policies) == 8
    assert {policy["request_limit"] for policy in policies} == {999999}
    epoch = store.start(run_id, binding="web")
    assert store.reserve_request(run_id, epoch, "search")

    changed_run = store.create_run(
        configuration(),
        mode="online",
        account_id=account["id"],
        environment={"auto_bind_network": True, "use_default_quotas": True},
    )
    changed = observation | {
        "egress_ip": "203.0.113.21",
        "ip_group": "203.0.113.21",
        "http_egress_ip": "203.0.113.21",
        "browser_egress_ip": "203.0.113.21",
    }
    with pytest.raises(CollectionError, match="outside the accepted group"):
        store.verify_run_network(changed_run, changed)


def test_four_dimension_quota_is_atomic_and_versioned(store):
    account = store.create_account("xhs", "quota-a", {})
    policies = [
        store.create_quota_policy(
            "platform", "xhs", window_seconds=900, request_limit=2
        ),
        store.create_quota_policy(
            "operation", "xhs", operation="search", window_seconds=3600, request_limit=5
        ),
        store.create_quota_policy(
            "account", account["id"], window_seconds=86400, request_limit=5
        ),
        store.create_quota_policy(
            "ip_group", "203.0.113.10", window_seconds=900, request_limit=5
        ),
    ]
    run_id, epoch = online_run(
        store, account, environment={"ip_group": "203.0.113.10"}
    )
    assert store.reserve_request(run_id, epoch, "search")
    assert store.reserve_request(run_id, epoch, "search")
    with pytest.raises(CollectionError) as raised:
        store.reserve_request(run_id, epoch, "search")
    assert raised.value.kind == "quota_exhausted"
    assert raised.value.retry_after is not None
    with store.engine.begin() as conn:
        bucket_rows = conn.execute(select(quota_buckets)).mappings().all()
        reservation_rows = conn.execute(select(quota_reservations)).mappings().all()
    assert max(row["reserved_requests"] for row in bucket_rows) == 2
    assert len(reservation_rows) == 2

    replacement = store.create_quota_policy(
        "platform", "xhs", window_seconds=900, request_limit=3
    )
    assert replacement["version"] == 2
    with store.engine.begin() as conn:
        old = conn.execute(
            select(quota_policies).where(quota_policies.c.id == policies[0]["id"])
        ).mappings().one()
    assert old["enabled"] is False


def test_online_request_fails_closed_when_quota_layer_is_missing(store):
    account = store.create_account("xhs", "incomplete-quota", {})
    for dimension, subject, operation in (
        ("platform", "xhs", None),
        ("operation", "xhs", "search"),
        ("account", account["id"], None),
    ):
        store.create_quota_policy(
            dimension,
            subject,
            operation=operation,
            window_seconds=900,
            request_limit=5,
        )
    run_id, epoch = online_run(store, account, environment={})
    with pytest.raises(CollectionError) as raised:
        store.reserve_request(run_id, epoch, "search")
    assert raised.value.kind == "quota_policy_missing"
    assert store.get_run(run_id)["requests"] == 0
    with store.engine.begin() as conn:
        assert conn.execute(select(quota_buckets)).all() == []


def test_postgres_concurrent_runs_cannot_cross_platform_bucket(store):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock integration test")
    store.create_quota_policy(
        "platform", "xhs", window_seconds=900, request_limit=5
    )
    store.create_quota_policy(
        "operation", "xhs", operation="search", window_seconds=900, request_limit=20
    )
    store.create_quota_policy(
        "ip_group", "203.0.113.10", window_seconds=900, request_limit=20
    )
    attempts = []
    for index in range(12):
        account = store.create_account("xhs", f"parallel-{index}", {})
        store.create_quota_policy(
            "account", account["id"], window_seconds=900, request_limit=20
        )
        attempts.append(
            online_run(store, account, environment={"ip_group": "203.0.113.10"})
        )

    def reserve(owner):
        try:
            store.reserve_request(owner[0], owner[1], "search")
            return "admitted"
        except CollectionError as exc:
            return exc.kind

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(reserve, attempts))
    assert outcomes.count("admitted") == 5
    assert outcomes.count("quota_exhausted") == 7
    platform = next(
        row for row in store.quota_summary() if row["dimension"] == "platform"
    )
    assert platform["used"] == 5


async def test_online_worker_observes_exit_before_adapter_request(store, tmp_path):
    account = store.create_account("xhs", "worker-network", {})
    store.confirm_account_network(
        account["id"], proxy_ref="proxy-1", egress_ip="203.0.113.10"
    )
    for dimension, subject, operation in (
        ("platform", "xhs", None),
        ("operation", "xhs", "search"),
        ("operation", "xhs", "detail"),
        ("account", account["id"], None),
        ("ip_group", "203.0.113.10", None),
    ):
        store.create_quota_policy(
            dimension,
            subject,
            operation=operation,
            window_seconds=900,
            request_limit=20,
        )
    run_id = store.create_run(
        configuration(
            content_limit=1,
            comment_limit=0,
            reply_limit=0,
            download_media=True,
        ),
        mode="online",
        account_id=account["id"],
    )

    class BoundSession:
        binding = "online-binding"

        def check_unchanged(self):
            return None

    async def observer(_session):
        return {
            "proxy_ref": "proxy-1",
            "egress_ip": "203.0.113.10",
            "ip_group": "203.0.113.10",
            "http_egress_ip": "203.0.113.10",
            "browser_egress_ip": "203.0.113.10",
            "source": "test",
            "outcome": "success",
        }

    class Downloader:
        calls = []
        closed = False

        async def download(self, content_id, media):
            self.calls.append((content_id, media))
            return [
                {
                    "media_index": 0,
                    "kind": "video",
                    "status": "downloaded",
                    "path": f"media/xhs/{content_id}/video-0.mp4",
                    "bytes": 12,
                    "sha256": "test",
                    "content_type": "video/mp4",
                }
            ]

        async def close(self):
            self.closed = True

    downloader = Downloader()

    result = await run_worker(
        store,
        run_id,
        lambda budget: FixtureAdapter("xhs", budget, Samples(tmp_path)),
        session=BoundSession(),
        network_observer=observer,
        media_downloader=downloader,
    )
    assert result["status"] == "completed"
    snapshot = store.snapshot(run_id)
    assert snapshot["network_observations"][0]["outcome"] == "success"
    assert all(row["status"] == "consumed" for row in snapshot["quota_reservations"])
    assert downloader.calls == [("synthetic-1", [])]
    assert downloader.closed is True
    with store.engine.connect() as conn:
        content_data = conn.scalar(
            select(contents.c.data).where(
                contents.c.platform == "xhs", contents.c.id == "synthetic-1"
            )
        )
    assert content_data["media_downloads"][0]["status"] == "downloaded"
    assert any(event["kind"] == "media_downloaded" for event in snapshot["events"])
