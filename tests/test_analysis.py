"""Manual analysis tests: no paid model calls, including the SDK contract test."""

import asyncio
import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import insert, update
from test_web import collect

from social_crawler.analysis.data import (
    AnalysisError,
    AnalysisReader,
    create_session,
    get_session,
    tool_result,
)
from social_crawler.analysis.service import AnalysisService
from social_crawler.interfaces.web import Console, ConsoleError, Handler
from social_crawler.storage.store import (
    analysis_sessions,
    comments,
    contents,
    task_items,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = Console(tmp_path)
    app.agent_calls = []

    async def fake(reader, session, prompt, directory, emit, control):
        assert reader.artifact_root == app.artifact_root
        app.agent_calls.append((session, prompt))
        emit("session", "fake-session-id", {})
        emit("tool", "coverage", {})
        emit("evidence", "", reader.coverage())
        emit("tool", "search", {})
        result = reader.query()
        emit("evidence", "", result)
        emit("delta", "分析结果：", {})
        await asyncio.sleep(0.05)
        if result["items"]:
            record = result["items"][0]
            emit("delta", f"[查看原文]({record['evidence_url']})", {})
        emit("boundary", "", {})

    app.analysis.runner = fake
    yield app
    app.analysis.shutdown()
    for thread in app.threads:
        thread.join(timeout=10)
    app.worker_pool.shutdown(wait_seconds=10)


def finished(app, session_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = app.analysis.get(session_id)
        if result["session"]["status"] not in {"running", "stopping"}:
            return result
        time.sleep(0.01)
    pytest.fail("Agent did not finish")


def test_readonly_scope_queries_and_literal_search(store):
    with store.engine.begin() as conn:
        for platform, item_id, text in [
            ("xhs", "p1", "续航 100%"),
            ("xhs", "p2", "价格"),
            ("douyin", "p1", "另一平台"),
        ]:
            conn.execute(
                insert(contents).values(
                    platform=platform,
                    id=item_id,
                    observed_at=1,
                    data={
                        "text": text,
                        "cookie": "TOP-SECRET",
                        "xsec_token": "TOP-SECRET",
                        "raw": {"session": "TOP-SECRET"},
                        "url": "https://example.com/?token=secret",
                    },
                )
            )
        conn.execute(
            insert(comments).values(
                platform="xhs",
                content_id="p1",
                id="c1",
                root_id=None,
                parent_id=None,
                observed_at=1,
                data={"text": "续航差"},
            )
        )
        sid = create_session(conn, {"kind": "post", "platform": "xhs", "content_id": "p1"})
        session = get_session(conn, sid)
    reader = AnalysisReader(store.engine, session)
    assert reader.coverage()["posts"] == reader.coverage()["comments"] == 1
    assert reader.query(platform="douyin")["total"] == 0
    assert reader.query(content_id="p2")["total"] == 0
    assert reader.query(q="%")["total"] == 1
    assert reader.query(q="_")["total"] == 0
    assert "TOP-SECRET" not in json.dumps(reader.query())
    assert "url" not in reader.query()["items"][0]["data"]
    with pytest.raises(AnalysisError):
        reader.list_runs()
    with pytest.raises(AnalysisError):
        reader.evidence("douyin/content/p1/p1")
    assert reader.query(kind="comment")["items"][0]["id"] == "c1"


def test_readonly_media_is_scoped_sanitized_and_returns_images(app):
    from PIL import Image

    relative = "media/xhs/media-post/image-0.png"
    path = app.artifact_root / relative
    path.parent.mkdir(parents=True)
    Image.new("RGB", (24, 16), "#307358").save(path)
    with app.store() as store, store.engine.begin() as conn:
        conn.execute(
            insert(contents).values(
                platform="xhs",
                id="media-post",
                observed_at=1,
                data={
                    "text": "带图片的帖子",
                    "media_downloads": [
                        {
                            "media_index": 0,
                            "kind": "image",
                            "status": "downloaded",
                            "content_type": "image/png",
                            "bytes": path.stat().st_size,
                            "path": relative,
                        }
                    ],
                },
            )
        )
        sid = create_session(
            conn,
            {"kind": "post", "platform": "xhs", "content_id": "media-post"},
        )
        session = get_session(conn, sid)
    with app.store() as store:
        reader = AnalysisReader(store.engine, session, app.artifact_root)
        listing = reader.list_media("xhs", "media-post")
        assert listing["downloaded"] == 1
        assert "path" not in json.dumps(listing)
        inspected = reader.inspect_media("xhs", "media-post", 0)
        response = tool_result(inspected)
        assert inspected["_media_blocks"]
        assert response["content"][1]["type"] == "image"
        assert response["content"][1]["mimeType"] == "image/jpeg"
        assert "_media_blocks" not in response["content"][0]["text"]
        assert "path" not in response["content"][0]["text"]
        with pytest.raises(AnalysisError, match="当前帖子"):
            reader.list_media("xhs", "another-post")


def test_media_paths_cannot_escape_artifact_root(app, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-an-image")
    with app.store() as store, store.engine.begin() as conn:
        conn.execute(
            insert(contents).values(
                platform="xhs",
                id="unsafe-media",
                observed_at=1,
                data={
                    "media_downloads": [
                        {
                            "media_index": 0,
                            "kind": "image",
                            "status": "downloaded",
                            "path": str(outside),
                        }
                    ]
                },
            )
        )
        sid = create_session(
            conn,
            {"kind": "post", "platform": "xhs", "content_id": "unsafe-media"},
        )
        session = get_session(conn, sid)
    with app.store() as store:
        reader = AnalysisReader(store.engine, session, app.artifact_root)
        with pytest.raises(AnalysisError, match="不存在"):
            reader.inspect_media("xhs", "unsafe-media", 0)


def test_run_snapshot_is_frozen_deduplicated_and_sanitized(app):
    run_id = collect(app)
    result = app.analysis.create({"kind": "run", "run_id": run_id})
    sid = result["session"]["id"]
    with app.store() as store:
        reader = AnalysisReader(store.engine, result["session"])
        before = reader.query()
        assert before["total"] == 3
        assert reader.coverage()["collection"]["mode"] == "offline"
        item = before["items"][0]
        with store.engine.begin() as conn:
            conn.execute(update(contents).values(data={"text": "LATEST DIFFERENT DATA"}))
            conn.execute(
                update(task_items).values(
                    snapshot={
                        "id": "changed",
                        "kind": "content",
                        "content_id": "changed",
                        "data": {"text": "MODIFIED TASK"},
                    }
                )
            )
        assert reader.query() == before
        assert reader.query(limit=1)["next_offset"] == 1
        assert reader.query(offset=3)["next_offset"] is None
        with pytest.raises(AnalysisError):
            reader.run_data("different-run")
    assert app.analysis.evidence(sid, item["citation"])["data"] == item["data"]
    assert app.agent_calls == []


def test_manual_only_config_resume_and_history(app, monkeypatch):
    run_id = collect(app)
    data = app.analysis.create({"kind": "run", "run_id": run_id})
    sid = data["session"]["id"]
    app.analysis.list()
    app.analysis.get(sid)
    app.dashboard()
    assert app.agent_calls == []
    with pytest.raises(AnalysisError, match="API Key"):
        app.analysis.send(sid, "分析")
    assert app.analysis.get(sid)["messages"] == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    app.analysis.send(sid, "分析")
    result = finished(app, sid)
    assert result["session"]["status"] == "completed"
    assert any(m["role"] == "assistant" and "查看原文" in m["text"] for m in result["messages"])
    app.analysis.send(sid, "继续对比")
    assert finished(app, sid)["session"]["status"] == "completed"
    assert len(app.agent_calls) == 2
    assert app.agent_calls[1][0]["sdk_session_id"] == "fake-session-id"
    assert "sdk_session_id" not in app.analysis.get(sid)["session"]
    restored = AnalysisService(app.store, app.directory / "agent", app.lock)
    assert len(restored.get(sid)["messages"]) == len(app.analysis.get(sid)["messages"])
    assert len(app.agent_calls) == 2


def test_cancel_duplicate_send_and_failure_do_not_retry(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    entered = threading.Event()

    async def wait_forever(reader, session, prompt, directory, emit, control):
        emit("delta", "部分结果", {})
        entered.set()
        await asyncio.Event().wait()

    app.analysis.runner = wait_forever
    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    app.analysis.send(sid, "开始")
    assert entered.wait(3)
    with pytest.raises(AnalysisError, match="正在分析"):
        app.analysis.send(sid, "重复")
    with pytest.raises(ConsoleError, match="Agent"):
        app.require_idle()
    app.analysis.stop(sid)
    data = finished(app, sid)
    assert data["session"]["status"] == "stopped"
    assert data["messages"][-1]["text"] == "部分结果"

    async def broken(*args):
        raise RuntimeError("secret credential must not be exposed")

    app.analysis.runner = broken
    app.analysis.send(sid, "重试")
    data = finished(app, sid)
    assert data["session"]["status"] == "failed"
    assert "secret" not in json.dumps(data)
    assert len([m for m in data["messages"] if m["role"] == "user"]) == 2


def test_restart_marks_orphan_without_model_call(app):
    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    with app.store() as store, store.engine.begin() as conn:
        conn.execute(
            update(analysis_sessions)
            .where(analysis_sessions.c.id == sid)
            .values(status="running", sdk_session_id="keep-for-manual-resume")
        )
    service = AnalysisService(app.store, app.directory / "agent", app.lock)
    assert service.get(sid)["session"]["status"] == "interrupted"
    assert app.agent_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_url,model,expected_base_url",
    [
        ("https://api.anthropic.com", "sonnet", "https://api.anthropic.com"),
        (
            "https://model.example.com/anthropic/v1/messages",
            "example-model",
            "https://model.example.com/anthropic",
        ),
    ],
)
@pytest.mark.parametrize("with_image", [False, True])
async def test_actual_sdk_adapter_contract_without_network(
    app, monkeypatch, tmp_path, api_url, model, expected_base_url, with_image
):
    import claude_agent_sdk as sdk

    from social_crawler.analysis.agent import run_agent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    collect(app)
    session = app.analysis.create({"kind": "all"})["session"] | {
        "model": model,
        "sdk_session_id": "previous-sdk-session",
    }
    definitions = {}
    original_server = sdk.create_sdk_mcp_server

    def server(**kwargs):
        definitions.update({tool.name: tool for tool in kwargs["tools"]})
        return original_server(**kwargs)

    monkeypatch.setattr(sdk, "create_sdk_mcp_server", server)
    seen = []

    class FakeClient:
        def __init__(self, options):
            assert options.tools == options.setting_sources == options.skills == []
            assert options.resume == "previous-sdk-session"
            assert options.model == model
            assert options.env["ANTHROPIC_API_KEY"] == "page-secret"
            assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""
            assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == model
            assert options.env["ANTHROPIC_BASE_URL"] == expected_base_url
            assert options.fallback_model == "fallback-model"
            assert options.max_turns == 24 and options.max_budget_usd == 1.5
            assert options.max_buffer_size == 12 * 1024 * 1024
            assert options.thinking == {"type": "enabled", "budget_tokens": 4096}
            assert options.effort == "medium"
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            if with_image:
                messages = [message async for message in prompt]
                assert len(messages) == 1
                blocks = messages[0]["message"]["content"]
                assert blocks[0] == {"type": "text", "text": "分析"}
                assert blocks[1] == {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "fixture"},
                }
            else:
                assert prompt == "分析"
            assert isinstance(
                await self.options.can_use_tool("Bash", {}, None), sdk.PermissionResultDeny
            )
            assert isinstance(
                await self.options.can_use_tool("mcp__analysis__search", {}, None),
                sdk.PermissionResultAllow,
            )
            coverage = await definitions["coverage"].handler({})
            assert json.loads(coverage["content"][0]["text"])["posts"] == 3
            result = await definitions["search"].handler({"q": "", "limit": 1})
            assert json.loads(result["content"][0]["text"])["total"] == 3
            assert set(definitions) == {
                "coverage",
                "search",
                "list_runs",
                "run_data",
                "list_media",
                "inspect_media",
            }
            assert definitions["inspect_media"].input_schema["required"] == [
                "platform",
                "content_id",
                "media_index",
            ]

        async def receive_response(self):
            yield sdk.SystemMessage(subtype="init", data={"session_id": "new-sdk-session"})
            yield sdk.StreamEvent(
                uuid="e",
                session_id="new-sdk-session",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "你好"},
                },
            )
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="你好")], model="sonnet")
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="后续")], model="sonnet")
            yield sdk.ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="new-sdk-session",
                terminal_reason="completed",
            )

    monkeypatch.setattr(sdk, "ClaudeSDKClient", FakeClient)
    with app.store() as store:
        await run_agent(
            AnalysisReader(store.engine, session),
            session,
            "分析",
            tmp_path / "sdk",
            lambda *args: seen.append(args),
            {
                "cancel": threading.Event(),
                "settings": {
                    "api_key": "page-secret",
                    "api_url": api_url,
                    "model": model,
                    "fallback_model": "fallback-model",
                    "max_turns": 24,
                    "max_budget_usd": 1.5,
                    "max_buffer_size_mb": 12,
                    "thinking_mode": "enabled",
                    "thinking_budget_tokens": 4096,
                    "effort": "medium",
                },
                "attachment_blocks": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "fixture"},
                    }
                ]
                if with_image
                else [],
            },
        )
    assert [event[1] for event in seen if event[0] == "delta"] == ["你好", "后续"]
    assert any(event[0] == "evidence" for event in seen)


@pytest.mark.asyncio
async def test_workspace_agent_enables_claude_code_tools_skills_and_commands(
    app, monkeypatch, tmp_path
):
    import claude_agent_sdk as sdk

    from social_crawler.analysis.agent import run_agent

    session = app.analysis.create({"kind": "all", "mode": "workspace"})["session"] | {
        "model": "example-model"
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed = {}

    class FakeClient:
        def __init__(self, options):
            observed["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            assert prompt == "处理工作区"

        async def receive_response(self):
            yield sdk.ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="workspace-session",
                terminal_reason="completed",
            )

    monkeypatch.setattr(sdk, "ClaudeSDKClient", FakeClient)
    with app.store() as store:
        await run_agent(
            AnalysisReader(store.engine, session),
            session,
            "处理工作区",
            tmp_path / "sdk",
            lambda *_: None,
            {
                "cancel": threading.Event(),
                "settings": {
                    "api_key": "page-secret",
                    "api_url": "https://api.anthropic.com",
                    "model": "example-model",
                },
                "attachment_blocks": [],
                "workspace": workspace,
            },
        )
    options = observed["options"]
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.permission_mode == "bypassPermissions"
    assert options.can_use_tool is None
    assert options.skills == "all"
    assert options.setting_sources == ["user", "project", "local"]
    assert options.cwd == str(workspace)
    assert options.system_prompt["preset"] == "claude_code"


def test_analysis_mode_is_validated_and_persisted(app):
    readonly = app.analysis.create({"kind": "all"})["session"]
    workspace = app.analysis.create({"kind": "all", "mode": "workspace"})["session"]
    assert readonly["scope"]["mode"] == "readonly"
    assert workspace["scope"]["mode"] == "workspace"
    with pytest.raises(AnalysisError, match="执行模式"):
        app.analysis.create({"kind": "all", "mode": "unknown"})


@pytest.fixture
def server(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_routes_require_manual_authenticated_post(server, monkeypatch):
    app, server = server

    def request(method, path, body=None, token=True):
        connection = HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Console-Token"] = app.token
        connection.request(
            method, path, body=json.dumps(body) if body is not None else None, headers=headers
        )
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    assert request("GET", "/api/analysis/sessions")[0] == 200
    status, data = request("POST", "/api/analysis/sessions", {"scope": {"kind": "all"}})
    assert status == 200
    sid = data["session"]["id"]
    assert request("GET", f"/api/analysis/sessions/{sid}")[0] == 200
    assert (
        request("POST", f"/api/analysis/sessions/{sid}/messages", {"message": "分析"}, False)[0]
        == 403
    )
    assert app.agent_calls == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    assert request("POST", f"/api/analysis/sessions/{sid}/messages", {"message": "分析"})[0] == 200
    assert finished(app, sid)["session"]["status"] == "completed"
    assert len(app.agent_calls) == 1


@pytest.mark.browser
async def test_three_entry_points_stream_history_and_evidence(server, monkeypatch, tmp_path):
    from playwright.async_api import async_playwright, expect

    app, server = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    run_id = collect(app)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{server.server_port}")
            await page.locator('[data-page="library"]').click()
            await page.locator("[data-content]").first.click()
            await page.locator("#analyze-post").click()
            await page.get_by_role("button", name="只读分析", exact=False).click()
            await expect(page.locator("#analysis-scope")).to_contain_text("帖子")
            assert app.agent_calls == []
            await page.locator("#analysis-send").click()
            await expect(page.locator("#analysis-status")).to_have_text("已完成")
            await expect(page.locator(".analysis-message.assistant")).to_contain_text("分析结果")
            await page.get_by_role("button", name="查看原文", exact=True).click()
            await expect(page.locator("#modal")).to_contain_text("分析证据")
            await page.locator("#close-modal").click()
            await page.locator("#analysis-prompt").fill("再解释一下")
            await page.locator("#analysis-send").click()
            await expect(page.locator("#analysis-status")).to_have_text("已完成")
            assert len(app.agent_calls) == 2
            await page.locator('[data-page="workbench"]').click()
            await page.locator(f'[data-run="{run_id}"]').click()
            await page.locator("#analyze-run").click()
            await page.get_by_role("button", name="只读分析", exact=False).click()
            await expect(page.locator("#analysis-scope")).to_contain_text("已保存快照")
            assert len(app.agent_calls) == 2
            await page.locator("#analysis-send").click()
            await expect(page.locator("#analysis-status")).to_have_text("已完成")
            await page.locator("#analysis-new").click()
            await expect(page.locator("#analysis-scope")).to_contain_text("自由分析")
            await page.locator("#analysis-prompt").fill("库里有哪些内容？")
            await page.locator("#analysis-send").click()
            await expect(page.locator("#analysis-status")).to_have_text("已完成")
            await page.screenshot(path=str(tmp_path / "agent-desktop.png"), full_page=True)
            assert len(app.agent_calls) == 4
            await page.reload()
            await page.locator("[data-analysis-session]").first.click()
            await expect(page.locator(".analysis-message.assistant")).to_contain_text("分析结果")
            assert len(app.agent_calls) == 4
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(tmp_path / "agent-mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workspace_agent_mode_is_explicit_visible_and_fixed_to_session(
    server, monkeypatch
):
    from playwright.async_api import async_playwright, expect

    app, server = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(f"http://127.0.0.1:{server.server_port}/#analysis")
            mode = page.locator("#analysis-mode")
            await expect(mode).to_have_value("readonly")
            await mode.select_option("workspace")
            await expect(page.locator(".analysis-agent-warning")).to_contain_text(
                "执行任意命令"
            )
            await page.locator("#analysis-prompt").fill("检查工作区")
            await page.locator("#analysis-send").click()
            await expect(page.locator("#analysis-status")).to_have_text("已完成")
            await expect(mode).to_be_disabled()
            await expect(mode).to_have_attribute(
                "title", "本会话模式已锁定；如需更换请新建会话"
            )
            assert app.agent_calls[-1][0]["scope"]["mode"] == "workspace"
            await expect(page.locator("[data-analysis-session]").first).to_contain_text(
                "工作区 Agent"
            )
            await page.locator("#analysis-new").click()
            await expect(mode).to_be_enabled()
            await expect(mode).to_have_value("readonly")
            await expect(page.locator(".analysis-agent-warning")).to_have_count(0)
        finally:
            await browser.close()


def test_frozen_json_queries_and_message_persistence_on_both_databases(store):
    from sqlalchemy import select

    from social_crawler.analysis.service import add_message
    from social_crawler.domain.models import RunConfig
    from social_crawler.storage.store import analysis_messages, tasks

    run_id = store.create_run(RunConfig(platform="xhs", keywords=["续航"]), mode="offline")
    with store.engine.begin() as conn:
        task_id = conn.scalar(select(tasks.c.id).where(tasks.c.run_id == run_id).limit(1))
        conn.execute(
            insert(task_items).values(
                task_id=task_id,
                item_id="p",
                snapshot={
                    "id": "p",
                    "content_id": "p",
                    "kind": "content",
                    "data": {"text": "续航 100%", "cookie": "hidden"},
                },
            )
        )
        sid = create_session(conn, {"kind": "run", "run_id": run_id})
        add_message(conn, sid, "user", "分析")
        session = get_session(conn, sid)
    reader = AnalysisReader(store.engine, session)
    result = reader.query(q="100%")
    assert result["total"] == 1
    assert "hidden" not in json.dumps(result)
    assert reader.evidence(result["items"][0]["citation"])["data"]["text"] == "续航 100%"
    with store.engine.connect() as conn:
        assert conn.scalar(select(analysis_messages.c.text)) == "分析"


def test_free_chat_task_sources_and_versioned_evidence(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    run_id = collect(app)
    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    sources = []

    async def query_sources(reader, session, prompt, directory, emit, control):
        data = reader.run_data(run_id) if prompt == "task" else reader.query()
        emit("evidence", "", data)
        sources.append(data["items"][0])

    app.analysis.runner = query_sources
    for prompt in ("task", "live", "changed"):
        if prompt == "changed":
            with app.store() as store, store.engine.begin() as conn:
                conn.execute(update(contents).values(data={"text": "updated"}))
        app.analysis.send(sid, prompt)
        assert finished(app, sid)["session"]["status"] == "completed"
    assert sources[0]["evidence_url"]
    assert sources[1]["citation"] != sources[2]["citation"]
    for source in sources:
        assert app.analysis.evidence(sid, source["citation"])["data"] == source["data"]


def test_private_page_settings_persistence_and_validation(tmp_path, monkeypatch):
    import stat

    from social_crawler.analysis.config import AgentSettings, sdk_base_url

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    config = AgentSettings(tmp_path / "agent-settings.json", threading.RLock())
    public = config.save(
        {
            "api_url": "https://model.example.com/anthropic/v1/messages",
            "api_key": "page-secret",
            "model": "example-model",
            "fallback_model": "fallback-model",
            "max_turns": "24",
            "max_budget_usd": "1.5",
            "max_buffer_size_mb": "12",
            "thinking_mode": "enabled",
            "thinking_budget_tokens": "4096",
            "effort": "medium",
        }
    )
    assert public["sdk_base_url"] == "https://model.example.com/anthropic"
    assert public["model"] == "example-model" and public["configured"]
    assert public["fallback_model"] == "fallback-model"
    assert public["max_turns"] == 24 and public["max_budget_usd"] == 1.5
    assert public["max_buffer_size_mb"] == 12
    assert public["thinking_mode"] == "enabled"
    assert public["thinking_budget_tokens"] == 4096
    assert public["effort"] == "medium"
    assert "secret" not in json.dumps(public)
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600
    config.save({"api_key": "", "model": "another-model"})
    restored = AgentSettings(config.path, threading.RLock())
    assert restored.load()["api_key"] == "page-secret"
    assert restored.public()["model"] == "another-model"
    assert restored.public()["max_buffer_size_mb"] == 12
    for invalid in [
        "ftp://example.com",
        "https://secret@example.com",
        "https://example.com/?token=secret",
    ]:
        with pytest.raises(AnalysisError):
            config.save({"api_url": invalid})
    assert config.load()["api_url"] == "https://model.example.com/anthropic/v1/messages"
    assert (
        sdk_base_url("https://example.com/anthropic/v1/messages") == "https://example.com/anthropic"
    )
    assert sdk_base_url("https://example.com/v1") == "https://example.com"
    assert sdk_base_url("https://example.com/v1/chat/completions") == "https://example.com"
    for invalid in [
        {"max_turns": "0"},
        {"max_budget_usd": "free"},
        {"max_buffer_size_mb": "65"},
        {"thinking_mode": "legacy"},
        {"thinking_mode": "enabled", "thinking_budget_tokens": ""},
        {"effort": "extreme"},
        {"fallback_model": "bad model"},
    ]:
        with pytest.raises(AnalysisError):
            config.save(invalid)


def test_chat_completions_path_is_accepted_without_claiming_service_availability(
    tmp_path, monkeypatch
):
    from social_crawler.analysis.config import AgentSettings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    monkeypatch.setenv(
        "CRAWLER_AGENT_API_URL", "https://model.example.com/v1/chat/completions"
    )
    config = AgentSettings(tmp_path / "agent-settings.json", threading.RLock())

    public = config.public()
    assert public["configured"]
    assert public["api_key_configured"]
    assert public["sdk_base_url"] == "https://model.example.com"
    assert public["validation_error"] == ""

    saved = config.save(
        {
            "api_url": "https://model.example.com/anthropic/v1/messages",
            "api_key": "",
            "model": "example-model",
        }
    )
    assert saved["configured"]
    assert saved["validation_error"] == ""
    assert saved["sdk_base_url"] == "https://model.example.com/anthropic"
    assert config.load()["api_key"] == "env-secret"

    monkeypatch.setenv(
        "CRAWLER_AGENT_API_URL", "https://url-secret@example.com?token=query-secret"
    )
    unsafe = AgentSettings(tmp_path / "unsafe-settings.json", threading.RLock()).public()
    assert unsafe["api_url"] == ""
    assert "secret" not in json.dumps(unsafe)


@pytest.mark.asyncio
async def test_explicit_model_check_uses_production_sdk_path(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk

    from social_crawler.analysis.agent import check_agent

    seen = {}

    async def fake_query(*, prompt, options):
        seen.update(prompt=prompt, options=options)
        yield sdk.AssistantMessage(
            content=[sdk.TextBlock(text="你好，我是测试模型。")], model="example-model"
        )
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="check-session",
            terminal_reason="completed",
        )

    monkeypatch.setattr(sdk, "query", fake_query)
    result = await check_agent(
        {
            "api_key": "check-secret",
            "api_url": "https://model.example.com/v1/chat/completions",
            "model": "example-model",
        },
        tmp_path / "agent-check",
    )
    assert result == {
        "available": True,
        "model": "example-model",
        "reply": "你好，我是测试模型。",
    }
    assert seen["options"].tools == seen["options"].allowed_tools == []
    assert seen["options"].max_buffer_size == 8 * 1024 * 1024
    assert seen["options"].thinking is None and seen["options"].effort is None
    assert seen["options"].env["ANTHROPIC_BASE_URL"] == "https://model.example.com"
    assert seen["options"].env["ANTHROPIC_API_KEY"] == "check-secret"


def test_settings_changes_apply_to_next_turn_without_model_calls(app):
    entered = threading.Event()
    release = threading.Event()
    observed = []

    async def wait_for_release(reader, session, prompt, directory, emit, control):
        observed.append((session["model"], dict(control["settings"])))
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    app.analysis.runner = wait_for_release
    app.analysis.settings.save(
        {
            "api_url": "https://model.example.com/anthropic/v1/messages",
            "api_key": "first-secret",
            "model": "example-model",
        }
    )
    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    assert observed == []
    app.analysis.send(sid, "开始")
    assert entered.wait(3)
    app.analysis.settings.save({"api_key": "second-secret", "model": "next-model"})
    release.set()
    assert finished(app, sid)["session"]["status"] == "completed"
    app.analysis.send(sid, "继续")
    assert finished(app, sid)["session"]["status"] == "completed"
    assert observed[0][0] == "example-model" and observed[0][1]["api_key"] == "first-secret"
    assert observed[1][0] == "next-model" and observed[1][1]["api_key"] == "second-secret"
    assert "secret" not in json.dumps(app.analysis.get(sid))


def test_settings_http_is_authenticated_and_never_echoes_key(server):
    app, server = server
    connection = HTTPConnection("127.0.0.1", server.server_port)
    payload = {
        "api_key": "http-secret",
        "api_url": "https://model.example.com/anthropic/v1/messages",
        "model": "example-model",
    }
    for token, expected in [(False, 403), (True, 200)]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Console-Token"] = app.token
        connection.request("POST", "/api/analysis/settings", json.dumps(payload), headers)
        response = connection.getresponse()
        data = response.read().decode()
        assert response.status == expected
        assert "http-secret" not in data
    connection.request("GET", "/api/analysis/settings")
    response = connection.getresponse()
    public = json.loads(response.read())
    assert public["api_key_configured"] and public["model"] == "example-model"
    connection.close()
    assert app.agent_calls == []


@pytest.mark.browser
async def test_configured_model_can_be_checked_explicitly_in_modal(
    server, monkeypatch
):
    from playwright.async_api import async_playwright, expect

    app, server = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    monkeypatch.setenv(
        "CRAWLER_AGENT_API_URL", "https://model.example.com/v1/chat/completions"
    )
    checked = []

    def check_settings(body):
        checked.append(body)
        return {"available": True, "model": body["model"], "reply": "我是测试模型。"}

    monkeypatch.setattr(app.analysis, "check_settings", check_settings)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(f"http://127.0.0.1:{server.server_port}/#analysis")
            await expect(page.locator("#analysis-send")).to_be_enabled()
            await page.locator("#analysis-settings").click()
            form = page.locator("#agent-settings-form")
            await expect(form).to_be_visible()
            await expect(form.locator("#agent-settings-error")).to_be_hidden()
            await expect(form.locator("[name=api_url]")).to_have_value(
                "https://model.example.com/v1/chat/completions"
            )
            await expect(form.locator("#agent-effective-url")).to_have_text(
                "https://model.example.com"
            )
            await form.get_by_role("button", name="检测可用性").click()
            await expect(form.locator("#agent-test-result")).to_contain_text(
                "检测成功 · sonnet：我是测试模型。"
            )
            assert checked and checked[0]["api_key"] == ""
        finally:
            await browser.close()


@pytest.mark.browser
async def test_model_configuration_from_both_pages_without_inference(server, tmp_path):
    from playwright.async_api import async_playwright, expect

    app, server = server
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(f"http://127.0.0.1:{server.server_port}/#analysis")
            await expect(page.locator("#analysis-send")).to_be_disabled()
            await page.locator("#analysis-settings").click()
            form = page.locator("#agent-settings-form")
            await form.locator("[name=api_url]").fill(
                "https://model.example.com/anthropic/v1/messages"
            )
            await form.locator("[name=model]").fill("example-model")
            await form.locator("[name=api_key]").fill("browser-secret")
            await form.locator("summary").click()
            await form.locator("[name=fallback_model]").fill("fallback-model")
            await form.locator("[name=max_turns]").fill("24")
            await form.locator("[name=max_budget_usd]").fill("1.5")
            await form.locator("[name=max_buffer_size_mb]").fill("12")
            await form.locator("[name=thinking_mode]").select_option("adaptive")
            await form.locator("[name=effort]").select_option("medium")
            await form.get_by_role("button", name="保存模型配置").click()
            await expect(form.locator("[name=api_key]")).to_have_value("")
            await expect(form.locator("#agent-effective-url")).to_have_text(
                "https://model.example.com/anthropic"
            )
            await expect(page.locator("#analysis-send")).to_be_enabled()
            assert app.agent_calls == []
            assert "browser-secret" not in await page.locator("body").inner_text()
            await page.locator("#close-modal").click()
            await page.locator("[data-page=settings]").click()
            await page.get_by_role("tab", name="Agent 模型", exact=True).click()
            form = page.locator("#agent-settings-form")
            await expect(form.locator("[name=model]")).to_have_value("example-model")
            await expect(form.locator("[name=api_key]")).to_have_value("")
            await form.locator("summary").click()
            await expect(form.locator("[name=fallback_model]")).to_have_value("fallback-model")
            await expect(form.locator("[name=max_turns]")).to_have_value("24")
            await expect(form.locator("[name=max_budget_usd]")).to_have_value("1.5")
            await expect(form.locator("[name=max_buffer_size_mb]")).to_have_value("12")
            await expect(form.locator("[name=thinking_mode]")).to_have_value("adaptive")
            await expect(form.locator("[name=effort]")).to_have_value("medium")
            await form.locator("[name=model]").fill("next-model")
            await form.get_by_role("button", name="保存模型配置").click()
            await expect(page.locator("#toast")).to_contain_text("模型配置已保存")
            await page.reload()
            await page.get_by_role("tab", name="Agent 模型", exact=True).click()
            await expect(page.locator("#agent-settings-form [name=model]")).to_have_value(
                "next-model"
            )
            assert app.analysis.settings.load()["api_key"] == "browser-secret"
            assert app.agent_calls == []
            await page.screenshot(path=str(tmp_path / "agent-settings.png"), full_page=True)
        finally:
            await browser.close()


def test_attachments_local_validation_scope_and_manual_send(app, monkeypatch):
    import base64
    import io

    from PIL import Image

    from social_crawler.analysis.attachments import MAX_FILE

    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    other = app.analysis.create({"kind": "all"})["session"]["id"]
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "#307358").save(image, format="PNG")
    png = app.analysis.upload(
        sid, {"name": "测试图片.png", "content": base64.b64encode(image.getvalue()).decode()}
    )
    doc = app.analysis.upload(
        sid,
        {"name": "需求.md", "content": base64.b64encode("# 用户需求\n电池续航".encode()).decode()},
    )
    assert "text" not in doc
    meta, path = app.analysis.attachment(sid, png["id"])
    assert path.read_bytes() == image.getvalue()
    assert path.stat().st_mode & 0o777 == 0o600
    assert app.agent_calls == []
    assert app.analysis.get(sid)["messages"] == []
    with pytest.raises(AnalysisError, match="不属于"):
        app.analysis.attachment(other, png["id"])
    for name, payload in [
        ("bad.png", b"<svg onload=alert(1)>"),
        ("bad.html", b"<script>"),
        ("a.txt", b"\x00abc"),
    ]:
        with pytest.raises(AnalysisError):
            app.analysis.upload(sid, {"name": name, "content": base64.b64encode(payload).decode()})
    with pytest.raises(AnalysisError, match="8 MB"):
        app.analysis.upload(sid, {"name": "big.txt", "content": "a" * (MAX_FILE * 2)})
    with pytest.raises(AnalysisError):
        app.analysis.attachment(sid, "../../settings")
    with pytest.raises(AnalysisError):
        app.analysis.attachments.prepare(sid, [png["id"]] * 7)
    captured = []

    async def fake(reader, session, prompt, directory, emit, control):
        captured.extend(control["attachment_blocks"])
        emit("delta", "**附件已收到**", {})

    app.analysis.runner = fake
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-only")
    app.analysis.rename(sid, "手动命名的会话")
    app.analysis.send(sid, "结合附件分析", [png["id"], doc["id"]])
    result = finished(app, sid)
    assert result["session"]["title"] == "手动命名的会话"
    assert len(result["messages"][0]["data"]["attachments"]) == 2
    assert any(b["type"] == "image" for b in captured)
    assert any("用户需求" in b.get("text", "") for b in captured)
    assert "base64" not in json.dumps(result)
    restarted = AnalysisService(app.store, app.analysis.directory, app.lock)
    assert restarted.attachment(sid, doc["id"])[0]["text"] == "# 用户需求\n电池续航"
    assert len(restarted.get(sid)["messages"][0]["data"]["attachments"]) == 2


def test_pdf_docx_and_text_attachment_extraction(app):
    import base64
    import io
    import zipfile

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    sid = app.analysis.create({"kind": "all"})["session"]["id"]

    def upload(name, value):
        a = app.analysis.upload(sid, {"name": name, "content": base64.b64encode(value).decode()})
        return app.analysis.attachment(sid, a["id"])[0]

    assert upload("旧编码.txt", "你好".encode("gb18030"))["text"] == "你好"
    assert upload("长文.txt", b"a" * 100_001)["truncated"]
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = io.BytesIO()
    writer.write(pdf)
    assert "Hello PDF" in upload("notes.pdf", pdf.getvalue())["text"]
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>需求文档</w:t></w:r></w:p></w:body></w:document>',
        )
    assert "需求文档" in upload("notes.docx", docx.getvalue())["text"]
    assert app.agent_calls == []


def test_attachment_http_routes_and_download_headers(server):
    import base64

    app, server = server

    def request(method, path, body=None, authenticated=True):
        conn = HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Console-Token"] = app.token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        status = r.status
        h = dict(r.getheaders())
        conn.close()
        return status, data, h

    sid = app.analysis.create({"kind": "all"})["session"]["id"]
    url = f"/api/analysis/sessions/{sid}/attachments"
    body = {"name": "材料.md", "content": base64.b64encode(b"# Hello").decode()}
    assert request("POST", url, body, False)[0] == 403
    status, raw, _ = request("POST", url, body)
    assert status == 200
    meta = json.loads(raw)
    status, raw, headers = request("GET", meta["url"])
    assert status == 200 and raw == b"# Hello"
    assert headers["Content-Type"] == "application/octet-stream"
    assert "filename*=UTF-8" in headers["Content-Disposition"]
    assert request("GET", "/vendor/marked.umd.js")[0] == 200
    assert request("GET", "/vendor/purify.min.js")[0] == 200
    status, raw, headers = request("GET", f"/api/analysis/sessions/{sid}/export")
    assert status == 200 and raw.decode().startswith("# 自由分析")
    assert "text/markdown" in headers["Content-Type"]
    assert app.agent_calls == []
