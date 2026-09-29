import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, inspect, text

from social_crawler.domain.models import CollectionError, RunConfig
from social_crawler.orchestration.scheduler import Scheduler, next_occurrence
from social_crawler.storage.migrations import downgrade_database, upgrade_database
from social_crawler.storage.store import accounts, runs, schedules


def environment(name="account-a"):
    return {
        "account_ref": name,
        "cookie_file": f"data/{name}.cookies.json",
        "profile_dir": f"profiles/{name}",
        "headless": True,
        "session_version": "1",
        "binding_version": "1",
        "impersonate": "chrome150",
        "douyin_webid": "",
    }


def test_interval_schedule_dispatch_creates_unassigned_task_idempotently(store):
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        {
            "name": "灰度计划 A",
            "config": RunConfig(
                platform="xhs", keywords=["咖啡"], max_requests=10
            ).model_dump(),
            "kind": "interval",
            "interval_minutes": 60,
            "next_run_at": 100,
        }
    )

    dispatched = scheduler.dispatch_due(now=400, versions={"test": "1"})
    assert len(dispatched) == 1
    run_id = dispatched[0]["run_id"]
    assert store.pending_scheduled_runs()[0]["run_id"] == run_id
    assert store.get_run_context(run_id) == {
        "run_id": run_id,
        "account_id": None,
        "schedule_id": schedule["id"],
        "scheduled_for": 100,
        "environment": {},
    }
    assert scheduler.dispatch_due(now=400) == []
    assert scheduler.list()[0]["next_run_at"] > 400


def test_schedule_coalesces_occurrence_while_previous_run_is_active(store):
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        {
            "name": "不积压计划",
            "config": {"platform": "xhs", "keywords": ["咖啡"]},
            "kind": "interval",
            "interval_minutes": 15,
            "next_run_at": 100,
        }
    )
    first = scheduler.dispatch_due(now=100)[0]
    with store.engine.begin() as conn:
        conn.execute(
            schedules.update()
            .where(schedules.c.id == schedule["id"])
            .values(next_run_at=200)
        )

    assert scheduler.dispatch_due(now=200) == []
    assert [row["run_id"] for row in store.pending_scheduled_runs()] == [first["run_id"]]
    assert scheduler.list()[0]["next_run_at"] > 200

    with store.engine.begin() as conn:
        conn.execute(
            runs.update().where(runs.c.id == first["run_id"]).values(status="canceled")
        )
        conn.execute(
            schedules.update()
            .where(schedules.c.id == schedule["id"])
            .values(next_run_at=300)
        )
    assert len(scheduler.dispatch_due(now=300)) == 1


def test_xhs_http_adapter_survives_schedule_dispatch(store):
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        {
            "name": "HTTPX 灰度计划",
            "config": {
                "platform": "xhs",
                "keywords": ["咖啡"],
                "adapter": "httpx",
            },
            "kind": "interval",
            "interval_minutes": 60,
            "next_run_at": 100,
        }
    )

    assert schedule["config"]["adapter"] == "httpx"
    dispatched = scheduler.dispatch_due(now=400)
    assert dispatched[0]["config"]["adapter"] == "httpx"
    assert store.get_run(dispatched[0]["run_id"])["config"]["adapter"] == "httpx"


def test_schedule_is_account_agnostic_and_requires_safe_minimum_interval(store):
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        {
            "name": "自动选账号",
            "config": {"platform": "douyin", "keywords": ["咖啡"]},
            "kind": "interval",
            "interval_minutes": 60,
        }
    )
    assert schedule["account_id"] is None
    assert schedule["config"]["sort"] == "latest"
    with pytest.raises(ValueError, match="15 minutes"):
        next_occurrence(
            {"kind": "interval", "interval_minutes": 5}, after=time.time()
        )


def test_cron_schedule_uses_timezone_and_supports_weekdays(store):
    zone = ZoneInfo("Asia/Shanghai")
    friday = datetime(2026, 9, 11, 10, 0, tzinfo=zone).timestamp()
    expected = datetime(2026, 9, 14, 9, 0, tzinfo=zone).timestamp()
    spec = {
        "kind": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
    }

    assert next_occurrence(spec, after=friday) == expected
    schedule = Scheduler(store).create(
        {
            "name": "工作日观察",
            "config": {"platform": "xhs", "keywords": ["咖啡"]},
            **spec,
            "next_run_at": expected,
        }
    )
    assert schedule["cron_expression"] == "0 9 * * 1-5"


@pytest.mark.parametrize(
    "expression",
    ["* * * * *", "0 0 1 1 * 2027", "0 0 31 2 *", "not-a-cron"],
)
def test_cron_schedule_rejects_too_frequent_or_invalid_expressions(store, expression):
    with pytest.raises(ValueError, match="Cron"):
        Scheduler(store).create(
            {
                "name": "无效计划",
                "config": {"platform": "xhs", "keywords": ["咖啡"]},
                "kind": "cron",
                "cron_expression": expression,
                "timezone": "Asia/Shanghai",
            }
        )


def test_schedule_can_be_edited_and_soft_deleted_without_dispatch(store):
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        {
            "name": "待修改计划",
            "config": {"platform": "xhs", "keywords": ["咖啡"]},
            "kind": "cron",
            "cron_expression": "0 */6 * * *",
            "timezone": "Asia/Shanghai",
        }
    )
    updated = scheduler.update(
        schedule["id"],
        {
            "name": "工作日计划",
            "config": {"platform": "douyin", "keywords": ["茶"]},
            "kind": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
        },
    )
    assert updated["name"] == "工作日计划"
    assert updated["config"]["platform"] == "douyin"
    assert updated["cron_expression"] == "0 9 * * 1-5"
    assert updated["next_run_at"] > time.time()

    scheduler.delete(schedule["id"])
    assert scheduler.list() == []
    assert scheduler.dispatch_due(now=updated["next_run_at"] + 1) == []
    with pytest.raises(ValueError, match="Unknown schedule"):
        scheduler.update(schedule["id"], {"name": "不能恢复"})


def test_pool_assignment_prefers_least_recently_used_and_is_sticky(store):
    older = store.create_account("xhs", "账号 A", environment("account-a"))
    newer = store.create_account("xhs", "账号 B", environment("account-b"))
    with store.engine.begin() as conn:
        conn.execute(
            accounts.update()
            .where(accounts.c.id == newer["id"])
            .values(last_success_at=200)
        )
    assert store.available_accounts("xhs")[0]["id"] == older["id"]
    run_id = store.create_run(
        RunConfig(platform="xhs", keywords=["咖啡"]), mode="online"
    )
    store.assign_run_account(run_id, older["id"], environment={"account_ref": "account-a"})
    assert store.get_run_context(run_id)["account_id"] == older["id"]
    with pytest.raises(ValueError, match="cannot switch"):
        store.assign_run_account(run_id, newer["id"])


def test_account_unavailable_scheduled_run_can_be_assigned_on_resume(store):
    account = store.create_account("douyin", "恢复账号", environment("restored"))
    run_id = store.create_run(
        RunConfig(platform="douyin", keywords=["咖啡"]), mode="online"
    )
    store.block_pending_run(run_id, CollectionError("account_unavailable"))

    store.assign_run_account(run_id, account["id"], environment={"account_ref": "restored"})
    epoch = store.start(run_id, binding="new-binding", resume=True)

    assert epoch == 1
    assert store.get_run(run_id)["status"] == "running"
    assert store.get_run_context(run_id)["account_id"] == account["id"]


def test_risk_event_cools_account_and_is_aggregatable(store):
    store.set_automatic_cooldown(True)
    account = store.create_account("xhs", "账号 A", environment())
    run_id = store.create_run(
        RunConfig(platform="xhs", keywords=["咖啡"]),
        mode="online",
        account_id=account["id"],
    )
    task = store.snapshot(run_id)["tasks"][0]
    store.record_operation(
        run_id,
        task["id"],
        operation="search",
        attempt=1,
        started_at=time.time(),
        duration_ms=120,
        outcome="error",
        error_kind="rate_limit",
    )
    store.record_risk(
        run_id,
        operation="search",
        error=CollectionError("rate_limit", retry_after=120),
    )

    cooled = store.get_account(account["id"])
    assert cooled["status"] == "cooling"
    assert cooled["cooldown_until"] > time.time()
    with pytest.raises(CollectionError, match="cooling"):
        store.account_preflight(account["id"])
    report = store.risk_summary()
    assert report["groups"][0]["failures"] == 1
    assert report["risk_events"][0]["action"] == "cooldown"


def test_expired_cooldown_requires_probe_and_canary_before_ready(store):
    account = store.create_account("xhs", "恢复账号", environment())
    store.set_account_status(account["id"], "cooling", cooldown_until=100)
    assert store.available_accounts("xhs", now=101) == []
    assert store.get_account(account["id"])["status"] == "probe_due"
    with pytest.raises(CollectionError) as raised:
        store.account_preflight(account["id"], now=101)
    assert raised.value.kind == "account_probe_due"

    first = store.create_recovery_probe(account["id"], now=102)
    duplicate = store.create_recovery_probe(account["id"], now=102)
    assert duplicate["id"] == first["id"]
    store.start_recovery_probe(first["id"], now=103)
    result = store.finish_recovery_probe(
        first["id"], success=True, request_count=2, now=104
    )
    assert result["status"] == "recovering"
    assert store.available_accounts("xhs", now=105) == []
    assert store.claim_recovery_canary(account["id"], now=105)
    assert not store.claim_recovery_canary(account["id"], now=105)
    assert store.finish_recovery_canary(account["id"], success=True, now=106) == "ready"
    assert store.available_accounts("xhs", now=107)[0]["id"] == account["id"]


def test_three_failed_recovery_probes_require_manual_review(store):
    account = store.create_account("xhs", "连续失败账号", environment())
    store.set_account_status(account["id"], "probe_due")
    now = 100.0
    for expected_attempt in range(1, 4):
        probe = store.create_recovery_probe(account["id"], now=now)
        assert probe["attempt"] == expected_attempt
        result = store.finish_recovery_probe(
            probe["id"],
            success=False,
            request_count=1,
            error_kind="verification_required",
            now=now + 1,
        )
        if expected_attempt < 3:
            assert result["status"] == "cooling"
            now = result["next_allowed_at"] + 1
            assert store.available_accounts("xhs", now=now) == []
        else:
            assert result["status"] == "disabled"
    assert store.get_account(account["id"])["status"] == "disabled"


def test_recovery_configuration_block_does_not_consume_failure_attempt(store):
    account = store.create_account("xhs", "配置待修复账号", environment())
    store.set_account_status(account["id"], "probe_due")

    first = store.create_recovery_probe(account["id"], now=100)
    result = store.finish_recovery_probe(
        first["id"],
        success=False,
        request_count=0,
        error_kind="quota_policy_missing",
        now=101,
    )
    assert result["status"] == "probe_due"
    assert result["outcome"] == "config_blocked"
    assert store.due_recovery_accounts(now=102) == []

    retry = store.create_recovery_probe(account["id"], now=102)
    assert retry["attempt"] == 1


def test_alembic_baseline_creates_and_versions_fresh_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    upgrade_database(url)
    engine = create_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
        assert {
            "accounts",
            "proxies",
            "schedules",
            "operation_metrics",
            "risk_events",
            "network_observations",
            "quota_policies",
            "quota_buckets",
            "quota_reservations",
            "recovery_probes",
        } <= names
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_proxy_pool"
            )
        columns = {column["name"] for column in inspect(engine).get_columns("schedules")}
        assert {"cron_expression", "deleted_at"} <= columns
    finally:
        engine.dispose()


def test_unattended_canary_migration_has_non_destructive_rollback(tmp_path):
    url = f"sqlite:///{tmp_path / 'rollback.db'}"
    upgrade_database(url)
    downgrade_database(url, "0002_pool_account_assignment")
    engine = create_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
        assert "accounts" in names and "runs" in names
        assert "network_observations" not in names
        assert "quota_policies" not in names
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0002_pool_account_assignment"
            )
    finally:
        engine.dispose()
