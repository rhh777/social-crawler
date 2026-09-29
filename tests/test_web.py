"""Offline console integration: real HTTP routes, persistence, worker, credential boundaries."""

import json
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fixture_adapter import FixtureAdapter, submit_fixture
from sqlalchemy import func, insert, select, update

from social_crawler.domain.models import RunConfig
from social_crawler.interfaces.web import Console, ConsoleError, Handler
from social_crawler.storage.store import accounts, contents, runs


@pytest.fixture(autouse=True)
def configured_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLER_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")
    app = Console(tmp_path)
    yield app
    for thread in app.threads:
        thread.join(timeout=10)
    app.worker_pool.shutdown(wait_seconds=10)


def test_database_environment_overrides_saved_web_setting(tmp_path, monkeypatch):
    first = Console(tmp_path)
    first.settings["database_url"] = "sqlite:///saved.db"
    first.path.write_text(json.dumps(first.settings))
    first.worker_pool.shutdown(wait_seconds=10)

    override = "sqlite:///" + str(tmp_path / "launcher.db")
    monkeypatch.setenv("CRAWLER_DATABASE_URL", override)
    second = Console(tmp_path)
    try:
        assert second.settings["database_url"] == override
    finally:
        second.worker_pool.shutdown(wait_seconds=10)


def wait_job(app, job):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with app.lock:
            current = dict(app.jobs[job["id"]])
        if current["status"] not in {"queued", "running"}:
            assert current["status"] == "completed", current
            return current
        time.sleep(0.02)
    pytest.fail("Background job did not finish")


def collect(app, platform="xhs", **overrides):
    config = {
        "platform": platform,
        "keywords": ["咖啡"],
        "min_interval": 0,
        "content_limit": 3,
        "comment_limit": 3,
        "reply_limit": 2,
    } | overrides
    job = submit_fixture(app, {"config": config})
    wait_job(app, job)
    return job["run_id"]


def test_all_platform_collection_results_and_export(console):
    ids = [collect(console, p) for p in ("xhs", "rednote", "douyin")]
    data = console.dashboard()
    assert data["stats"]["runs"] == 3
    assert data["stats"]["contents"] > 0
    assert data["stats"]["comments"] > 0
    assert all(r["status"] == "completed" for r in data["runs"])
    assert all(r["post_count"] == 3 for r in data["runs"])
    assert all(r["comment_count"] > 0 for r in data["runs"])
    result = console.results({"run_id": [ids[0]]})
    assert result["total"] == 3
    assert all(
        item["platform"] == "xhs" and item["source_mode"] == "offline" for item in result["items"]
    )
    first = result["items"][0]
    detail = console.detail("xhs", first["id"])
    assert detail["comments"]
    assert (console.root / "data/validation" / ids[0] / "report.json").exists()


def test_worker_pool_runs_different_platforms_concurrently(tmp_path, monkeypatch):

    app = Console(tmp_path, worker_count=2)
    original = FixtureAdapter.fetch
    lock = threading.Lock()
    active = 0
    peak = 0

    async def paced(self, request):
        nonlocal active, peak
        import asyncio

        with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return await original(self, request)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(FixtureAdapter, "fetch", paced)
    try:
        jobs = [
            submit_fixture(app,
                {
                    "config": {
                        "platform": platform,
                        "keywords": ["并发"],
                        "min_interval": 0,
                        "content_limit": 1,
                        "comment_limit": 0,
                    },
                }
            )
            for platform in ("xhs", "douyin")
        ]
        for job in jobs:
            wait_job(app, job)
        assert peak == 2
    finally:
        app.worker_pool.shutdown(wait_seconds=10)


def test_worker_pool_serializes_the_same_platform_account(tmp_path, monkeypatch):

    app = Console(tmp_path, worker_count=2)
    original = FixtureAdapter.fetch
    lock = threading.Lock()
    active = 0
    peak = 0

    async def paced(self, request):
        nonlocal active, peak
        import asyncio

        with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return await original(self, request)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(FixtureAdapter, "fetch", paced)
    try:
        jobs = [
            submit_fixture(app,
                {
                    "config": {
                        "platform": "xhs",
                        "keywords": [f"串行-{index}"],
                        "min_interval": 0,
                        "content_limit": 1,
                        "comment_limit": 0,
                    },
                }
            )
            for index in range(2)
        ]
        for job in jobs:
            wait_job(app, job)
        assert peak == 1
        assert {app.jobs[job["id"]]["worker_slot"] for job in jobs} <= {1, 2}
    finally:
        app.worker_pool.shutdown(wait_seconds=10)


def test_unicode_search_literal_wildcards_and_pagination(console):
    with console.store() as store, store.engine.begin() as conn:
        conn.execute(
            insert(contents),
            [
                {
                    "platform": "xhs",
                    "id": str(i),
                    "data": {
                        "title": "咖啡 100%_" if i == 0 else "家居",
                        "author_name": "测试作者",
                    },
                    "observed_at": i,
                }
                for i in range(30)
            ],
        )
    assert console.results({"q": ["咖啡"]})["total"] == 1
    assert console.results({"q": ["%_"]})["total"] == 1
    assert console.results({"q": ["测试作者"]})["total"] == 30
    first = console.results({"page": ["1"]})
    second = console.results({"page": ["2"]})
    assert len(first["items"]) == 24 and len(second["items"]) == 6
    assert [item["id"] for item in first["items"] + second["items"]] == [str(i) for i in reversed(range(30))]
    assert first["total"] == second["total"] == 30
    assert console.results({"page": ["3"]})["items"] == []
    assert console.results({"platform": ["douyin"]})["total"] == 0


def test_cookie_validation_private_persistence_and_proxy(console):
    result = console.save(
        {
            "platform": "xhs",
            "cookie_text": "web_session=private-value; a1=secret",
            "proxy_url": "socks5://name:proxy-secret@localhost:1234",
        }
    )
    assert result["accounts"]["xhs"]["cookie_state"] == "configured"
    assert "private-value" in result["accounts"]["xhs"]["cookie_text"]
    assert result["accounts"]["xhs"]["proxy_url"] == "socks5://name:proxy-secret@localhost:1234"
    env = console.environment("xhs")
    for path in (console.path, console.resolve(env.cookie_file), console.resolve(env.proxy_file)):
        assert path.stat().st_mode & 0o777 == 0o600
    before = console.path.read_text()
    with pytest.raises(ConsoleError):
        console.save(
            {
                "platform": "xhs",
                "cookie_text": '[{"name":"web_session","value":"expired","expires":1}]',
            }
        )
    assert console.path.read_text() == before
    restored = Console(console.root)
    assert restored.cookie_check("xhs")["cookie_state"] == "configured"
    restored.save({"platform": "xhs", "clear_proxy": True})
    assert restored.environment("xhs").proxy_file is None


def test_existing_settings_are_migrated_with_isolated_rednote_account(tmp_path):
    settings_path = tmp_path / "data/web/settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "database_url": "sqlite:///" + str(tmp_path / "data/offline.db"),
                "accounts": {
                    "xhs": {
                        "account_ref": "xhs-primary",
                        "profile_dir": "profiles/xhs-primary",
                        "cookie_file": "data/cookies/xhs.json",
                        "impersonate": "chrome136",
                    },
                    "douyin": {
                        "account_ref": "douyin-primary",
                        "profile_dir": "profiles/douyin-primary",
                        "cookie_file": "data/cookies/douyin.json",
                        "impersonate": "chrome",
                    },
                },
            }
        )
    )
    app = Console(tmp_path)
    try:
        rednote = app.environment("rednote")
        assert rednote.account_ref == "rednote-primary"
        assert Path(rednote.profile_dir) == tmp_path / "profiles/rednote-primary"
        assert Path(rednote.cookie_file) == tmp_path / "data/cookies/rednote.json"
        settings = app.public_settings()
        assert settings["accounts"]["xhs"]["impersonate"] == "chrome150"
        assert settings["accounts"]["douyin"]["impersonate"] == "chrome150"
        assert settings["accounts"]["xhs"]["binding_version"].endswith("-chrome150")
        account_pool = {row["id"]: row for row in settings["account_pool"]}
        assert "legacy-rednote" in account_pool
        assert account_pool["legacy-xhs"]["impersonate"] == "chrome150"
        assert account_pool["legacy-douyin"]["impersonate"] == "chrome150"
    finally:
        app.worker_pool.shutdown(wait_seconds=10)


def test_multi_account_pool_and_account_agnostic_schedule(console):
    account = console.add_account(
        {
            "platform": "xhs",
            "name": "灰度账号 A",
            "cookie_text": "web_session=account-a; a1=stable",
            "proxy_url": "socks5://name:secret@127.0.0.1:1080",
        }
    )
    env = console.environment("xhs", account["id"])
    assert env.account_ref == "灰度账号 A"
    assert env.consistency_policy == "warn"
    assert "legacy-xhs" in {row["id"] for row in console.public_settings()["account_pool"]}
    assert env.profile_dir != console.environment("xhs").profile_dir
    details = next(
        row for row in console.public_settings()["account_pool"] if row["id"] == account["id"]
    )
    assert details["proxy_url"].startswith("socks5://")
    assert "account-a" in details["cookie_text"]

    updated = console.update_account(
        account["id"],
        {
            "name": "灰度账号 B",
            "environment": {
                "session_version": "2",
                "headless": True,
                "consistency_policy": "strict",
            },
        },
    )
    assert updated["name"] == "灰度账号 B"
    assert updated["session_version"] == "2" and updated["headless"] is True
    assert updated["consistency_policy"] == "strict"

    schedule = console.create_schedule(
        {
            "name": "低负载观察 A",
            "kind": "interval",
            "interval_minutes": 360,
            "config": {
                "platform": "xhs",
                "keywords": ["咖啡"],
                "content_limit": 5,
                "comment_limit": 3,
                "reply_parents": 0,
                "reply_limit": 0,
                "max_requests": 15,
                "min_interval": 10,
            },
        }
    )
    assert schedule["account_id"] is None
    assert console.public_settings()["schedules"][0]["id"] == schedule["id"]
    console.set_schedule_enabled(schedule["id"], {"enabled": False})
    assert console.public_settings()["schedules"][0]["enabled"] is False

    updated = console.update_schedule(
        schedule["id"],
        {
            "name": "工作日低负载观察",
            "kind": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "config": schedule["config"] | {"keywords": ["咖啡", "茶"]},
        },
    )
    assert updated["cron_expression"] == "0 9 * * 1-5"
    assert updated["config"]["keywords"] == ["咖啡", "茶"]
    console.delete_schedule(schedule["id"])
    assert console.public_settings()["schedules"] == []


def test_adspower_profile_is_unique_per_xhs_account(console):
    first = console.add_account(
        {
            "platform": "xhs",
            "name": "AdsPower A",
            "environment": {
                "browser_provider": "adspower",
                "adspower_profile_id": "profile-a",
            },
        }
    )
    assert console.environment("xhs", first["id"]).browser_provider == "adspower"

    with pytest.raises(ConsoleError, match="不能共用 AdsPower 环境"):
        console.add_account(
            {
                "platform": "xhs",
                "name": "AdsPower B",
                "environment": {
                    "browser_provider": "adspower",
                    "adspower_profile_id": "profile-a",
                },
            }
        )


def test_switching_adspower_proxy_owner_clears_old_network_binding(console):
    account = console.add_account({"platform": "xhs", "name": "Proxy owner"})
    with console.store() as store:
        store.confirm_account_network(
            account["id"], proxy_ref="old-project-proxy", egress_ip="203.0.113.10"
        )

    console.update_account(
        account["id"],
        {
            "environment": {
                "browser_provider": "adspower",
                "adspower_profile_id": "profile-a",
            }
        },
    )
    with console.store() as store:
        assert store.get_account_network_binding(account["id"]) is None
        store.confirm_account_network(
            account["id"], proxy_ref="profile-a-ref", egress_ip="203.0.113.11"
        )

    console.update_account(
        account["id"], {"environment": {"adspower_profile_id": "profile-b"}}
    )
    with console.store() as store:
        assert store.get_account_network_binding(account["id"]) is None


def test_adspower_provider_supports_douyin_profile_auth(console):
    account = console.add_account(
        {
            "platform": "douyin",
            "name": "Douyin AdsPower",
            "environment": {
                "browser_provider": "adspower",
                "adspower_profile_id": "profile-a",
            },
        }
    )

    env = console.environment("douyin", account["id"])
    detail = console.account_detail(account["id"])
    assert env.browser_provider == "adspower"
    assert env.adspower_profile_id == "profile-a"
    assert detail["status"] == "ready"
    assert detail["cookie_state"] == "managed"

    with pytest.raises(ConsoleError, match="不能共用 AdsPower 环境"):
        console.add_account(
            {
                "platform": "xhs",
                "name": "Cross-platform duplicate",
                "environment": {
                    "browser_provider": "adspower",
                    "adspower_profile_id": "profile-a",
                },
            }
        )


def test_kameleo_profile_is_unique_and_xhs_only(console):
    profile_id = "11111111-1111-4111-8111-111111111111"
    first = console.add_account(
        {
            "platform": "xhs",
            "name": "Kameleo A",
            "environment": {
                "browser_provider": "kameleo",
                "kameleo_profile_id": profile_id,
            },
        }
    )
    assert console.environment("xhs", first["id"]).browser_provider == "kameleo"
    detail = console.account_detail(first["id"])
    assert detail["status"] == "ready"
    assert detail["cookie_state"] == "managed"
    assert detail["cookie_count"] == 0
    assert detail["cookie_text"] == ""

    with pytest.raises(ConsoleError, match="不能共用 Kameleo 环境"):
        console.add_account(
            {
                "platform": "xhs",
                "name": "Kameleo B",
                "environment": {
                    "browser_provider": "kameleo",
                    "kameleo_profile_id": profile_id,
                },
            }
        )

    with pytest.raises(ConsoleError, match="仅支持小红书"):
        console.add_account(
            {
                "platform": "douyin",
                "name": "unsupported",
                "environment": {
                    "browser_provider": "kameleo",
                    "kameleo_profile_id": "22222222-2222-4222-8222-222222222222",
                },
            }
        )


def test_browser_profile_inventory_marks_existing_bindings(console, monkeypatch):
    from social_crawler.interfaces import web

    account = console.add_account(
        {
            "platform": "xhs",
            "name": "Bound account",
            "environment": {
                "browser_provider": "adspower",
                "adspower_profile_id": "profile-a",
            },
        }
    )

    class Runtime:
        def __init__(self, base_url, *, timeout):
            assert base_url == "http://127.0.0.1:50325"
            assert timeout == 15

        async def list_profiles(self):
            return [
                {
                    "id": "profile-b",
                    "name": "Available",
                    "state": "",
                    "detail": "",
                    "compatible": True,
                },
                {
                    "id": "profile-a",
                    "name": "Bound",
                    "state": "",
                    "detail": "",
                    "compatible": True,
                },
            ]

        async def close(self):
            pass

    monkeypatch.setattr(web, "AdsPowerClient", Runtime)

    for_current = console.browser_profiles("adspower", account_id=account["id"])
    by_id = {item["id"]: item for item in for_current["items"]}
    assert by_id["profile-a"]["available"] is True
    assert by_id["profile-a"]["bound_account_name"] == "Bound account"
    assert by_id["profile-b"]["available"] is True

    for_new = console.browser_profiles("adspower", platform="xhs")
    by_id = {item["id"]: item for item in for_new["items"]}
    assert by_id["profile-a"]["available"] is False
    assert by_id["profile-b"]["available"] is True

    for_douyin = console.browser_profiles("adspower", platform="douyin")
    by_id = {item["id"]: item for item in for_douyin["items"]}
    assert by_id["profile-a"]["available"] is False
    assert by_id["profile-b"]["available"] is True


def test_repair_does_not_reenable_a_manually_disabled_account(console, monkeypatch):
    account = console.add_account({"platform": "xhs", "name": "已停用账号"})
    console.set_account_state(account["id"], {"status": "disabled"})

    def unexpected_repair(*args, **kwargs):
        pytest.fail("disabled accounts must not enter the repair flow")

    monkeypatch.setattr(console, "maintain_account", unexpected_repair)
    completed = wait_job(
        console,
        console.account_maintenance(
            {"action": "repair", "account_id": account["id"]}
        ),
    )

    assert completed["result"]["summary"]["skipped"] == 1
    assert completed["result"]["results"][0]["outcome"] == "skipped"
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "disabled"


def test_one_click_account_repair_confirms_exit_quotas_and_ready(console, monkeypatch):
    from unittest.mock import AsyncMock

    from social_crawler.interfaces import web

    account = console.add_account(
        {
            "platform": "xhs",
            "name": "一键修复账号",
            "cookie_text": "web_session=repair-session; a1=stable",
            "proxy_url": "socks5://name:secret@127.0.0.1:1080",
        }
    )
    with console.store() as store:
        store.set_account_status(account["id"], "cooling", cooldown_until=time.time() + 3600)
    observation = {
        "proxy_ref": "proxy-ref",
        "egress_ip": "203.0.113.10",
        "ip_group": "203.0.113.10",
        "http_egress_ip": "203.0.113.10",
        "browser_egress_ip": "203.0.113.10",
        "source": "test",
        "outcome": "success",
    }
    monkeypatch.setattr(web, "observe_dual_exit", AsyncMock(return_value=observation))
    monkeypatch.setattr(
        console,
        "probe",
        AsyncMock(
            return_value={
                "login_state": "verified",
                "account_nickname": "修复后账号",
            }
        ),
    )

    completed = wait_job(
        console,
        console.account_maintenance({"action": "repair", "account_id": account["id"]}),
    )
    assert completed["result"]["summary"] == {
        "total": 1,
        "ready": 1,
        "needs_login": 0,
        "failed": 0,
    }
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "ready"
        assert store.get_account_network_binding(account["id"])["ip_group"] == "203.0.113.10"
        policies = store.quota_summary()
    assert {policy["dimension"] for policy in policies} == {
        "platform",
        "operation",
        "account",
        "ip_group",
    }
    assert len(policies) == 8
    assert {policy["request_limit"] for policy in policies} == {999999}


def test_one_click_account_repair_marks_missing_cookie_for_login(console):
    account = console.add_account({"platform": "xhs", "name": "缺少登录账号"})
    completed = wait_job(
        console,
        console.account_maintenance({"action": "repair", "account_id": account["id"]}),
    )
    assert completed["result"]["summary"]["needs_login"] == 1
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "login_required"


def test_account_can_be_created_before_login_with_unique_generated_profile(console):
    first = console.add_account({"platform": "xhs", "name": "待登录 A"})
    second = console.add_account({"platform": "xhs", "name": "待登录 B"})
    assert first["status"] == second["status"] == "login_required"
    first_env = console.environment("xhs", first["id"])
    second_env = console.environment("xhs", second["id"])
    assert first_env.profile_dir != second_env.profile_dir
    assert Path(first_env.cookie_file).read_text() == "[]"
    result = wait_job(
        console,
        console.check(
            {"kind": "cookie", "platform": "xhs", "account_id": first["id"]}
        ),
    )
    assert result["account_id"] == first["id"]
    assert result["result"]["cookie_state"] in {"invalid", "missing"}


def test_online_check_probes_cooling_account_and_recovers_it(console, monkeypatch):
    from unittest.mock import AsyncMock

    console.save({"platform": "xhs", "cookie_text": "web_session=session; a1=stable"})
    with console.store() as store:
        store.set_account_status(
            "legacy-xhs", "cooling", cooldown_until=time.time() + 3600
        )
    monkeypatch.setattr(
        console,
        "probe",
        AsyncMock(
            return_value={
                "message": "平台搜索请求成功",
                "state": "reachable",
                "sample_count": 1,
                "login_state": "verified",
                "account_nickname": "测试账号",
            }
        ),
    )

    completed = wait_job(
        console,
        console.check(
            {"kind": "online", "platform": "xhs", "account_id": "legacy-xhs"}
        ),
    )

    assert completed["result"]["account_recovered"] is True
    with console.store() as store:
        assert store.get_account("legacy-xhs")["status"] == "ready"


def test_douyin_playwright_storage_state_is_not_reduced_to_cookies(console):
    storage_state = {
        "cookies": [{"name": "sessionid", "value": "login", "domain": ".douyin.com"}],
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [{"name": "xmst", "value": "synthetic-xmst"}],
            }
        ],
    }

    result = console.save(
        {"platform": "douyin", "cookie_text": json.dumps(storage_state)}
    )
    env = console.environment("douyin")
    persisted = json.loads(Path(env.cookie_file).read_text())

    assert persisted["origins"] == storage_state["origins"]
    assert persisted["cookies"][0]["name"] == "sessionid"
    assert result["accounts"]["douyin"]["browser_state"] == "configured"


def test_database_redaction_and_online_sqlite_rejected(console):
    with pytest.raises(ConsoleError, match="PostgreSQL"):
        console.collect({"mode": "online", "config": {"platform": "xhs", "keywords": ["咖啡"]}})
    console.save(
        {"database_url": "postgresql://a:db-secret@localhost:5432/db?sslpassword=query-secret"}
    )
    public = json.dumps(console.public_settings())
    assert "db-secret" not in public and "query-secret" not in public
    with pytest.raises(ConsoleError):
        console.save({"database_url": "mysql://localhost/test"})


def test_online_preflight_failure_does_not_leave_an_interrupted_run(console, monkeypatch):
    original_store = console.store

    @contextmanager
    def postgres_store():
        with original_store() as store:
            # Exercise the online orchestration path without requiring a test
            # PostgreSQL service; no platform request is made in this test.
            store.engine.dialect.name = "postgresql"
            yield store

    monkeypatch.setattr(console, "store", postgres_store)
    with pytest.raises(ConsoleError, match="可用账号"):
        console.collect(
            {"mode": "online", "config": {"platform": "xhs", "keywords": ["咖啡"]}}
        )

    with original_store() as store, store.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(runs)) == 0


def test_new_online_run_is_created_with_its_account_binding(console, monkeypatch):
    console.save({"platform": "xhs", "cookie_text": "web_session=session; a1=stable"})
    with console.store() as store:
        store.set_account_status("legacy-xhs", "ready")
    original_store = console.store

    @contextmanager
    def postgres_store():
        with original_store() as store:
            store.engine.dialect.name = "postgresql"
            yield store

    def intercept(platform, run_id, resource_keys, work, *, account_id=None):
        return {
            "platform": platform,
            "run_id": run_id,
            "account_id": account_id,
            "status": "queued",
        }

    monkeypatch.setattr(console, "store", postgres_store)
    monkeypatch.setattr(console, "launch_collection", intercept)
    job = console.collect(
        {"mode": "online", "config": {"platform": "xhs", "keywords": ["咖啡"]}}
    )

    assert job["account_id"] == "legacy-xhs"
    with original_store() as store:
        run = store.get_run(job["run_id"])
        context = store.get_run_context(job["run_id"])
    assert run["status"] == "pending"
    assert run["binding"]
    assert context["account_id"] == "legacy-xhs"
    assert context["environment"]["auto_bind_network"] is True
    assert context["environment"]["use_default_quotas"] is True
    assert context["environment"]["cookie_digest"]


def test_managed_browser_online_run_uses_profile_auth_without_cookie_file(
    console, monkeypatch
):
    account = console.add_account(
        {
            "platform": "xhs",
            "name": "Kameleo profile auth",
            "environment": {
                "browser_provider": "kameleo",
                "kameleo_profile_id": "11111111-1111-4111-8111-111111111111",
            },
        }
    )
    original_store = console.store

    @contextmanager
    def postgres_store():
        with original_store() as store:
            store.engine.dialect.name = "postgresql"
            yield store

    def intercept(platform, run_id, resource_keys, work, *, account_id=None):
        return {
            "platform": platform,
            "run_id": run_id,
            "account_id": account_id,
            "status": "queued",
        }

    monkeypatch.setattr(console, "store", postgres_store)
    monkeypatch.setattr(console, "launch_collection", intercept)
    job = console.collect(
        {
            "mode": "online",
            "account_id": account["id"],
            "config": {
                "platform": "xhs",
                "adapter": "browser",
                "keywords": ["咖啡"],
            },
        }
    )

    with original_store() as store:
        context = store.get_run_context(job["run_id"])
    assert context["account_id"] == account["id"]
    assert context["environment"]["cookie_digest"] is None
    assert context["environment"]["proxy_source"] == "kameleo_profile"


def test_douyin_adspower_run_keeps_curl_cffi_and_allows_direct_exit(
    console, monkeypatch
):
    account = console.add_account(
        {
            "platform": "douyin",
            "name": "Douyin AdsPower hybrid",
            "environment": {
                "browser_provider": "adspower",
                "adspower_profile_id": "profile-a",
            },
        }
    )
    original_store = console.store

    @contextmanager
    def postgres_store():
        with original_store() as store:
            store.engine.dialect.name = "postgresql"
            yield store

    def intercept(platform, run_id, resource_keys, work, *, account_id=None):
        return {
            "platform": platform,
            "run_id": run_id,
            "account_id": account_id,
            "status": "queued",
        }

    monkeypatch.setattr(console, "store", postgres_store)
    monkeypatch.setattr(console, "launch_collection", intercept)
    request = {
        "mode": "online",
        "account_id": account["id"],
        "config": {"platform": "douyin", "keywords": ["咖啡"]},
    }

    job = console.collect(request)

    with original_store() as store:
        context = store.get_run_context(job["run_id"])
    assert context["environment"]["cookie_digest"] is None
    assert context["environment"]["http_transport"] == "curl_cffi"
    assert context["environment"]["proxy_source"] == "adspower_profile+direct"


def test_new_online_run_can_require_a_specific_ready_account(console, monkeypatch):
    account = console.add_account(
        {
            "platform": "xhs",
            "name": "指定账号",
            "cookie_text": "web_session=selected-session; a1=stable",
        }
    )
    original_store = console.store

    @contextmanager
    def postgres_store():
        with original_store() as store:
            store.engine.dialect.name = "postgresql"
            yield store

    def intercept(platform, run_id, resource_keys, work, *, account_id=None):
        return {
            "platform": platform,
            "run_id": run_id,
            "account_id": account_id,
            "status": "queued",
        }

    monkeypatch.setattr(console, "store", postgres_store)
    monkeypatch.setattr(console, "launch_collection", intercept)
    job = console.collect(
        {
            "mode": "online",
            "account_id": account["id"],
            "config": {"platform": "xhs", "keywords": ["咖啡"]},
        }
    )

    assert job["account_id"] == account["id"]
    with original_store() as store:
        assert store.get_run_context(job["run_id"])["account_id"] == account["id"]
        store.set_account_status(account["id"], "disabled")

    with pytest.raises(ConsoleError, match="可用账号"):
        console.collect(
            {
                "mode": "online",
                "account_id": account["id"],
                "config": {"platform": "xhs", "keywords": ["茶"]},
            }
        )


def test_queue_submission_failure_marks_run_failed(console, monkeypatch):
    def reject(*args, **kwargs):
        raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(console.worker_pool, "submit", reject)
    with pytest.raises(RuntimeError, match="queue failure"):
        submit_fixture(console,
            { "config": {"platform": "xhs", "keywords": ["咖啡"]}}
        )

    run = console.dashboard()["runs"][0]
    assert run["status"] == "failed"
    assert run["interrupted"] is False
    with console.store() as store:
        snapshot = store.snapshot(run["id"])
    assert snapshot["tasks"][0]["state"]["stop_reason"] == "worker_start_failed"
    assert snapshot["events"][-1]["kind"] == "startup_failed"


def test_exclusive_job_and_settings_mutation(console):
    started, finish = threading.Event(), threading.Event()
    job = console.launch(
        "test",
        "xhs",
        lambda: (started.set(), finish.wait(5), {})[-1],
        resource_keys=console.account_resource_keys("xhs"),
    )
    started.wait(1)
    try:
        with pytest.raises(ConsoleError):
            console.save({"platform": "xhs"})
        # Reading a cookie file does not compete for the browser resource.
        wait_job(console, console.check({"kind": "cookie"}))
    finally:
        finish.set()
    wait_job(console, job)


def test_account_actions_are_scoped_to_their_execution_resources(console, monkeypatch):
    from unittest.mock import AsyncMock

    from social_crawler.interfaces import web

    busy_account = console.add_account({"platform": "xhs", "name": "采集中账号"})
    idle_account = console.add_account({"platform": "xhs", "name": "空闲账号"})
    started, finish = threading.Event(), threading.Event()
    collection = console.launch_collection(
        "xhs",
        "busy-run",
        console.account_resource_keys("xhs", busy_account["id"]),
        lambda: (started.set(), finish.wait(5), {})[-1],
        account_id=busy_account["id"],
    )
    assert started.wait(1)

    monkeypatch.setattr(web, "capture_platform_cookies", AsyncMock(return_value=2))

    try:
        with pytest.raises(ConsoleError, match="该账号或浏览器环境"):
            console.check(
                {
                    "kind": "login",
                    "platform": "xhs",
                    "account_id": busy_account["id"],
                }
            )

        login = wait_job(
            console,
            console.check(
                {
                    "kind": "login",
                    "platform": "xhs",
                    "account_id": idle_account["id"],
                }
            ),
        )
        assert login["result"]["cookie_count"] == 2

        def verified(account, *, repair):
            result = console.maintenance_result(account)
            result.update(outcome="ready", message="验证通过")
            return result

        monkeypatch.setattr(console, "maintain_account", verified)
        maintenance = wait_job(
            console,
            console.account_maintenance({"action": "verify"}),
        )
        assert maintenance["result"]["summary"]["skipped"] == 1
        skipped = [
            row
            for row in maintenance["result"]["results"]
            if row["outcome"] == "skipped"
        ]
        assert [row["id"] for row in skipped] == [busy_account["id"]]
    finally:
        finish.set()
    wait_job(console, collection)


def prepare_online_resume(console, run_id, monkeypatch):
    from functools import partial
    from unittest.mock import AsyncMock

    from social_crawler.interfaces import web
    from social_crawler.storage.store import Store, run_contexts

    console.save({"platform": "xhs", "cookie_text": "web_session=session; a1=stable"})
    original_store = console.store
    with original_store() as store:
        store.set_account_status("legacy-xhs", "ready")
        with store.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(mode="online"))
            conn.execute(update(run_contexts).where(run_contexts.c.run_id == run_id)
                         .values(account_id="legacy-xhs"))

    @contextmanager
    def postgres_store():
        with original_store() as store:
            store.engine.dialect.name = "postgresql"
            yield store

    class Browser(FixtureAdapter):
        def __init__(self, session, budget, samples):
            super().__init__(session.platform, budget, samples)

    monkeypatch.setattr(console, "store", postgres_store)
    monkeypatch.setattr(web, "XHSBrowser", Browser)
    monkeypatch.setattr(Store, "_reserve_window_quotas", lambda *args, **kwargs: None)
    monkeypatch.setattr(Store, "verify_run_network", lambda *args, **kwargs: None)
    monkeypatch.setattr(web, "run_worker", partial(
        web.run_worker, network_observer=AsyncMock(return_value={}),
    ))


def test_budget_resume_and_interruption_detection(console, monkeypatch):
    run_id = collect(console, max_requests=1)
    with console.store() as store:
        assert store.get_run(run_id)["status"] == "partial"
    prepare_online_resume(console, run_id, monkeypatch)
    wait_job(
        console, console.collect({"extra_requests": 40, "extra_seconds": 30}, resume_id=run_id)
    )
    with console.store() as store:
        assert store.get_run(run_id)["status"] == "completed"
        orphan = store.create_run(RunConfig(platform="douyin", keywords=["test"]), mode="offline")
        with store.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == orphan).values(status="running"))
    assert next(r for r in console.dashboard()["runs"] if r["id"] == orphan)["interrupted"]


def test_account_api_hides_legacy_run_local_stop_as_account_failure(console):
    account = console.add_account({"platform": "xhs", "name": "预算历史账号"})
    with console.store() as store:
        with store.engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(accounts.c.id == account["id"])
                .values(
                    status="cooling",
                    cooldown_until=time.time() + 3600,
                    last_failure_at=time.time(),
                    last_failure_kind="request_budget",
                )
            )

    public = next(
        row
        for row in console.public_settings()["account_pool"]
        if row["id"] == account["id"]
    )
    assert public["status"] == "cooling"
    assert public["last_failure_at"] is None
    assert public["last_failure_kind"] is None


def test_cancel_preserves_data(console, monkeypatch):

    original = FixtureAdapter.fetch

    async def paced(self, request):
        import asyncio

        await asyncio.sleep(0.15)
        return await original(self, request)

    monkeypatch.setattr(FixtureAdapter, "fetch", paced)
    job = submit_fixture(console,
        { "config": {"platform": "xhs", "keywords": ["test"], "min_interval": 0}}
    )
    with console.store() as store:
        store.request_cancel(job["run_id"])
    wait_job(console, job)
    with console.store() as store:
        assert store.get_run(job["run_id"])["status"] == "canceled"


def test_http_security_errors_and_export(console, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            conn.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers=headers or {},
            )
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    try:
        status, raw, headers = request("GET", "/")
        assert status == 200 and "拾集" in raw.decode()
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, raw, _ = request("GET", "/app.js")
        assert status == 200
        assert '<option value="latest" selected>最新发布</option>' in raw.decode()
        assert '<option value="httpx">直接接口请求（试用）</option>' in raw.decode()
        assert '<option value="curl_cffi">模拟浏览器指纹请求（试用）</option>' in raw.decode()
        assert 'name="download_media" type="checkbox"' in raw.decode()
        assert 'name="cron_expression"' in raw.decode()
        assert '["max_pages", "最大页数", 100, 1, 1000]' in raw.decode()
        assert '["max_pages", "最大页数", config.max_pages, 1, 1000]' in raw.decode()
        status, bootstrap, _ = request("GET", "/api/bootstrap")
        reasons = json.loads(bootstrap)["settings"]["ui_labels"]["reasons"]
        assert reasons["request_budget"] == "单次任务请求上限已用尽"
        assert reasons["quota_exhausted"] == "风控窗口额度已用尽"
        assert 'id="modal-run-budget"' in raw.decode()
        monkeypatch.setattr(
            console,
            "browser_profiles",
            lambda provider, **kwargs: {
                "provider": provider,
                "platform": kwargs["platform"],
                "items": [],
            },
        )
        status, raw, _ = request(
            "GET", "/api/browser-profiles?provider=adspower&platform=rednote"
        )
        assert status == 200
        assert json.loads(raw) == {
            "provider": "adspower",
            "platform": "rednote",
            "items": [],
        }
        status, raw, _ = request("GET", "/api/health", headers={"Host": "localhost:9876"})
        assert status == 200 and json.loads(raw) == {
            "status": "ok",
            "workers": 1,
            "queued": 0,
            "running": 0,
        }
        assert request("GET", "/data/postgres.url")[0] == 404
        assert request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0] == 403
        monkeypatch.setenv("CRAWLER_WEB_ALLOWED_HOSTS", "192.0.2.10")
        assert request("GET", "/api/bootstrap", headers={"Host": "192.0.2.10:30865"})[0] == 200
        assert (
            request(
                "GET",
                "/api/bootstrap",
                headers={
                    "Host": "192.0.2.10:30865",
                    "Origin": "http://192.0.2.10:30865",
                },
            )[0]
            == 200
        )
        assert (
            request(
                "GET",
                "/api/bootstrap",
                headers={
                    "Host": "192.0.2.10:30865",
                    "Origin": "http://evil.example",
                },
            )[0]
            == 403
        )
        monkeypatch.delenv("CRAWLER_WEB_ALLOWED_HOSTS")
        assert request("POST", "/api/settings", {})[0] == 403
        status, raw, _ = request("GET", "/api/bootstrap")
        token = json.loads(raw)["token"]
        headers = {"X-Console-Token": token, "Content-Type": "application/json"}
        assert (
            request("POST", "/api/settings", {}, headers | {"Origin": "https://evil.example"})[0]
            == 403
        )
        status, raw, _ = request(
            "POST",
            "/api/runs",
            {
                "mode": "offline",
                "config": {
                    "platform": "xhs",
                    "keywords": ["http test"],
                    "min_interval": 0,
                    "adapter": "httpx",
                    "download_media": True,
                },
            },
            headers,
        )
        assert status == 400, raw
        assert "仅支持真实采集" in json.loads(raw)["error"]
        job = submit_fixture(console, {"config": {
            "platform": "xhs", "keywords": ["http test"], "adapter": "httpx",
            "download_media": True,
        }})
        wait_job(console, job)
        with console.store() as store:
            assert store.get_run(job["run_id"])["config"]["adapter"] == "httpx"
            assert store.get_run(job["run_id"])["config"]["download_media"] is True
        status, raw, _ = request("GET", f"/api/runs/{job['run_id']}")
        detail = json.loads(raw)
        assert status == 200
        assert detail["run"]["requests"] > 0
        assert detail["run"]["config"]["max_requests"] >= detail["run"]["requests"]
        status, raw, hdrs = request("GET", f"/api/runs/{job['run_id']}/export")
        assert status == 200 and "attachment" in hdrs["Content-Disposition"]
        assert json.loads(raw)["items"]
        assert request("POST", "/api/check", {"kind": "unknown"}, headers)[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_console_on_configured_database(console, store):
    url = store.engine.url
    if store.engine.dialect.name == "postgresql":
        url = url.set(query=dict(url.query) | {"options": f"-csearch_path={store.test_schema}"})
    console.settings["database_url"] = url.render_as_string(hide_password=False)
    run_id = collect(console)
    assert console.results({"run_id": [run_id], "q": ["合成标题"]})["total"] == 3
    assert console.dashboard()["stats"]["comments"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"status_code": 0, "user": {"uid": "123", "sec_uid": "self-id"}}, "verified"),
        ({"status_code": 0, "user": {"uid": "0", "sec_uid": "guest"}}, "unverified"),
        (
            {"status_code": 0, "aweme_list": [{"author": {"uid": "123", "sec_uid": "someone"}}]},
            "unverified",
        ),
        ({"status_code": 8, "user": {"uid": "123", "sec_uid": "old"}}, "auth"),
    ],
)
async def test_douyin_identity_uses_only_successful_self_response(payload, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from social_crawler.interfaces.web import douyin_identity

    response = SimpleNamespace(status_code=200, json=lambda: payload)
    security = SimpleNamespace(
        sign=AsyncMock(
            return_value=SimpleNamespace(
                url="https://www.douyin.com/aweme/v1/web/user/profile/self/",
                headers={},
                cookie_header="test",
            )
        )
    )
    adapter = SimpleNamespace(
        security=security,
        parameters=lambda _: {},
        client=SimpleNamespace(get=AsyncMock(return_value=response)),
        session=SimpleNamespace(config=SimpleNamespace(user_agent="test")),
    )
    budget = SimpleNamespace(admit=AsyncMock())
    if expected == "auth":
        from social_crawler.domain.models import CollectionError

        with pytest.raises(CollectionError) as failure:
            await douyin_identity(adapter, budget)
        assert failure.value.kind == "access_denied"
        return
    result = await douyin_identity(adapter, budget)
    assert result == expected
    budget.admit.assert_awaited_once_with("search")
    assert security.sign.call_args.args[0] == "/aweme/v1/web/user/profile/self/"


@pytest.mark.asyncio
async def test_douyin_identity_returns_verified_nickname():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from social_crawler.interfaces.web import douyin_identity

    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "status_code": 0,
            "user": {"uid": "123", "sec_uid": "self-id", "nickname": "抖音昵称"},
        },
    )
    adapter = SimpleNamespace(
        security=SimpleNamespace(
            sign=AsyncMock(
                return_value=SimpleNamespace(
                    url="https://example.test", headers={}, cookie_header=""
                )
            )
        ),
        parameters=lambda _: {},
        client=SimpleNamespace(get=AsyncMock(return_value=response)),
        session=SimpleNamespace(config=SimpleNamespace(user_agent="test")),
    )
    state, profile = await douyin_identity(
        adapter, SimpleNamespace(admit=AsyncMock()), include_profile=True
    )
    assert state == "verified"
    assert profile == {
        "user_id": "123",
        "sec_uid": "self-id",
        "nickname": "抖音昵称",
    }


def test_stop_during_resume_startup_is_not_lost(console, monkeypatch):
    import asyncio

    from social_crawler.interfaces import web

    run_id = collect(console, max_requests=1)
    original = web.run_worker

    async def delayed_start(*args, **kwargs):
        await asyncio.sleep(0.1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(web, "run_worker", delayed_start)
    prepare_online_resume(console, run_id, monkeypatch)
    job = console.collect({"extra_requests": 40}, resume_id=run_id)
    with console.store() as store:
        store.request_cancel(run_id)
    wait_job(console, job)
    with console.store() as store:
        assert store.get_run(run_id)["status"] == "canceled"


def test_account_and_proxy_diagnostics_are_explicit(console):
    console.save({"platform": "xhs", "cookie_text": "web_session=test"})
    result = wait_job(console, console.check({"kind": "account", "platform": "xhs"}))
    assert result["result"]["state"] == "configured"
    result = wait_job(console, console.check({"kind": "proxy", "platform": "xhs"}))
    assert result["result"]["state"] == "direct"


def test_account_nickname_replaces_only_generated_alias(console):
    account_ref, updated = console.adopt_identity_nickname("xhs", "xhs-primary", "  登录   昵称  ")
    assert (account_ref, updated) == ("登录 昵称", True)
    assert console.environment("xhs").account_ref == "登录 昵称"

    account_ref, updated = console.adopt_identity_nickname("xhs", "登录 昵称", "另一个昵称")
    assert (account_ref, updated) == ("登录 昵称", False)


def test_run_headless_override_does_not_mutate_account_default(console):
    env = console.environment("xhs")
    inherited = console.run_environment(env, RunConfig(platform="xhs", keywords=["咖啡"]))
    overridden = console.run_environment(
        env, RunConfig(platform="xhs", keywords=["咖啡"], headless=True)
    )
    assert inherited.headless is env.headless is False
    assert overridden.headless is True
    assert console.environment("xhs").headless is False


def use_store(console, store):
    url = store.engine.url
    if store.engine.dialect.name == "postgresql":
        url = url.set(query=dict(url.query) | {"options": f"-csearch_path={store.test_schema}"})
    console.settings["database_url"] = url.render_as_string(hide_password=False)


def test_lifecycle_dates_filter_old_notes_separately(console, store):
    from social_crawler.domain.timestamps import unix_timestamp

    use_store(console, store)
    today = unix_timestamp("2026-09-11T00:00:00+08:00")
    records = [
        ("today", today + 1, today + 5, today + 10),
        ("week-old", today - 7 * 86400, None, today + 10),
        ("last-second", (today + 86400 - 1) * 1000, None, today + 10),
        ("tomorrow", today + 86400, None, today + 86400),
        ("unknown", None, None, today + 10),
    ]
    with store.engine.begin() as conn:
        for item_id, published, updated, observed in records:
            conn.execute(
                insert(contents).values(
                    platform="xhs",
                    id=item_id,
                    data={
                        "title": "笔记",
                        "published_at": published,
                        "source_updated_at": updated,
                        "author_name": "测试作者",
                        "detail_complete": True,
                    },
                    observed_at=observed,
                )
            )
    # Legacy rows are backfilled additively; their first collection date stays unknown.
    base = {"date_from": ["2026-09-11"], "date_to": ["2026-09-11"]}

    def ids(extra):
        return {i["id"] for i in console.results(base | extra)["items"]}

    assert ids({"time_field": ["published_at"]}) == {"today", "last-second"}
    assert ids({"time_field": ["collected_at"]}) == {"today", "week-old", "last-second", "unknown"}
    assert ids({"time_field": ["source_updated_at"]}) == {"today"}
    assert ids({"time_field": ["first_collected_at"]}) == set()
    unknown = console.results({"time_field": ["published_at"], "time_known": ["unknown"]})
    assert {i["id"] for i in unknown["items"]} == {"unknown"}
    assert console.results({"author": ["测试"], "detail": ["complete"]})["total"] == 5
    assert console.results({"detail": ["partial"]})["total"] == 0
    with pytest.raises(ConsoleError):
        console.results({"date_from": ["2026-09-12"], "date_to": ["2026-09-11"]})
    with pytest.raises(ConsoleError):
        console.results({"time_field": ["bogus"]})
    row = console.detail("xhs", "today")["item"]
    assert row["published_at"] == today + 1 and row["first_collected_at"] is None


def test_first_collection_time_does_not_change_on_recollection(console, store):
    use_store(console, store)
    collect(console)
    before = {i["id"]: i for i in console.results({})["items"]}
    assert all(i["first_collected_at"] for i in before.values())
    collect(console)
    after = {i["id"]: i for i in console.results({})["items"]}
    for key in before:
        assert after[key]["first_collected_at"] == before[key]["first_collected_at"]
        assert after[key]["observed_at"] >= before[key]["observed_at"]


def test_tasks_filters_apply_before_pagination_and_use_execution_dates(console, store):
    from social_crawler.domain.timestamps import unix_timestamp
    from social_crawler.storage.store import events

    use_store(console, store)
    today = unix_timestamp("2026-09-11T00:00:00+08:00")
    created = []
    for i in range(25):
        run_id = store.create_run(
            RunConfig(
                platform="xhs" if i == 0 else "douyin",
                keywords=["旧咖啡笔记" if i == 0 else "最新笔记"],
            ),
            mode="offline",
        )
        with store.engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(created_at=today - 86400 + i, status="completed" if i == 0 else "partial")
            )
            conn.execute(
                insert(events).values(
                    run_id=run_id,
                    kind="attempt_started",
                    at=today + 10 if i == 0 else today - 86400,
                    data={},
                )
            )
        created.append(run_id)
    assert len(console.dashboard()["runs"]) == 20
    assert len(console.dashboard({"page": ["2"]})["runs"]) == 5
    result = console.dashboard(
        {
            "q": ["咖啡"],
            "platform": ["xhs"],
            "status": ["completed"],
            "date_from": ["2026-09-11"],
            "date_to": ["2026-09-11"],
            "time_field": ["executed_at"],
        }
    )
    assert result["total"] == 1 and result["runs"][0]["id"] == created[0]
    assert (
        console.dashboard(
            {"q": ["咖啡"], "time_field": ["created_at"], "date_from": ["2026-09-11"]}
        )["total"]
        == 0
    )
    assert console.dashboard({"mode": ["online"]})["total"] == 0


def test_dashboard_includes_and_searches_execution_account_and_schedule(console, store):
    from social_crawler.orchestration.scheduler import Scheduler

    use_store(console, store)
    account = store.create_account("xhs", "采集账号甲", {})
    schedule = Scheduler(store).create(
        {
            "name": "每日品牌观察",
            "kind": "interval",
            "interval_minutes": 60,
            "config": {"platform": "xhs", "keywords": ["咖啡"]},
        }
    )
    run_id = store.create_run(
        RunConfig(platform="xhs", keywords=["咖啡"]),
        mode="online",
        account_id=account["id"],
        schedule_id=schedule["id"],
    )

    row = next(item for item in console.dashboard()["runs"] if item["id"] == run_id)
    assert row["account_name"] == "采集账号甲"
    assert row["schedule_name"] == "每日品牌观察"
    assert console.dashboard({"q": ["采集账号甲"]})["total"] == 1
    assert console.dashboard({"q": ["品牌观察"]})["total"] == 1


def test_diagnostics_persist_and_invalidate_after_file_change(console):
    console.save({"platform": "xhs", "cookie_text": "web_session=first"})
    job = console.check({"platform": "xhs", "kind": "cookie"})
    wait_job(console, job)
    assert console.diagnostic_jobs()[-1]["current"]
    reloaded = Console(console.root)
    assert reloaded.diagnostic_jobs()[-1]["current"]
    reloaded.resolve(reloaded.environment("xhs").cookie_file).write_text("web_session=second")
    assert not reloaded.diagnostic_jobs()[-1]["current"]
    assert "first" not in reloaded.checks_path.read_text()
    assert reloaded.checks_path.stat().st_mode & 0o777 == 0o600


def test_diagnostic_history_batches_accounts_and_rechecks_changed_files(console, monkeypatch):
    account = console.add_account({
        "platform": "xhs", "name": "批量检测", "cookie_text": "web_session=first",
    })
    fingerprints = {
        kind: console.check_fingerprint(kind, "xhs", account["id"])
        for kind in ("cookie", "proxy")
    }
    console.jobs = {
        str(i): {"id": str(i), "kind": kind, "platform": "xhs", "account_id": account["id"],
                 "fingerprint": fingerprints[kind], "status": "completed"}
        for i in range(60) for kind in ["cookie" if i % 2 else "proxy"]
    }
    original_store = console.store
    original_fingerprint = console.check_fingerprint
    store_calls, fingerprint_calls = [], []

    @contextmanager
    def counted_store():
        store_calls.append(1)
        with original_store() as store:
            yield store

    def counted_fingerprint(*args, **kwargs):
        fingerprint_calls.append(args)
        return original_fingerprint(*args, **kwargs)

    monkeypatch.setattr(console, "store", counted_store)
    monkeypatch.setattr(console, "check_fingerprint", counted_fingerprint)
    assert all(job["current"] for job in console.diagnostic_jobs())
    assert len(store_calls) == 1
    assert len(fingerprint_calls) == 2
    Path(console.environment("xhs", account["id"]).cookie_file).write_text("web_session=second")
    changed = console.diagnostic_jobs()
    assert all(not job["current"] for job in changed if job["kind"] == "cookie")
    assert all(job["current"] for job in changed if job["kind"] == "proxy")
    console.delete_account(account["id"])
    assert all(not job["current"] for job in console.diagnostic_jobs())


def test_compact_settings_defer_large_credentials_to_account_detail(console):
    cookie = "web_session=" + "x" * 200_000
    account = console.add_account({"platform": "xhs", "name": "大凭据", "cookie_text": cookie})
    settings = console.public_settings(include_credentials=False)
    assert len(json.dumps(settings)) < 50_000
    assert all("cookie_text" not in row for row in settings["accounts"].values())
    assert all("cookie_text" not in row for row in settings["account_pool"])
    summary = next(a for a in settings["account_pool"] if a["id"] == account["id"])
    detail = console.account_detail(account["id"])
    assert json.loads(detail["cookie_text"])[0]["value"] == "x" * 200_000
    assert detail["cookie_text"] == Path(detail["cookie_file"]).read_text().strip()
    assert summary == {key: value for key, value in detail.items() if key != "cookie_text"}
    assert summary["cookie_state"] == "configured"


def test_proxy_exit_geo_uses_proxy_without_cookies(monkeypatch):
    from social_crawler.interfaces.web import proxy_diagnostic

    calls = []

    class HTTP:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            from types import SimpleNamespace

            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "success": True,
                    "ip": "203.0.113.7",
                    "country_code": "CN",
                    "region": "Jiangxi",
                    "city": "Nanchang",
                    "latitude": 28.68,
                    "longitude": 115.88,
                    "postal": "330008",
                },
            )

    monkeypatch.setattr("curl_cffi.requests.Session", HTTP)
    result = proxy_diagnostic("socks5://test:secret@localhost:1234", "xhs", "chrome150")
    assert result["ip"] == "203.0.113.7" and result["location"]["city"] == "Nanchang"
    assert all(c[1]["proxy"] == "socks5://test:secret@localhost:1234" for c in calls)
    assert all("headers" not in c[1] for c in calls)
    assert calls[1][0] == "https://ipwho.is/"


def test_source_updated_time_is_not_collection_time():
    from social_crawler.adapters.xhs.parsing import content_item
    from social_crawler.domain.timestamps import unix_timestamp

    item = content_item(
        {"id": "note", "time": 1_700_000_000_000, "last_update_time": 1_710_000_000_000}
    )
    assert unix_timestamp(item.data["source_updated_at"]) == 1_710_000_000
    assert unix_timestamp("2026-09-11T00:00:00+08:00") == unix_timestamp("2026-09-10T16:00:00Z")
    assert unix_timestamp("not-a-time") is None
    assert unix_timestamp(float("nan")) is None


@pytest.fixture
def session_recovery_account(console, monkeypatch):
    from unittest.mock import AsyncMock

    from social_crawler.interfaces import web

    account = console.add_account({"platform": "xhs", "name": "恢复测试", "cookie_text":
                                   "web_session=old; a1=synthetic"})
    env = console.environment("xhs", account["id"])
    profile = Path(env.profile_dir)
    profile.mkdir(parents=True)
    (profile / "session-recovery.json").write_text('{"user_id":"expected"}')
    observation = {"proxy_ref": "test-ref", "egress_ip": "203.0.113.10",
                   "ip_group": "203.0.113.10", "http_egress_ip": "203.0.113.10",
                   "browser_egress_ip": "203.0.113.10", "source": "test", "outcome": "success"}
    with console.store() as store:
        store.confirm_account_network(account["id"], proxy_ref="test-ref",
                                      egress_ip="203.0.113.10")
        console.ensure_account_quota_coverage(store, account, "203.0.113.10")
        store.require_session_recovery(account["id"])
    monkeypatch.setattr(web, "observe_dual_exit", AsyncMock(return_value=observation))
    return account, env


async def test_session_recovery_uses_persistent_quota_then_fresh_probe(
    console, monkeypatch, session_recovery_account,
):
    from social_crawler.environments.session import Session
    from social_crawler.environments.session_recovery import read_recovery_receipt
    from social_crawler.interfaces import web
    from social_crawler.storage.store import events

    account, env = session_recovery_account
    old_session = Session(env, "xhs")
    with console.store() as store:
        store.set_automatic_cooldown(True)

    async def capture(platform, **kwargs):
        assert kwargs["mode"] == "recover"
        assert kwargs["environment_session"].config == env
        await kwargs["admit"]("search")
        web.write_cookie_file(kwargs["output"], [{"name": "web_session", "value": "new",
                                                 "domain": ".xiaohongshu.com"}])
        return 1

    monkeypatch.setattr(web, "capture_platform_cookies", capture)
    result = await console.recover_session(env, "xhs", account["id"], automatic=True)
    assert result["state"] == "probe_due"
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "probe_due"
        with store.engine.connect() as connection:
            run = connection.execute(select(runs)).mappings().one()
            assert run["mode"] == "online"
            assert run["requests"] == 1
            assert "new" not in json.dumps(connection.execute(select(events.c.data)).scalars().all())
    from social_crawler.domain.models import CollectionError
    with pytest.raises(CollectionError, match="Cookie file changed"):
        old_session.check_unchanged()
    assert Session(env, "xhs").cookie_value("web_session") == "new"
    assert read_recovery_receipt(env.profile_dir)["outcome"] == "recovered"


async def test_recovery_failure_is_persistent_and_not_retried(
    console, monkeypatch, session_recovery_account,
):
    from unittest.mock import AsyncMock

    from social_crawler.domain.models import CollectionError
    from social_crawler.environments.session_recovery import read_recovery_receipt
    from social_crawler.interfaces import web

    account, env = session_recovery_account
    capture = AsyncMock(side_effect=CollectionError("auth_expired"))
    monkeypatch.setattr(web, "capture_platform_cookies", capture)
    before = Path(env.cookie_file).read_bytes()
    with pytest.raises(CollectionError):
        await console.recover_session(env, "xhs", account["id"], automatic=True)
    assert Path(env.cookie_file).read_bytes() == before
    assert read_recovery_receipt(env.profile_dir)["outcome"] == "auth_expired"
    result = await console.recover_session(env, "xhs", account["id"], automatic=True)
    assert result["state"] == "skipped"
    assert console.dispatch_session_recoveries() is False
    assert capture.await_count == 1
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "login_required"


@pytest.mark.parametrize("kind", ["identity_mismatch", "verification_required", "access_denied"])
def test_scheduler_does_not_auto_recover_platform_stops(
    console, session_recovery_account, kind,
):
    from social_crawler.storage.store import accounts

    account, _ = session_recovery_account
    with console.store() as store:
        with store.engine.begin() as conn:
            conn.execute(update(accounts).where(accounts.c.id == account["id"]).values(
                last_failure_kind=kind,
            ))
    assert console.dispatch_session_recoveries() is False


def test_recovery_scheduler_requires_identity_binding(console, session_recovery_account):
    _, env = session_recovery_account
    (Path(env.profile_dir) / "session-recovery.json").unlink()
    assert console.dispatch_session_recoveries() is False


async def test_recovery_cannot_export_without_quota(console, monkeypatch, session_recovery_account):
    from social_crawler.domain.models import CollectionError
    from social_crawler.interfaces import web
    from social_crawler.storage.store import quota_policies

    account, env = session_recovery_account
    with console.store() as store:
        with store.engine.begin() as conn:
            conn.execute(update(quota_policies).values(enabled=False))

    async def capture(platform, **kwargs):
        await kwargs["admit"]("search")
        pytest.fail("Recovery passed missing quota coverage")

    monkeypatch.setattr(web, "capture_platform_cookies", capture)
    before = Path(env.cookie_file).read_bytes()
    with pytest.raises(CollectionError) as failure:
        await console.recover_session(env, "xhs", account["id"])
    assert failure.value.kind == "quota_policy_missing"
    assert Path(env.cookie_file).read_bytes() == before
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "login_required"


@pytest.mark.browser
async def test_account_edit_has_no_diagnostic_workflow_in_real_browser(console):
    from playwright.async_api import async_playwright, expect

    account = console.add_account({"platform": "xhs", "name": "界面恢复测试"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                await page.locator('[data-page="accounts"]').click()
                await page.locator(f'[data-account-detail="{account["id"]}"]').click()
                form = page.locator("#account-detail-form")
                await expect(form.locator("[data-account-check], [data-detail-availability]")).to_have_count(0)
                await form.locator("summary").filter(has_text="高级配置与登录凭据").click()
                await expect(form.get_by_role("button", name="从已有浏览器同步登录")).to_have_count(0)
                toggle = form.locator('[name="auto_session_recovery"]')
                await expect(toggle).to_be_checked()
                await toggle.uncheck()
                await form.get_by_role("button", name="保存账号配置").click()
                await expect(page.locator("#modal")).not_to_be_visible()
                assert console.environment("xhs", account["id"]).auto_session_recovery is False
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
@pytest.mark.parametrize("platform", ["xhs", "douyin"])
async def test_account_profile_picker_discovers_and_persists_selection(
    console, monkeypatch, platform
):
    from playwright.async_api import async_playwright, expect

    account = console.add_account({"platform": platform, "name": "环境选择测试"})

    def profiles(provider, **_kwargs):
        assert provider == "adspower"
        return {
            "provider": provider,
            "items": [
                {
                    "id": "profile-a",
                    "name": "XHS primary",
                    "state": "closed",
                    "detail": "No. 1",
                    "compatible": True,
                    "available": True,
                    "bound_account_id": None,
                    "bound_account_name": None,
                }
            ],
        }

    monkeypatch.setattr(console, "browser_profiles", profiles)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                await page.locator('[data-page="accounts"]').click()
                await page.locator(f'[data-account-detail="{account["id"]}"]').click()
                form = page.locator("#account-detail-form")
                await form.locator("summary").filter(has_text="高级配置与登录凭据").click()
                await expect(form.locator('[name="browser_provider"] option')).to_have_count(
                    3 if platform == "xhs" else 2
                )
                await form.locator('[name="browser_provider"]').select_option("adspower")
                profile = form.locator('[name="adspower_profile_id"]')
                await expect(profile.locator("option")).to_have_count(2)
                await profile.select_option("profile-a")
                await form.get_by_role("button", name="保存账号配置").click()
                await expect(page.locator("#modal")).not_to_be_visible()
                env = console.environment(platform, account["id"])
                assert env.browser_provider == "adspower"
                assert env.adspower_profile_id == "profile-a"
                assert env.kameleo_profile_id is None
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_media_endpoint_supports_byte_ranges(console):
    run_id = collect(console, comment_limit=0, reply_limit=0)
    item = console.results({"run_id": [run_id]})["items"][0]
    relative = Path("media") / "xhs" / item["id"] / "video-0.mp4"
    path = console.artifact_root / relative
    path.parent.mkdir(parents=True)
    payload = b"\x00\x00\x00\x18ftyp" + bytes(range(64))
    path.write_bytes(payload)
    with console.store() as store, store.engine.begin() as conn:
        data = dict(
            conn.scalar(
                select(contents.c.data).where(
                    contents.c.platform == "xhs", contents.c.id == item["id"]
                )
            )
        )
        data["media_downloads"] = [
            {
                "media_index": 0,
                "kind": "video",
                "status": "downloaded",
                "path": relative.as_posix(),
                "bytes": len(payload),
                "sha256": "test",
                "content_type": "video/mp4",
            }
        ]
        conn.execute(
            update(contents)
            .where(contents.c.platform == "xhs", contents.c.id == item["id"])
            .values(data=data)
        )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request(
            "GET",
            f"/api/media?platform=xhs&id={item['id']}&index=0",
            headers={"Range": "bytes=4-11"},
        )
        response = conn.getresponse()
        assert response.status == 206
        assert response.getheader("Content-Type") == "video/mp4"
        assert response.getheader("Accept-Ranges") == "bytes"
        assert response.getheader("Content-Range") == f"bytes 4-11/{len(payload)}"
        assert response.read() == payload[4:12]
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("state,reason", [("expired", "auth_expired"),
                                          ("unverified", "identity_unverified")])
def test_online_unconfirmed_identity_stops_account(console, monkeypatch, state, reason):
    from unittest.mock import AsyncMock

    from social_crawler.domain.models import CollectionError

    account = console.add_account({"platform": "xhs", "name": "在线停止测试",
                                   "cookie_text": "web_session=synthetic"})
    monkeypatch.setattr(console, "probe", AsyncMock(return_value={"login_state": state}))
    monkeypatch.setattr(console, "launch", lambda kind, platform, work, **kwargs: work())
    with pytest.raises(CollectionError) as failure:
        console.check({"kind": "online", "platform": "xhs", "account_id": account["id"]})
    assert failure.value.kind == reason
    with console.store() as store:
        row = store.get_account(account["id"])
        assert row["status"] == "login_required"
        assert row["last_failure_kind"] == reason


def test_failed_reauth_never_leaves_account_ready(console, monkeypatch):
    from unittest.mock import AsyncMock

    from social_crawler.domain.models import CollectionError
    from social_crawler.interfaces import web

    account = console.add_account({"platform": "xhs", "name": "人工登录停止测试",
                                   "cookie_text": "web_session=synthetic"})
    monkeypatch.setattr(web, "capture_platform_cookies",
                        AsyncMock(side_effect=CollectionError("identity_mismatch")))
    monkeypatch.setattr(console, "launch", lambda kind, platform, work, **kwargs: work())
    with pytest.raises(CollectionError):
        console.check({"kind": "login", "platform": "xhs", "account_id": account["id"]})
    with console.store() as store:
        assert store.get_account(account["id"])["status"] == "login_required"


def test_auto_recovery_switch_blocks_dispatch(console, session_recovery_account):
    from social_crawler.storage.store import accounts

    account, _ = session_recovery_account
    with console.store() as store:
        row = store.get_account(account["id"])
        with store.engine.begin() as conn:
            conn.execute(update(accounts).where(accounts.c.id == account["id"]).values(
                environment=row["environment"] | {"auto_session_recovery": False},
            ))
    assert console.dispatch_session_recoveries() is False


@pytest.mark.parametrize("platform,cid", [("xhs", "66fad51c000000001b0224b8"), ("douyin", "7123456789012345678")])
def test_post_runs_are_visible_in_results_and_source_filters(console, platform, cid):
    run_id = collect(console, platform, source_type="posts", keywords=[], post_targets=[cid])
    data = console.results({"run_id": [run_id], "mode": ["offline"]})
    assert data["total"] == 1 and data["items"][0]["id"] == cid
    assert data["items"][0]["source_mode"] == "offline"
    assert console.dashboard({"source_type": ["posts"], "q": [cid]})["total"] == 1
    assert console.dashboard({"source_type": ["keyword"]})["total"] == 0


@pytest.mark.browser
async def test_targeted_collection_form_and_progress_in_real_browser(console):
    from playwright.async_api import async_playwright, expect

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = console
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                await page.locator('[data-page="workbench"]').click()
                await page.locator('#new-collection').click()
                form = page.locator('#collect-form')
                await form.locator('[name="source_type"]').select_option('posts')
                await expect(form.locator('#keywords')).not_to_be_visible()
                await expect(form.locator('[name="content_limit"]')).not_to_be_visible()
                await form.locator('[name="post_targets"]').fill('66fad51c000000001b0224b8\n无效输入')
                await form.locator('[name="comment_scope"]').select_option('none')
                await expect(form.locator('[name="comment_limit"]')).not_to_be_visible()
                await form.locator('[name="comment_scope"]').select_option('replies')
                await form.locator('details.advanced summary').click()
                await expect(form.locator('[name="download_media"]')).not_to_be_checked()
                await form.locator('[name="max_pages"]').fill('321')
                await expect(form.locator('[name="mode"]')).to_have_count(0)
                await expect(form.locator('[name="account_id"]')).to_be_visible()

                async def create_run(route):
                    body = route.request.post_data_json
                    assert body.get("mode", "online") == "online"
                    await route.fulfill(json=submit_fixture(console, body))

                await page.route('**/api/runs', create_run)
                await form.locator('[name="download_media"]').uncheck()
                await form.screenshot(path='/tmp/social-crawler-posts-form.png')
                await form.locator('[type="submit"]').click()
                await expect(page.locator('#modal-run-keywords')).to_contain_text('指定帖子')
                await expect(page.locator('#run-progress')).to_contain_text('输入无效', timeout=15000)
                await expect(page.locator('#run-progress')).to_contain_text('评论回复', timeout=15000)
                await expect(page.locator('#run-progress')).to_contain_text('66fad51c000000001b0224b8')
                await page.locator('#modal').screenshot(path='/tmp/social-crawler-posts-progress.png')
                assert console.dashboard()["runs"][0]["config"]["max_pages"] == 321
                assert console.dashboard()["runs"][0]["config"]["download_media"] is False
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_tokens_are_redacted_in_public_views_and_preserved_in_schedule_updates(console):
    from social_crawler.orchestration.scheduler import Scheduler

    secret = 'unique-private-post-token'
    target = 'https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8?xsec_token=' + secret
    run_id = collect(console, source_type='posts', keywords=[], post_targets=[target])
    schedule = console.create_schedule({
        'name': 'private posts', 'kind': 'cron', 'cron_expression': '0 9 * * *', 'timezone': 'UTC',
        'config': {'platform': 'xhs', 'source_type': 'posts', 'post_targets': [target]},
    })
    for public in (schedule, console.public_settings(), console.dashboard(), console.set_schedule_enabled(schedule['id'], {'enabled': False})):
        assert secret not in json.dumps(public)
    result = console.update_schedule(schedule['id'], {'name': 'edited', 'config': schedule['config']})
    assert secret not in json.dumps(result)
    with console.store() as store:
        assert Scheduler(store).list()[0]['config']['post_targets'] == [target]
        assert store.get_run(run_id)['config']['post_targets'] == [target]


@pytest.mark.parametrize("mode", ["offline", "demo", "unknown"])
def test_collection_rejects_non_online_modes_without_creating_runs(console, mode):
    with pytest.raises(ConsoleError, match="仅支持真实采集"):
        console.collect({"mode": mode, "config": {"platform": "xhs", "keywords": ["test"]}})
    assert console.dashboard()["total"] == 0


def test_cannot_resume_archived_synthetic_run(console):
    run_id = collect(console, max_requests=1)
    with pytest.raises(ConsoleError, match="仅支持恢复真实采集任务"):
        console.collect({}, resume_id=run_id)
    with console.store() as store:
        assert store.get_run(run_id)["status"] == "partial"


def test_web_requires_database_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("CRAWLER_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="CRAWLER_DATABASE_URL"):
        Console(tmp_path)
    assert not (tmp_path / "data/offline.db").exists()


def test_retired_fingerprint_is_migrated_in_settings_and_account_pool(console):
    from social_crawler.environments.session import EnvironmentConfig

    legacy = {"douyin_fingerprint": "old-unused-value", "user_agent": "custom-browser",
              "impersonate": "chrome136"}
    console.settings["accounts"]["xhs"].update(legacy)
    console.path.write_text(json.dumps(console.settings))
    with console.store() as store:
        account = store.get_account("legacy-xhs")
        source = account["environment"] | legacy
        store.upsert_account(account["id"], account["platform"], account["name"], source)
    restored = Console(console.root)
    try:
        saved = json.loads(restored.path.read_text())["accounts"]["xhs"]
        assert "douyin_fingerprint" not in saved
        with restored.store() as store:
            migrated = store.get_account("legacy-xhs")
        assert migrated["environment"] == {k: v for k, v in source.items()
                                           if k != "douyin_fingerprint"}
        assert migrated["status"] == account["status"]
        assert saved["user_agent"] == "custom-browser" and saved["impersonate"] == "chrome136"
        EnvironmentConfig.model_validate(migrated["environment"])
        assert len(restored.public_settings(include_credentials=False)["account_pool"]) == 3
        restored.migrate_account_browser_identities()
        with restored.store() as store:
            assert store.get_account("legacy-xhs")["updated_at"] == migrated["updated_at"]
    finally:
        restored.worker_pool.shutdown(wait_seconds=10)


def test_database_startup_error_is_actionable_and_does_not_leak_credentials(monkeypatch, capsys):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sqlalchemy.exc import OperationalError

    from social_crawler.interfaces import web

    app = SimpleNamespace(settings={"database_url": "postgresql://test:private-secret@localhost/db"},
                          worker_pool=Mock())
    monkeypatch.setattr(web, "Console", lambda *args, **kwargs: app)

    def fail(url):
        raise OperationalError("startup", {}, Exception("private-secret"))

    monkeypatch.setattr(web, "upgrade_database", fail)
    with pytest.raises(SystemExit) as error:
        web.main([])
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert "CRAWLER_DATABASE_URL" in output
    assert "变量留空" in output
    assert "private-secret" not in output
    app.worker_pool.shutdown.assert_called_once()
