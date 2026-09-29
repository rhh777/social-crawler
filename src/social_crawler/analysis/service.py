"""Manual turn lifecycle and durable conversation history."""

import asyncio
import logging
import threading
import time
from pathlib import Path

import anyio
from sqlalchemy import insert, select, update

from social_crawler.analysis.agent import check_agent, run_agent
from social_crawler.analysis.attachments import Attachments
from social_crawler.analysis.config import AgentSettings
from social_crawler.analysis.data import AnalysisError, AnalysisReader, create_session, get_session
from social_crawler.storage.store import analysis_messages, analysis_sessions, analysis_sources

logger = logging.getLogger(__name__)


def add_message(conn, session_id, role, text="", data=None):
    return conn.execute(
        insert(analysis_messages).values(
            session_id=session_id,
            role=role,
            text=text,
            data=data or {},
            created_at=time.time(),
        )
    ).inserted_primary_key[0]


class AnalysisService:
    def __init__(self, store_factory, directory, lock, *, workspace=None, artifact_root=None):
        self.store_factory = store_factory
        self.directory = Path(directory)
        self.workspace = Path(workspace or directory).resolve()
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else None
        self.lock = lock
        self.settings = AgentSettings(self.directory.parent / "agent-settings.json", lock)
        self.attachments = Attachments(self.directory.parent / "analysis-attachments")
        self.active = {}
        self.recovered = set()
        self.runner = run_agent

    def recover(self, store):
        # Only mark orphaned turns; never resume or start a model automatically.
        database = str(store.engine.url)
        if database in self.recovered:
            return
        with store.engine.begin() as conn:
            conn.execute(
                update(analysis_sessions)
                .where(analysis_sessions.c.status.in_(["running", "stopping"]))
                .values(status="interrupted", error="服务重启中断了分析，请手动发送消息继续")
            )
        self.recovered.add(database)

    def list(self):
        with self.lock, self.store_factory() as store:
            self.recover(store)
            with store.engine.connect() as conn:
                rows = conn.execute(
                    select(analysis_sessions)
                    .order_by(analysis_sessions.c.updated_at.desc())
                    .limit(100)
                ).mappings()
                return {
                    "sessions": [self.public_session(dict(r)) for r in rows],
                    "configuration": self.settings.public(),
                }

    @staticmethod
    def public_session(session):
        return {k: v for k, v in session.items() if k != "sdk_session_id"}

    def get(self, session_id):
        with self.lock, self.store_factory() as store:
            self.recover(store)
            with store.engine.connect() as conn:
                session = get_session(conn, session_id)
                messages = conn.execute(
                    select(analysis_messages)
                    .where(analysis_messages.c.session_id == session_id)
                    .order_by(analysis_messages.c.id)
                ).mappings()
                return {
                    "session": self.public_session(session),
                    "messages": [dict(m) for m in messages],
                }

    def create(self, scope):
        with self.lock, self.store_factory() as store:
            self.recover(store)
            with store.engine.begin() as conn:
                session_id = create_session(conn, scope)
        return self.get(session_id)

    def evidence(self, session_id, citation):
        with self.store_factory() as store, store.engine.connect() as conn:
            session = get_session(conn, session_id)
            record = conn.execute(
                select(analysis_sources.c.record).where(
                    analysis_sources.c.session_id == session_id,
                    analysis_sources.c.record["citation"].as_string() == citation,
                )
            ).scalar()
        if record:
            return record
        with self.store_factory() as store:
            return AnalysisReader(store.engine, session).evidence(citation)

    def export(self, session_id):
        data = self.get(session_id)
        sections = ["# " + data["session"]["title"]]
        for message in data["messages"]:
            if message["role"] not in {"user", "assistant"}:
                continue
            label = "你" if message["role"] == "user" else message["data"].get("model", "Agent")
            section = "## " + label + "\n\n" + message["text"]
            for attachment in message["data"].get("attachments", []):
                section += "\n\n附件：" + attachment["name"]
            sections.append(section)
        return "\n\n---\n\n".join(sections) + "\n"

    def upload(self, session_id, body):
        with self.lock, self.store_factory() as store, store.engine.connect() as conn:
            get_session(conn, session_id)
            return self.attachments.save(session_id, body)

    def attachment(self, session_id, attachment_id):
        with self.store_factory() as store, store.engine.connect() as conn:
            get_session(conn, session_id)
        return self.attachments.load(session_id, attachment_id)

    def check_settings(self, body):
        settings = self.settings.resolve(body)
        if not settings["api_key"]:
            raise AnalysisError("请先填写 API Key，再检测模型可用性")
        return anyio.run(check_agent, settings, self.directory.parent / "agent-check")

    def rename(self, session_id, title):
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise AnalysisError("会话名称需要为 1–100 字")
        with self.lock, self.store_factory() as store, store.engine.begin() as conn:
            get_session(conn, session_id)
            conn.execute(
                update(analysis_sessions)
                .where(analysis_sessions.c.id == session_id)
                .values(title=title.strip())
            )
        return self.get(session_id)

    def send(self, session_id, prompt, attachment_ids=None):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise AnalysisError("请输入 1–20000 字的分析问题")
        with self.lock, self.store_factory() as store:
            settings = self.settings.load()
            if not settings["api_key"]:
                raise AnalysisError("请先在页面的 Agent 配置中填写 API Key，再发送分析问题")
            self.recover(store)
            with store.engine.begin() as conn:
                session = get_session(conn, session_id)
                if session["status"] in {"running", "stopping"} or session_id in self.active:
                    raise AnalysisError("当前会话正在分析，请先停止或等待结束")
                attachments, blocks = self.attachments.prepare(
                    session_id, [] if attachment_ids is None else attachment_ids
                )
                model = settings["model"]
                title = session["title"]
                if (
                    session["scope"]["kind"] == "all"
                    and session["title"] == "自由分析"
                    and not conn.execute(
                        select(analysis_messages.c.id)
                        .where(analysis_messages.c.session_id == session_id)
                        .limit(1)
                    ).first()
                ):
                    title = prompt.strip()[:60]
                claimed = conn.execute(
                    update(analysis_sessions)
                    .where(
                        analysis_sessions.c.id == session_id,
                        analysis_sessions.c.status.not_in(["running", "stopping"]),
                    )
                    .values(
                        status="running",
                        error=None,
                        model=model,
                        title=title,
                        updated_at=time.time(),
                    )
                )
                if claimed.rowcount != 1:
                    raise AnalysisError("当前会话正在分析")
                add_message(conn, session_id, "user", prompt.strip(), {"attachments": attachments})
                session.update(status="running", error=None, model=model)
            control = {
                "cancel": threading.Event(),
                "loop": None,
                "cancel_scope": None,
                "client": None,
                "settings": settings,
                "attachment_blocks": blocks,
                "workspace": self.workspace,
            }
            thread = threading.Thread(
                target=self._work,
                args=(session, prompt.strip(), control),
                daemon=True,
                name=f"analysis-{session_id[:8]}",
            )
            control["thread"] = thread
            self.active[session_id] = control
            try:
                thread.start()
            except Exception:
                self.active.pop(session_id, None)
                with store.engine.begin() as conn:
                    conn.execute(
                        update(analysis_sessions)
                        .where(analysis_sessions.c.id == session_id)
                        .values(status="failed", error="无法启动分析进程，请手动重试")
                    )
                raise AnalysisError("无法启动分析进程，请手动重试") from None
        return {"session_id": session_id, "status": "running"}

    def stop(self, session_id):
        with self.lock:
            control = self.active.get(session_id)
            if control:
                control["cancel"].set()
                with self.store_factory() as store, store.engine.begin() as conn:
                    conn.execute(
                        update(analysis_sessions)
                        .where(
                            analysis_sessions.c.id == session_id,
                            analysis_sessions.c.status == "running",
                        )
                        .values(status="stopping", updated_at=time.time())
                    )
                if control["loop"] and control["cancel_scope"]:

                    def interrupt():
                        client = control["client"]
                        cancel_scope = control["cancel_scope"]
                        if client:

                            async def request_stop():
                                try:
                                    await client.interrupt()
                                except Exception:
                                    cancel_scope.cancel()

                            control["loop"].create_task(request_stop())
                            # This is stop cleanup, not an execution or token budget.
                            control["loop"].call_later(3, cancel_scope.cancel)
                        else:
                            cancel_scope.cancel()

                    try:
                        control["loop"].call_soon_threadsafe(interrupt)
                    except RuntimeError:
                        pass  # The worker finished concurrently.
        return self.get(session_id)

    def shutdown(self):
        with self.lock:
            running = list(self.active.items())
        for session_id, _ in running:
            self.stop(session_id)
        for _, control in running:
            control["thread"].join(timeout=25)

    def _work(self, session, prompt, control):
        session_id = session["id"]
        final_status, error = "completed", None
        try:
            with self.store_factory() as store:
                reader = AnalysisReader(store.engine, session, self.artifact_root)
                assistant_id = None
                text = ""
                last_write = 0

                def flush():
                    nonlocal last_write
                    if assistant_id is not None:
                        with store.engine.begin() as conn:
                            conn.execute(
                                update(analysis_messages)
                                .where(analysis_messages.c.id == assistant_id)
                                .values(text=text)
                            )
                        last_write = time.monotonic()

                def emit(kind, value, data):
                    nonlocal assistant_id, text
                    if kind == "delta":
                        if assistant_id is None:
                            with store.engine.begin() as conn:
                                assistant_id = add_message(
                                    conn, session_id, "assistant", data={"model": session["model"]}
                                )
                            text = ""
                        text += value
                        if time.monotonic() - last_write > 0.15:
                            flush()
                    elif kind == "boundary":
                        flush()
                        assistant_id = None
                        text = ""
                    elif kind == "session" and value:
                        with store.engine.begin() as conn:
                            conn.execute(
                                update(analysis_sessions)
                                .where(analysis_sessions.c.id == session_id)
                                .values(sdk_session_id=value)
                            )
                    elif kind == "tool":
                        flush()
                        with store.engine.begin() as conn:
                            add_message(conn, session_id, "tool", value)
                    elif kind == "evidence":
                        records = data.get("items")
                        if records is None:
                            posts = data.get("posts")
                            records = posts.get("items", []) if isinstance(posts, dict) else []
                        records = [r for r in records if "citation" in r]
                        if records:
                            with store.engine.begin() as conn:
                                # Cache the exact source returned to this conversation, so
                                # source links remain valid even when the crawler updates it.
                                if session["scope"]["kind"] != "run":
                                    for record in records:
                                        key = {
                                            "session_id": session_id,
                                            "kind": record["kind"],
                                            "content_id": record["content_id"],
                                            "item_id": record["citation"],
                                        }
                                        predicate = [
                                            analysis_sources.c[k] == v for k, v in key.items()
                                        ]
                                        if conn.execute(
                                            select(analysis_sources.c.item_id).where(*predicate)
                                        ).first():
                                            conn.execute(
                                                update(analysis_sources)
                                                .where(*predicate)
                                                .values(record=record)
                                            )
                                        else:
                                            conn.execute(
                                                insert(analysis_sources).values(
                                                    **key, record=record
                                                )
                                            )
                                add_message(
                                    conn,
                                    session_id,
                                    "sources",
                                    data={
                                        "items": [
                                            {
                                                k: r.get(k)
                                                for k in (
                                                    "citation",
                                                    "evidence_url",
                                                    "id",
                                                    "kind",
                                                    "content_id",
                                                    "platform",
                                                )
                                            }
                                            for r in records
                                        ]
                                    },
                                )

                async def execute():
                    control["loop"] = asyncio.get_running_loop()
                    # SDK cleanup uses AnyIO shielding. A raw task.cancel() can
                    # interrupt that cleanup and leave the CLI subprocess alive.
                    with anyio.CancelScope() as cancel_scope:
                        control["cancel_scope"] = cancel_scope
                        if not control["cancel"].is_set():
                            await self.runner(
                                reader, session, prompt, self.directory / session_id, emit, control
                            )

                try:
                    asyncio.run(execute())
                finally:
                    flush()
        except asyncio.CancelledError:
            final_status = "stopped"
        except AnalysisError as exc:
            final_status, error = "failed", str(exc)
        except Exception as exc:
            # SDK exception text may contain environment details or credentials.
            logger.warning("Agent analysis failed (%s)", type(exc).__name__)
            final_status = "failed"
            error = "Agent 连接失败，请检查 API 凭据、模型及网络后手动重试"
        finally:
            with self.lock:
                if control["cancel"].is_set():
                    final_status, error = "stopped", None
                try:
                    with self.store_factory() as store, store.engine.begin() as conn:
                        conn.execute(
                            update(analysis_sessions)
                            .where(analysis_sessions.c.id == session_id)
                            .values(status=final_status, error=error, updated_at=time.time())
                        )
                finally:
                    self.active.pop(session_id, None)
