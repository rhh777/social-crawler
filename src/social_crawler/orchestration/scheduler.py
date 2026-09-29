"""Persistent, single-application schedule dispatcher.

The database uniqueness constraint makes an occurrence idempotent. PostgreSQL
leases are intentionally a later milestone; callers must still enforce one web
application instance for this pilot.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import CroniterError, croniter
from sqlalchemy import insert, select, update

from social_crawler.domain.models import RunConfig
from social_crawler.domain.redaction import redact
from social_crawler.storage.store import run_contexts, runs, schedules


def next_occurrence(spec: dict, *, after: float) -> float:
    kind = spec["kind"]
    if kind == "cron":
        expression = " ".join(str(spec.get("cron_expression") or "").split())
        if len(expression.split()) != 5:
            raise ValueError("Cron expression must contain exactly 5 fields")
        try:
            zone = ZoneInfo(spec.get("timezone") or "Asia/Shanghai")
            current = datetime.fromtimestamp(after, zone)
            return croniter(expression, current).get_next(datetime).timestamp()
        except (CroniterError, KeyError, OverflowError, ValueError):
            raise ValueError("Cron expression or timezone is invalid") from None
    if kind == "interval":
        minutes = int(spec.get("interval_minutes") or 0)
        if not 15 <= minutes <= 7 * 24 * 60:
            raise ValueError("Interval must be between 15 minutes and 7 days")
        previous = float(spec.get("next_run_at") or after)
        step = minutes * 60
        return previous + max(1, int((after - previous) // step) + 1) * step
    if kind != "daily":
        raise ValueError("Schedule kind must be cron, interval or daily")
    value = str(spec.get("daily_time") or "")
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        zone = ZoneInfo(spec.get("timezone") or "Asia/Shanghai")
    except (ValueError, KeyError):
        raise ValueError("Daily schedule requires a valid time and timezone") from None
    local = datetime.fromtimestamp(after, zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def schedule_spec(body: dict) -> dict:
    kind = str(body.get("kind") or "").strip()
    spec = {
        "kind": kind,
        "interval_minutes": body.get("interval_minutes") if kind == "interval" else None,
        "daily_time": body.get("daily_time") if kind == "daily" else None,
        "cron_expression": (
            " ".join(str(body.get("cron_expression") or "").split())
            if kind == "cron"
            else None
        ),
        "timezone": str(body.get("timezone") or "Asia/Shanghai").strip(),
    }
    first = next_occurrence(spec, after=time.time())
    if kind == "cron":
        previous = first
        for _ in range(8):
            following = next_occurrence(spec, after=previous)
            if following - previous < 15 * 60:
                raise ValueError("Cron schedule interval must be at least 15 minutes")
            previous = following
    return spec


class Scheduler:
    def __init__(self, store):
        self.store = store

    def create(self, body: dict):
        config = RunConfig.model_validate(body["config"])
        spec = schedule_spec(body)
        now = time.time()
        first = float(body.get("next_run_at") or next_occurrence(spec, after=now))
        # Validate all fields even when an explicit first run is supplied.
        next_occurrence(spec | {"next_run_at": first}, after=first)
        values = dict(
            id=uuid.uuid4().hex,
            name=str(body.get("name") or "").strip()[:120],
            # Plans are intentionally not pinned to an account. The dispatcher
            # creates a normal task and the execution layer chooses a currently
            # eligible account from the matching platform pool.
            account_id=None,
            config=config.model_dump(),
            enabled=bool(body.get("enabled", True)),
            deleted_at=None,
            next_run_at=first,
            last_dispatched_at=None,
            last_run_id=None,
            created_at=now,
            updated_at=now,
            **spec,
        )
        if not values["name"]:
            raise ValueError("Schedule name is required")
        with self.store.engine.begin() as conn:
            conn.execute(insert(schedules).values(**values))
        return values

    def list(self):
        with self.store.engine.begin() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(schedules)
                    .where(schedules.c.deleted_at.is_(None))
                    .order_by(schedules.c.created_at.desc())
                ).mappings()
            ]

    def update(self, schedule_id: str, body: dict):
        now = time.time()
        with self.store.engine.begin() as conn:
            statement = select(schedules).where(
                schedules.c.id == schedule_id, schedules.c.deleted_at.is_(None)
            )
            if conn.dialect.name == "postgresql":
                statement = statement.with_for_update(of=schedules)
            current = conn.execute(statement).mappings().one_or_none()
            if current is None:
                raise ValueError("Unknown schedule ID")
            merged = dict(current) | body
            config_data = dict(merged["config"])
            if config_data.get("platform") == current["config"].get("platform"):
                for field in ("post_targets", "seed_contents"):
                    original = current["config"].get(field)
                    if original and config_data.get(field) == redact(original):
                        config_data[field] = original
            # The editor does not round-trip redacted share-link tokens.
            if (config_data.get("source_type") == "posts" and "post_targets" not in config_data
                    and current["config"].get("source_type") == "posts"
                    and config_data.get("platform") == current["config"].get("platform")):
                config_data["post_targets"] = current["config"]["post_targets"]
            config = RunConfig.model_validate(config_data)
            spec = schedule_spec(merged)
            name = str(merged.get("name") or "").strip()[:120]
            if not name:
                raise ValueError("Schedule name is required")
            next_run_at = float(
                body.get("next_run_at") or next_occurrence(spec, after=now)
            )
            # Validate a following occurrence even when tests or imports provide
            # an explicit first execution time.
            next_occurrence(spec | {"next_run_at": next_run_at}, after=next_run_at)
            values = {
                "name": name,
                "config": config.model_dump(),
                "next_run_at": next_run_at,
                "updated_at": now,
                **spec,
            }
            if "enabled" in body:
                values["enabled"] = bool(body["enabled"])
            conn.execute(
                update(schedules).where(schedules.c.id == schedule_id).values(**values)
            )
            return dict(current) | values

    def delete(self, schedule_id: str):
        now = time.time()
        with self.store.engine.begin() as conn:
            result = conn.execute(
                update(schedules)
                .where(
                    schedules.c.id == schedule_id,
                    schedules.c.deleted_at.is_(None),
                )
                .values(enabled=False, deleted_at=now, updated_at=now)
            )
            if result.rowcount != 1:
                raise ValueError("Unknown schedule ID")

    def set_enabled(self, schedule_id: str, enabled: bool):
        with self.store.engine.begin() as conn:
            result = conn.execute(
                update(schedules)
                .where(
                    schedules.c.id == schedule_id,
                    schedules.c.deleted_at.is_(None),
                )
                .values(enabled=enabled, updated_at=time.time())
            )
            if result.rowcount != 1:
                raise ValueError("Unknown schedule ID")

    def dispatch_due(self, *, now=None, versions=None):
        now = float(now or time.time())
        dispatched = []
        with self.store.engine.begin() as conn:
            statement = (
                select(schedules)
                .where(
                    schedules.c.enabled.is_(True),
                    schedules.c.deleted_at.is_(None),
                    schedules.c.next_run_at <= now,
                )
                .order_by(schedules.c.next_run_at)
            )
            if conn.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True, of=schedules)
            for row in conn.execute(statement).mappings():
                schedule = {key: row[key] for key in schedules.c.keys()}
                scheduled_for = schedule["next_run_at"]
                following = next_occurrence(schedule, after=now)
                active_run = conn.execute(
                    select(runs.c.id)
                    .join(run_contexts, run_contexts.c.run_id == runs.c.id)
                    .where(
                        run_contexts.c.schedule_id == schedule["id"],
                        runs.c.status.in_(["pending", "running"]),
                    )
                    .limit(1)
                ).first()
                if active_run is not None:
                    # Coalesce this occurrence instead of building an unbounded
                    # queue behind an occupied account/profile resource.
                    conn.execute(
                        update(schedules)
                        .where(schedules.c.id == schedule["id"])
                        .values(next_run_at=following, updated_at=now)
                    )
                    continue
                config = RunConfig.model_validate(schedule["config"])
                run_id = self.store._create_run(
                    conn,
                    config,
                    mode="online",
                    versions=versions,
                    account_id=None,
                    schedule_id=schedule["id"],
                    scheduled_for=scheduled_for,
                    environment={},
                )
                conn.execute(
                    update(schedules)
                    .where(schedules.c.id == schedule["id"])
                    .values(
                        next_run_at=following,
                        last_dispatched_at=now,
                        last_run_id=run_id,
                        updated_at=now,
                    )
                )
                dispatched.append(
                    {
                        "run_id": run_id,
                        "schedule_id": schedule["id"],
                        "account_id": None,
                        "scheduled_for": scheduled_for,
                        "config": config.model_dump(),
                    }
                )
        return dispatched
