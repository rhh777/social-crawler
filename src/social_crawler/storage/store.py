import hashlib
import json
import time
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    case,
    create_engine,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from social_crawler.domain.models import CollectionError, Item, Operation, PageResult, RunConfig
from social_crawler.domain.redaction import redact
from social_crawler.domain.targets import parse_target, resolved_post_target, valid_token
from social_crawler.domain.timestamps import unix_timestamp

metadata = MetaData()
DEFAULT_QUOTA_LIMITS = {
    "platform": (3600, 999999),
    "operation": (3600, 999999),
    "account": (3600, 999999),
    "ip_group": (3600, 999999),
}
runs = Table(
    "runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("platform", String, nullable=False),
    Column("config", JSON, nullable=False),
    Column("mode", String, nullable=False),
    Column("status", String, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("binding", String, nullable=False),
    Column("requests", Integer, nullable=False),
    Column("elapsed", Float, nullable=False),
    Column("cancel_requested", Boolean, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("versions", JSON, nullable=False),
)
tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("operation", String, nullable=False),
    Column("keyword", String, nullable=False),
    Column("content_id", String, nullable=False),
    Column("root_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("target", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("state", JSON, nullable=False),
)
task_items = Table(
    "task_items",
    metadata,
    Column("task_id", String, ForeignKey("tasks.id"), primary_key=True),
    Column("item_id", String, primary_key=True),
    Column("snapshot", JSON, nullable=False),
)
contents = Table(
    "contents",
    metadata,
    Column("platform", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("data", JSON, nullable=False),
    Column("observed_at", Float, nullable=False),
)
# Separate table allows an additive upgrade of existing CLI databases. Historical
# publication times can be recovered; first collection times cannot be inferred.
content_times = Table(
    "content_times",
    metadata,
    Column("platform", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("published_at", Float, index=True),
    Column("source_updated_at", Float, index=True),
    Column("first_collected_at", Float, index=True),
)

comments = Table(
    "comments",
    metadata,
    Column("platform", String, primary_key=True),
    Column("content_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("root_id", String),
    Column("parent_id", String),
    Column("data", JSON, nullable=False),
    Column("observed_at", Float, nullable=False),
)
hits = Table(
    "hits",
    metadata,
    Column("run_id", String, ForeignKey("runs.id"), primary_key=True),
    Column("keyword", String, primary_key=True),
    Column("content_id", String, primary_key=True),
)
post_sources = Table(
    "post_sources", metadata,
    Column("run_id", String, ForeignKey("runs.id"), primary_key=True),
    Column("source_id", String, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("display", String, nullable=False),
    Column("task_id", String),
    Column("content_id", String),
    Column("status", String, nullable=False),
    Column("error", String),
)
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("at", Float, nullable=False),
    Column("kind", String, nullable=False),
    Column("data", JSON, nullable=False),
)

# Additive control-plane tables deliberately avoid changing the original run/task
# tables. Existing installations can create them safely before Alembic takes over
# all future schema evolution.
accounts = Table(
    "accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("platform", String, nullable=False, index=True),
    Column("name", String, nullable=False),
    Column("environment", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("cooldown_until", Float),
    Column("last_success_at", Float),
    Column("last_failure_at", Float),
    Column("last_failure_kind", String),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("platform", "name", name="uq_accounts_platform_name"),
)
proxies = Table(
    "proxies", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("proxy_file", String),
    Column("proxy_env", String),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
system_settings = Table(
    "system_settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", JSON, nullable=False),
)
schedules = Table(
    "schedules",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    # A schedule describes collection intent. The executing account is selected
    # when an occurrence is dispatched, so disabled/cooling accounts do not pin
    # the whole schedule. Kept nullable for compatibility with pilot rows.
    Column("account_id", String, ForeignKey("accounts.id"), nullable=True, index=True),
    Column("config", JSON, nullable=False),
    Column("kind", String, nullable=False),
    Column("interval_minutes", Integer),
    Column("daily_time", String),
    Column("cron_expression", String),
    Column("timezone", String, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("deleted_at", Float),
    Column("next_run_at", Float, nullable=False, index=True),
    Column("last_dispatched_at", Float),
    Column("last_run_id", String, ForeignKey("runs.id")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
run_contexts = Table(
    "run_contexts",
    metadata,
    Column("run_id", String, ForeignKey("runs.id"), primary_key=True),
    Column("account_id", String, ForeignKey("accounts.id"), index=True),
    Column("schedule_id", String, ForeignKey("schedules.id"), index=True),
    Column("scheduled_for", Float),
    Column("environment", JSON, nullable=False),
    UniqueConstraint("schedule_id", "scheduled_for", name="uq_scheduled_occurrence"),
)
operation_metrics = Table(
    "operation_metrics",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("task_id", String, ForeignKey("tasks.id"), nullable=False),
    Column("account_id", String, ForeignKey("accounts.id"), index=True),
    Column("platform", String, nullable=False, index=True),
    Column("operation", String, nullable=False, index=True),
    Column("attempt", Integer, nullable=False),
    Column("started_at", Float, nullable=False, index=True),
    Column("duration_ms", Integer, nullable=False),
    Column("outcome", String, nullable=False, index=True),
    Column("error_kind", String),
    Column("item_count", Integer, nullable=False),
    Column("response_has_more", Boolean),
)
risk_events = Table(
    "risk_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.id"), index=True),
    Column("account_id", String, ForeignKey("accounts.id"), index=True),
    Column("platform", String, nullable=False, index=True),
    Column("operation", String),
    Column("kind", String, nullable=False, index=True),
    Column("action", String, nullable=False),
    Column("at", Float, nullable=False, index=True),
    Column("data", JSON, nullable=False),
)

# Unattended-canary safety records. Account network bindings are intentionally
# separate from the environment JSON: accepting a new exit is an auditable
# operator decision, not an incidental proxy configuration update.
account_network_bindings = Table(
    "account_network_bindings",
    metadata,
    Column("account_id", String, ForeignKey("accounts.id"), primary_key=True),
    Column("proxy_ref", String, nullable=False),
    Column("egress_ip", String, nullable=False),
    Column("ip_group", String, nullable=False, index=True),
    Column("confirmed_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
recovery_probes = Table(
    "recovery_probes",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("accounts.id"), nullable=False, index=True),
    Column("platform", String, nullable=False, index=True),
    # A nullable unique key prevents concurrent schedulers from creating two
    # active probes. It is cleared when the probe reaches a terminal state.
    Column("active_key", String, unique=True),
    Column("status", String, nullable=False, index=True),
    Column("attempt", Integer, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("next_allowed_at", Float),
    Column("request_count", Integer, nullable=False),
    Column("network_observation_id", String),
    Column("outcome", String),
    Column("error_kind", String),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
network_observations = Table(
    "network_observations",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), index=True),
    Column("probe_id", String, ForeignKey("recovery_probes.id"), index=True),
    Column("account_id", String, ForeignKey("accounts.id"), nullable=False, index=True),
    Column("platform", String, nullable=False, index=True),
    Column("proxy_ref", String, nullable=False),
    Column("egress_ip", String),
    Column("ip_group", String, index=True),
    Column("country", String),
    Column("region", String),
    Column("city", String),
    Column("asn", String),
    Column("isp", String),
    Column("source", String, nullable=False),
    Column("observed_at", Float, nullable=False, index=True),
    Column("latency_ms", Integer),
    Column("http_egress_ip", String),
    Column("browser_egress_ip", String),
    Column("outcome", String, nullable=False, index=True),
    Column("error_kind", String),
    Column("data", JSON, nullable=False),
)
quota_policies = Table(
    "quota_policies",
    metadata,
    Column("id", String, primary_key=True),
    Column("dimension", String, nullable=False, index=True),
    Column("subject", String, nullable=False, index=True),
    Column("operation", String, index=True),
    Column("window_seconds", Integer, nullable=False),
    Column("request_limit", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("supersedes_id", String, ForeignKey("quota_policies.id")),
    Column("created_at", Float, nullable=False),
    UniqueConstraint(
        "dimension", "subject", "operation", "version", name="uq_quota_policy_version"
    ),
)
quota_buckets = Table(
    "quota_buckets",
    metadata,
    Column("policy_id", String, ForeignKey("quota_policies.id"), primary_key=True),
    Column("window_start", Float, primary_key=True),
    Column("reserved_requests", Integer, nullable=False),
    Column("completed_requests", Integer, nullable=False),
    Column("updated_at", Float, nullable=False),
)
quota_reservations = Table(
    "quota_reservations",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("operation", String, nullable=False, index=True),
    Column("policy_windows", JSON, nullable=False),
    Column("requested", Integer, nullable=False),
    Column("consumed", Integer, nullable=False),
    Column("status", String, nullable=False, index=True),
    Column("created_at", Float, nullable=False),
    Column("settled_at", Float),
)
quota_policy_audits = Table(
    "quota_policy_audits",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("policy_id", String, ForeignKey("quota_policies.id"), nullable=False, index=True),
    Column("action", String, nullable=False),
    Column("at", Float, nullable=False, index=True),
    Column("data", JSON, nullable=False),
)


def merged(old: dict, new: dict) -> dict:
    """An empty observation must not erase an earlier useful field."""
    if old.get("detail_complete") and not new.get("detail_observed"):
        new = {
            k: v
            for k, v in new.items()
            if k
            not in {
                "title",
                "text",
                "author_id",
                "author_name",
                "published_at",
                "source_updated_at",
                "media",
                "detail_complete",
                "detail_observed",
                "quality",
            }
        }
    return old | {k: v for k, v in new.items() if v is not None and v != "" and v != [] and v != {}}


analysis_sessions = Table(
    "analysis_sessions", metadata,
    Column("id", String, primary_key=True),
    Column("scope", JSON, nullable=False),
    Column("coverage", JSON, nullable=False),
    Column("title", String, nullable=False),
    Column("status", String, nullable=False),
    Column("sdk_session_id", String),
    Column("model", String),
    Column("error", String),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
analysis_messages = Table(
    "analysis_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String, ForeignKey("analysis_sessions.id"), nullable=False, index=True),
    Column("role", String, nullable=False),
    Column("text", String, nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
analysis_sources = Table(
    "analysis_sources", metadata,
    Column("session_id", String, ForeignKey("analysis_sessions.id"), primary_key=True),
    Column("kind", String, primary_key=True),
    Column("content_id", String, primary_key=True),
    Column("item_id", String, primary_key=True),
    Column("record", JSON, nullable=False),
)


class Store:
    def __init__(self, url: str, *, connect_args: dict | None = None):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args or {})
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_connect(conn, _):
                conn.isolation_level = None
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=10000")

            @event.listens_for(self.engine, "begin")
            def sqlite_begin(conn):
                conn.exec_driver_sql("BEGIN IMMEDIATE")

    def initialize(self):
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            legacy = conn.execute(
                select(contents)
                .outerjoin(
                    content_times,
                    (contents.c.platform == content_times.c.platform)
                    & (contents.c.id == content_times.c.id),
                )
                .where(content_times.c.id.is_(None))
            ).mappings()
            for row in legacy:
                self._content_times(
                    conn, row["platform"], row["id"], row["data"], first=None, backfill=True
                )

    def _content_times(self, conn, platform, item_id, data, *, first, backfill=False):
        values = {
            "published_at": unix_timestamp(data.get("published_at")),
            "source_updated_at": unix_timestamp(data.get("source_updated_at")),
        }
        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        statement = insert_fn(content_times).values(
            platform=platform, id=item_id, first_collected_at=first, **values
        )
        # Concurrent readers may backfill the same legacy row. Never overwrite
        # newer worker observations or replace a known first collection time.
        if backfill:
            statement = statement.on_conflict_do_nothing(index_elements=["platform", "id"])
        else:
            statement = statement.on_conflict_do_update(
                index_elements=["platform", "id"], set_=values
            )
        conn.execute(statement)

    def close(self):
        self.engine.dispose()

    def _run(self, conn, run_id: str) -> dict:
        row = (
            conn.execute(select(runs).where(runs.c.id == run_id).with_for_update())
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("Unknown run ID")
        return dict(row)

    def _check_epoch(self, run: dict, epoch: int):
        if run["epoch"] != epoch or run["status"] != "running":
            raise CollectionError("stale_worker", "This worker no longer owns the run")

    def _event(self, conn, run_id: str, kind: str, data: dict):
        conn.execute(
            insert(events).values(run_id=run_id, at=time.time(), kind=kind, data=redact(data))
        )

    def event(self, run_id: str, kind: str, data: dict):
        with self.engine.begin() as conn:
            self._event(conn, run_id, kind, data)

    def _task(
        self,
        conn,
        run_id,
        operation,
        *,
        keyword="",
        content_id="",
        root_id="",
        target=1,
        input=None,
        identity_key=None,
    ):
        identity = json.dumps(
            [run_id, str(operation), keyword, content_id, root_id], ensure_ascii=False
        )
        if identity_key is not None:
            identity = json.dumps(["v2", identity, identity_key], ensure_ascii=False)
        task_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
        if row:
            result = dict(row)
            if input:
                state = dict(result["state"])
                incoming = {k: v for k, v in input.items() if v not in (None, "", [], {})}
                # A context-free alias must not replace a token's matching source.
                if valid_token(state["input"].get("xsec_token")) and not valid_token(incoming.get("xsec_token")):
                    incoming.pop("xsec_source", None)
                    incoming.pop("xsec_token", None)
                state["input"] = state["input"] | incoming
                conn.execute(update(tasks).where(tasks.c.id == task_id).values(state=state))
                result["state"] = state
            return result
        sequence = len(conn.execute(select(tasks.c.id).where(tasks.c.run_id == run_id)).all())
        state = {
            "input": input or {},
            "context": {},
            "count": 0,
            "pages": 0,
            "cursor_history": [],
            "no_progress": 0,
            "stop_reason": None,
            "response_has_more": None,
            "samples": [],
            "skipped": [],
            "replay_remaining": 0,
        }
        values = dict(
            id=task_id,
            run_id=run_id,
            operation=str(operation),
            keyword=keyword,
            content_id=content_id,
            root_id=root_id,
            target=target,
            sequence=sequence,
            status="pending" if target else "completed",
            state=state,
        )
        if not target:
            state["stop_reason"] = "not_requested"
        conn.execute(insert(tasks).values(**values))
        return values

    def _create_run(
        self,
        conn,
        config: RunConfig,
        *,
        mode: str,
        binding: str = "",
        versions=None,
        account_id: str | None = None,
        schedule_id: str | None = None,
        scheduled_for: float | None = None,
        environment=None,
    ) -> str:
        run_id = uuid.uuid4().hex
        now = time.time()
        conn.execute(
            insert(runs).values(
                id=run_id,
                platform=config.platform,
                config=config.model_dump(),
                mode=mode,
                status="pending",
                epoch=0,
                binding=binding,
                requests=0,
                elapsed=0,
                cancel_requested=False,
                created_at=now,
                updated_at=now,
                versions=versions or {},
            )
        )
        conn.execute(
            insert(run_contexts).values(
                run_id=run_id,
                account_id=account_id,
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
                environment=redact(environment or {}),
            )
        )
        for keyword in config.keywords:
            self._task(conn, run_id, Operation.SEARCH, keyword=keyword, target=config.content_limit)
        for position, raw_target in enumerate(config.post_targets):
            source_id = hashlib.sha256(raw_target.encode()).hexdigest()[:32]
            values = dict(
                run_id=run_id, source_id=source_id, position=position,
                display=redact(raw_target), status="pending",
            )
            try:
                target = parse_target(config.platform, raw_target)
                task = self._task(
                    conn, run_id, Operation.RESOLVE_TARGET,
                    identity_key=source_id, input={"target": target, "source_id": source_id},
                )
                values.update(task_id=task["id"], content_id=target.get("id"))
            except CollectionError as exc:
                values.update(status="invalid", error=exc.message)
            conn.execute(insert(post_sources).values(**values))
        for seed in config.seed_contents:
            content_id = seed.get("id", "")
            if not content_id:
                raise ValueError("seed_contents requires id")
            self._task(conn, run_id, Operation.DETAIL, content_id=content_id, input=seed)
        self._event(
            conn,
            run_id,
            "created",
            {"mode": mode, "account_id": account_id, "schedule_id": schedule_id},
        )
        return run_id

    def create_run(
        self,
        config: RunConfig,
        *,
        mode: str,
        binding: str = "",
        versions=None,
        account_id: str | None = None,
        schedule_id: str | None = None,
        scheduled_for: float | None = None,
        environment=None,
    ) -> str:
        with self.engine.begin() as conn:
            return self._create_run(
                conn,
                config,
                mode=mode,
                binding=binding,
                versions=versions,
                account_id=account_id,
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
                environment=environment,
            )

    def get_run(self, run_id):
        with self.engine.begin() as conn:
            return self._run(conn, run_id)

    def get_run_context(self, run_id):
        with self.engine.begin() as conn:
            row = conn.execute(
                select(run_contexts).where(run_contexts.c.run_id == run_id)
            ).mappings().first()
            return dict(row) if row else {}

    def update_run_environment(self, run_id, *, binding, environment):
        with self.engine.begin() as conn:
            self._run(conn, run_id)
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(binding=binding, updated_at=time.time())
            )
            conn.execute(
                update(run_contexts)
                .where(run_contexts.c.run_id == run_id)
                .values(environment=redact(environment))
            )

    def get_account_network_binding(self, account_id):
        with self.engine.begin() as conn:
            row = conn.execute(
                select(account_network_bindings).where(
                    account_network_bindings.c.account_id == account_id
                )
            ).mappings().first()
            return dict(row) if row else None

    def clear_account_network_binding(self, account_id):
        """Forget an accepted exit after an explicit account proxy change."""
        now = time.time()
        with self.engine.begin() as conn:
            if conn.execute(select(accounts.c.id).where(accounts.c.id == account_id)).scalar() is None:
                raise ValueError("Unknown account ID")
            conn.execute(
                account_network_bindings.delete().where(
                    account_network_bindings.c.account_id == account_id
                )
            )
            conn.execute(
                update(accounts)
                .where(
                    accounts.c.id == account_id,
                    accounts.c.status != "disabled",
                    accounts.c.last_failure_kind.in_(
                        {"egress_unconfirmed", "egress_changed", "egress_mismatch"}
                    ),
                )
                .values(
                    status="ready",
                    cooldown_until=None,
                    last_failure_at=None,
                    last_failure_kind=None,
                    updated_at=now,
                )
            )

    def confirm_account_network(self, account_id, *, proxy_ref, egress_ip, ip_group=None):
        """Accept the observed exit for an explicitly configured account network."""
        now = time.time()
        ip_group = ip_group or egress_ip
        values = dict(
            account_id=account_id,
            proxy_ref=proxy_ref,
            egress_ip=egress_ip,
            ip_group=ip_group,
            confirmed_at=now,
            updated_at=now,
        )
        with self.engine.begin() as conn:
            if conn.execute(select(accounts.c.id).where(accounts.c.id == account_id)).scalar() is None:
                raise ValueError("Unknown account ID")
            insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
            conn.execute(
                insert_fn(account_network_bindings)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={key: value for key, value in values.items() if key != "account_id"},
                )
            )
            # This is an explicit operator confirmation, so configuration-only
            # egress failures are resolved at the same audit boundary. Requiring
            # a cooldown probe and canary after a successful dual-exit check adds
            # no platform-safety signal and leaves the account unnecessarily
            # unavailable.
            conn.execute(
                update(accounts)
                .where(
                    accounts.c.id == account_id,
                    accounts.c.last_failure_kind.in_(
                        {"egress_unconfirmed", "egress_changed", "egress_mismatch"}
                    ),
                )
                .values(
                    status="ready",
                    cooldown_until=None,
                    last_failure_kind=None,
                    updated_at=now,
                )
            )
        return values

    def record_network_observation(
        self,
        *,
        account_id,
        platform,
        proxy_ref,
        observation,
        run_id=None,
        probe_id=None,
    ):
        if bool(run_id) == bool(probe_id):
            raise ValueError("A network observation belongs to exactly one run or probe")
        now = time.time()
        observation_id = uuid.uuid4().hex
        values = dict(
            id=observation_id,
            run_id=run_id,
            probe_id=probe_id,
            account_id=account_id,
            platform=platform,
            proxy_ref=proxy_ref,
            egress_ip=observation.get("egress_ip"),
            ip_group=observation.get("ip_group"),
            country=observation.get("country"),
            region=observation.get("region"),
            city=observation.get("city"),
            asn=observation.get("asn"),
            isp=observation.get("isp"),
            source=observation.get("source", "ipwho.is"),
            observed_at=float(observation.get("observed_at", now)),
            latency_ms=observation.get("latency_ms"),
            http_egress_ip=observation.get("http_egress_ip"),
            browser_egress_ip=observation.get("browser_egress_ip"),
            outcome=observation.get("outcome", "error"),
            error_kind=observation.get("error_kind"),
            data=redact(observation.get("data", {})),
        )
        with self.engine.begin() as conn:
            account = conn.execute(
                select(accounts).where(accounts.c.id == account_id)
            ).mappings().first()
            if account is None or account["platform"] != platform:
                raise ValueError("Account does not match network observation")
            if run_id:
                run = self._run(conn, run_id)
                context = conn.execute(
                    select(run_contexts).where(run_contexts.c.run_id == run_id)
                ).mappings().first()
                if run["platform"] != platform or not context or context["account_id"] != account_id:
                    raise ValueError("Run does not match network observation")
            if probe_id:
                probe = conn.execute(
                    select(recovery_probes).where(recovery_probes.c.id == probe_id)
                ).mappings().first()
                if not probe or probe["account_id"] != account_id:
                    raise ValueError("Probe does not match network observation")
            conn.execute(insert(network_observations).values(**values))
            if probe_id:
                conn.execute(
                    update(recovery_probes)
                    .where(recovery_probes.c.id == probe_id)
                    .values(network_observation_id=observation_id, updated_at=now)
                )
            if run_id and values["outcome"] == "success":
                environment = dict(context["environment"])
                environment.update(
                    network_observation_id=observation_id,
                    egress_ip=values["egress_ip"],
                    ip_group=values["ip_group"],
                    proxy_ref=proxy_ref,
                )
                conn.execute(
                    update(run_contexts)
                    .where(run_contexts.c.run_id == run_id)
                    .values(environment=redact(environment))
                )
        return values

    def verify_run_network(self, run_id, observation):
        context = self.get_run_context(run_id)
        run = self.get_run(run_id)
        account_id = context.get("account_id")
        if not account_id:
            raise CollectionError("account_unavailable")
        recorded = self.record_network_observation(
            run_id=run_id,
            account_id=account_id,
            platform=run["platform"],
            proxy_ref=observation["proxy_ref"],
            observation=observation,
        )
        environment = context.get("environment") or {}
        self.verify_account_network(
            account_id,
            recorded,
            accept_unconfirmed=environment.get("auto_bind_network") is True,
        )
        if environment.get("use_default_quotas") is True:
            self.ensure_default_quota_coverage(
                account_id,
                run["platform"],
                recorded["ip_group"],
            )
        return recorded

    def verify_account_network(self, account_id, observation, *, accept_unconfirmed=False):
        recorded = observation
        if recorded["outcome"] != "success":
            raise CollectionError(recorded["error_kind"] or "proxy_unavailable")
        binding = self.get_account_network_binding(account_id)
        if binding is None:
            if accept_unconfirmed:
                self.confirm_account_network(
                    account_id,
                    proxy_ref=recorded["proxy_ref"],
                    egress_ip=recorded["egress_ip"],
                    ip_group=recorded["ip_group"],
                )
                return recorded
            raise CollectionError(
                "egress_unconfirmed", "Observed exit has not been accepted for this account"
            )
        if binding["proxy_ref"] != recorded["proxy_ref"]:
            raise CollectionError("egress_changed", "The account proxy fingerprint changed")
        if binding["ip_group"] != recorded["ip_group"]:
            raise CollectionError("egress_changed", "The observed exit is outside the accepted group")
        return recorded

    def ensure_default_quota_coverage(self, account_id, platform, ip_group):
        """Create inherited Web defaults only where no policy was ever configured."""
        specs = [
            ("platform", platform, None),
            *(("operation", platform, str(operation)) for operation in Operation),
            ("account", account_id, None),
            ("ip_group", ip_group, None),
        ]
        created = []
        now = time.time()
        insert_fn = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        with self.engine.begin() as conn:
            for dimension, subject, operation in specs:
                operation_clause = (
                    quota_policies.c.operation.is_(None)
                    if operation is None
                    else quota_policies.c.operation == operation
                )
                if conn.execute(
                    select(quota_policies.c.id).where(
                        quota_policies.c.dimension == dimension,
                        quota_policies.c.subject == subject,
                        operation_clause,
                    ).limit(1)
                ).first():
                    continue
                window_seconds, request_limit = DEFAULT_QUOTA_LIMITS[dimension]
                policy_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"social-crawler:default-quota:{dimension}:{subject}:{operation or ''}",
                ).hex
                values = dict(
                    id=policy_id,
                    dimension=dimension,
                    subject=subject,
                    operation=operation,
                    window_seconds=window_seconds,
                    request_limit=request_limit,
                    enabled=True,
                    version=1,
                    supersedes_id=None,
                    created_at=now,
                )
                result = conn.execute(
                    insert_fn(quota_policies)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                if result.rowcount:
                    conn.execute(
                        insert(quota_policy_audits).values(
                            policy_id=policy_id,
                            action="default_created",
                            at=now,
                            data={
                                "window_seconds": window_seconds,
                                "request_limit": request_limit,
                                "enabled": True,
                            },
                        )
                    )
                    created.append(values)
        return created

    def list_runs(self, limit=20):
        with self.engine.begin() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        runs.c.id,
                        runs.c.platform,
                        runs.c.mode,
                        runs.c.status,
                        runs.c.requests,
                        runs.c.created_at,
                    )
                    .order_by(runs.c.created_at.desc())
                    .limit(max(1, min(limit, 100)))
                ).mappings()
            ]

    def pending_scheduled_runs(self):
        with self.engine.begin() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        runs.c.id.label("run_id"),
                        run_contexts.c.account_id,
                        run_contexts.c.schedule_id,
                        run_contexts.c.scheduled_for,
                    )
                    .join(run_contexts, run_contexts.c.run_id == runs.c.id)
                    .where(
                        runs.c.status == "pending",
                        run_contexts.c.schedule_id.is_not(None),
                    )
                    .order_by(run_contexts.c.scheduled_for)
                ).mappings()
            ]

    def start(
        self,
        run_id: str,
        *,
        binding: str,
        reset_contexts=False,
        resume=False,
        extra_requests=0,
        extra_seconds=0,
        preserve_cancel=False,
    ) -> int:
        """Caller holds local account/profile locks throughout this invocation."""
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            if run["status"] != "pending" and not resume:
                raise ValueError("Existing run requires explicit resume")
            if run["status"] == "completed":
                raise ValueError("Completed run is immutable; create a new observation")
            if extra_requests < 0 or extra_seconds < 0:
                raise ValueError("Additional budgets must be nonnegative")
            config = dict(run["config"])
            config["max_requests"] += extra_requests
            config["max_seconds"] += extra_seconds
            RunConfig.model_validate(config)
            epoch = run["epoch"] + 1
            elapsed = run["elapsed"]
            if run["status"] == "running":
                started_at = conn.execute(
                    select(events.c.at)
                    .where(events.c.run_id == run_id, events.c.kind == "attempt_started")
                    .order_by(events.c.id.desc())
                    .limit(1)
                ).scalar()
                # After a hard crash, conservatively charge time since the last start,
                # including downtime. An explicit budget increase can then resume it.
                if started_at is not None:
                    elapsed += max(0, time.time() - started_at)
            rebuild = reset_contexts or binding != run["binding"]
            for row in conn.execute(select(tasks).where(tasks.c.run_id == run_id)).mappings():
                if row["status"] == "completed":
                    continue
                state = dict(row["state"])
                if (
                    row["status"] == "partial"
                    and row["content_id"]
                    and row["operation"] in {"detail", "comments", "replies"}
                    and state.get("stop_reason") == "content_unavailable"
                ):
                    # Preserve the coverage gap without retrying a known unavailable
                    # item each time the rest of this observation is resumed.
                    continue
                if row["operation"] == "replies":
                    root_data = conn.execute(
                        select(comments.c.data).where(
                            comments.c.platform == run["platform"],
                            comments.c.content_id == row["content_id"],
                            comments.c.id == row["root_id"],
                        )
                    ).scalar()
                    if root_data:
                        sub_cursor = root_data.get("sub_cursor")
                        if not sub_cursor and state["count"] == 1:
                            saved_reply_ids = (
                                conn.execute(
                                    select(task_items.c.item_id).where(
                                        task_items.c.task_id == row["id"]
                                    )
                                )
                                .scalars()
                                .all()
                            )
                            if len(saved_reply_ids) == 1:
                                sub_cursor = saved_reply_ids[0]
                        task_input = dict(state["input"])
                        task_input["root_comment"] = {
                            "id": row["root_id"],
                            "text": root_data.get("text"),
                            "reply_count": root_data.get("reply_count"),
                            "sub_cursor": sub_cursor,
                        }
                        state["input"] = task_input
                if rebuild:
                    state.update(
                        context={},
                        cursor_history=[],
                        no_progress=0,
                        replay_remaining=state["pages"],
                    )
                state.update(stop_reason=None, error=None)
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == row["id"])
                    .values(status="pending", state=state)
                )
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="running",
                    epoch=epoch,
                    binding=binding,
                    config=config,
                    elapsed=elapsed,
                    cancel_requested=run["cancel_requested"]
                    if preserve_cancel or not resume
                    else False,
                    updated_at=time.time(),
                )
            )
            self._event(
                conn, run_id, "attempt_started", {"epoch": epoch, "context_rebuilt": rebuild}
            )
            return epoch

    def request_cancel(self, run_id):
        with self.engine.begin() as conn:
            self._run(conn, run_id)
            conn.execute(update(runs).where(runs.c.id == run_id).values(cancel_requested=True))
            self._event(conn, run_id, "cancel_requested", {})

    def reserve_request(self, run_id, epoch, operation):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            self._check_epoch(run, epoch)
            if run["cancel_requested"]:
                raise CollectionError("canceled")
            if run["requests"] >= run["config"]["max_requests"]:
                raise CollectionError("request_budget")
            reservation_id = None
            if run["mode"] == "online":
                reservation_id = self._reserve_window_quotas(conn, run, operation)
            conn.execute(
                update(runs).where(runs.c.id == run_id).values(requests=run["requests"] + 1)
            )
            self._event(
                conn,
                run_id,
                "request_admitted",
                {
                    "operation": operation,
                    "number": run["requests"] + 1,
                    "quota_reservation_id": reservation_id,
                },
            )
            return reservation_id

    def _reserve_window_quotas(self, conn, run, operation, *, now=None):
        now = float(now or time.time())
        context = conn.execute(
            select(run_contexts).where(run_contexts.c.run_id == run["id"])
        ).mappings().first()
        environment = dict(context["environment"]) if context else {}
        account_id = context["account_id"] if context else None
        ip_group = environment.get("ip_group")
        operation_policy = operation
        if operation == "resolve_target" and not conn.execute(select(quota_policies.c.id).where(
            quota_policies.c.dimension == "operation",
            quota_policies.c.subject == run["platform"], quota_policies.c.operation == operation,
        )).first():
            # Existing installations share the search bucket until a dedicated
            # resolver policy is configured. Global/account/IP caps still apply.
            operation_policy = "search"
        subjects = [
            and_(
                quota_policies.c.dimension == "platform",
                quota_policies.c.subject == run["platform"],
            ),
            and_(
                quota_policies.c.dimension == "operation",
                quota_policies.c.subject == run["platform"],
                quota_policies.c.operation == operation_policy,
            ),
        ]
        if account_id:
            subjects.append(
                and_(
                    quota_policies.c.dimension == "account",
                    quota_policies.c.subject == account_id,
                )
            )
        if ip_group:
            subjects.append(
                and_(
                    quota_policies.c.dimension == "ip_group",
                    quota_policies.c.subject == ip_group,
                )
            )
        policies = [
            dict(row)
            for row in conn.execute(
                select(quota_policies)
                .where(
                    quota_policies.c.enabled.is_(True),
                    or_(*subjects),
                    or_(
                        quota_policies.c.operation.is_(None),
                        quota_policies.c.operation == operation,
                        and_(quota_policies.c.dimension == "operation",
                             quota_policies.c.operation == operation_policy),
                    ),
                )
                .order_by(quota_policies.c.id)
            ).mappings()
        ]
        required_dimensions = {"platform", "operation", "account", "ip_group"}
        covered_dimensions = {policy["dimension"] for policy in policies}
        if covered_dimensions != required_dimensions:
            missing = sorted(required_dimensions - covered_dimensions)
            raise CollectionError(
                "quota_policy_missing",
                "Online request is missing quota coverage: " + ", ".join(missing),
            )
        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        windows = []
        exhausted_until = []
        for policy in policies:
            window_start = float(int(now // policy["window_seconds"]) * policy["window_seconds"])
            conn.execute(
                insert_fn(quota_buckets)
                .values(
                    policy_id=policy["id"],
                    window_start=window_start,
                    reserved_requests=0,
                    completed_requests=0,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["policy_id", "window_start"])
            )
            bucket_query = select(quota_buckets).where(
                quota_buckets.c.policy_id == policy["id"],
                quota_buckets.c.window_start == window_start,
            )
            if conn.dialect.name == "postgresql":
                bucket_query = bucket_query.with_for_update()
            bucket = conn.execute(bucket_query).mappings().one()
            if bucket["reserved_requests"] + 1 > policy["request_limit"]:
                exhausted_until.append(window_start + policy["window_seconds"])
            windows.append(
                {
                    "policy_id": policy["id"],
                    "window_start": window_start,
                    "window_end": window_start + policy["window_seconds"],
                }
            )
        if exhausted_until:
            raise CollectionError(
                "quota_exhausted",
                "One or more request windows are exhausted",
                retry_after=min(exhausted_until),
            )
        for window in windows:
            conn.execute(
                update(quota_buckets)
                .where(
                    quota_buckets.c.policy_id == window["policy_id"],
                    quota_buckets.c.window_start == window["window_start"],
                )
                .values(
                    reserved_requests=quota_buckets.c.reserved_requests + 1,
                    updated_at=now,
                )
            )
        reservation_id = uuid.uuid4().hex
        conn.execute(
            insert(quota_reservations).values(
                id=reservation_id,
                run_id=run["id"],
                operation=operation,
                policy_windows=windows,
                requested=1,
                consumed=0,
                status="reserved",
                created_at=now,
                settled_at=None,
            )
        )
        return reservation_id

    def create_quota_policy(
        self,
        dimension,
        subject,
        *,
        operation=None,
        window_seconds,
        request_limit,
        enabled=True,
    ):
        if dimension not in {"platform", "operation", "account", "ip_group"}:
            raise ValueError("Invalid quota dimension")
        subject = str(subject).strip()
        if not subject:
            raise ValueError("Quota subject is required")
        valid_operations = {str(value) for value in Operation}
        if operation is not None and operation not in valid_operations:
            raise ValueError("Quota policy operation is invalid")
        if dimension == "operation" and operation is None:
            raise ValueError("Operation quota requires an operation")
        if dimension in {"platform", "operation"} and subject not in {
            "xhs",
            "rednote",
            "douyin",
        }:
            raise ValueError("Platform quota subject is invalid")
        if not 60 <= int(window_seconds) <= 31 * 86400 or int(request_limit) < 1:
            raise ValueError("Invalid quota window or request limit")
        now = time.time()
        with self.engine.begin() as conn:
            prior = conn.execute(
                select(quota_policies)
                .where(
                    quota_policies.c.dimension == dimension,
                    quota_policies.c.subject == subject,
                    quota_policies.c.operation.is_(None)
                    if operation is None
                    else quota_policies.c.operation == operation,
                )
                .order_by(quota_policies.c.version.desc())
                .limit(1)
            ).mappings().first()
            policy_id = uuid.uuid4().hex
            version = (prior["version"] if prior else 0) + 1
            if prior and prior["enabled"]:
                conn.execute(
                    update(quota_policies)
                    .where(quota_policies.c.id == prior["id"])
                    .values(enabled=False)
                )
            values = dict(
                id=policy_id,
                dimension=dimension,
                subject=subject,
                operation=operation,
                window_seconds=int(window_seconds),
                request_limit=int(request_limit),
                enabled=bool(enabled),
                version=version,
                supersedes_id=prior["id"] if prior else None,
                created_at=now,
            )
            conn.execute(insert(quota_policies).values(**values))
            conn.execute(
                insert(quota_policy_audits).values(
                    policy_id=policy_id,
                    action="created" if prior is None else "replaced",
                    at=now,
                    data=redact(
                        {
                            "supersedes_id": values["supersedes_id"],
                            "window_seconds": values["window_seconds"],
                            "request_limit": values["request_limit"],
                            "enabled": values["enabled"],
                        }
                    ),
                )
            )
        return values

    def quota_summary(self, *, now=None):
        now = float(now or time.time())
        with self.engine.begin() as conn:
            rows = []
            for policy in conn.execute(
                select(quota_policies)
                .where(quota_policies.c.enabled.is_(True))
                .order_by(quota_policies.c.dimension, quota_policies.c.subject)
            ).mappings():
                rows.append(self._quota_usage(conn, policy, now))
        return rows

    @staticmethod
    def _quota_usage(conn, policy, now):
        window_start = float(int(now // policy["window_seconds"]) * policy["window_seconds"])
        bucket = conn.execute(
            select(quota_buckets).where(
                quota_buckets.c.policy_id == policy["id"],
                quota_buckets.c.window_start == window_start,
            )
        ).mappings().first()
        used = bucket["reserved_requests"] if bucket else 0
        return dict(policy) | {
            "window_start": window_start,
            "window_end": window_start + policy["window_seconds"],
            "used": used,
            "completed": bucket["completed_requests"] if bucket else 0,
            "remaining": max(0, policy["request_limit"] - used),
        }

    def _settle_quota_reservations(self, conn, run_id, operation, *, consumed=1):
        now = time.time()
        reservations = conn.execute(
            select(quota_reservations).where(
                quota_reservations.c.run_id == run_id,
                quota_reservations.c.operation == operation,
                quota_reservations.c.status == "reserved",
            )
        ).mappings().all()
        for reservation in reservations:
            actual = reservation["requested"] if consumed else 0
            for window in reservation["policy_windows"]:
                if actual:
                    conn.execute(
                        update(quota_buckets)
                        .where(
                            quota_buckets.c.policy_id == window["policy_id"],
                            quota_buckets.c.window_start == window["window_start"],
                        )
                        .values(
                            completed_requests=quota_buckets.c.completed_requests + actual,
                            updated_at=now,
                        )
                    )
            conn.execute(
                update(quota_reservations)
                .where(quota_reservations.c.id == reservation["id"])
                .values(
                    consumed=actual,
                    status="consumed" if actual else "not_sent",
                    settled_at=now,
                )
            )

    def next_task(self, run_id, epoch, *, preferred_content_id=None, defer_search=False):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            self._check_epoch(run, epoch)
            if run["cancel_requested"]:
                raise CollectionError("canceled")
            ordering = [tasks.c.sequence]
            if defer_search:
                # Browser notes open from the loaded result page; finish them
                # before another search replaces it.
                ordering.insert(0, case((tasks.c.operation == "search", 1), else_=0))
            if preferred_content_id:
                ordering.insert(0, case((tasks.c.content_id == preferred_content_id, 0), else_=1))
            row = (
                conn.execute(
                    select(tasks)
                    .where(tasks.c.run_id == run_id, tasks.c.status.in_(["pending", "running"]))
                    .order_by(*ordering)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            conn.execute(update(tasks).where(tasks.c.id == row["id"]).values(status="running"))
            return dict(row)

    def _save_item(self, conn, run, task, item: Item):
        item.data = item.data | {"source_operation": task["operation"], "observed_at": time.time()}
        platform = run["platform"]
        table = contents if item.kind == "content" else comments
        key = {"platform": platform, "id": item.id}
        if item.kind == "comment":
            key["content_id"] = item.content_id
        predicate = [table.c[k] == v for k, v in key.items()]
        existing = conn.execute(select(table).where(*predicate)).mappings().first()
        data = merged(existing["data"] if existing else {}, item.data)
        values = dict(data=data, observed_at=time.time())
        if item.kind == "comment":
            values.update(root_id=item.root_id, parent_id=item.parent_id)
        if existing:
            conn.execute(update(table).where(*predicate).values(**values))
        else:
            conn.execute(insert(table).values(**key, **values))
        if item.kind == "content":
            self._content_times(
                conn, platform, item.id, data, first=None if existing else time.time()
            )
        predicate = (task_items.c.task_id == task["id"], task_items.c.item_id == item.id)
        snapshot = item.model_dump(exclude={"children"})
        if conn.execute(select(task_items.c.item_id).where(*predicate)).first():
            conn.execute(update(task_items).where(*predicate).values(snapshot=snapshot))
        else:
            conn.execute(
                insert(task_items).values(task_id=task["id"], item_id=item.id, snapshot=snapshot)
            )

    def _derive(self, conn, run, task, item):
        config = RunConfig.model_validate(run["config"])
        if task["operation"] == "search":
            found = conn.execute(
                select(hits).where(
                    hits.c.run_id == run["id"],
                    hits.c.keyword == task["keyword"],
                    hits.c.content_id == item.id,
                )
            ).first()
            if not found:
                conn.execute(
                    insert(hits).values(
                        run_id=run["id"], keyword=task["keyword"], content_id=item.id
                    )
                )
            # Browser note tasks reopen these results rather than note documents.
            self._task(
                conn, run["id"], "detail", content_id=item.id,
                input=item.data | {"search_keyword": task["keyword"]},
            )
        elif task["operation"] == "detail":
            if not item.data.get("detail_complete"):
                return
            self._task(
                conn,
                run["id"],
                "comments",
                content_id=item.id,
                target=-1 if config.unlimited_comments else config.comment_limit,
                input=item.data | {
                    k: v for k, v in task["state"]["input"].items() if k == "search_keyword"
                },
            )
        elif task["operation"] == "comments" and config.reply_parents and config.reply_limit:
            if not (item.children or (item.data.get("reply_count") or 0) > 0):
                return
            existing = (
                conn.execute(
                    select(tasks).where(
                        tasks.c.run_id == run["id"],
                        tasks.c.operation == "replies",
                        tasks.c.content_id == item.content_id,
                    )
                )
                .mappings()
                .all()
            )
            if len(existing) >= config.reply_parents:
                return
            child_task = self._task(
                conn,
                run["id"],
                "replies",
                content_id=item.content_id,
                root_id=item.id,
                target=config.reply_limit,
                input=dict(task["state"]["input"])
                | {
                    "root_comment": {
                        "id": item.id,
                        "text": item.data.get("text"),
                        "reply_count": item.data.get("reply_count"),
                        "sub_cursor": item.data.get("sub_cursor"),
                    }
                },
            )
            child_state = dict(child_task["state"])
            seen = set()
            for child in item.children:
                if (
                    child.kind != "comment"
                    or child.content_id != item.content_id
                    or child.root_id != item.id
                ):
                    raise CollectionError("schema_changed", "Embedded reply has wrong scope")
                if child.id not in seen and len(seen) < config.reply_limit:
                    self._save_item(conn, run, child_task, child)
                    seen.add(child.id)
            child_state["count"] = len(seen)
            if item.data.get("sub_cursor"):
                child_state["context"] = {"cursor": item.data["sub_cursor"]}
            if len(seen) >= config.reply_limit:
                child_state.update(
                    stop_reason="limit", response_has_more=item.data.get("sub_has_more")
                )
                status = "completed"
            elif item.data.get("sub_has_more") is False:
                child_state.update(stop_reason="exhausted", response_has_more=False)
                status = "completed"
            else:
                status = "pending"
            conn.execute(
                update(tasks)
                .where(tasks.c.id == child_task["id"])
                .values(state=child_state, status=status)
            )

    def post_context(self, run_id, content_id):
        """Prefer a supplied valid token, then previously observed content context."""
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            existing = conn.execute(select(contents.c.data).where(
                contents.c.platform == run["platform"], contents.c.id == content_id,
            )).scalar() or {}
            context = {k: existing[k] for k in ("xsec_token", "xsec_source") if existing.get(k)}
            for raw in run["config"].get("post_targets", []):
                try:
                    target = parse_target(run["platform"], raw)
                except CollectionError:
                    continue
                if target.get("id") == content_id and valid_token(target.get("xsec_token")):
                    context = target
            for state in conn.execute(select(tasks.c.state).where(
                tasks.c.run_id == run_id, tasks.c.operation == "resolve_target",
            )).scalars():
                target = state.get("resolved_target") or {}
                if target.get("id") == content_id and valid_token(target.get("xsec_token")):
                    context = target
            return {k: context[k] for k in ("xsec_token", "xsec_source") if context.get(k)}

    def _commit_target(self, conn, run, task, page, fault):
        target = page.resolved_target
        if not target or not target.get("id"):
            raise CollectionError("invalid_target", "未解析到帖子 ID")
        checked = resolved_post_target(
            run["platform"],
            target["id"],
            **{
                key: target[key]
                for key in ("xsec_token", "xsec_source")
                if target.get(key)
            },
        )
        target = dict(target) | {"id": checked["id"], "url": checked["url"]}
        if page.items:
            raise CollectionError("schema_changed", "解析任务不能写入内容")
        detail = self._task(conn, run["id"], "detail", content_id=target["id"], input=target | {"targeted_post": "1"})
        conn.execute(update(post_sources).where(
            post_sources.c.run_id == run["id"],
            post_sources.c.source_id == task["state"]["input"]["source_id"],
        ).values(content_id=target["id"], status="resolved", task_id=detail["id"], error=None))
        if fault:
            fault("after_items")
        state = dict(task["state"]) | {
            "count": 1, "pages": task["state"]["pages"] + 1,
            "response_has_more": False, "stop_reason": "target_resolved",
            "resolved_target": target,
        }
        state.pop("error", None)
        conn.execute(update(tasks).where(tasks.c.id == task["id"]).values(
            state=state, status="completed",
        ))
        self._event(conn, run["id"], "target_resolved", {"content_id": target["id"]})
        if fault:
            fault("before_commit")
        return {"status": "completed", "state": state}

    def commit_page(self, run_id, task_id, epoch, page: PageResult, *, fault=None) -> dict:
        """Commit items, derived tasks, coverage and cursor as one atomic unit."""
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            self._check_epoch(run, epoch)
            task = dict(
                conn.execute(select(tasks).where(tasks.c.id == task_id, tasks.c.run_id == run_id))
                .mappings()
                .one()
            )
            if task["status"] not in {"running", "pending"}:
                raise CollectionError("stale_worker", "Task already ended")
            if task["operation"] == "resolve_target":
                return self._commit_target(conn, run, task, page, fault)
            config = RunConfig.model_validate(run["config"])
            state = dict(task["state"])
            seen = set(
                conn.execute(
                    select(task_items.c.item_id).where(task_items.c.task_id == task_id)
                ).scalars()
            )
            before = len(seen)
            for item in page.items:
                expected_kind = (
                    "content" if task["operation"] in {"search", "detail"} else "comment"
                )
                if item.kind != expected_kind or (
                    task["content_id"] and item.content_id != task["content_id"]
                ):
                    raise CollectionError("schema_changed", "Item does not match task scope")
                if task["operation"] == "replies" and item.root_id != task["root_id"]:
                    raise CollectionError("schema_changed", "Reply has wrong root")
                if task["operation"] != "detail" and (
                    item.id in seen
                    or (task["target"] >= 0 and len(seen) >= task["target"])
                ):
                    continue
                self._save_item(conn, run, task, item)
                self._derive(conn, run, task, item)
                seen.add(item.id)
            if fault:
                fault("after_items")
            state["count"] = len(seen)
            state["pages"] += 1
            state["response_has_more"] = page.response_has_more
            state["skipped"] = state["skipped"] + page.skipped
            if page.sample_ref:
                state["samples"] = state["samples"] + [page.sample_ref]
            new_count = len(seen) - before
            context_key = json.dumps(page.next_context, sort_keys=True)
            loop = bool(page.next_context) and context_key in state["cursor_history"]
            replay = state.get("replay_remaining", 0)
            state["replay_remaining"] = max(0, replay - 1)
            state["no_progress"] = state["no_progress"] + 1 if new_count == 0 and not replay else 0
            reason = page.stop_reason
            if task["operation"] == "detail" and page.items:
                reason = page.stop_reason or (
                    "detail_obtained"
                    if all(i.data.get("detail_complete") for i in page.items)
                    else "incomplete_fields"
                )
                status = "completed" if reason == "detail_obtained" else "partial"
            elif task["target"] >= 0 and len(seen) >= task["target"]:
                reason, status = "limit", "completed"
            elif page.response_has_more is False:
                reason, status = "exhausted", "completed"
            elif reason:
                status = "partial"
            elif page.response_has_more is True and not page.next_context:
                reason, status = "missing_cursor", "partial"
            elif loop or state["no_progress"] >= config.max_no_progress:
                reason, status = "no_progress", "partial"
            elif state["pages"] >= config.max_pages:
                reason, status = "page_limit", "partial"
            elif not page.next_context:
                reason, status = "unknown_extent", "partial"
            else:
                status = "pending"
            # Scope limits are not evidence that the platform is exhausted.
            state["stop_reason"] = reason
            if page.next_context:
                state["context"] = page.next_context
                state["cursor_history"] = state["cursor_history"] + [context_key]
            conn.execute(
                update(tasks).where(tasks.c.id == task_id).values(state=state, status=status)
            )
            self._event(
                conn,
                run_id,
                "page_saved",
                {
                    "task_id": task_id,
                    "operation": task["operation"],
                    "new_items": new_count,
                    "count": len(seen),
                    "status": status,
                    "stop_reason": reason,
                },
            )
            if fault:
                fault("before_commit")
        if fault:
            fault("after_commit")
        return {"status": status, "state": state}

    def fail_task(self, run_id, task_id, epoch, error: CollectionError):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            self._check_epoch(run, epoch)
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
            state = dict(task["state"])
            if error.sample_ref:
                state["samples"] = list(dict.fromkeys(state["samples"] + [error.sample_ref]))
            state.update(
                stop_reason=error.kind,
                error={
                    "kind": error.kind,
                    "message": error.message,
                    "retry_after": error.retry_after,
                },
            )
            if task["operation"] == "resolve_target":
                conn.execute(update(post_sources).where(
                    post_sources.c.run_id == run_id,
                    post_sources.c.source_id == state["input"]["source_id"],
                ).values(status=error.outcome, error=error.message))
            skipped = (
                error.kind == "content_unavailable"
                and task["content_id"]
                and task["operation"] in {"detail", "comments", "replies"}
            )
            if skipped:
                entry = {"id": task["content_id"], "reason": error.kind}
                state["skipped"] = state.get("skipped", [])
                if entry not in state["skipped"]:
                    state["skipped"].append(entry)
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(state=state, status=error.outcome)
            )
            self._event(
                conn, run_id, "task_skipped" if skipped else "operation_failed",
                {"task_id": task_id, "error": state["error"]},
            )

    def finish(
        self,
        run_id,
        epoch,
        *,
        elapsed: float,
        reason: str | None = None,
        outcome: str | None = None,
        blocked=False,
    ):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            self._check_epoch(run, epoch)
            if run["cancel_requested"]:
                reason = "canceled"
            statuses = []
            for task in conn.execute(select(tasks).where(tasks.c.run_id == run_id)).mappings():
                status = task["status"]
                if status in {"pending", "running"}:
                    status = (
                        "canceled"
                        if reason == "canceled"
                        else outcome
                        or ("blocked" if blocked else "partial")
                    )
                    state = dict(task["state"])
                    state["stop_reason"] = reason or "interrupted"
                    conn.execute(
                        update(tasks)
                        .where(tasks.c.id == task["id"])
                        .values(status=status, state=state)
                    )
                statuses.append(status)
            if conn.execute(select(post_sources.c.source_id).where(
                post_sources.c.run_id == run_id, post_sources.c.status == "invalid",
            )).first():
                statuses.append("partial")
            status = (
                "canceled"
                if reason == "canceled"
                else "failed"
                if outcome == "failed" or "failed" in statuses
                else "blocked"
                if outcome == "blocked" or blocked or "blocked" in statuses
                else "completed"
                if all(s == "completed" for s in statuses)
                else "partial"
            )
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status=status, elapsed=run["elapsed"] + max(0, elapsed), updated_at=time.time()
                )
            )
            self._event(
                conn,
                run_id,
                "attempt_finished",
                {"epoch": epoch, "status": status, "reason": reason},
            )
            context = conn.execute(
                select(run_contexts).where(run_contexts.c.run_id == run_id)
            ).mappings().first()
            if context and context["account_id"]:
                values = {"updated_at": time.time()}
                if status == "completed":
                    values["last_success_at"] = time.time()
                # Account failure state is owned by record_risk and the recovery
                # state machine. A run-local stop such as request_budget must not
                # overwrite the reason for an existing account cooldown.
                conn.execute(
                    update(accounts)
                    .where(accounts.c.id == context["account_id"])
                    .values(**values)
                )
        return status

    def list_proxies(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(
                select(proxies).order_by(proxies.c.created_at, proxies.c.id)
            ).mappings()]

    def get_proxy(self, proxy_id):
        with self.engine.connect() as conn:
            row = conn.execute(select(proxies).where(proxies.c.id == proxy_id)).mappings().first()
            if row is None:
                raise ValueError("代理不存在，请刷新列表后重试")
            return dict(row)

    def upsert_proxy(self, proxy_id, name, *, proxy_file=None, proxy_env=None, sync_accounts=False):
        now = time.time()
        values = dict(name=name, proxy_file=proxy_file, proxy_env=proxy_env, updated_at=now)
        with self.engine.begin() as conn:
            exists = conn.scalar(select(proxies.c.id).where(proxies.c.id == proxy_id))
            if exists:
                conn.execute(update(proxies).where(proxies.c.id == proxy_id).values(**values))
            else:
                conn.execute(insert(proxies).values(id=proxy_id, created_at=now, **values))
            if sync_accounts:
                rows = conn.execute(select(accounts).where(accounts.c.status != "deleted")).mappings()
                for row in rows:
                    if row["environment"].get("proxy_id") != proxy_id:
                        continue
                    environment = row["environment"] | {"proxy_file": proxy_file, "proxy_env": proxy_env}
                    conn.execute(update(accounts).where(accounts.c.id == row["id"]).values(
                        environment=environment, updated_at=now,
                    ))
        return self.get_proxy(proxy_id)

    def delete_proxy(self, proxy_id):
        with self.engine.begin() as conn:
            conn.execute(proxies.delete().where(proxies.c.id == proxy_id))

    def upsert_account(self, account_id, platform, name, environment, *, status="ready"):
        if status not in {
            "ready",
            "cooling",
            "probe_due",
            "recovering",
            "canary_running",
            "login_required",
            "disabled",
        }:
            raise ValueError("Invalid account status")
        now = time.time()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(accounts).where(accounts.c.id == account_id)
            ).mappings().first()
            values = dict(
                platform=platform,
                name=name,
                environment=environment,
                status=status if not existing else existing["status"],
                updated_at=now,
            )
            if existing:
                conn.execute(update(accounts).where(accounts.c.id == account_id).values(**values))
            else:
                conn.execute(
                    insert(accounts).values(
                        id=account_id,
                        created_at=now,
                        cooldown_until=None,
                        last_success_at=None,
                        last_failure_at=None,
                        last_failure_kind=None,
                        **values,
                    )
                )
        return self.get_account(account_id)

    def create_account(self, platform, name, environment):
        return self.upsert_account(uuid.uuid4().hex, platform, name, environment)

    def get_account(self, account_id):
        with self.engine.begin() as conn:
            row = conn.execute(select(accounts).where(accounts.c.id == account_id)).mappings().first()
            if row is None:
                raise ValueError("Unknown account ID")
            return dict(row)

    def automatic_cooldown_enabled(self):
        with self.engine.connect() as conn:
            return self._automatic_cooldown_enabled(conn)

    @staticmethod
    def _automatic_cooldown_enabled(conn):
        return conn.execute(
            select(system_settings.c.value).where(system_settings.c.key == "automatic_cooldown")
        ).scalar_one_or_none() is True

    def set_automatic_cooldown(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("automatic_cooldown must be a boolean")
        with self.engine.begin() as conn:
            statement = (pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert)(
                system_settings
            ).values(key="automatic_cooldown", value=enabled)
            conn.execute(statement.on_conflict_do_update(
                index_elements=["key"], set_={"value": enabled},
            ))

    def delete_account(self, account_id):
        # Keep historical foreign keys and files; a tombstone also prevents legacy
        # account seeding from silently recreating a deleted account on restart.
        self.get_account(account_id)
        with self.engine.begin() as conn:
            conn.execute(update(accounts).where(accounts.c.id == account_id).values(
                status="deleted", cooldown_until=None, updated_at=time.time(),
            ))
            conn.execute(update(recovery_probes).where(
                recovery_probes.c.active_key == account_id,
            ).values(active_key=None, status="completed", outcome="deleted",
                     finished_at=time.time(), updated_at=time.time()))

    def list_accounts(self, platform=None, *, include_deleted=False):
        with self.engine.begin() as conn:
            statement = select(accounts).order_by(accounts.c.platform, accounts.c.created_at)
            if not include_deleted:
                statement = statement.where(accounts.c.status != "deleted")
            if platform:
                statement = statement.where(accounts.c.platform == platform)
            return [dict(row) for row in conn.execute(statement).mappings()]

    def available_accounts(self, platform, *, now=None):
        """Return runnable accounts in least-recently-successful order.

        Expired cooldowns become probe_due here. They remain ineligible until a
        bounded recovery probe and canary both succeed.
        """
        now = float(now or time.time())
        with self.engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(
                    accounts.c.platform == platform,
                    accounts.c.status == "cooling",
                    accounts.c.cooldown_until.is_not(None),
                    accounts.c.cooldown_until <= now,
                )
                .values(status="probe_due", cooldown_until=None, updated_at=now)
            )
            rows = [
                dict(row)
                for row in conn.execute(
                    select(accounts).where(
                        accounts.c.platform == platform,
                        accounts.c.status == "ready",
                    )
                ).mappings()
            ]
        return sorted(
            rows,
            key=lambda row: (
                row["last_success_at"] is not None,
                row["last_success_at"] or row["created_at"],
                row["created_at"],
            ),
        )

    def due_recovery_accounts(self, *, now=None):
        now = float(now or time.time())
        with self.engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(
                    accounts.c.status == "cooling",
                    accounts.c.cooldown_until.is_not(None),
                    accounts.c.cooldown_until <= now,
                )
                .values(status="probe_due", cooldown_until=None, updated_at=now)
            )
            return [
                dict(row)
                for row in conn.execute(
                    select(accounts)
                    .where(
                        or_(
                            accounts.c.status == "recovering",
                            and_(
                                accounts.c.status == "probe_due",
                                or_(
                                    accounts.c.last_failure_kind.is_(None),
                                    accounts.c.last_failure_kind.not_in(
                                        {
                                            "quota_policy_missing",
                                            "egress_unconfirmed",
                                            "egress_changed",
                                            "egress_mismatch",
                                        }
                                    ),
                                ),
                            ),
                        )
                    )
                    .order_by(accounts.c.updated_at)
                ).mappings()
            ]

    def assign_run_account(self, run_id, account_id, *, environment=None):
        """Persist the execution resource chosen for an unassigned task."""
        now = time.time()
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            context = conn.execute(
                select(run_contexts).where(run_contexts.c.run_id == run_id)
            ).mappings().first()
            resumable_unassigned = (
                run["status"] == "blocked"
                and context is not None
                and context["account_id"] is None
                and {
                    task["state"].get("stop_reason")
                    for task in conn.execute(
                        select(tasks).where(tasks.c.run_id == run_id)
                    ).mappings()
                }
                == {"account_unavailable"}
            )
            if run["status"] != "pending" and not resumable_unassigned:
                raise ValueError("Only a pending or account-unavailable run can be assigned")
            account = conn.execute(
                select(accounts).where(accounts.c.id == account_id)
            ).mappings().first()
            if account is None or account["platform"] != run["platform"]:
                raise ValueError("Account does not match the run platform")
            if context is None:
                raise ValueError("Run context is missing")
            if context["account_id"] and context["account_id"] != account_id:
                raise ValueError("A run cannot switch accounts after assignment")
            conn.execute(
                update(run_contexts)
                .where(run_contexts.c.run_id == run_id)
                .values(account_id=account_id, environment=redact(environment or {}))
            )
            self._event(
                conn,
                run_id,
                "account_assigned",
                {"account_id": account_id, "strategy": "least_recently_used", "at": now},
            )

    def require_session_recovery(self, account_id, *, reason="auth_expired"):
        """Mark an authoritative auth failure for one independent maintenance attempt."""
        with self.engine.begin() as conn:
            conn.execute(update(accounts).where(accounts.c.id == account_id).values(
                status="login_required", cooldown_until=None,
                last_failure_kind=reason, last_failure_at=time.time(),
                updated_at=time.time(),
            ))

    def set_account_status(self, account_id, status, *, cooldown_until=None):
        if status not in {
            "ready",
            "cooling",
            "probe_due",
            "recovering",
            "canary_running",
            "login_required",
            "disabled",
        }:
            raise ValueError("Invalid account status")
        values = {
            "status": status,
            "cooldown_until": cooldown_until if status == "cooling" else None,
            "updated_at": time.time(),
        }
        if status == "ready":
            values["last_failure_kind"] = None
        with self.engine.begin() as conn:
            result = conn.execute(
                update(accounts)
                .where(accounts.c.id == account_id, accounts.c.status != "deleted")
                .values(**values)
            )
            if result.rowcount != 1:
                raise ValueError("Unknown account ID")
            if status == "ready":
                conn.execute(update(recovery_probes).where(
                    recovery_probes.c.active_key == account_id,
                ).values(active_key=None, status="completed", outcome="manual_reset",
                         finished_at=time.time(), updated_at=time.time()))

    def account_preflight(self, account_id, *, now=None):
        now = now or time.time()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(accounts).where(accounts.c.id == account_id).with_for_update()
            ).mappings().first()
            if row is None:
                raise CollectionError("account_unavailable", "Account does not exist")
            if row["status"] == "cooling" and row["cooldown_until"] and row["cooldown_until"] <= now:
                conn.execute(
                    update(accounts)
                    .where(accounts.c.id == account_id)
                    .values(status="probe_due", cooldown_until=None, updated_at=now)
                )
                row = dict(row) | {"status": "probe_due", "cooldown_until": None}
            if row["status"] != "ready":
                error = CollectionError(
                    "account_" + row["status"],
                    f"Account is {row['status']}",
                    retry_after=row["cooldown_until"],
                )
                raise error

    def create_recovery_probe(self, account_id, *, now=None):
        now = float(now or time.time())
        try:
            with self.engine.begin() as conn:
                account = conn.execute(
                    select(accounts).where(accounts.c.id == account_id).with_for_update()
                ).mappings().first()
                if account is None:
                    raise ValueError("Unknown account ID")
                if (
                    account["status"] == "cooling"
                    and account["cooldown_until"]
                    and account["cooldown_until"] <= now
                ):
                    conn.execute(
                        update(accounts)
                        .where(accounts.c.id == account_id)
                        .values(status="probe_due", cooldown_until=None, updated_at=now)
                    )
                    account = dict(account) | {"status": "probe_due"}
                if account["status"] != "probe_due":
                    raise CollectionError("account_" + account["status"])
                latest = conn.execute(
                    select(recovery_probes)
                    .where(recovery_probes.c.account_id == account_id)
                    .order_by(recovery_probes.c.created_at.desc())
                    .limit(1)
                ).mappings().first()
                if latest is None or latest["outcome"] == "success":
                    attempt = 1
                elif latest["outcome"] == "config_blocked":
                    attempt = latest["attempt"]
                else:
                    attempt = latest["attempt"] + 1
                values = dict(
                    id=uuid.uuid4().hex,
                    account_id=account_id,
                    platform=account["platform"],
                    active_key=account_id,
                    status="pending",
                    attempt=attempt,
                    started_at=None,
                    finished_at=None,
                    next_allowed_at=None,
                    request_count=0,
                    network_observation_id=None,
                    outcome=None,
                    error_kind=None,
                    created_at=now,
                    updated_at=now,
                )
                conn.execute(insert(recovery_probes).values(**values))
                return values | {"_created": True}
        except IntegrityError:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(recovery_probes).where(recovery_probes.c.active_key == account_id)
                ).mappings().one()
                return dict(row) | {"_created": False}

    def start_recovery_probe(self, probe_id, *, now=None):
        now = float(now or time.time())
        with self.engine.begin() as conn:
            probe = conn.execute(
                select(recovery_probes)
                .where(recovery_probes.c.id == probe_id)
                .with_for_update()
            ).mappings().first()
            if not probe or probe["status"] != "pending":
                raise ValueError("Recovery probe is not pending")
            conn.execute(
                update(recovery_probes)
                .where(recovery_probes.c.id == probe_id)
                .values(status="running", started_at=now, updated_at=now)
            )

    def finish_recovery_probe(
        self, probe_id, *, success, request_count, error_kind=None, now=None
    ):
        now = float(now or time.time())
        if not 0 <= int(request_count) <= 3:
            raise ValueError("Recovery probe request count exceeds the hard limit")
        with self.engine.begin() as conn:
            probe = conn.execute(
                select(recovery_probes)
                .where(recovery_probes.c.id == probe_id)
                .with_for_update()
            ).mappings().first()
            if not probe or probe["status"] not in {"pending", "running"}:
                raise ValueError("Recovery probe is not active")
            if success:
                account_status, next_allowed = "recovering", None
                outcome = "success"
            elif error_kind in {
                "quota_policy_missing",
                "egress_unconfirmed",
                "egress_changed",
                "egress_mismatch",
            }:
                account_status, next_allowed = "probe_due", None
                outcome = "config_blocked"
            elif error_kind == "auth_expired":
                account_status, next_allowed = "login_required", None
                outcome = "blocked"
            elif probe["attempt"] >= 3:
                account_status, next_allowed = "disabled", None
                outcome = "manual_review"
            else:
                delay = min(24 * 3600, 3600 * (2 ** (probe["attempt"] - 1)))
                account_status, next_allowed = "cooling", now + delay
                outcome = "blocked"
            conn.execute(
                update(recovery_probes)
                .where(recovery_probes.c.id == probe_id)
                .values(
                    active_key=None,
                    status="completed",
                    finished_at=now,
                    next_allowed_at=next_allowed,
                    request_count=int(request_count),
                    outcome=outcome,
                    error_kind=error_kind,
                    updated_at=now,
                )
            )
            conn.execute(
                update(accounts)
                .where(accounts.c.id == probe["account_id"])
                .values(
                    status=account_status,
                    cooldown_until=next_allowed,
                    last_failure_at=None if success else now,
                    last_failure_kind=None if success else error_kind,
                    updated_at=now,
                )
            )
        return {
            "status": account_status,
            "next_allowed_at": next_allowed,
            "outcome": outcome,
        }

    def finish_recovery_canary(self, account_id, *, success, error_kind=None, now=None):
        now = float(now or time.time())
        with self.engine.begin() as conn:
            account = conn.execute(
                select(accounts).where(accounts.c.id == account_id).with_for_update()
            ).mappings().first()
            if not account or account["status"] != "canary_running":
                raise ValueError("Account is not awaiting a recovery canary")
            status = "ready" if success else "cooling"
            next_allowed = None if success else now + 3600
            conn.execute(
                update(accounts)
                .where(accounts.c.id == account_id)
                .values(
                    status=status,
                    cooldown_until=next_allowed,
                    last_success_at=now if success else account["last_success_at"],
                    last_failure_at=account["last_failure_at"] if success else now,
                    last_failure_kind=None if success else error_kind,
                    updated_at=now,
                )
            )
        return status

    def claim_recovery_canary(self, account_id, *, now=None):
        now = float(now or time.time())
        with self.engine.begin() as conn:
            result = conn.execute(
                update(accounts)
                .where(
                    accounts.c.id == account_id,
                    accounts.c.status == "recovering",
                )
                .values(status="canary_running", updated_at=now)
            )
            return result.rowcount == 1

    def block_pending_run(self, run_id, error: CollectionError):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            if run["status"] != "pending":
                raise ValueError("Only a pending run can be blocked before start")
            for task in conn.execute(select(tasks).where(tasks.c.run_id == run_id)).mappings():
                state = dict(task["state"])
                state.update(
                    stop_reason=error.kind,
                    error={
                        "kind": error.kind,
                        "message": error.message,
                        "retry_after": error.retry_after,
                    },
                )
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == task["id"])
                    .values(status=error.outcome, state=state)
                )
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(status=error.outcome, updated_at=time.time())
            )
            self._event(
                conn,
                run_id,
                "preflight_blocked",
                {"kind": error.kind, "retry_after": error.retry_after},
            )

    def fail_pending_run(self, run_id, *, reason: str, error_type: str):
        """Finish a run that could not start without exposing exception details."""
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            if run["status"] != "pending":
                return False
            for task in conn.execute(select(tasks).where(tasks.c.run_id == run_id)).mappings():
                state = dict(task["state"])
                state.update(
                    stop_reason=reason,
                    error={"kind": reason, "type": error_type},
                )
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == task["id"])
                    .values(status="failed", state=state)
                )
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(status="failed", updated_at=time.time())
            )
            self._event(
                conn,
                run_id,
                "startup_failed",
                {"reason": reason, "error_type": error_type},
            )
            return True

    def record_operation(
        self,
        run_id,
        task_id,
        *,
        operation,
        attempt,
        started_at,
        duration_ms,
        outcome,
        error_kind=None,
        item_count=0,
        response_has_more=None,
    ):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            context = conn.execute(
                select(run_contexts).where(run_contexts.c.run_id == run_id)
            ).mappings().first()
            conn.execute(
                insert(operation_metrics).values(
                    run_id=run_id,
                    task_id=task_id,
                    account_id=context["account_id"] if context else None,
                    platform=run["platform"],
                    operation=operation,
                    attempt=attempt,
                    started_at=started_at,
                    duration_ms=max(0, int(duration_ms)),
                    outcome=outcome,
                    error_kind=error_kind,
                    item_count=max(0, int(item_count)),
                    response_has_more=response_has_more,
                )
            )
            # An admitted request is conservatively treated as sent once the
            # adapter attempt returns, including errors and timeouts.
            self._settle_quota_reservations(conn, run_id, operation, consumed=1)

    def record_risk(self, run_id, *, operation, error: CollectionError):
        cooldowns = {
            "rate_limit": 60 * 60,
            "verification_required": 24 * 60 * 60,
            "access_denied": 24 * 60 * 60,
            "proxy_unavailable": 30 * 60,
            "egress_unconfirmed": 24 * 60 * 60,
            "egress_changed": 24 * 60 * 60,
            "egress_mismatch": 24 * 60 * 60,
        }
        now = time.time()
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            context = conn.execute(
                select(run_contexts).where(run_contexts.c.run_id == run_id)
            ).mappings().first()
            account_id = context["account_id"] if context else None
            if error.kind in {"auth_expired", "identity_mismatch", "identity_unverified"}:
                action, status, cooldown_until = "require_login", "login_required", None
            elif error.kind in cooldowns and self._automatic_cooldown_enabled(conn):
                seconds = error.retry_after if error.retry_after is not None else cooldowns[error.kind]
                action, status, cooldown_until = "cooldown", "cooling", now + max(0, seconds)
            elif error.kind in cooldowns:
                action, status, cooldown_until = "manual_review", None, None
            elif error.kind == "quota_exhausted":
                action, status, cooldown_until = "wait_for_quota", None, error.retry_after
            elif error.kind == "quota_policy_missing":
                action, status, cooldown_until = "configure_quota", None, None
            elif error.kind == "schema_changed":
                action, status, cooldown_until = "inspect_parser", None, None
            else:
                action, status, cooldown_until = "stop", None, None
            conn.execute(
                insert(risk_events).values(
                    run_id=run_id,
                    account_id=account_id,
                    platform=run["platform"],
                    operation=operation,
                    kind=error.kind,
                    action=action,
                    at=now,
                    data=redact({"retry_after": error.retry_after, "sample_ref": error.sample_ref}),
                )
            )
            if account_id and (status or error.kind in cooldowns):
                conn.execute(
                    update(accounts)
                    .where(accounts.c.id == account_id, accounts.c.status != "deleted")
                    .values(
                        **({"status": status, "cooldown_until": cooldown_until} if status else {}),
                        last_failure_at=now,
                        last_failure_kind=error.kind,
                        updated_at=now,
                    )
                )
        return {"action": action, "cooldown_until": cooldown_until}

    @staticmethod
    def _metric_statement(since):
        return select(
            operation_metrics.c.account_id, operation_metrics.c.platform,
            operation_metrics.c.operation,
            func.count().label("attempts"),
            func.sum(case((operation_metrics.c.outcome == "success", 1), else_=0)).label("successes"),
            func.sum(operation_metrics.c.item_count).label("items"),
            func.sum(operation_metrics.c.duration_ms).label("duration_ms"),
        ).where(operation_metrics.c.started_at >= since).group_by(
            operation_metrics.c.account_id, operation_metrics.c.platform,
            operation_metrics.c.operation,
        )

    @staticmethod
    def _metric_group(row):
        group = dict(row)
        group["failures"] = group["attempts"] - group["successes"]
        group["success_rate"] = round(group["successes"] / group["attempts"], 4)
        group["avg_duration_ms"] = round(group["duration_ms"] / group["attempts"])
        return group

    def risk_page(self, section, *, since, page=1, page_size=10, platform="",
                  account_id="", operation="", dimension="", q=""):
        """Count and page in SQL, before materializing records or computing quota usage."""
        tables = {"risk_events": (risk_events, risk_events.c.at),
                  "network_observations": (network_observations, network_observations.c.observed_at),
                  "recovery_probes": (recovery_probes, recovery_probes.c.created_at)}
        if section == "groups":
            table = operation_metrics
            statement = self._metric_statement(since)
            ordering = [table.c.account_id, table.c.platform, table.c.operation]
        elif section == "quotas":
            table = quota_policies
            statement = select(table).where(table.c.enabled.is_(True))
            ordering = [table.c.dimension, table.c.subject, table.c.operation, table.c.id]
            if dimension:
                statement = statement.where(table.c.dimension == dimension)
            if q:
                pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                matching_accounts = select(accounts.c.id).where(accounts.c.name.ilike(pattern, escape="\\"))
                statement = statement.where(or_(
                    table.c.subject.ilike(pattern, escape="\\"),
                    and_(table.c.dimension == "account", table.c.subject.in_(matching_accounts)),
                ))
        elif section in tables:
            table, timestamp = tables[section]
            statement = select(table).where(timestamp >= since)
            ordering = [timestamp.desc(), table.c.id.desc()]
        else:
            raise ValueError("未知的观测分类")
        if section != "quotas":
            if platform:
                statement = statement.where(table.c.platform == platform)
            if account_id:
                statement = statement.where(table.c.account_id == account_id)
        if operation and "operation" in table.c:
            statement = statement.where(table.c.operation == operation)
        page_size = max(1, min(int(page_size), 50))
        with self.engine.begin() as conn:
            total = conn.scalar(select(func.count()).select_from(statement.subquery()))
            pages = max(1, (total + page_size - 1) // page_size)
            page = max(1, min(int(page), pages))
            rows = conn.execute(statement.order_by(*ordering).limit(page_size)
                                .offset((page - 1) * page_size)).mappings().all()
            if section == "groups":
                items = [self._metric_group(row) for row in rows]
            elif section == "quotas":
                now = time.time()
                items = [self._quota_usage(conn, row, now) for row in rows]
            else:
                items = [dict(row) for row in rows]
        return {"section": section, "items": items, "total": total, "page": page,
                "page_size": page_size, "pages": pages}

    def risk_summary(self, *, since=None):
        since = since if since is not None else time.time() - 7 * 86400
        with self.engine.begin() as conn:
            groups = [self._metric_group(row) for row in conn.execute(
                self._metric_statement(since).order_by(
                    operation_metrics.c.account_id, operation_metrics.c.platform,
                    operation_metrics.c.operation,
                )
            ).mappings()]
            risks = [dict(row) for row in conn.execute(
                select(risk_events).where(risk_events.c.at >= since)
                .order_by(risk_events.c.at.desc(), risk_events.c.id.desc()).limit(200)
            ).mappings()]
        return {
            "since": since, "groups": groups, "risk_events": risks,
            "quotas": self.quota_summary(),
            "network_observations": self.list_network_observations(since=since, limit=100),
            "recovery_probes": self.list_recovery_probes(since=since, limit=100),
        }

    def list_network_observations(self, *, since=None, limit=100, run_id=None):
        with self.engine.begin() as conn:
            statement = select(network_observations)
            if since is not None:
                statement = statement.where(network_observations.c.observed_at >= since)
            if run_id is not None:
                statement = statement.where(network_observations.c.run_id == run_id)
            return [
                dict(row)
                for row in conn.execute(
                    statement.order_by(network_observations.c.observed_at.desc()).limit(
                        max(1, min(int(limit), 500))
                    )
                ).mappings()
            ]

    def list_recovery_probes(self, *, since=None, limit=100, account_id=None):
        with self.engine.begin() as conn:
            statement = select(recovery_probes)
            if since is not None:
                statement = statement.where(recovery_probes.c.created_at >= since)
            if account_id is not None:
                statement = statement.where(recovery_probes.c.account_id == account_id)
            return [
                dict(row)
                for row in conn.execute(
                    statement.order_by(recovery_probes.c.created_at.desc()).limit(
                        max(1, min(int(limit), 500))
                    )
                ).mappings()
            ]

    def snapshot(self, run_id):
        with self.engine.begin() as conn:
            run = self._run(conn, run_id)
            task_rows = [
                dict(r)
                for r in conn.execute(
                    select(tasks).where(tasks.c.run_id == run_id).order_by(tasks.c.sequence)
                ).mappings()
            ]
            item_rows = [
                dict(r)
                for r in conn.execute(
                    select(task_items).join(tasks).where(tasks.c.run_id == run_id)
                ).mappings()
            ]
            return {
                "run": run,
                "context": dict(
                    conn.execute(
                        select(run_contexts).where(run_contexts.c.run_id == run_id)
                    ).mappings().first()
                    or {}
                ),
                "tasks": task_rows,
                "items": item_rows,
                "post_sources": [dict(r) for r in conn.execute(
                    select(post_sources).where(post_sources.c.run_id == run_id)
                    .order_by(post_sources.c.position)
                ).mappings()],
                "hits": [
                    dict(r)
                    for r in conn.execute(select(hits).where(hits.c.run_id == run_id)).mappings()
                ],
                "events": [
                    dict(r)
                    for r in conn.execute(
                        select(events).where(events.c.run_id == run_id).order_by(events.c.id)
                    ).mappings()
                ],
                "metrics": [
                    dict(r)
                    for r in conn.execute(
                        select(operation_metrics)
                        .where(operation_metrics.c.run_id == run_id)
                        .order_by(operation_metrics.c.id)
                    ).mappings()
                ],
                "risk_events": [
                    dict(r)
                    for r in conn.execute(
                        select(risk_events)
                        .where(risk_events.c.run_id == run_id)
                        .order_by(risk_events.c.id)
                    ).mappings()
                ],
                "network_observations": [
                    dict(r)
                    for r in conn.execute(
                        select(network_observations)
                        .where(network_observations.c.run_id == run_id)
                        .order_by(network_observations.c.observed_at)
                    ).mappings()
                ],
                "quota_reservations": [
                    dict(r)
                    for r in conn.execute(
                        select(quota_reservations)
                        .where(quota_reservations.c.run_id == run_id)
                        .order_by(quota_reservations.c.created_at)
                    ).mappings()
                ],
            }
