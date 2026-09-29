"""Targeted collection: real task graph, protocol fixtures and bounded resolution."""

import json
from types import SimpleNamespace

import pytest
from fixture_adapter import FixtureAdapter
from pydantic import ValidationError
from sqlalchemy import insert, select

from social_crawler.adapters.samples import Samples
from social_crawler.adapters.targets import resolve_target
from social_crawler.domain.models import CollectionError, PageResult, RunConfig, TaskRequest
from social_crawler.domain.redaction import redact
from social_crawler.domain.targets import parse_target
from social_crawler.orchestration.budget import RequestBudget
from social_crawler.orchestration.report import export_run
from social_crawler.orchestration.scheduler import Scheduler
from social_crawler.orchestration.worker import run_worker
from social_crawler.storage.store import contents

XHS = "66fad51c000000001b0224b8"
DY = "7123456789012345678"
TOKEN = "private-fixture-token"
XHS_URL = f"https://www.xiaohongshu.com/explore/{XHS}?xsec_token={TOKEN}&xsec_source=pc_feed"
REDNOTE_URL = f"https://www.rednote.com/explore/{XHS}?xsec_token={TOKEN}&xsec_source=pc_feed"


def config(platform="xhs", targets=None, **overrides):
    default_target = {"xhs": XHS_URL, "rednote": REDNOTE_URL, "douyin": DY}[platform]
    return RunConfig(
        platform=platform,
        source_type="posts",
        post_targets=targets or [default_target],
        min_interval=0,
        comment_limit=3,
        reply_limit=3,
        **overrides,
    )


@pytest.mark.parametrize(
    "platform,value,expected",
    [
        ("douyin", DY, DY),
        ("douyin", f"https://www.douyin.com/note/{DY}", DY),
        ("douyin", f"https://www.douyin.com/?modal_id={DY}", DY),
        ("douyin", f"看看 https://www.iesdouyin.com/share/video/{DY}/ 分享给你", DY),
        ("xhs", XHS, XHS),
        ("xhs", f"推荐这篇 {XHS_URL} 打开小红书", XHS),
        ("xhs", f"https://www.xiaohongshu.com/discovery/item/{XHS}", XHS),
        ("rednote", f"RedNote share: {REDNOTE_URL}", XHS),
    ],
)
def test_target_formats(platform, value, expected):
    assert parse_target(platform, value)["id"] == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://www.xiaohongshu.com.evil.test/explore/" + XHS,
        "https://evil.test@www.xiaohongshu.com/explore/" + XHS,
        "https://www.xiaohongshu.com:9999/explore/" + XHS,
        "https://www.xiaohongshu.com/user/profile/" + XHS,
        f"https://www.douyin.com/video/{DY}",
        "http://127.0.0.1/explore/" + XHS,
        "不是链接",
        XHS_URL + " " + XHS_URL,
    ],
)
def test_reject_wrong_platform_non_post_and_unsafe_urls(value):
    with pytest.raises(CollectionError, match="."):
        parse_target("xhs", value)


def test_config_compatibility_and_partial_validation():
    legacy = RunConfig(
        platform="xhs", keywords=[" coffee ", "coffee"], seed_contents=[{"id": "old"}]
    )
    assert legacy.keywords == ["coffee"]
    assert RunConfig.model_validate(legacy.model_dump()).seed_contents == [{"id": "old"}]
    assert config(targets=[XHS_URL, "bad"]).keywords == []
    with pytest.raises(ValidationError):
        config(targets=["bad"])
    with pytest.raises(ValidationError):
        config(keywords=["unwanted search"])
    with pytest.raises(ValidationError):
        RunConfig(platform="xhs")
    assert parse_target("xhs", XHS_URL)["xsec_token"] == TOKEN
    assert TOKEN not in json.dumps(redact(config().model_dump()))


def test_rednote_targets_require_scoped_full_url():
    target = parse_target("rednote", REDNOTE_URL)
    assert target == {
        "id": XHS,
        "url": f"https://www.rednote.com/explore/{XHS}",
        "xsec_token": TOKEN,
        "xsec_source": "pc_feed",
    }
    with pytest.raises(CollectionError, match="完整帖子链接"):
        parse_target("rednote", XHS)
    with pytest.raises(CollectionError, match="xsec_token"):
        parse_target("rednote", f"https://www.rednote.com/explore/{XHS}")
    with pytest.raises(CollectionError, match="平台不匹配"):
        parse_target("rednote", XHS_URL)


@pytest.mark.parametrize(
    "platform,cid", [("xhs", XHS), ("rednote", XHS), ("douyin", DY)]
)
async def test_posts_graph_dedup_pagination_and_export(store, tmp_path, platform, cid):
    url = {
        "xhs": XHS_URL,
        "rednote": REDNOTE_URL,
        "douyin": f"https://www.douyin.com/video/{cid}",
    }[platform]
    inputs = (
        [
            url,
            f"https://www.rednote.com/discovery/item/{cid}?xsec_token={TOKEN}",
            "bad",
        ]
        if platform == "rednote"
        else [cid, url, "bad"]
    )
    run_id = store.create_run(config(platform, inputs), mode="offline")
    samples = Samples(tmp_path)
    result = await run_worker(store, run_id, lambda b: FixtureAdapter(platform, b, samples))
    assert result["status"] == "partial"  # the invalid input remains visible
    snapshot = store.snapshot(run_id)
    operations = [t["operation"] for t in snapshot["tasks"]]
    assert "search" not in operations
    assert (
        operations.count("detail")
        == operations.count("comments")
        == operations.count("replies")
        == 1
    )
    assert len(snapshot["post_sources"]) == 3
    assert [s["status"] for s in snapshot["post_sources"]] == ["resolved", "resolved", "invalid"]
    for task in snapshot["tasks"]:
        if task["operation"] in {"comments", "replies"}:
            assert task["state"]["count"] == 3
            assert task["state"]["pages"] == 2
        if platform in {"xhs", "rednote"} and task["operation"] in {
            "detail",
            "comments",
            "replies",
        }:
            assert task["state"]["input"]["xsec_token"] == TOKEN
    report = export_run(store, run_id, tmp_path / "export")
    assert report["contents"] == 1 and report["comments_including_replies"] == 6
    assert not report["automated_scope_gate"]
    assert TOKEN not in (tmp_path / "export/run-config.json").read_text()
    assert TOKEN not in (tmp_path / "export/post-sources.json").read_text()


async def test_posts_resume_preserves_resolved_targets_and_comment_cursor(store, tmp_path):
    run_id = store.create_run(config(max_requests=2), mode="offline")

    def factory(b):
        return FixtureAdapter("xhs", b, Samples(tmp_path))

    result = await run_worker(store, run_id, factory)
    assert result["status"] == "partial"
    first = store.snapshot(run_id)
    assert next(t for t in first["tasks"] if t["operation"] == "comments")["state"]["context"]
    result = await run_worker(store, run_id, factory, resume=True, extra_requests=20)
    assert result["status"] == "completed"
    snapshot = store.snapshot(run_id)
    assert (
        next(t for t in snapshot["tasks"] if t["operation"] == "resolve_target")["state"]["pages"]
        == 1
    )
    assert len([t for t in snapshot["tasks"] if t["operation"] == "detail"]) == 1
    report = export_run(store, run_id, tmp_path / "export")
    assert report["automated_scope_gate"]
    assert report["contents"] == 1 and report["comments_including_replies"] == 6


async def test_detail_only_skips_comments_and_passes_requested_scope(store, tmp_path):
    cfg = config()
    cfg.comment_limit = 0
    run_id = store.create_run(cfg, mode="offline")
    await run_worker(store, run_id, lambda b: FixtureAdapter("xhs", b, Samples(tmp_path)))
    report = export_run(store, run_id, tmp_path / "export")
    assert report["automated_scope_gate"] and report["comments_including_replies"] == 0
    assert report["requests"] == 1


def test_target_and_derived_task_transaction_roll_back_together(store):
    run_id = store.create_run(config(), mode="offline")
    epoch = store.start(run_id, binding="offline")
    task = store.next_task(run_id, epoch)

    def crash(stage):
        if stage == "before_commit":
            raise RuntimeError("simulated crash")

    page = PageResult(resolved_target=parse_target("xhs", XHS_URL), response_has_more=False)
    with pytest.raises(RuntimeError):
        store.commit_page(run_id, task["id"], epoch, page, fault=crash)
    snapshot = store.snapshot(run_id)
    assert len(snapshot["tasks"]) == 1 and snapshot["post_sources"][0]["status"] == "pending"
    store.commit_page(run_id, task["id"], epoch, page)
    assert len(store.snapshot(run_id)["tasks"]) == 2


async def test_short_link_resolves_and_counts_each_hop(store):
    run_id = store.create_run(config(targets=["https://xhslink.com/a/abc"]), mode="offline")
    epoch = store.start(run_id, binding="offline")
    budget = RequestBudget(store, run_id, epoch, config())
    visited = []

    async def redirect(url):
        visited.append(url)
        return 302, {"location": "/a/next" if len(visited) == 1 else XHS_URL}

    result = await resolve_target(
        TaskRequest(
            operation="resolve_target",
            input={"target": parse_target("xhs", "https://xhslink.com/a/abc")},
        ),
        "xhs",
        budget,
        None,
        transport=redirect,
    )
    assert result.resolved_target["xsec_token"] == TOKEN
    assert store.get_run(run_id)["requests"] == 2
    assert len(visited) == 2


async def test_xhs_curl_short_link_uses_matching_transport(monkeypatch):
    created = []

    class Response:
        status_code = 302

        def __init__(self, location):
            self.headers = {"location": location}

    class CurlSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.closed = False
            created.append(self)

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response(XHS_URL)

        async def close(self):
            self.closed = True

    async def admit(_operation):
        pass

    monkeypatch.setattr("social_crawler.adapters.targets.AsyncSession", CurlSession)
    session = SimpleNamespace(
        config=SimpleNamespace(impersonate="chrome150", consistency_policy="off"),
        environment_snapshot=None,
        effective_user_agent="Mozilla/5.0 Chrome/150.0.0.0",
        proxy="socks5h://proxy.example:1080",
    )
    result = await resolve_target(
        TaskRequest(
            operation="resolve_target",
            input={"target": parse_target("xhs", "https://xhslink.com/a/abc")},
        ),
        "xhs",
        SimpleNamespace(admit=admit, config=SimpleNamespace(request_timeout=20)),
        session,
        adapter="curl_cffi",
    )

    assert result.resolved_target["id"] == XHS
    client = created[0]
    assert client.kwargs == {
        "impersonate": "chrome150",
        "proxy": "socks5h://proxy.example:1080",
        "trust_env": False,
        "default_headers": False,
        "discard_cookies": True,
        "max_clients": 1,
    }
    assert client.calls[0][1]["allow_redirects"] is False
    assert client.closed


@pytest.mark.parametrize("destination", ["http://127.0.0.1/private", "https://xhslink.com/a/abc"])
async def test_redirect_validation_prevents_external_or_loop_requests(destination):
    calls = []

    async def admit(op):
        calls.append(op)

    async def redirect(url):
        return 302, {"location": destination}

    with pytest.raises(CollectionError) as exc:
        await resolve_target(
            TaskRequest(
                operation="resolve_target",
                input={"target": parse_target("xhs", "https://xhslink.com/a/abc")},
            ),
            "xhs",
            SimpleNamespace(admit=admit),
            None,
            transport=redirect,
        )
    assert exc.value.kind == "invalid_target" and len(calls) == 1


async def test_short_alias_and_long_url_share_detail(store, tmp_path, monkeypatch):
    from social_crawler.orchestration import worker

    original = worker.resolve_target

    async def redirect(url):
        return 302, {"location": XHS_URL}

    async def injected(request, platform, budget, session, **kwargs):
        return await original(request, platform, budget, session, transport=redirect)

    monkeypatch.setattr(worker, "resolve_target", injected)
    run_id = store.create_run(
        config(targets=["https://xhslink.com/a/abc", XHS_URL]), mode="offline"
    )
    await run_worker(store, run_id, lambda b: FixtureAdapter("xhs", b, Samples(tmp_path)))
    snapshot = store.snapshot(run_id)
    assert len({s["task_id"] for s in snapshot["post_sources"]}) == 1
    assert sum(t["operation"] == "detail" for t in snapshot["tasks"]) == 1


def test_cached_context_and_schedule_edit_do_not_drop_tokens(store):
    with store.engine.begin() as conn:
        conn.execute(
            insert(contents).values(
                platform="xhs", id=XHS, observed_at=1, data={"xsec_token": TOKEN}
            )
        )
    run_id = store.create_run(config(targets=[XHS]), mode="offline")
    assert store.post_context(run_id, XHS)["xsec_token"] == TOKEN
    service = Scheduler(store)
    schedule = service.create(
        {
            "name": "posts",
            "kind": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "config": config().model_dump(),
        }
    )
    public_config = redact(schedule["config"])
    public_config.pop("post_targets")
    updated = service.update(schedule["id"], {"config": public_config, "name": "new time"})
    assert updated["config"]["post_targets"] == [XHS_URL]
    dispatched = service.dispatch_due(now=updated["next_run_at"] + 1)
    assert len(dispatched) == 1
    assert all(
        t["operation"] == "resolve_target" for t in store.snapshot(dispatched[0]["run_id"])["tasks"]
    )


async def test_unavailable_post_does_not_block_other_posts(store, tmp_path):
    second = "66fad51c000000001b0224b9"

    class Adapter(FixtureAdapter):
        async def fetch(self, request):
            if request.content_id == XHS:
                raise CollectionError("content_unavailable")
            return await super().fetch(request)

    run_id = store.create_run(config(targets=[XHS_URL, second]), mode="offline")
    result = await run_worker(store, run_id, lambda b: Adapter("xhs", b, Samples(tmp_path)))
    assert result["status"] == "partial"
    snapshot = store.snapshot(run_id)
    assert any(
        t["operation"] == "replies" and t["content_id"] == second and t["status"] == "completed"
        for t in snapshot["tasks"]
    )
    with store.engine.connect() as conn:
        assert set(conn.execute(select(contents.c.id)).scalars()) == {second}


@pytest.mark.parametrize("platform,cid", [("xhs", XHS), ("douyin", DY)])
async def test_real_http_adapters_receive_target_and_reply_context(store, tmp_path, platform, cid):
    from urllib.parse import parse_qs, urlsplit

    from social_crawler.adapters.douyin.http import DouyinHTTP
    from social_crawler.adapters.xhs.http import XHSHTTP
    from social_crawler.environments.session import EnvironmentConfig, Session

    cookie_file = tmp_path / "session.json"
    cookie_file.write_text(
        json.dumps(
            {"a1": "fixture-device", "web_session": "fixture-login", "sessionid": "fixture-login"}
        )
    )
    session = Session(
        EnvironmentConfig(cookie_file=str(cookie_file), consistency_policy="off"), platform
    )
    samples = Samples(tmp_path / "samples")
    protocol = FixtureAdapter(platform, None, None)
    observed = []

    def response(url, body=None):
        path = urlsplit(url).path
        data = (
            json.loads(body)
            if body
            else {
                k: v[-1] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()
            }
        )
        observed.append((path, data))
        if path.endswith(("/feed", "/detail/")):
            raw = protocol._content(cid)
            payload = (
                {"items": [{"id": cid, "note_card": raw}]}
                if platform == "xhs"
                else {"aweme_detail": raw}
            )
        else:
            root = data.get("root_comment_id") or data.get("comment_id")
            cursor = data.get("cursor") or "0"
            first = cursor == "0"
            parent = root or cid
            ids = [parent + "-1", parent + "-2"] if first else [parent + "-2", parent + "-3"]
            payload = {
                "comments": [protocol._comment(i, not root) for i in ids],
                "has_more": first,
                "cursor": "1" if first else "2",
            }
        return (
            {"success": True, "code": 0, "data": payload}
            if platform == "xhs"
            else {"status_code": 0, **payload}
        )

    async def xhs_transport(method, url, headers, body):
        return 200, response(url, body), {}

    async def dy_transport(url, headers):
        return 200, response(url), {}

    cfg = config(platform)
    if platform == "xhs":
        cfg.adapter = "httpx"
    run_id = store.create_run(cfg, mode="offline")

    def factory(budget):
        return (
            XHSHTTP(session, budget, samples, transport=xhs_transport)
            if platform == "xhs"
            else DouyinHTTP(session, budget, samples, transport=dy_transport)
        )

    result = await run_worker(store, run_id, factory)
    assert result["status"] == "completed"
    assert len(observed) == 5
    assert not any("search" in path for path, _ in observed)
    if platform == "xhs":
        assert all(data["xsec_token"] == TOKEN for _, data in observed)
        assert observed[0][1]["source_note_id"] == cid
        assert observed[-1][1]["root_comment_id"] == cid + "-1"
    else:
        assert observed[0][1]["aweme_id"] == cid
        assert observed[-1][1]["comment_id"] == cid + "-1"
    assert observed[-1][1]["cursor"] == "1"


def test_resolver_uses_legacy_search_quota_without_bypassing_global_caps(store):
    account = store.upsert_account("target-account", "xhs", "target", {})
    run_id = store.create_run(
        config(), mode="online", account_id=account["id"], environment={"ip_group": "fixture-ip"}
    )
    for dimension, subject, operation in [
        ("platform", "xhs", None),
        ("operation", "xhs", "search"),
        ("account", account["id"], None),
        ("ip_group", "fixture-ip", None),
    ]:
        store.create_quota_policy(
            dimension, subject, operation=operation, window_seconds=86400, request_limit=1
        )
    epoch = store.start(run_id, binding="offline")
    store.reserve_request(run_id, epoch, "resolve_target")
    assert store.get_run(run_id)["requests"] == 1
    with pytest.raises(CollectionError) as failure:
        store.reserve_request(run_id, epoch, "resolve_target")
    assert failure.value.kind == "quota_exhausted"
    assert all(row["used"] == 1 for row in store.quota_summary())


@pytest.mark.parametrize("adapter", ["httpx", "curl_cffi"])
async def test_missing_http_context_is_target_local_and_never_requests_search(store, adapter):
    run_id = store.create_run(config(targets=[XHS], adapter=adapter), mode="online")

    class NoRequests:
        async def fetch(self, request):
            pytest.fail("Missing token must fail before any platform request")

        async def close(self):
            pass

    result = await run_worker(store, run_id, lambda budget: NoRequests())
    assert result["status"] == "partial" and result["requests"] == 0
    snapshot = store.snapshot(run_id)
    assert snapshot["tasks"][-1]["state"]["stop_reason"] == "missing_access_context"


async def test_new_post_collection_refreshes_existing_content_without_duplicate_rows(
    store, tmp_path
):
    reports = []
    for _ in range(2):
        run_id = store.create_run(config(), mode="offline")
        await run_worker(store, run_id, lambda b: FixtureAdapter("xhs", b, Samples(tmp_path)))
        reports.append(export_run(store, run_id, tmp_path / run_id))
    assert all(r["requests"] == 5 and r["contents"] == 1 for r in reports)
    with store.engine.connect() as conn:
        assert list(conn.execute(select(contents.c.id)).scalars()) == [XHS]


def test_disabled_resolver_policy_does_not_fall_back_to_search(store):
    account = store.upsert_account("disabled-target-account", "xhs", "target", {})
    run_id = store.create_run(
        config(), mode="online", account_id=account["id"], environment={"ip_group": "fixture-ip"}
    )
    for dimension, subject, operation in [
        ("platform", "xhs", None),
        ("operation", "xhs", "search"),
        ("account", account["id"], None),
        ("ip_group", "fixture-ip", None),
    ]:
        store.create_quota_policy(
            dimension, subject, operation=operation, window_seconds=86400, request_limit=5
        )
    store.create_quota_policy(
        "operation",
        "xhs",
        operation="resolve_target",
        window_seconds=86400,
        request_limit=5,
        enabled=False,
    )
    epoch = store.start(run_id, binding="offline")
    with pytest.raises(CollectionError) as failure:
        store.reserve_request(run_id, epoch, "resolve_target")
    assert failure.value.kind == "quota_policy_missing"
    assert store.get_run(run_id)["requests"] == 0
