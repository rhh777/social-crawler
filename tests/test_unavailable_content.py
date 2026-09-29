import json
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from test_xhs_http import Budget, Signer, online_session

from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.browser import XHSBrowser
from social_crawler.adapters.xhs.errors import check_response
from social_crawler.adapters.xhs.http import XHSHTTP
from social_crawler.domain.errors import check_response as generic_check_response
from social_crawler.domain.models import CollectionError, RunConfig, TaskRequest
from social_crawler.orchestration.worker import run_worker

MISSING = {"code": -510000, "success": False, "msg": "笔记不存在", "data": None}


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (200, MISSING, "content_unavailable"),
        (200, MISSING | {"code": "-510000"}, "content_unavailable"),
        (403, MISSING, "access_denied"),
        (401, MISSING, "auth_expired"),
        (461, MISSING, "verification_required"),
        (429, MISSING, "rate_limit"),
        (200, MISSING | {"code": -999}, "access_denied"),
        (200, MISSING | {"msg": "访问受限"}, "access_denied"),
        (200, MISSING | {"msg": "需要安全验证"}, "verification_required"),
        (200, MISSING | {"code": -510001, "msg": "当前内容无法展示"}, "content_unavailable"),
        (200, MISSING | {"code": -510002, "msg": "笔记正在审核中，请稍后查看"}, "content_unavailable"),
        (200, MISSING | {"code": -510001, "msg": "访问受限"}, "access_denied"),
        (200, MISSING | {"code": -510003, "msg": "当前内容无法展示"}, "access_denied"),
    ],
)
def test_missing_note_classification_preserves_risk_errors(status, payload, kind):
    with pytest.raises(CollectionError) as found:
        check_response(status, payload)
    assert found.value.kind == kind


def test_xhs_missing_note_code_does_not_change_other_platforms():
    with pytest.raises(CollectionError) as found:
        generic_check_response(200, MISSING)
    assert found.value.kind == "access_denied"


@pytest.mark.parametrize("status", [200, 403, 404])
async def test_browser_missing_note_is_correlated_without_halting_budget(tmp_path, status):
    budget = Budget()
    budget.halted = None
    budget.halt = lambda error: setattr(budget, "halted", error)
    browser = XHSBrowser(online_session(tmp_path), budget, Samples(tmp_path / "samples"))

    class Response:
        url = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
        request = Mock(method="POST", post_data='{"source_note_id":"missing"}')

        async def json(self):
            return MISSING

        async def all_headers(self):
            return {}

    response = Response()
    response.status = status
    await browser._capture(response)
    if status == 403:
        assert budget.halted.kind == "access_denied"
        assert not browser.exchanges
        return
    assert budget.halted is None
    assert browser._take(TaskRequest(operation="detail", content_id="another")) is None
    with pytest.raises(CollectionError) as found:
        browser._take(TaskRequest(operation="detail", content_id="missing"))
    assert found.value.kind == "content_unavailable"
    assert found.value.sample_ref
    assert budget.halted is None


async def test_http_worker_skips_missing_note_and_preserves_skip_on_resume(store, tmp_path):
    account = store.create_account("xhs", "missing-note-test", {})
    config = RunConfig(
        platform="xhs", keywords=["test"], content_limit=2, comment_limit=1,
        min_interval=0, max_requests=2,
    )
    run_id = store.create_run(config, mode="offline", account_id=account["id"])
    calls = []

    async def transport(method, url, headers, body):
        path = urlsplit(url).path
        data = json.loads(body) if body else {}
        calls.append((path, data.get("source_note_id")))
        if path.endswith("/search/notes"):
            payload = {"items": [
                {"id": cid, "model_type": "note", "xsec_token": "synthetic-token",
                 "note_card": {"display_title": cid}}
                for cid in ("missing", "available")
            ], "has_more": False}
        elif path.endswith("/feed"):
            if data["source_note_id"] == "missing":
                return 200, MISSING, {}
            payload = {"items": [{"id": "available", "note_card": {
                "note_id": "available", "desc": "Full text", "time": 1700000000000,
                "user": {"user_id": "author"},
            }}]}
        else:
            payload = {"comments": [{"id": "comment", "content": "Saved comment"}],
                       "has_more": False, "cursor": ""}
        return 200, {"code": 0, "success": True, "data": payload}, {}

    session = online_session(tmp_path)

    def factory(budget):
        return XHSHTTP(session, budget, Samples(tmp_path / "samples"),
                       transport=transport, signer=Signer())

    first = await run_worker(store, run_id, factory)
    assert first["status"] == "partial"
    assert first["requests"] == 2
    result = await run_worker(store, run_id, factory, resume=True, extra_requests=10)
    assert result["status"] == "partial"  # The unavailable detail remains a coverage gap.
    snapshot = store.snapshot(run_id)
    missing = next(t for t in snapshot["tasks"] if t["content_id"] == "missing")
    assert missing["state"]["stop_reason"] == "content_unavailable"
    assert missing["state"]["skipped"] == [{"id": "missing", "reason": "content_unavailable"}]
    assert missing["state"]["samples"]
    assert calls.count(("/api/sns/web/v1/feed", "missing")) == 1
    remaining = [t for t in snapshot["tasks"] if t["content_id"] == "available"]
    assert {t["operation"] for t in remaining} == {"detail", "comments"}
    assert all(t["status"] == "completed" for t in remaining)
    assert not snapshot["risk_events"]
    assert any(e["kind"] == "task_skipped" for e in snapshot["events"])
    assert store.get_account(account["id"])["status"] == "ready"
