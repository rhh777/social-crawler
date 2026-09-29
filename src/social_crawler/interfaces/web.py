"""Local Web console. Reuses the same adapters, worker and database as the CLI."""

import argparse
import asyncio
import copy
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import signal
import tempfile
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as daytime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from social_crawler.adapters.douyin.http import DouyinHTTP
from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.browser import XHSBrowser
from social_crawler.adapters.xhs.http import XHSHTTP
from social_crawler.adapters.xhs.sites import is_xhs_api_adapter, is_xhs_platform
from social_crawler.analysis.data import AnalysisError
from social_crawler.analysis.service import AnalysisService
from social_crawler.domain.errors import check_response
from social_crawler.domain.models import CollectionError, RunConfig, TaskRequest
from social_crawler.domain.redaction import redact
from social_crawler.environments.account_browser import AccountBrowser, CollectionBrowserViewer
from social_crawler.environments.adspower import AdsPowerClient
from social_crawler.environments.browser_runtime import CURL_CFFI_IMPERSONATE
from social_crawler.environments.cookie_capture import (
    PLATFORMS,
    capture_platform_cookies,
    write_cookie_file,
)
from social_crawler.environments.environment_snapshot import load_environment_snapshot
from social_crawler.environments.kameleo import KameleoClient
from social_crawler.environments.managed_browser import (
    is_managed_browser_provider,
    managed_browser_profile_fingerprint,
    managed_browser_resource_key,
    managed_browser_supports_platform,
    managed_browser_transport,
)
from social_crawler.environments.network import observe_dual_exit
from social_crawler.environments.session import (
    EnvironmentConfig,
    Session,
    exclusive,
    load_cookies,
    load_proxy,
    load_storage_origins,
    validate_proxy_url,
)
from social_crawler.environments.session_recovery import (
    file_digest,
    identity_from_payload,
    read_recovery_receipt,
    recovery_key,
)
from social_crawler.interfaces.browser_gateway import proxy_browser
from social_crawler.interfaces.cli import versions
from social_crawler.orchestration.budget import RequestBudget
from social_crawler.orchestration.pool import WorkerPool
from social_crawler.orchestration.report import export_run
from social_crawler.orchestration.scheduler import Scheduler
from social_crawler.orchestration.worker import run_worker
from social_crawler.storage.migrations import upgrade_database
from social_crawler.storage.store import (
    Store,
    accounts,
    comments,
    content_times,
    contents,
    events,
    hits,
    run_contexts,
    runs,
    schedules,
    task_items,
    tasks,
)

STATIC = Path(__file__).with_name("web_static")
UI_LABELS = json.loads((STATIC / "zh-CN.json").read_text())
logger = logging.getLogger(__name__)

ACCOUNT_MAINTENANCE_KINDS = {"account_verify", "account_repair", "account_check"}
RUN_LOCAL_STOP_REASONS = {
    "canceled",
    "deadline",
    "internal_error",
    "interrupted",
    "request_budget",
    "stale_worker",
}


class ConsoleError(ValueError):
    """A deliberately user-facing validation error, containing no driver output."""


def web_store(url):
    args = (
        {
            "connect_timeout": 5,
            "options": make_url(url).query.get("options", "") + " -c statement_timeout=15000",
        }
        if make_url(url).get_backend_name() == "postgresql"
        else {}
    )
    return Store(url, connect_args=args)


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def error_message(exc):
    if isinstance(exc, (ConsoleError, AnalysisError)):
        return str(exc)
    if isinstance(exc, ValidationError):
        return "配置有误：" + "；".join(
            " / ".join(
                f"第 {part + 1} 项" if isinstance(part, int)
                else UI_LABELS["fields"].get(part, "配置项")
                for part in e["loc"]
            ) + "：" + UI_LABELS["validation"].get(e["type"], "内容无效")
            for e in exc.errors(include_input=False, include_context=False)
        )
    if isinstance(exc, SQLAlchemyError):
        return "数据库连接或查询失败，请在账号与连接中检查地址、服务与连接测试。"
    if isinstance(exc, CollectionError):
        if (
            exc.kind == "verification_required"
            and exc.message == "Douyin returned a slider challenge before WebSign initialization"
        ):
            return "采集链路检测：抖音返回滑块验证，当前浏览器安全会话不可用"
        return "采集链路检测：" + UI_LABELS["reasons"].get(exc.kind, "未识别的采集异常")
    if isinstance(exc, ValueError) and str(exc) in UI_LABELS["errors"]:
        return UI_LABELS["errors"][str(exc)]
    # Transport and database exception text can contain credentials.
    return {
        FileNotFoundError: "配置文件不存在，请检查路径。",
        TimeoutError: "检测超时，请检查代理、登录状态或稍后重试。",
    }.get(type(exc), "操作失败，请检查配置与连接；详细原因请查看服务日志。")


def validate_database(value):
    url = make_url(value)
    if url.drivername not in {"sqlite", "postgresql", "postgresql+psycopg"}:
        raise ConsoleError("仅支持 PostgreSQL 或 SQLite")
    return value


def validated_proxy(value):
    try:
        return validate_proxy_url(value)
    except ValueError as exc:
        raise ConsoleError("代理地址格式错误，请使用标准代理地址，且不要包含转义或 HTML 字符") from exc


def param(query, key, default=""):
    return query.get(key, [default])[0]


def date_window(query):
    """Calendar-day boundaries, including the entire end date, in the displayed zone."""
    try:
        zone = ZoneInfo("Asia/Shanghai")
        start = date.fromisoformat(param(query, "date_from")) if param(query, "date_from") else None
        end = date.fromisoformat(param(query, "date_to")) if param(query, "date_to") else None
        if start and end and start > end:
            raise ValueError
        return (
            datetime.combine(start, daytime.min, zone).timestamp() if start else None,
            datetime.combine(end + timedelta(days=1), daytime.min, zone).timestamp()
            if end
            else None,
        )
    except (ValueError, OverflowError):
        raise ConsoleError("日期范围无效，请检查起止日期") from None


def in_window(column, window):
    start, end = window
    return ([column >= start] if start is not None else []) + (
        [column < end] if end is not None else []
    )


def contains_pattern(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def proxy_diagnostic(proxy, platform, impersonate):
    from curl_cffi.requests import Session as HTTPSession

    if not proxy:
        return {"message": "当前使用直连，未配置代理", "state": "direct"}
    started = time.monotonic()
    result = {"state": "unavailable", "geo_state": "unknown", "geo_source": "IPWho.is"}
    # Both requests traverse the configured proxy. Never use the machine's direct
    # IP as a fallback, and never send platform cookies to the geo service.
    with HTTPSession(trust_env=False) as http:
        try:
            response = http.get(
                PLATFORMS[platform].url,
                proxy=proxy,
                timeout=15,
                impersonate=impersonate,
                allow_redirects=False,
            )
            result.update(
                state="reachable" if response.status_code < 400 else "restricted",
                message="代理可连接平台"
                if response.status_code < 400
                else f"平台返回 HTTP {response.status_code}",
                latency_ms=round((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            detail = str(exc).lower()
            if "rejected by the socks5 server" in detail or "authentication" in detail:
                result["message"] = "SOCKS5 代理拒绝了请求，请检查认证、线路或目标站点限制"
                result["failure_kind"] = "proxy_rejected"
            elif "timed out" in detail or "timeout" in detail:
                result["message"] = "通过代理连接目标平台超时"
                result["failure_kind"] = "target_timeout"
            elif "resolve" in detail:
                result["message"] = "代理无法解析目标平台域名"
                result["failure_kind"] = "dns_failure"
            else:
                result["message"] = "代理暂时无法连接目标平台"
                result["failure_kind"] = "connection_failure"
        try:
            response = http.get(
                "https://ipwho.is/",
                proxy=proxy,
                timeout=15,
                impersonate=impersonate,
                params={
                    "fields": "success,ip,country,country_code,region,city,latitude,longitude,postal,connection.isp,timezone.id"
                },
            )
            data = response.json()
            if response.status_code != 200 or data.get("success") is not True:
                raise ValueError
            address = str(ipaddress.ip_address(data["ip"]))
            result.update(
                geo_state="located",
                ip=address,
                location={
                    key: data.get(key)
                    for key in (
                        "country",
                        "country_code",
                        "region",
                        "city",
                        "latitude",
                        "longitude",
                        "postal",
                    )
                },
                isp=(data.get("connection") or {}).get("isp"),
                timezone=(data.get("timezone") or {}).get("id"),
            )
        except Exception as exc:
            detail = str(exc).lower()
            result["geo_message"] = (
                "代理服务器拒绝了出口 IP 查询请求"
                if "rejected by the socks5 server" in detail or "authentication" in detail
                else "出口归属地查询暂不可用，请稍后重试"
            )
    return result


class Console:
    def __init__(self, root, *, worker_count=1):
        self.root = Path(root).resolve()
        artifacts = Path(os.environ.get("CRAWLER_ARTIFACTS_DIR", "artifacts"))
        self.artifact_root = (
            artifacts.resolve() if artifacts.is_absolute() else (self.root / artifacts).resolve()
        )
        self.directory = self.root / "data/web"
        self.path = self.directory / "settings.json"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.checks_path = self.directory / "checks.json"
        try:
            self.jobs = {j["id"]: j for j in json.loads(self.checks_path.read_text())}
        except (OSError, ValueError):
            self.jobs = {}
        # Resource keys stay internal because profile paths and proxy endpoints
        # must not be exposed through the jobs API.
        self.job_resources: dict[str, frozenset[str]] = {}
        self.account_browsers = {}
        self.collection_browsers = {}
        self.worker_pool = WorkerPool(worker_count)
        self.worker_count = worker_count
        self.token = secrets.token_urlsafe(32)
        if self.path.exists():
            self.settings = json.loads(self.path.read_text())
            changed = False
            default_accounts = self.defaults()["accounts"]
            configured_accounts = self.settings.setdefault("accounts", {})
            for platform, environment in default_accounts.items():
                if platform not in configured_accounts:
                    configured_accounts[platform] = environment
                    changed = True
            for environment in configured_accounts.values():
                if self._migrate_environment_config(environment):
                    changed = True
            if changed:
                private_write(self.path, json.dumps(self.settings, ensure_ascii=False, indent=2))
        else:
            self.settings = self.defaults()
        # Deployment and launcher configuration must win over a connection URL
        # persisted by an earlier process (for example, a local pgserver port).
        if database_url := os.environ.get("CRAWLER_DATABASE_URL"):
            self.settings["database_url"] = database_url
        self.threads = []
        self.scheduler_stop = threading.Event()
        self.scheduler_thread = None
        if not self.settings.get("database_url"):
            raise ValueError("Configure CRAWLER_DATABASE_URL or start scripts/start_local.sh first")
        self.sync_legacy_accounts()
        self.migrate_account_browser_identities()
        try:
            self.migrate_account_proxies()
        except Exception as exc:
            logger.warning("account_proxy_migration_failed type=%s", type(exc).__name__)
        self.analysis = AnalysisService(
            self.store,
            self.directory / "agent",
            self.lock,
            workspace=self.root,
            artifact_root=self.artifact_root,
        )

    @staticmethod
    def _migrate_environment_config(environment):
        """Remove retired fields explicitly, retaining strict validation for typos."""
        changed = "douyin_fingerprint" in environment
        environment.pop("douyin_fingerprint", None)
        return Console._migrate_browser_identity(environment) or changed

    @staticmethod
    def _migrate_browser_identity(environment):
        """Lock generated/floating legacy presets without overriding custom pairs."""

        if environment.get("user_agent") or environment.get("impersonate") not in {
            "chrome",
            "chrome136",
        }:
            return False
        environment["impersonate"] = CURL_CFFI_IMPERSONATE
        binding = str(environment.get("binding_version") or "1")
        suffix = f"-{CURL_CFFI_IMPERSONATE}"
        if not binding.endswith(suffix):
            environment["binding_version"] = binding + suffix
        return True

    def defaults(self):
        db_file = self.root / "data/postgres.url"
        database = os.environ.get("CRAWLER_DATABASE_URL") or (
            db_file.read_text().strip()
            if db_file.is_file()
            else ""
        )
        accounts = {}
        default_headless = os.environ.get("CRAWLER_DEFAULT_HEADLESS")
        if default_headless is not None and default_headless.lower() not in {
            "1",
            "true",
            "yes",
            "0",
            "false",
            "no",
        }:
            raise ValueError("CRAWLER_DEFAULT_HEADLESS must be true or false")
        for platform in PLATFORMS:
            config = self.root / f"configs/probe-{platform}.toml"
            env = (
                tomllib.loads(config.read_text()).get("environment", {}) if config.exists() else {}
            )
            env.setdefault("account_ref", platform + "-primary")
            env.setdefault("profile_dir", f"profiles/{platform}-primary")
            env["cookie_file"] = env.get("cookie_file") or f"data/cookies/{platform}.json"
            if default_headless is not None:
                env["headless"] = default_headless.lower() in {"1", "true", "yes"}
            accounts[platform] = EnvironmentConfig.model_validate(env).model_dump()
        return {"database_url": database, "accounts": accounts}

    def resolve(self, path):
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.root / p

    def sync_legacy_accounts(self, *, force=False, only_platform=None):
        """Seed the account pool without invalidating existing local settings."""
        try:
            with self.store() as store:
                existing_rows = store.list_accounts(include_deleted=True)
                existing_ids = {row["id"] for row in existing_rows}
                deleted_ids = {row["id"] for row in existing_rows if row["status"] == "deleted"}
                for platform, environment in self.settings["accounts"].items():
                    if only_platform and platform != only_platform:
                        continue
                    account_id = "legacy-" + platform
                    if account_id in deleted_ids or (account_id in existing_ids and not force):
                        continue
                    status = (
                        "ready"
                        if self.cookie_check(platform)["cookie_state"] == "configured"
                        else "login_required"
                    )
                    store.upsert_account(
                        account_id,
                        platform,
                        environment.get("account_ref") or platform + "-primary",
                        environment,
                        status=status,
                    )
        except Exception as exc:
            logger.warning("account_sync_failed type=%s", type(exc).__name__)

    def migrate_account_browser_identities(self):
        """Apply the same legacy environment migrations to pooled accounts."""

        try:
            with self.store() as store:
                for account in store.list_accounts():
                    environment = copy.deepcopy(account["environment"])
                    if self._migrate_environment_config(environment):
                        store.upsert_account(
                            account["id"],
                            account["platform"],
                            account["name"],
                            environment,
                        )
        except Exception as exc:
            logger.warning("account_browser_identity_migration_failed type=%s", type(exc).__name__)

    def migrate_account_proxies(self):
        """Register existing sources without rewriting credentials or changing egress."""
        with self.lock, self.store() as store:
            known = {(p["proxy_file"], p["proxy_env"]): p for p in store.list_proxies()}
            for account in store.list_accounts():
                source = copy.deepcopy(account["environment"])
                if source.get("proxy_id"):
                    continue
                path = source.get("proxy_file")
                key = (str(self.resolve(path).resolve()) if path else None, source.get("proxy_env"))
                if not any(key):
                    continue
                proxy = known.get(key)
                if proxy is None:
                    proxy = store.upsert_proxy(uuid.uuid4().hex, account["name"] + "的代理",
                                               proxy_file=key[0], proxy_env=key[1])
                    known[key] = proxy
                source.update(proxy_id=proxy["id"], proxy_file=key[0], proxy_env=key[1])
                store.upsert_account(account["id"], account["platform"], account["name"], source)

    def proxy_url(self, proxy):
        path = proxy.get("proxy_file")
        return load_proxy(proxy_file=str(self.resolve(path)) if path else None,
                          proxy_env=proxy.get("proxy_env"),
                          apply_relay=False)

    def public_proxy(self, proxy, assigned, *, include_credentials=False):
        try:
            value = self.proxy_url(proxy)
        except (OSError, ValueError):
            value = ""
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        endpoint = f"{host}:{parsed.port}" if parsed.port else host
        result = {"id": proxy["id"], "name": proxy["name"], "scheme": parsed.scheme,
                  "endpoint": endpoint, "configured": bool(value),
                  "authenticated": bool(parsed.username), "accounts": assigned}
        if include_credentials:
            result["proxy_url"] = value
        return result

    def list_proxies(self):
        with self.store() as store:
            accounts = store.list_accounts()
            return [self.public_proxy(proxy, [
                {"id": a["id"], "name": a["name"], "platform": a["platform"]}
                for a in accounts if a["environment"].get("proxy_id") == proxy["id"]
            ]) for proxy in store.list_proxies()]

    def proxy_detail(self, proxy_id):
        with self.store() as store:
            return self.public_proxy(store.get_proxy(proxy_id), [], include_credentials=True)

    def save_proxy(self, body, proxy_id=None):
        with self.lock, self.store() as store:
            existing = store.get_proxy(proxy_id) if proxy_id else None
            name = " ".join(str(body.get("name", existing["name"] if existing else "")).split())[:80]
            if not name:
                raise ConsoleError("代理名称不能为空")
            value = body.get("proxy_url")
            if value is not None or not existing:
                value = str(value or "").strip()
                if not value:
                    raise ConsoleError("请填写代理地址")
                value = validated_proxy(value)
                if not urlsplit(value).port:
                    raise ConsoleError("代理端口必须在 1 到 65535 之间")
            assigned = [a for a in store.list_accounts()
                        if a["environment"].get("proxy_id") == proxy_id] if existing else []
            if value is not None and existing:
                for account in assigned:
                    self.require_resources_idle(self.account_resource_keys(
                        account["platform"], account["id"]))
                if existing["proxy_file"]:
                    self.require_resources_idle(["proxy_file:" + str(
                        self.resolve(existing["proxy_file"]).resolve())])
            proxy_id = proxy_id or uuid.uuid4().hex
            path = existing["proxy_file"] if existing else None
            proxy_env = existing["proxy_env"] if existing else None
            if value is not None:
                # Publish a new file before committing bindings together. A failed
                # database write cannot alter any account's existing connection.
                path = str(self.directory / "proxies" / f"{proxy_id}-{uuid.uuid4().hex}.proxy")
                proxy_env = None
                private_write(Path(path), value)
            proxy = store.upsert_proxy(proxy_id, name, proxy_file=path, proxy_env=proxy_env,
                                       sync_accounts=value is not None)
            if value is not None and existing:
                for account in assigned:
                    store.clear_account_network_binding(account["id"])
            return self.public_proxy(proxy, [], include_credentials=True)

    def delete_proxy(self, proxy_id):
        with self.lock, self.store() as store:
            store.get_proxy(proxy_id)
            if any(a["environment"].get("proxy_id") == proxy_id for a in store.list_accounts()):
                raise ConsoleError("代理仍被账号使用，请先为关联账号更换代理或选择本机直连")
            store.delete_proxy(proxy_id)
        return {"message": "代理已删除"}

    def bind_account_proxy(self, source, proxy_id):
        if not proxy_id:
            source.update(proxy_id=None, proxy_file=None, proxy_env=None)
            return
        with self.store() as store:
            try:
                proxy = store.get_proxy(proxy_id)
            except ValueError as exc:
                raise ConsoleError("代理不存在，请刷新列表后重新选择") from exc
        source.update({key: proxy[key] for key in ("proxy_file", "proxy_env")}, proxy_id=proxy_id)

    def environment(self, platform, account_id=None):
        if platform not in PLATFORMS:
            raise ConsoleError("未知平台")
        if account_id:
            with self.store() as store:
                account = store.get_account(account_id)
            return self.account_environment(account, platform)
        else:
            source = self.settings["accounts"][platform]
        return self.resolve_environment(source)

    def account_environment(self, account, platform):
        if account["status"] == "deleted":
            raise ConsoleError("账号已删除")
        if account["platform"] != platform:
            raise ConsoleError("账号与采集平台不匹配")
        return self.resolve_environment(account["environment"])

    def resolve_environment(self, source):
        env = EnvironmentConfig.model_validate(copy.deepcopy(source))
        for key in ("cookie_file", "profile_dir", "proxy_file"):
            if getattr(env, key):
                setattr(env, key, str(self.resolve(getattr(env, key))))
        return env

    @staticmethod
    def run_environment(env, config):
        """Apply execution-only overrides without changing the saved account default."""
        if config.headless is None:
            return env
        return env.model_copy(update={"headless": config.headless})

    def adopt_identity_nickname(self, platform, original_ref, nickname):
        """Replace only a generated account alias; never overwrite a user's custom alias."""
        nickname = " ".join(str(nickname or "").split())[:80]
        if not nickname:
            return original_ref, False
        with self.lock:
            account = self.settings["accounts"][platform]
            current = str(account.get("account_ref", ""))
            generated = {"primary", f"{platform}-primary"}
            if current != original_ref or current not in generated or current == nickname:
                return current, False
            account["account_ref"] = nickname
            private_write(self.path, json.dumps(self.settings, ensure_ascii=False, indent=2))
            return nickname, True

    @contextmanager
    def store(self):
        store = web_store(self.settings["database_url"])
        try:
            store.initialize()
            yield store
        finally:
            store.close()

    def public_settings(self, *, include_credentials=True):
        accounts = {}
        for platform, data in self.settings["accounts"].items():
            env = self.environment(platform)
            try:
                cookie_text = Path(env.cookie_file).read_text().strip() if include_credentials else ""
            except OSError:
                cookie_text = ""
            try:
                proxy_url = load_proxy(
                    proxy_env=env.proxy_env,
                    proxy_file=env.proxy_file,
                    apply_relay=False,
                )
            except ValueError:
                proxy_url = ""
            accounts[platform] = (
                copy.deepcopy(data)
                | self.cookie_check(platform, env=env)
                | {"proxy_url": proxy_url}
                | ({"cookie_text": cookie_text} if include_credentials else {})
            )
        try:
            with self.store() as store:
                account_pool = [
                    self.public_account(account, include_credentials=include_credentials)
                    for account in store.list_accounts()
                ]
                schedule_rows = redact(Scheduler(store).list())
                automatic_cooldown = store.automatic_cooldown_enabled()
            proxy_rows = self.list_proxies()
        except Exception:
            account_pool, schedule_rows = [], []
            proxy_rows = []
            automatic_cooldown = False
        return {
            "database": make_url(self.settings["database_url"])
            .set(query={})
            .render_as_string(hide_password=True),
            "accounts": accounts,
            "checks": self.diagnostic_jobs(),
            "runtime": {
                "workers": self.worker_count,
            },
            "account_pool": account_pool,
            "proxies": proxy_rows,
            "schedules": schedule_rows,
            "automatic_cooldown": automatic_cooldown,
            "ui_labels": UI_LABELS,
        }

    def public_account(self, account, *, include_credentials=True):
        """Return the complete local account configuration used by the UI."""
        env = self.account_environment(account, account["platform"])
        try:
            cookie_text = (
                Path(env.cookie_file).read_text().strip()
                if include_credentials
                and not is_managed_browser_provider(env.browser_provider)
                else ""
            )
        except OSError:
            cookie_text = ""
        try:
            proxy_url = load_proxy(
                proxy_env=env.proxy_env,
                proxy_file=env.proxy_file,
                apply_relay=False,
            )
        except ValueError:
            proxy_url = ""
        public = {
            key: account[key]
            for key in (
                "id",
                "platform",
                "name",
                "status",
                "cooldown_until",
                "last_success_at",
                "last_failure_at",
                "last_failure_kind",
            )
        }
        # Older runs could write task-local termination reasons into the account
        # history. Do not present those as the cause of account cooling.
        if public["last_failure_kind"] in RUN_LOCAL_STOP_REASONS:
            public["last_failure_at"] = None
            public["last_failure_kind"] = None
        return public | env.model_dump() | self.cookie_check(account["platform"], env=env) | {
            "proxy_url": proxy_url,
            "collection_viewer": bool(self.collection_browser_for_account(account["id"])),
        } | ({"cookie_text": cookie_text} if include_credentials else {})

    def account_detail(self, account_id):
        with self.store() as store:
            account = store.get_account(account_id)
        return self.public_account(account)

    def browser_profiles(self, provider, *, platform="xhs", account_id=None):
        """Discover safe profile metadata and annotate existing account bindings."""
        if provider not in {"adspower", "kameleo"}:
            raise ConsoleError("未知浏览器环境提供方")
        if account_id:
            with self.store() as store:
                account = store.get_account(account_id)
            platform = account["platform"]
            env = self.account_environment(account, platform)
        else:
            env = self.environment(platform)
        if not managed_browser_supports_platform(provider, platform):
            raise ConsoleError("该浏览器环境不支持当前平台")

        async def discover():
            if provider == "adspower":
                client = AdsPowerClient(
                    env.adspower_api_url,
                    timeout=min(env.adspower_start_timeout, 15),
                )
            else:
                client = KameleoClient(
                    env.kameleo_api_url,
                    timeout=min(env.kameleo_start_timeout, 15),
                )
            try:
                return await client.list_profiles()
            finally:
                await client.close()

        try:
            profiles = asyncio.run(discover())
        except CollectionError as exc:
            display = "AdsPower" if provider == "adspower" else "Kameleo"
            raise ConsoleError(f"{display} 运行服务不可用，无法读取环境列表") from exc

        bindings = {}
        with self.store() as store:
            for row in store.list_accounts():
                try:
                    other = EnvironmentConfig.model_validate(row["environment"])
                except ValidationError:
                    continue
                if other.browser_provider != provider:
                    continue
                profile_id = (
                    other.adspower_profile_id
                    if provider == "adspower"
                    else other.kameleo_profile_id
                )
                if profile_id:
                    bindings[profile_id] = {
                        "account_id": row["id"],
                        "account_name": row["name"],
                    }

        items = []
        for profile in profiles:
            binding = bindings.get(profile["id"])
            bound_elsewhere = bool(binding and binding["account_id"] != account_id)
            items.append(
                profile
                | {
                    "available": bool(profile.get("compatible", True))
                    and not bound_elsewhere,
                    "bound_account_id": binding["account_id"] if binding else None,
                    "bound_account_name": binding["account_name"] if binding else None,
                }
            )
        items.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return {"provider": provider, "items": items}

    def add_account(self, body):
        with self.lock:
            return self._add_account(body)

    def _add_account(self, body):
        platform = body.get("platform")
        if platform not in PLATFORMS:
            raise ConsoleError("未知平台")
        name = " ".join(str(body.get("name") or "").split())[:80]
        if not name:
            raise ConsoleError("账号名称不能为空")
        account_id = uuid.uuid4().hex
        requested_environment = body.get("environment") or {}
        source = copy.deepcopy(self.settings["accounts"][platform])
        source.update(requested_environment)
        source.update(
            account_ref=name,
            profile_dir=requested_environment.get("profile_dir")
            or f"profiles/{platform}-{account_id[:8]}",
            cookie_file=str(self.directory / "accounts" / f"{account_id}.cookies.json"),
            proxy_file=None,
            proxy_env=None,
            proxy_id=None,
        )
        requested_provider = requested_environment.get("browser_provider", "chromium")
        required_profile_id = {
            "adspower": requested_environment.get("adspower_profile_id"),
            "kameleo": requested_environment.get("kameleo_profile_id"),
        }.get(requested_provider)
        if is_managed_browser_provider(requested_provider) and not required_profile_id:
            source.update(
                browser_provider="chromium",
                adspower_profile_id=None,
                kameleo_profile_id=None,
            )
        cookie_text = str(body.get("cookie_text") or "").strip()
        if cookie_text:
            temporary = self.directory / "accounts" / f".{account_id}.validate"
            private_write(temporary, cookie_text)
            try:
                cookies = load_cookies(temporary, platform)
                origins = load_storage_origins(temporary, platform)
                if not any(
                    cookie["name"] in PLATFORMS[platform].required_cookies
                    and cookie["value"]
                    for cookie in cookies
                ):
                    raise ConsoleError("登录凭据缺少未过期的会话字段")
            finally:
                temporary.unlink(missing_ok=True)
            try:
                raw = json.loads(cookie_text)
            except json.JSONDecodeError:
                raw = None
            persisted = (
                {"cookies": cookies, "origins": origins}
                if platform == "douyin" and isinstance(raw, dict) and "origins" in raw
                else cookies
            )
            private_write(
                Path(source["cookie_file"]), json.dumps(persisted, ensure_ascii=False)
            )
        else:
            # The account can be created first and authenticated through the
            # interactive browser flow from its detail dialog.
            private_write(Path(source["cookie_file"]), "[]")
        proxy_url = str(body.get("proxy_url") or "").strip()
        if "proxy_id" in body:
            self.bind_account_proxy(source, body["proxy_id"])
        elif proxy_url:
            proxy_url = validated_proxy(proxy_url)
            source["proxy_file"] = str(
                self.directory / "accounts" / f"{account_id}.proxy"
            )
            private_write(Path(source["proxy_file"]), proxy_url)
        env = EnvironmentConfig.model_validate(source)
        if is_managed_browser_provider(
            env.browser_provider
        ) and not managed_browser_supports_platform(env.browser_provider, platform):
            provider = "AdsPower" if env.browser_provider == "adspower" else "Kameleo"
            message = (
                "Kameleo 浏览器目前仅支持小红书账号"
                if env.browser_provider == "kameleo"
                else f"{provider} 浏览器暂不支持该平台"
            )
            raise ConsoleError(message)
        with self.store() as store:
            existing_resources = {
                self.browser_resource_key(
                    EnvironmentConfig.model_validate(row["environment"])
                )
                for row in store.list_accounts()
            }
            if self.browser_resource_key(env) in existing_resources:
                message = (
                    f"不同账号不能共用 {'AdsPower' if env.browser_provider == 'adspower' else 'Kameleo'} 环境"
                    if is_managed_browser_provider(env.browser_provider)
                    else "不同账号不能共用浏览器数据目录"
                )
                raise ConsoleError(message)
            if not env.proxy_id and env.proxy_file:
                proxy = store.upsert_proxy(uuid.uuid4().hex, name + "的代理",
                                           proxy_file=env.proxy_file)
                env.proxy_id = proxy["id"]
            account = store.upsert_account(
                account_id,
                platform,
                name,
                env.model_dump(),
                status=(
                    "ready"
                    if cookie_text or is_managed_browser_provider(env.browser_provider)
                    else "login_required"
                ),
            )
        return {
            key: account[key]
            for key in (
                "id",
                "platform",
                "name",
                "status",
                "cooldown_until",
            )
        }

    def set_account_state(self, account_id, body):
        with self.lock:
            with self.store() as store:
                account = store.get_account(account_id)
                keys = self.account_resource_keys(account["platform"], account_id)
                if body.get("status") != "disabled":
                    self.require_resources_idle(keys)
                store.set_account_status(
                    account_id,
                    body.get("status"),
                    cooldown_until=body.get("cooldown_until"),
                )
                return store.get_account(account_id)

    def open_account_browser(self, account_id, *, auto_save=False):
        with self.lock:
            existing = self.account_browsers.get(account_id)
            if existing and existing.active:
                existing.last_activity = time.monotonic()
                if auto_save:
                    existing.enable_auto_save()
                return existing.public()
            viewer = self.collection_browser_for_account(account_id)
            if viewer:
                if auto_save:
                    raise ConsoleError("该账号正在采集；请停止任务后再重新登录")
                return viewer.public()
            with self.store() as store:
                account = store.get_account(account_id)
            if account["status"] == "deleted":
                raise ConsoleError("账号已删除")
            env = self.environment(account["platform"], account_id)
            # Browsing owns this profile/session, not every account on the same proxy.
            keys = [key for key in self.account_resource_keys(account["platform"], account_id)
                    if not key.startswith(("proxy:", "proxy_file:"))]
            self.require_resources_idle(keys)
            def saved():
                with self.store() as store:
                    current = store.get_account(account_id)
                    if current['status'] != 'disabled':
                        store.set_account_status(account_id, 'ready')

            session = AccountBrowser(
                account, env, keys, on_saved=saved, auto_save=auto_save
            )
            self.account_browsers[account_id] = session
            session.start()
            return session.public()

    def account_browser_command(self, account_id, body):
        with self.lock:
            session = self.account_browsers.get(account_id)
            if not session or body.get("session_id") != session.id:
                session = next(
                    (
                        viewer
                        for viewer in self.collection_browsers.values()
                        if viewer.account_id == account_id
                        and body.get("session_id") == viewer.id
                    ),
                    None,
                )
            if not session or body.get("session_id") != session.id:
                raise ConsoleError("浏览器窗口已关闭，请从账号列表重新打开")
        if body.get("kind") == "close":
            if getattr(session, "read_only", False):
                raise ConsoleError("采集浏览器仅供观看；请在任务详情中停止采集")
            return session.close()
        return session.request(body)

    def collection_browser_for_account(self, account_id):
        return next(
            (
                viewer
                for viewer in self.collection_browsers.values()
                if viewer.account_id == account_id and viewer.active
            ),
            None,
        )

    def browser_session(self, session_id):
        return next(
            (
                session
                for session in (
                    *tuple(self.account_browsers.values()),
                    *tuple(self.collection_browsers.values()),
                )
                if session.id == session_id
            ),
            None,
        )

    def close_account_browsers(self):
        for session in list(self.account_browsers.values()):
            session.stopped.set()
        for session in list(self.account_browsers.values()):
            session.thread.join(timeout=5)
        for viewer in list(self.collection_browsers.values()):
            viewer.close()

    def copy_account(self, account_id):
        """Copy reusable configuration into independent, unauthenticated resources."""
        with self.lock:
            with self.store() as store:
                source = store.get_account(account_id)
                names = {row["name"] for row in store.list_accounts(include_deleted=True)}
            env = self.environment(source["platform"], account_id)
            name = source["name"][:60] + " 副本"
            suffix = 2
            while name in names:
                name = source["name"][:60] + f" 副本 {suffix}"
                suffix += 1
            environment = env.model_dump(exclude={
                "account_ref", "profile_dir", "cookie_file", "proxy_file", "proxy_env",
                "adspower_profile_id", "kameleo_profile_id", "proxy_id",
            })
            environment.update(
                expected_user_id=None,
                douyin_webid="",
                browser_provider="chromium",
            )
            proxy_choice = (
                {"proxy_id": env.proxy_id} if env.proxy_id else
                {"proxy_url": load_proxy(proxy_env=env.proxy_env, proxy_file=env.proxy_file,
                                         apply_relay=False)}
            )
            return self.add_account({
                "platform": source["platform"], "name": name, "environment": environment,
                **proxy_choice,
            })

    def delete_account(self, account_id):
        with self.lock:
            with self.store() as store:
                account = store.get_account(account_id)
                self.require_resources_idle(self.account_resource_keys(account["platform"], account_id))
                # Pending persisted runs may not yet have an in-process job.
                with store.engine.connect() as conn:
                    active = conn.execute(select(runs.c.id).join(run_contexts).where(
                        run_contexts.c.account_id == account_id,
                        runs.c.status.in_(["pending", "running"]),
                    )).first()
                if active:
                    raise ConsoleError("该账号还有未结束的任务，请先完成或停止任务")
                store.delete_account(account_id)
            return {"message": "账号已从账号池删除；历史记录和本地会话文件保留"}

    def update_account(self, account_id, body):
        """Update one pooled account without changing any other account."""
        with self.lock:
            with self.store() as store:
                account = store.get_account(account_id)
                self.require_resources_idle(self.account_resource_keys(account["platform"], account_id))
                original = EnvironmentConfig.model_validate(account["environment"])
                environment = body.get("environment") or {}
                if "proxy_id" in environment:
                    raise ConsoleError("请通过账号的代理选项设置代理")
                env = EnvironmentConfig.model_validate(original.model_dump() | environment)
                if is_managed_browser_provider(
                    env.browser_provider
                ) and not managed_browser_supports_platform(
                    env.browser_provider, account["platform"]
                ):
                    provider = "AdsPower" if env.browser_provider == "adspower" else "Kameleo"
                    message = (
                        "Kameleo 浏览器目前仅支持小红书账号"
                        if env.browser_provider == "kameleo"
                        else f"{provider} 浏览器暂不支持该平台"
                    )
                    raise ConsoleError(message)
                writes = []
                # A new path must not point into another running account's files.
                self.require_resources_idle([
                    "account:" + account["platform"] + ":" + env.account_ref,
                    self.browser_resource_key(env),
                    "cookie:" + str(self.resolve(env.cookie_file).resolve()),
                ])
                name = " ".join(str(body.get("name") or account["name"]).split())[:80]
                if not name:
                    raise ConsoleError("账号名称不能为空")
                if "proxy_id" in body:
                    env.proxy_id = body["proxy_id"] or None
                    if not env.proxy_id:
                        env.proxy_file = env.proxy_env = None
                elif body.get("clear_proxy"):
                    env.proxy_id = None
                    env.proxy_file = env.proxy_env = None
                elif body.get("proxy_url") is not None:
                    env.proxy_id = None
                    value = str(body["proxy_url"] or "").strip()
                    if value:
                        value = validated_proxy(value)
                        env.proxy_env = None
                        env.proxy_file = str(
                            self.directory / "proxies" / f"{uuid.uuid4().hex}.proxy"
                        )
                        writes.append((self.resolve(env.proxy_file), value))
                    else:
                        env.proxy_file = env.proxy_env = None
                elif any(key in environment for key in ("proxy_file", "proxy_env")):
                    env.proxy_id = None
                if env.proxy_id:
                    source = env.model_dump()
                    self.bind_account_proxy(source, env.proxy_id)
                    env = EnvironmentConfig.model_validate(source)
                cookie_text = body.get("cookie_text")
                if cookie_text is not None and str(cookie_text).strip() != "":
                    value = str(cookie_text).strip()
                    temporary = self.directory / "accounts" / f".{account_id}.validate"
                    private_write(temporary, value)
                    try:
                        cookies = load_cookies(temporary, account["platform"])
                        origins = load_storage_origins(temporary, account["platform"])
                        if not any(
                            c["name"] in PLATFORMS[account["platform"]].required_cookies
                            and c["value"]
                            for c in cookies
                        ):
                            raise ConsoleError("登录凭据缺少未过期的会话字段")
                    finally:
                        temporary.unlink(missing_ok=True)
                    try:
                        raw = json.loads(value)
                    except json.JSONDecodeError:
                        raw = None
                    persisted = (
                        {"cookies": cookies, "origins": origins}
                        if account["platform"] == "douyin"
                        and isinstance(raw, dict)
                        and "origins" in raw
                        else cookies
                    )
                    writes.append((self.resolve(env.cookie_file), json.dumps(persisted, ensure_ascii=False)))
                if env.proxy_file and env.proxy_env:
                    raise ConsoleError("代理文件与环境变量只能配置其中一种")
                if not env.account_ref.strip() or not env.profile_dir.strip() or not env.cookie_file.strip():
                    raise ConsoleError("账号标识、浏览器目录和 登录凭据路径不能为空")
                if account["platform"] == "douyin" and env.expected_user_id:
                    raise ConsoleError("抖音暂不支持预期用户标识 核对，请留空")
                for row in store.list_accounts():
                    if row["id"] == account_id:
                        continue
                    other = EnvironmentConfig.model_validate(row["environment"])
                    if (
                        is_managed_browser_provider(env.browser_provider)
                        and other.browser_provider == env.browser_provider
                        and self.browser_resource_key(env) == self.browser_resource_key(other)
                    ):
                        provider = "AdsPower" if env.browser_provider == "adspower" else "Kameleo"
                        raise ConsoleError(f"不同账号不能共用 {provider} 环境")
                    if (
                        env.browser_provider == "chromium"
                        and other.browser_provider == "chromium"
                        and self.resolve(env.profile_dir).resolve()
                        == self.resolve(other.profile_dir).resolve()
                    ):
                        raise ConsoleError("不同账号不能共用浏览器数据目录")
                for path, value in writes:
                    self.require_resources_idle(["cookie:" + str(path.resolve()),
                                                 "proxy_file:" + str(path.resolve())])
                for path, value in writes:
                    private_write(path, value)
                if not env.proxy_id and (env.proxy_file or env.proxy_env):
                    proxy = store.upsert_proxy(uuid.uuid4().hex, name + "的代理",
                                               proxy_file=env.proxy_file, proxy_env=env.proxy_env)
                    env.proxy_id = proxy["id"]
                updated = store.upsert_account(
                    account_id, account["platform"], name, env.model_dump()
                )
                original_network = (
                    original.browser_provider,
                    original.adspower_profile_id,
                    original.kameleo_profile_id,
                    original.proxy_id,
                    original.proxy_file,
                    original.proxy_env,
                )
                updated_network = (
                    env.browser_provider,
                    env.adspower_profile_id,
                    env.kameleo_profile_id,
                    env.proxy_id,
                    env.proxy_file,
                    env.proxy_env,
                )
                if original_network != updated_network:
                    store.clear_account_network_binding(account_id)
                if body.get("status") is not None:
                    store.set_account_status(account_id, body["status"])
                    updated = store.get_account(account_id)
                return self.public_account(updated)

    def create_schedule(self, body):
        with self.store() as store:
            return redact(Scheduler(store).create(body))

    def update_schedule(self, schedule_id, body):
        with self.store() as store:
            return redact(Scheduler(store).update(schedule_id, body))

    def delete_schedule(self, schedule_id):
        with self.store() as store:
            Scheduler(store).delete(schedule_id)
        return {"message": "定时作业已删除"}

    def set_schedule_enabled(self, schedule_id, body):
        with self.store() as store:
            service = Scheduler(store)
            service.set_enabled(schedule_id, bool(body.get("enabled")))
            return redact(next(row for row in service.list() if row["id"] == schedule_id))

    def risk_report(self, query=None):
        query = query or {}
        days = max(1, min(int(param(query, "days", "7")), 90))
        with self.store() as store:
            since = time.time() - days * 86400
            if param(query, "section"):
                return store.risk_page(
                    param(query, "section"), since=since,
                    page=int(param(query, "page", "1")),
                    page_size=int(param(query, "page_size", "10")),
                    **{key: param(query, key, "").strip() for key in
                       ("platform", "account_id", "operation", "dimension", "q")},
                )
            return store.risk_summary(since=since)

    def create_quota_policy(self, body):
        operation = body.get("operation") or None
        with self.store() as store:
            return store.create_quota_policy(
                str(body.get("dimension") or ""),
                str(body.get("subject") or "").strip(),
                operation=operation,
                window_seconds=int(body.get("window_seconds") or 0),
                request_limit=int(body.get("request_limit") or 0),
                enabled=bool(body.get("enabled", True)),
            )

    @staticmethod
    def ensure_account_quota_coverage(store, account, ip_group):
        """Keep the maintenance endpoint aligned with automatic Web defaults."""
        return store.ensure_default_quota_coverage(
            account["id"], account["platform"], ip_group
        )

    @staticmethod
    def maintenance_result(account):
        return {
            "id": account["id"],
            "name": account["name"],
            "platform": account["platform"],
            "outcome": "failed",
            "cookie": "unknown",
            "proxy": "unknown",
            "platform_check": "not_run",
            "changes": [],
        }

    def set_maintenance_login_required(self, account_id, result, *, repair):
        if repair:
            with self.lock, self.store() as store:
                if store.get_account(account_id)["status"] == "disabled":
                    return
                store.set_account_status(account_id, "login_required")
            result["changes"].append("状态已改为需要登录")

    def require_maintenance_recovery(self, account_id, *, reason="auth_expired"):
        with self.lock, self.store() as store:
            if store.get_account(account_id)["status"] != "disabled":
                store.require_session_recovery(account_id, reason=reason)

    def maintain_account(self, account, *, repair):
        """Run full preflight and optionally resolve safe local blockers."""
        account_id = account["id"]
        platform = account["platform"]
        result = self.maintenance_result(account)
        env = self.environment(platform, account_id)
        cookie = self.cookie_check(platform, account_id, env=env)
        result["cookie"] = cookie.get("cookie_state", "missing")
        if result["cookie"] not in {"configured", "managed"}:
            self.set_maintenance_login_required(account_id, result, repair=repair)
            result.update(outcome="needs_login", message="缺少有效登录凭据，需要重新登录")
            return result

        try:
            session = Session(
                env,
                platform,
                profile_auth=is_managed_browser_provider(env.browser_provider),
            )
        except (OSError, ValueError):
            result.update(proxy="proxy_unavailable", message="账号连接配置无法加载，请检查代理和环境配置")
            return result

        with exclusive(self.keys(session)):
            observation = asyncio.run(observe_dual_exit(session))
            if observation["outcome"] != "success":
                result.update(
                    proxy=observation.get("error_kind") or "proxy_unavailable",
                    message=(
                        "托管浏览器出口检测失败"
                        if is_managed_browser_provider(env.browser_provider)
                        else "代理双出口检测失败"
                    ),
                )
                return result
            result["proxy"] = "verified"
            result["egress_ip"] = observation["egress_ip"]

            if repair:
                with self.store() as store:
                    store.confirm_account_network(
                        account_id,
                        proxy_ref=observation["proxy_ref"],
                        egress_ip=observation["egress_ip"],
                        ip_group=observation["ip_group"],
                    )
                    policies = self.ensure_account_quota_coverage(
                        store, account, observation["ip_group"]
                    )
                result["changes"].append("出口绑定已确认")
                if policies:
                    result["changes"].append(f"已补齐 {len(policies)} 条额度策略")

            try:
                probe = asyncio.run(self.probe(session))
            except CollectionError as exc:
                if exc.kind == "auth_expired":
                    self.set_maintenance_login_required(account_id, result, repair=repair)
                    if repair:
                        self.require_maintenance_recovery(account_id)
                    result.update(
                        outcome="needs_login",
                        platform_check="auth_expired",
                        message="代理正常，但平台登录已失效",
                    )
                    return result
                if repair and exc.kind in {
                    "identity_mismatch", "identity_unverified", "verification_required",
                    "access_denied", "rate_limit",
                }:
                    self.require_maintenance_recovery(account_id, reason=exc.kind)
                result.update(platform_check=exc.kind, message="平台链路检测失败")
                return result
            except Exception as exc:
                result.update(platform_check="internal_error", message=error_message(exc))
                return result

        result["platform_check"] = probe.get("login_state", "unverified")
        if result["platform_check"] != "verified":
            self.set_maintenance_login_required(account_id, result, repair=repair)
            if repair:
                self.require_maintenance_recovery(account_id, reason=(
                    "auth_expired" if result["platform_check"] == "expired"
                    else "identity_unverified"
                ))
            result.update(outcome="needs_login", message="平台可访问，但未能确认登录身份")
            return result
        if repair:
            with self.lock, self.store() as store:
                if store.get_account(account_id)["status"] != "disabled":
                    store.set_account_status(account_id, "ready")
                    result["changes"].append("账号已恢复为可运行")
        result.update(
            outcome="ready",
            message="检测通过并已修复配置" if repair else "检测通过",
            account_nickname=probe.get("account_nickname"),
        )
        return result

    def account_maintenance(self, body):
        action = str(body.get("action") or "")
        if action not in {"verify", "repair", "check"}:
            raise ConsoleError("未知账号维护操作")
        account_id = body.get("account_id")
        with self.lock:
            with self.store() as store:
                account_rows = store.list_accounts()
            if account_id:
                account_rows = [row for row in account_rows if row["id"] == account_id]
                if not account_rows:
                    raise ConsoleError("账号不存在")
            if not account_rows:
                raise ConsoleError("账号池为空")

            keys_by_account = {
                account["id"]: self.account_resource_keys(
                    account["platform"], account["id"]
                )
                for account in account_rows
            }
            busy_ids = {
                current_id
                for current_id, keys in keys_by_account.items()
                if self.resources_busy(keys)
            }
            if account_id and busy_ids:
                raise ConsoleError("该账号或浏览器环境正在执行任务或被查看，请关闭账号浏览器或结束该账号的任务后再操作")
            resource_keys = [
                key
                for account in account_rows
                if account["id"] not in busy_ids
                for key in keys_by_account[account["id"]]
            ]
            repair = action == "repair"

        def work():
            results = []
            for account in account_rows:
                if action in {"check", "repair"} and account["status"] == "disabled":
                    result = self.maintenance_result(account)
                    result.update(
                        outcome="skipped",
                        message="账号已手动停用，本次已跳过",
                    )
                    results.append(result)
                elif account["id"] in busy_ids:
                    result = self.maintenance_result(account)
                    result.update(
                        outcome="skipped",
                        message="账号正在执行采集或检测，本次已跳过",
                    )
                    results.append(result)
                else:
                    fingerprint = self.check_fingerprint(
                        "online", account["platform"], account["id"]
                    )
                    try:
                        result = self.maintain_account(account, repair=repair)
                    except Exception as exc:
                        result = self.maintenance_result(account)
                        result.update(
                            platform_check=exc.kind if isinstance(exc, CollectionError) else "internal_error",
                            message=error_message(exc),
                        )
                    result["checked_at"] = time.time()
                    result["fingerprint"] = fingerprint
                    if action == "check" and result["outcome"] == "ready":
                        result["message"] = "登录身份与搜索检查通过；详情、评论和回复未检查"
                    results.append(result)
            ready = sum(item["outcome"] == "ready" for item in results)
            needs_login = sum(item["outcome"] == "needs_login" for item in results)
            skipped = sum(item["outcome"] == "skipped" for item in results)
            failed = len(results) - ready - needs_login - skipped
            label = "可用性检查" if action == "check" else "修复配置" if repair else "检测"
            message = f"一键{label}完成：通过 {ready}，需登录 {needs_login}，失败 {failed}"
            if skipped:
                message += f"，跳过 {skipped}"
            summary = {
                "total": len(results),
                "ready": ready,
                "needs_login": needs_login,
                "failed": failed,
            }
            if skipped:
                summary["skipped"] = skipped
            return {
                "message": message,
                "action": action,
                "results": results,
                "summary": summary,
            }

        platform = account_rows[0]["platform"] if len(account_rows) == 1 else "all"
        return self.launch(
            "account_check" if action == "check" else "account_repair" if repair else "account_verify",
            platform,
            work,
            account_id=account_id,
            resource_keys=resource_keys,
        )

    def dispatch_schedules(self, *, now=None):
        with self.store() as store:
            pending = Scheduler(store).dispatch_due(now=now, versions=versions())
        launched = []
        for item in pending:
            try:
                launched.append(
                    self.collect(
                        {},
                        pending_run_id=item["run_id"],
                    )
                )
                logger.info(
                    "schedule_dispatched schedule_id=%s run_id=%s account_strategy=pool",
                    item["schedule_id"],
                    item["run_id"],
                )
            except Exception as exc:
                logger.warning(
                    "schedule_launch_blocked schedule_id=%s run_id=%s type=%s",
                    item["schedule_id"],
                    item["run_id"],
                    type(exc).__name__,
                )
        return launched

    def dispatch_session_recoveries(self):
        """At most one maintenance attempt; OS locks arbitrate all local processes."""
        with self.store() as store:
            candidates = store.list_accounts()
        for account in candidates:
            if account["status"] != "login_required":
                continue
            env = self.environment(account["platform"], account["id"])
            if not env.auto_session_recovery:
                continue
            receipt = read_recovery_receipt(env.profile_dir)
            if not (env.expected_user_id or receipt.get("user_id")):
                continue
            if account["last_failure_kind"] not in {None, "auth_expired"}:
                continue
            if account["last_failure_kind"] is None and Path(env.cookie_file).is_file():
                continue
            if receipt.get("attempt_key") == recovery_key(env):
                continue
            keys = self.account_resource_keys(account["platform"], account["id"])
            with self.lock:
                if self.resources_busy(keys):
                    continue

            def work(current=account, config=env, resource_keys=keys):
                with exclusive(resource_keys):
                    # Re-read state under the Profile lock, including attempts from other processes.
                    with self.store() as store:
                        if store.get_account(current["id"])["status"] != "login_required":
                            return {"state": "skipped", "message": "账号状态已变化"}
                    return asyncio.run(self.recover_session(
                        config, current["platform"], current["id"], automatic=True,
                    ))

            self.launch("recover", account["platform"], work,
                        account_id=account["id"], resource_keys=keys)
            return True
        return False

    async def recover_session(self, env, platform, account_id, *, automatic=False):
        """Caller owns resource locks. A fresh Session is built by the later probe."""
        receipt = read_recovery_receipt(env.profile_dir)
        key = recovery_key(env)
        if automatic and receipt.get("attempt_key") == key:
            return {"state": "skipped", "message": "该会话已经尝试恢复"}
        receipt_path = Path(env.profile_dir) / "session-recovery.json"
        write_cookie_file(receipt_path, receipt | {
            "attempt_key": key, "attempt_at": time.time(), "outcome": "running",
        })
        config = RunConfig(platform=platform, keywords=["咖啡"], max_requests=3,
                           max_seconds=90, min_interval=2, request_timeout=20,
                           network_retries=0)
        # Offline construction only avoids loading an absent/expired Cookie file.
        # The recovery run itself is online, with persistent quota and egress checks.
        session = Session(env, platform, offline=True)
        session.environment_snapshot, session.environment_snapshot_error = load_environment_snapshot(
            env.profile_dir
        )
        started = time.monotonic()
        epoch = None
        with self.store() as store:
            run_id = store.create_run(config, mode="online", binding=session.binding,
                                      account_id=account_id,
                                      environment={"session_recovery": True})
            try:
                observation = await observe_dual_exit(session)
                store.verify_run_network(run_id, observation)
                epoch = store.start(run_id, binding=session.binding)
                budget = RequestBudget(store, run_id, epoch, config)
                count = await capture_platform_cookies(
                    PLATFORMS[platform], output=Path(env.cookie_file),
                    profile=Path(env.profile_dir), timeout=75, channel=env.browser_channel,
                    proxy_env=env.proxy_env, proxy_file=env.proxy_file, mode="recover",
                    expected_user_id=env.expected_user_id, user_agent=env.user_agent,
                    headless=env.headless, admit=budget.admit, environment_session=session,
                    browser_provider=env.browser_provider,
                    adspower_profile_id=env.adspower_profile_id,
                    adspower_api_url=env.adspower_api_url,
                    adspower_start_timeout=env.adspower_start_timeout,
                    kameleo_profile_id=env.kameleo_profile_id,
                    kameleo_api_url=env.kameleo_api_url,
                    kameleo_start_timeout=env.kameleo_start_timeout,
                )
                # Validate the newly exported file before allowing the persisted probe.
                Session(env, platform)
                updated = read_recovery_receipt(env.profile_dir) | {
                    "attempt_key": recovery_key(env), "outcome": "recovered",
                }
                write_cookie_file(receipt_path, updated)
                store.event(run_id, "session_recovered", updated)
                state = "probe_due" if store.automatic_cooldown_enabled() else "ready"
                store.set_account_status(account_id, state)
                store.finish(run_id, epoch, elapsed=time.monotonic() - started,
                             reason="session_recovered")
                return {"state": state, "message": "登录态已导出，等待低负载恢复探针"
                        if state == "probe_due" else "登录态已导出，账号可直接试跑",
                        "cookie_count": count, "recovery": updated}
            except Exception as exc:
                error = exc if isinstance(exc, CollectionError) else CollectionError(
                    "deadline" if isinstance(exc, TimeoutError) else "identity_unverified"
                )
                updated = read_recovery_receipt(env.profile_dir) | {
                    "attempt_key": recovery_key(env), "outcome": error.kind,
                    "finished_at": time.time(),
                }
                write_cookie_file(receipt_path, updated)
                store.event(run_id, "session_recovery_failed", updated)
                store.record_risk(run_id, operation="search", error=error)
                store.set_account_status(account_id, "login_required")
                if epoch is None:
                    store.block_pending_run(run_id, error)
                else:
                    store.finish(run_id, epoch, elapsed=time.monotonic() - started,
                                 reason=error.kind, outcome=error.outcome)
                raise error from exc

    def dispatch_recovery_checks(self):
        """Launch at most one due probe; the DB active key arbitrates app instances."""
        with self.store() as store:
            if not store.automatic_cooldown_enabled():
                return False
            due = store.due_recovery_accounts()
        for account in due:
            try:
                env = self.environment(account["platform"], account["id"])
                session = Session(
                    env,
                    account["platform"],
                    profile_auth=is_managed_browser_provider(env.browser_provider),
                )
            except (OSError, ValueError):
                with self.store() as store:
                    store.set_account_status(account["id"], "login_required")
                continue
            keys = self.keys(session, offline=False)
            with self.lock:
                if self.resources_busy(keys):
                    continue

            def work(
                account_id=account["id"],
                current_session=session,
                resource_keys=keys,
                create_probe=account["status"] == "probe_due",
            ):
                with exclusive(resource_keys):
                    return asyncio.run(
                        self.recovery_check(
                            current_session, account_id, create_probe=create_probe
                        )
                    )

            self.launch(
                "recovery",
                account["platform"],
                work,
                account_id=account["id"],
                resource_keys=keys,
            )
            return True
        return False

    def start_scheduler(self, *, poll_seconds=5):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.scheduler_stop.clear()

        def loop():
            try:
                with self.store() as store:
                    stranded = store.pending_scheduled_runs()
            except Exception as exc:
                logger.warning("scheduler_recovery_scan_failed type=%s", type(exc).__name__)
                stranded = []
            for item in stranded:
                try:
                    self.collect(
                        {},
                        pending_run_id=item["run_id"],
                    )
                except Exception as exc:
                    logger.warning(
                        "scheduler_recovery_failed run_id=%s type=%s",
                        item["run_id"],
                        type(exc).__name__,
                    )
            while not self.scheduler_stop.is_set():
                try:
                    if not self.dispatch_session_recoveries() and not self.dispatch_recovery_checks():
                        self.dispatch_schedules()
                except Exception as exc:
                    logger.warning("scheduler_tick_failed type=%s", type(exc).__name__)
                self.scheduler_stop.wait(poll_seconds)

        self.scheduler_thread = threading.Thread(
            target=loop, name="crawler-scheduler", daemon=True
        )
        self.scheduler_thread.start()

    def stop_scheduler(self):
        self.scheduler_stop.set()
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=10)

    def check_fingerprint(self, kind, platform, account_id=None, *, env=None):
        if kind == "database":
            material = self.settings["database_url"]
        else:
            env = env if env is not None else self.environment(platform, account_id)
            material = env.model_dump_json()
            if kind != "proxy":
                try:
                    material += Path(env.cookie_file).read_text()
                except OSError:
                    pass
            try:
                material += load_proxy(proxy_env=env.proxy_env, proxy_file=env.proxy_file)
            except ValueError:
                pass
        return hashlib.sha256(material.encode()).hexdigest()

    def diagnostic_jobs(self):
        with self.lock:
            jobs = copy.deepcopy(list(self.jobs.values()))
        # Resolve accounts once per response, not once for every historical check.
        # Keep caches request-local so edits and external session exports invalidate
        # diagnostics immediately on the next poll.
        diagnostic = [j for j in jobs if j["kind"] not in ACCOUNT_MAINTENANCE_KINDS | {"collection"}]
        account_rows = {}
        if (any(j.get("account_id") and j["kind"] != "database" for j in diagnostic)
                or any(j["kind"] in ACCOUNT_MAINTENANCE_KINDS for j in jobs)):
            with self.store() as store:
                account_rows = {a["id"]: a for a in store.list_accounts()}
        environments = {}
        fingerprints = {}
        for job in jobs:
            if job["kind"] in ACCOUNT_MAINTENANCE_KINDS:
                job["current"] = True
                for result in (job.get("result") or {}).get("results", []):
                    account_id = result.get("id")
                    platform = result.get("platform")
                    key = ("online", platform, account_id)
                    try:
                        if account_id not in account_rows:
                            raise ConsoleError("账号已删除")
                        if key not in fingerprints:
                            env_key = (platform, account_id)
                            if env_key not in environments:
                                environments[env_key] = self.account_environment(
                                    account_rows[account_id], platform
                                )
                            fingerprints[key] = self.check_fingerprint(
                                *key, env=environments[env_key]
                            )
                        result["current"] = result.get("fingerprint") == fingerprints[key]
                    except (ValueError, OSError):
                        result["current"] = False
                    result.pop("fingerprint", None)
            elif job["kind"] != "collection":
                try:
                    key = (job["kind"], job["platform"], job.get("account_id"))
                    if key not in fingerprints:
                        env_key = key[1:]
                        if job["kind"] != "database" and env_key not in environments:
                            if account_id := job.get("account_id"):
                                if account_id not in account_rows:
                                    raise ConsoleError("账号已删除")
                                environments[env_key] = self.account_environment(
                                    account_rows[account_id], job["platform"]
                                )
                            else:
                                environments[env_key] = self.environment(job["platform"])
                        fingerprints[key] = self.check_fingerprint(
                            *key, env=environments.get(env_key)
                        )
                    job["current"] = job.get("fingerprint") == fingerprints[key]
                except (ValueError, OSError):
                    # Persisted diagnostics outlive deleted accounts and local
                    # configuration files. Keep their history without requiring
                    # a usable current session to load the console or dashboard.
                    job["current"] = False
        return jobs

    def runtime_status(self):
        with self.lock:
            return {
                "status": "ok",
                "workers": self.worker_count,
                "queued": sum(job["status"] == "queued" for job in self.jobs.values()),
                "running": sum(
                    job["status"] == "running" and job["kind"] == "collection"
                    for job in self.jobs.values()
                ),
            }

    def cookie_check(self, platform, account_id=None, *, env=None):
        recovery = {}
        try:
            env = env if env is not None else self.environment(platform, account_id)
            if is_managed_browser_provider(env.browser_provider):
                provider = "AdsPower" if env.browser_provider == "adspower" else "Kameleo"
                return {
                    "session_recovery": recovery,
                    "cookie_state": "managed",
                    "cookie_count": 0,
                    "cookie_message": f"登录状态由 {provider} 浏览器环境管理，运行时在线验证",
                    "browser_state": "managed",
                }
            receipt = read_recovery_receipt(env.profile_dir)
            recovery = {key: receipt.get(key) for key in (
                "outcome", "verified_at", "attempt_at", "user_id", "source",
            )}
            recovery["export_matches_file"] = receipt.get("new_digest") == file_digest(env.cookie_file)
            cookies = load_cookies(Path(env.cookie_file), platform)
            required = PLATFORMS[platform].required_cookies
            valid = any(c["name"] in required and c["value"] for c in cookies)
            origins = load_storage_origins(Path(env.cookie_file), platform)
            has_xmst = any(
                value["name"] == "xmst" and value["value"]
                for origin in origins
                for value in origin["localStorage"]
            )
            message = "会话字段存在；在线有效性待检测"
            if platform == "douyin" and not has_xmst:
                message = "登录凭据存在，但缺少浏览器本地签名材料；在线检测可能触发验证"
            return {
                "session_recovery": recovery,
                "cookie_state": "configured" if valid else "invalid",
                "cookie_count": len(cookies),
                "cookie_message": message if valid else "缺少有效会话字段或已过期",
                "browser_state": "configured" if has_xmst else "missing",
            }
        except Exception:
            return {
                "session_recovery": recovery,
                "cookie_state": "missing",
                "cookie_count": 0,
                "cookie_message": "未配置或登录凭据文件无法解析",
            }

    def save(self, body):
        with self.lock:
            if body.get("database_url"):
                self.require_idle()
            elif body.get("platform"):
                self.require_resources_idle(self.account_resource_keys(body["platform"]))
            if "automatic_cooldown" in body and type(body["automatic_cooldown"]) is not bool:
                raise ConsoleError("自动冷却开关必须为布尔值")
            proposed = copy.deepcopy(self.settings)
            if body.get("database_url"):
                proposed["database_url"] = validate_database(body["database_url"].strip())
            platform = body.get("platform")
            writes = []
            if platform:
                if platform not in PLATFORMS:
                    raise ConsoleError("未知平台")
                original = proposed["accounts"][platform]
                env = EnvironmentConfig.model_validate(original | body.get("environment", {}))
                if body.get("clear_proxy"):
                    env.proxy_id = None
                    env.proxy_file = env.proxy_env = None
                elif body.get("proxy_url"):
                    value = body["proxy_url"].strip()
                    value = validated_proxy(value)
                    env.proxy_env = None
                    env.proxy_id = None
                    env.proxy_file = str(self.directory / "proxies" / f"{uuid.uuid4().hex}.proxy")
                    writes.append((Path(env.proxy_file), value))
                if body.get("cookie_text"):
                    value = body["cookie_text"].strip()
                    # Parse before replacing a working configuration.
                    self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd, name = tempfile.mkstemp(dir=self.directory)
                    os.close(fd)
                    try:
                        Path(name).write_text(value)
                        cookies = load_cookies(Path(name), platform)
                        origins = load_storage_origins(Path(name), platform)
                        if not any(
                            c["name"] in PLATFORMS[platform].required_cookies and c["value"]
                            for c in cookies
                        ):
                            raise ConsoleError("登录凭据缺少未过期的会话字段")
                    finally:
                        Path(name).unlink(missing_ok=True)
                    env.cookie_file = str(self.directory / f"{platform}.cookies.json")
                    try:
                        source = json.loads(value)
                    except json.JSONDecodeError:
                        source = None
                    persisted: list[dict] | dict = cookies
                    if (
                        platform == "douyin"
                        and isinstance(source, dict)
                        and "origins" in source
                    ):
                        persisted = {"cookies": cookies, "origins": origins}
                    writes.append(
                        (Path(env.cookie_file), json.dumps(persisted, ensure_ascii=False))
                    )
                if env.proxy_file and env.proxy_env:
                    raise ConsoleError("代理文件与环境变量只能配置其中一种")
                if (
                    not env.account_ref.strip()
                    or not env.profile_dir.strip()
                    or not env.cookie_file.strip()
                ):
                    raise ConsoleError("账号标识、浏览器目录和 登录凭据路径不能为空")
                if platform == "douyin" and env.expected_user_id:
                    raise ConsoleError("抖音暂不支持预期用户标识 核对，请留空")
                proposed["accounts"][platform] = env.model_dump()
            for path, value in writes:
                private_write(path, value)
            private_write(self.path, json.dumps(proposed, ensure_ascii=False, indent=2))
            self.settings = proposed
            if platform or body.get("database_url"):
                self.sync_legacy_accounts(force=True, only_platform=platform)
                try:
                    self.migrate_account_proxies()
                except SQLAlchemyError as exc:
                    # Connection settings remain saveable while the database is
                    # offline; startup retries the additive inventory migration.
                    logger.warning("account_proxy_migration_failed type=%s", type(exc).__name__)
            if "automatic_cooldown" in body:
                with self.store() as store:
                    store.set_automatic_cooldown(body["automatic_cooldown"])
            return self.public_settings()

    def require_idle(self):
        if any(session.active for session in self.account_browsers.values()):
            raise ConsoleError("请先关闭已打开的账号浏览器")
        if self.analysis.active:
            raise ConsoleError("当前有 Agent 分析正在执行，请先停止或等待结束")
        if any(job["status"] in {"queued", "running"} for job in self.jobs.values()):
            raise ConsoleError("当前有采集或检测正在执行，请完成或停止后再操作")

    def resources_busy(self, resource_keys, *, diagnostics_only=False):
        """Return whether an in-process job already owns any requested resource."""
        requested = frozenset(resource_keys)
        if not requested:
            return False
        if any(session.active and not requested.isdisjoint(session.keys)
               for session in self.account_browsers.values()):
            return True
        return any(
            job["status"] in {"queued", "running"}
            and (not diagnostics_only or job["kind"] != "collection")
            and not requested.isdisjoint(self.job_resources.get(job_id, ()))
            for job_id, job in self.jobs.items()
        )

    def require_resources_idle(self, resource_keys):
        if self.resources_busy(resource_keys):
            raise ConsoleError("该账号或浏览器环境正在执行任务或被查看，请关闭账号浏览器或结束该账号的任务后再操作")

    def _trim_jobs(self):
        while len(self.jobs) > 80:
            finished = next(
                (
                    job_id
                    for job_id, job in self.jobs.items()
                    if job["status"] not in {"queued", "running"}
                ),
                None,
            )
            if finished is None:
                return
            del self.jobs[finished]
            self.job_resources.pop(finished, None)

    def launch(
        self,
        kind,
        platform,
        work,
        run_id=None,
        account_id=None,
        *,
        resource_keys=None,
    ):
        with self.lock:
            resource_keys = list(resource_keys or [])
            self.require_resources_idle(resource_keys)
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "kind": kind,
                "platform": platform,
                "run_id": run_id,
                "account_id": account_id,
                "status": "running",
                "created_at": time.time(),
                "fingerprint": None
                if kind == "collection" or kind in ACCOUNT_MAINTENANCE_KINDS
                else self.check_fingerprint(kind, platform, account_id),
            }
            self.jobs[job_id] = job
            self.job_resources[job_id] = frozenset(resource_keys)

            def target():
                try:
                    result = work()
                    with self.lock:
                        job.update(status="completed", result=result)
                except Exception as exc:
                    with self.lock:
                        job.update(status="failed", message=error_message(exc))
                finally:
                    with self.lock:
                        job["finished_at"] = time.time()
                        # Keep a bounded diagnostic history, containing no credentials.
                        self._trim_jobs()
                        if kind != "collection":
                            if kind in {"login", "recover", "online"} and job["status"] == "completed":
                                job["fingerprint"] = self.check_fingerprint(
                                    kind, platform, account_id
                                )
                            private_write(
                                self.checks_path,
                                json.dumps(
                                    [
                                        j
                                        for j in self.jobs.values()
                                        if j["kind"] != "collection" and j["status"] != "running"
                                    ],
                                    ensure_ascii=False,
                                ),
                            )

            thread = threading.Thread(target=target, daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
            thread.start()
            return copy.deepcopy(job)

    def launch_collection(self, platform, run_id, resource_keys, work, *, account_id=None):
        with self.lock:
            if self.resources_busy(resource_keys, diagnostics_only=True):
                raise ConsoleError("所选账号或浏览器环境正在验证或登录，请稍后再创建采集任务")
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "kind": "collection",
                "platform": platform,
                "run_id": run_id,
                "account_id": account_id,
                "status": "queued",
                "created_at": time.time(),
                "fingerprint": None,
            }
            self.jobs[job_id] = job
            self.job_resources[job_id] = frozenset(resource_keys)

            def on_start(slot):
                with self.lock:
                    job.update(status="running", started_at=time.time(), worker_slot=slot)

            def execute():
                try:
                    result = work()
                    with self.lock:
                        job.update(status="completed", result=result)
                except Exception as exc:
                    self.fail_pending_collection(run_id, exc)
                    with self.lock:
                        job.update(status="failed", message=error_message(exc))
                finally:
                    with self.lock:
                        job["finished_at"] = time.time()
                        self._trim_jobs()

            def on_cancel():
                with self.lock:
                    job.update(
                        status="canceled",
                        finished_at=time.time(),
                        message="服务停止前任务尚未开始",
                    )

            try:
                self.worker_pool.submit(
                    execute,
                    resource_keys=resource_keys,
                    on_start=on_start,
                    on_cancel=on_cancel,
                )
            except Exception:
                job.update(
                    status="failed",
                    finished_at=time.time(),
                    message="采集任务未能进入执行队列",
                )
                raise
            return copy.deepcopy(job)

    def fail_pending_collection(self, run_id, exc):
        """Turn pre-worker failures into an explicit terminal run state."""
        try:
            with self.store() as store:
                if store.get_run(run_id)["status"] == "pending":
                    store.fail_pending_run(
                        run_id,
                        reason="worker_start_failed",
                        error_type=type(exc).__name__,
                    )
        except Exception as record_exc:
            logger.error(
                "collection_start_failure_not_recorded run_id=%s error_type=%s",
                run_id,
                type(record_exc).__name__,
            )
        logger.error(
            "collection_job_failed run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )

    def select_account(self, store, platform, requested_id=None):
        """Choose an eligible execution resource before a new task is persisted."""
        if requested_id:
            account = store.get_account(requested_id)
            if account["platform"] != platform:
                raise ConsoleError("账号与采集平台不匹配")
            store.account_preflight(requested_id)
            return account
        candidates = store.available_accounts(platform)
        if not candidates:
            raise CollectionError(
                "account_unavailable", f"No ready account for platform {platform}"
            )
        busy = {
            job.get("account_id")
            for job in self.jobs.values()
            if job["status"] in {"queued", "running"}
        }
        idle = [account for account in candidates if account["id"] not in busy]
        return (idle or candidates)[0]

    def collect(self, body, resume_id=None, pending_run_id=None):
        with self.lock:
            if resume_id and any(
                job.get("run_id") == resume_id and job["status"] in {"queued", "running"}
                for job in self.jobs.values()
            ):
                raise ConsoleError("该任务已经在队列中或正在执行")
            with self.store() as store:
                existing_id = resume_id or pending_run_id
                account_id = None
                if existing_id:
                    run = store.get_run(existing_id)
                    context = store.get_run_context(existing_id)
                    account_id = context.get("account_id")
                    config = RunConfig.model_validate(run["config"])
                    if run["mode"] != "online":
                        raise ConsoleError("仅支持恢复真实采集任务")
                    if resume_id and run["status"] == "completed":
                        raise ConsoleError("已完成任务无需恢复")
                else:
                    config = RunConfig.model_validate(body["config"])
                    mode = body.get("mode", "online")
                    if mode != "online":
                        raise ConsoleError("仅支持真实采集")
                if store.engine.dialect.name != "postgresql":
                    raise ConsoleError("真实采集需要 PostgreSQL，请先配置数据库")
                if not account_id:
                    try:
                        account = self.select_account(
                            store, config.platform, body.get("account_id")
                        )
                        account_id = account["id"]
                    except CollectionError as exc:
                        if existing_id:
                            store.block_pending_run(existing_id, exc)
                            store.record_risk(existing_id, operation=None, error=exc)
                        logger.warning(
                            "account_assignment_failed run_id=%s platform=%s type=%s",
                            existing_id or "not-created",
                            config.platform,
                            exc.kind,
                        )
                        raise ConsoleError("当前没有符合条件的可用账号") from None
                account = store.get_account(account_id)
                env = self.environment(config.platform, account_id)
                env = self.run_environment(env, config)
                if account_id:
                    try:
                        store.account_preflight(account_id)
                    except CollectionError as exc:
                        if existing_id and store.get_run(existing_id)["status"] == "pending":
                            store.block_pending_run(existing_id, exc)
                            store.record_risk(existing_id, operation=None, error=exc)
                        raise ConsoleError("账号当前不可运行：" + UI_LABELS["reasons"].get(
                            exc.kind, "请检查账号状态"
                        )) from None
                try:
                    session = Session(
                        env,
                        config.platform,
                        profile_auth=(
                            is_managed_browser_provider(env.browser_provider)
                            and not is_xhs_api_adapter(config.adapter)
                        ),
                    )
                except (OSError, ValueError) as exc:
                    if existing_id and store.get_run(existing_id)["status"] == "pending":
                        error = CollectionError("auth_expired", "Account session is unavailable")
                        store.block_pending_run(existing_id, error)
                        store.record_risk(existing_id, operation=None, error=error)
                    raise ConsoleError("请先配置未过期的登录凭据，再开始真实采集") from exc
                extra_requests = int(body.get("extra_requests", 0))
                extra_seconds = int(body.get("extra_seconds", 0))
                if extra_requests < 0 or extra_seconds < 0:
                    raise ConsoleError("追加预算不能为负数")
                RunConfig.model_validate(
                    config.model_dump()
                    | {
                        "max_requests": config.max_requests + extra_requests,
                        "max_seconds": config.max_seconds + extra_seconds,
                    }
                )
                environment = {
                    "auto_bind_network": True,
                    "use_default_quotas": True,
                    "account_ref": env.account_ref,
                    "profile_dir": env.profile_dir,
                    "browser_channel": env.browser_channel,
                    "browser_provider": env.browser_provider,
                    "adspower_profile_ref": (
                        hashlib.sha256(env.adspower_profile_id.encode()).hexdigest()[:16]
                        if env.adspower_profile_id
                        else None
                    ),
                    "kameleo_profile_ref": (
                        hashlib.sha256(env.kameleo_profile_id.encode()).hexdigest()[:16]
                        if env.kameleo_profile_id
                        else None
                    ),
                    "headless": env.headless,
                    "consistency_policy": env.consistency_policy,
                    "environment_snapshot": (
                        {
                            "source": session.environment_snapshot.get("source"),
                            "captured_at": session.environment_snapshot.get("captured_at"),
                            "schema_version": session.environment_snapshot.get("schema_version"),
                        }
                        if session.environment_snapshot
                        else {"status": session.environment_snapshot_error}
                    ),
                    "session_version": env.session_version,
                    "binding_version": env.binding_version,
                    "http_transport": (
                        config.adapter
                        if is_xhs_api_adapter(config.adapter)
                        else "curl_cffi"
                        if config.platform == "douyin"
                        else None
                    ),
                    "http_browser_preset": (
                        env.impersonate
                        if config.platform == "douyin" or config.adapter == "curl_cffi"
                        else None
                    ),
                    "cookie_digest": session.cookie_digest[:16]
                    if session.cookie_digest
                    else None,
                    "proxy_ref": (
                        managed_browser_profile_fingerprint(env)
                        if is_managed_browser_provider(env.browser_provider)
                        else hashlib.sha256(session.proxy.encode()).hexdigest()[:16]
                        if session.proxy
                        else None
                    ),
                    "proxy_source": (
                        f"{managed_browser_transport(env)}+"
                        f"{'project' if session.proxy else 'direct'}"
                        if is_managed_browser_provider(env.browser_provider)
                        and (
                            config.platform == "douyin"
                            or is_xhs_api_adapter(config.adapter)
                        )
                        else
                        managed_browser_transport(env)
                        if is_managed_browser_provider(env.browser_provider)
                        and not is_xhs_api_adapter(config.adapter)
                        else "project"
                        if session.proxy
                        else "direct"
                    ),
                }
                run_id = existing_id or store.create_run(
                    config,
                    mode="online",
                    binding=session.binding,
                    versions=versions(),
                    account_id=account_id,
                    environment=environment,
                )
                if resume_id:
                    # Clear only the old attempt's cancellation before accepting
                    # the new attempt. Later stop requests must survive start().
                    with store.engine.begin() as conn:
                        conn.execute(
                            update(runs)
                            .where(runs.c.id == resume_id)
                            .values(cancel_requested=False)
                        )
                if not store.get_run_context(run_id).get("account_id"):
                    store.assign_run_account(
                        run_id,
                        account_id,
                        environment={
                            "auto_bind_network": True,
                            "use_default_quotas": True,
                            "account_ref": env.account_ref,
                            "profile_dir": env.profile_dir,
                            "consistency_policy": env.consistency_policy,
                            "session_version": env.session_version,
                            "binding_version": env.binding_version,
                            "http_transport": environment["http_transport"],
                            "http_browser_preset": environment["http_browser_preset"],
                            "proxy_configured": bool(session.proxy)
                            or is_managed_browser_provider(env.browser_provider),
                            "proxy_source": environment["proxy_source"],
                        },
                    )
                    logger.info(
                        "account_assigned run_id=%s platform=%s account_id=%s strategy=%s",
                        run_id,
                        config.platform,
                        account_id,
                        "requested" if body.get("account_id") else "least_recently_used",
                    )
                if existing_id:
                    store.update_run_environment(
                        run_id,
                        binding=session.binding,
                        environment=environment,
                    )

            resource_keys = self.keys(
                session,
                run_id=run_id,
                adapter=config.adapter,
            )

            def work():
                viewer = None
                watchable = (
                    is_xhs_platform(config.platform)
                    and config.adapter == "browser"
                    and is_managed_browser_provider(env.browser_provider)
                    and not env.headless
                )
                try:
                    if watchable:
                        viewer = CollectionBrowserViewer(run_id, account)
                        with self.lock:
                            self.collection_browsers = {
                                key: current
                                for key, current in self.collection_browsers.items()
                                if current.active
                            }
                            self.collection_browsers[run_id] = viewer
                        viewer.start()
                    with self.store() as worker_store:
                        directory = self.root / "data/validation" / run_id
                        samples = Samples(directory)
                        xhs_api = is_xhs_platform(config.platform) and is_xhs_api_adapter(config.adapter)
                        online_adapter = (
                            XHSHTTP
                            if xhs_api
                            else XHSBrowser
                            if is_xhs_platform(config.platform)
                            else DouyinHTTP
                        )
                        factory = (
                            (lambda b: online_adapter(
                                session, b, samples, transport_kind=config.adapter
                            ))
                            if xhs_api
                            else (
                                lambda b: online_adapter(
                                    session,
                                    b,
                                    samples,
                                    **({"display": viewer.display} if viewer else {}),
                                )
                            )
                        )
                        with exclusive(resource_keys):
                            worker_store.event(
                                run_id,
                                "worker_started",
                                {
                                    "account_ref": env.account_ref,
                                    "http_transport": environment["http_transport"],
                                    "http_browser_preset": environment["http_browser_preset"],
                                    "browser_viewer": bool(viewer and viewer.desktop),
                                },
                            )
                            asyncio.run(
                                run_worker(
                                    worker_store,
                                    run_id,
                                    factory,
                                    session=session,
                                    resume=bool(resume_id),
                                    reset_contexts=(
                                        is_xhs_platform(config.platform)
                                        and config.adapter == "browser"
                                    ),
                                    extra_requests=extra_requests,
                                    extra_seconds=extra_seconds,
                                    preserve_cancel=True,
                                    artifact_root=self.artifact_root,
                                )
                            )
                            return export_run(worker_store, run_id, directory)
                finally:
                    if viewer:
                        viewer.close()

            try:
                return self.launch_collection(
                    config.platform, run_id, resource_keys, work, account_id=account_id
                )
            except Exception as exc:
                self.fail_pending_collection(run_id, exc)
                raise

    def browser_resource_key(self, env):
        return (
            managed_browser_resource_key(env)
            if is_managed_browser_provider(env.browser_provider)
            else "profile:" + str(self.resolve(env.profile_dir).resolve())
        )

    def keys(self, session, *, run_id=None, offline=False, adapter=None):
        keys = ["account:" + session.platform + ":" + session.config.account_ref]
        if not offline and session.config.cookie_file and not session.profile_auth:
            keys.append("cookie:" + str(Path(session.config.cookie_file).resolve()))
        if not offline and session.config.proxy_file:
            keys.append("proxy_file:" + str(Path(session.config.proxy_file).resolve()))
        if run_id:
            keys.append("run:" + run_id)
        if not offline:
            uses_profile = (
                is_managed_browser_provider(session.config.browser_provider)
                or not is_xhs_platform(session.platform)
                or not is_xhs_api_adapter(adapter)
            )
            if uses_profile:
                keys.append(self.browser_resource_key(session.config))
            if session.proxy:
                proxy = urlsplit(session.proxy)
                keys.append(f"proxy:{proxy.hostname}:{proxy.port}")
        return keys

    def account_resource_keys(self, platform, account_id=None):
        """Build lock keys without requiring an already valid login session."""
        env = self.environment(platform, account_id)
        keys = [
            "account:" + platform + ":" + env.account_ref,
            self.browser_resource_key(env),
            "cookie:" + str(Path(env.cookie_file).resolve()),
        ]
        if env.proxy_file:
            keys.append("proxy_file:" + str(Path(env.proxy_file).resolve()))
        try:
            proxy_url = load_proxy(proxy_env=env.proxy_env, proxy_file=env.proxy_file)
        except (OSError, ValueError):
            proxy_url = ""
        if proxy_url:
            proxy = urlsplit(proxy_url)
            keys.append(f"proxy:{proxy.hostname}:{proxy.port}")
        return keys

    def check(self, body):
        kind, platform = body["kind"], body.get("platform", "xhs")
        account_id = body.get("account_id")
        if kind not in {"account", "cookie", "online", "login", "recover", "database", "proxy"}:
            raise ConsoleError("未知检测类型")
        with self.lock:
            env = self.environment(platform, account_id)
            db_url = self.settings["database_url"]
            resource_keys = (
                []
                if kind in {"database", "cookie"}
                else self.account_resource_keys(platform, account_id)
            )

            def work():
                if kind == "cookie":
                    return self.cookie_check(platform, account_id)
                if kind == "account":
                    if self.cookie_check(platform, account_id)["cookie_state"] not in {
                        "configured",
                        "managed",
                    }:
                        raise ConsoleError("账号缺少有效登录凭据，请先保存登录凭据 或完成浏览器登录")
                    session = Session(
                        env,
                        platform,
                        profile_auth=is_managed_browser_provider(env.browser_provider),
                    )
                    profile_exists = Path(env.profile_dir).is_dir()
                    return {
                        "message": "账号配置可用；在线登录状态仍需检测",
                        "profile_message": "浏览器数据目录已存在"
                        if profile_exists
                        else "首次采集时将创建浏览器目录",
                        "proxy_message": (
                            "代理由托管浏览器环境管理"
                            if is_managed_browser_provider(env.browser_provider)
                            else "已读取代理配置"
                            if session.proxy
                            else "使用直连"
                        ),
                        "state": "configured",
                    }
                if kind == "database":
                    started = time.monotonic()
                    store = web_store(db_url)
                    try:
                        with store.engine.connect() as conn:
                            conn.execute(text("SELECT 1"))
                        return {
                            "message": "数据库连接成功",
                            "dialect": store.engine.dialect.name,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                        }
                    finally:
                        store.close()
                if kind == "proxy":
                    proxy = load_proxy(proxy_env=env.proxy_env, proxy_file=env.proxy_file)
                    if account_id:
                        configured_proxy = load_proxy(
                            proxy_env=env.proxy_env,
                            proxy_file=env.proxy_file,
                            apply_relay=False,
                        )
                        network_session = SimpleNamespace(
                            platform=platform,
                            proxy=proxy,
                            configured_proxy=configured_proxy,
                            config=env,
                        )
                        observation = asyncio.run(observe_dual_exit(network_session))
                        if observation["outcome"] != "success":
                            raise CollectionError(observation["error_kind"])
                        with self.store() as store:
                            store.confirm_account_network(
                                account_id,
                                proxy_ref=observation["proxy_ref"],
                                egress_ip=observation["egress_ip"],
                                ip_group=observation["ip_group"],
                            )
                        return {
                            "message": (
                                "托管浏览器出口可用，已确认账号出口绑定"
                                if is_managed_browser_provider(env.browser_provider)
                                else "网络请求与浏览器出口一致，已确认账号出口绑定"
                            ),
                            "state": "reachable",
                            "geo_state": "located",
                            "ip": observation["egress_ip"],
                            "location": {
                                key: observation.get(key)
                                for key in ("country", "region", "city")
                            },
                            "isp": observation.get("isp"),
                            "latency_ms": observation.get("latency_ms"),
                            "geo_source": observation["source"],
                            "transport_match": True,
                        }
                    return proxy_diagnostic(proxy, platform, env.impersonate)
                with exclusive(resource_keys):
                    if kind in {"login", "recover"}:
                        previous_status = None
                        if account_id:
                            with self.store() as store:
                                previous_status = store.get_account(account_id)["status"]
                                if previous_status != "disabled":
                                    store.set_account_status(account_id, "login_required")
                        capture_session = None
                        if kind == "recover":
                            capture_session = Session(env, platform, offline=True)
                            (
                                capture_session.environment_snapshot,
                                capture_session.environment_snapshot_error,
                            ) = load_environment_snapshot(env.profile_dir)
                        count = asyncio.run(
                            capture_platform_cookies(
                                PLATFORMS[platform],
                                output=Path(env.cookie_file),
                                profile=Path(env.profile_dir),
                                timeout=180,
                                mode="reauth" if kind == "login" else "recover",
                                expected_user_id=env.expected_user_id,
                                user_agent=env.user_agent,
                                channel=env.browser_channel,
                                proxy_env=env.proxy_env,
                                proxy_file=env.proxy_file,
                                headless=False,
                                browser_provider=env.browser_provider,
                                adspower_profile_id=env.adspower_profile_id,
                                adspower_api_url=env.adspower_api_url,
                                adspower_start_timeout=env.adspower_start_timeout,
                                kameleo_profile_id=env.kameleo_profile_id,
                                kameleo_api_url=env.kameleo_api_url,
                                kameleo_start_timeout=env.kameleo_start_timeout,
                                environment_session=capture_session,
                            )
                        )
                        if account_id:
                            with self.store() as store:
                                previous_status = store.get_account(account_id)["status"]
                                store.set_account_status(
                                    account_id, "disabled" if previous_status == "disabled" else "ready"
                                )
                        return {
                            "message": "已确认身份并保存会话；账号保持停用，请手动启用"
                            if previous_status == "disabled"
                            else "已确认身份并保存会话，账号可直接试跑",
                            "cookie_count": count,
                        }
                    if self.cookie_check(platform, account_id)["cookie_state"] not in {
                        "configured",
                        "managed",
                    }:
                        raise ConsoleError("缺少有效登录凭据，请先保存配置或打开浏览器登录")
                    session = Session(
                        env,
                        platform,
                        profile_auth=is_managed_browser_provider(env.browser_provider),
                    )
                    account_status = None
                    if account_id:
                        with self.store() as store:
                            account_status = store.get_account(account_id)["status"]
                            # An explicit check may inspect an unavailable account.
                            # Only the opt-in automatic policy needs probe/canary stages.
                            automatic_cooldown = store.automatic_cooldown_enabled()
                    try:
                        if account_status in {"probe_due", "recovering"} and automatic_cooldown:
                            result = asyncio.run(
                                self.recovery_check(
                                    session,
                                    account_id,
                                    create_probe=account_status == "probe_due",
                                )
                            )
                        else:
                            result = asyncio.run(self.probe(session))
                    except CollectionError as exc:
                        if account_id and account_status != "disabled" and exc.kind in {
                            "auth_expired", "identity_mismatch", "identity_unverified",
                            "verification_required", "access_denied", "rate_limit",
                        }:
                            with self.store() as store:
                                store.require_session_recovery(account_id, reason=exc.kind)
                        raise
                    if result.get("login_state") in {"expired", "unverified"}:
                        reason = ("auth_expired" if result["login_state"] == "expired"
                                  else "identity_unverified")
                        if account_id and account_status != "disabled":
                            with self.store() as store:
                                store.require_session_recovery(account_id, reason=reason)
                        raise CollectionError(reason)
                    if (
                        account_id
                        and account_status in {"cooling", "probe_due", "recovering", "login_required"}
                        and result.get("login_state") == "verified"
                        and not (automatic_cooldown and account_status in {"probe_due", "recovering"})
                    ):
                        with self.store() as store:
                            store.set_account_status(account_id, "ready")
                        result["account_recovered"] = True
                    nickname = result.pop("account_nickname", None)
                    result.pop("account_user_id", None)
                    account_ref, updated = (
                        (env.account_ref, False)
                        if account_id
                        else self.adopt_identity_nickname(platform, env.account_ref, nickname)
                    )
                    result["account_ref"] = account_ref
                    if account_id and nickname:
                        result["account_message"] = f"平台识别到登录昵称：{nickname}"
                    if updated:
                        result["account_ref_updated"] = True
                        result["account_message"] = f"账号标识已自动更新为 {account_ref}"
                    return result

            return self.launch(
                kind,
                platform,
                work,
                account_id=account_id,
                resource_keys=resource_keys,
            )

    async def probe(self, session):
        # A single bounded search probes the existing production adapter. It is not
        # evidence of a logged-in identity unless user/me was actually observed.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            store = Store("sqlite:///" + directory + "/probe.db")
            store.initialize()
            config = RunConfig(
                platform=session.platform,
                keywords=["咖啡"],
                max_requests=3,
                max_seconds=90,
                min_interval=2,
                request_timeout=30,
                network_retries=0,
            )
            # This isolated store exists only to bound a user-triggered
            # diagnostic. It has no account, accepted egress, or quota-policy
            # records, so treating it as a production online run makes every
            # check fail closed with quota_policy_missing. The real session is
            # still used by the adapter; diagnostic mode only disables the
            # production quota lookup in this disposable store.
            run_id = store.create_run(config, mode="diagnostic", binding=session.binding)
            epoch = store.start(run_id, binding=session.binding)
            budget = RequestBudget(store, run_id, epoch, config, session)
            adapter = (XHSBrowser if is_xhs_platform(session.platform) else DouyinHTTP)(
                session, budget, Samples(Path(directory))
            )
            try:
                async with asyncio.timeout(60):
                    page = await adapter.fetch(
                        TaskRequest(operation="search", keyword="咖啡", sort=config.sort)
                    )
                    budget.check()
                    if session.platform == "douyin":
                        login_state, identity = await douyin_identity(
                            adapter, budget, include_profile=True
                        )
                    else:
                        login_state = "verified" if adapter.identity_verified else "unverified"
                        identity = adapter.identity_profile
                return {
                    "message": "平台搜索请求成功",
                    "state": "reachable",
                    "sample_count": len(page.items),
                    "login_state": login_state,
                    "account_nickname": identity.get("nickname"),
                    "account_user_id": identity.get("user_id"),
                    "login_message": {
                        "verified": "已确认登录身份",
                        "expired": "平台返回登录失效，请重新登录",
                        "unverified": "搜索请求可用；本次未确认登录身份",
                    }[login_state],
                }
            finally:
                await adapter.close()
                store.close()

    async def recovery_check(self, session, account_id, *, create_probe):
        """Run the persisted, quota-accounted recovery probe or canary."""
        probe_id = None
        run_id = None
        request_count = 0
        with self.store() as store:
            if create_probe:
                probe = store.create_recovery_probe(account_id)
                if not probe.get("_created"):
                    return {
                        "message": "该账号已有恢复探针在执行",
                        "state": "already_running",
                    }
                probe_id = probe["id"]
                store.start_recovery_probe(probe_id)
            elif not store.claim_recovery_canary(account_id):
                return {
                    "message": "该账号已有恢复试跑 在执行",
                    "state": "already_running",
                }
            config = RunConfig(
                platform=session.platform,
                keywords=["咖啡"],
                content_limit=1,
                comment_limit=0,
                reply_parents=0,
                reply_limit=0,
                max_requests=3,
                max_seconds=90,
                max_pages=1,
                min_interval=2,
                request_timeout=30,
                network_retries=0,
            )
            run_id = store.create_run(
                config,
                mode="online",
                binding=session.binding,
                versions=versions(),
                account_id=account_id,
                environment={"recovery_probe_id": probe_id, "recovery_canary": not create_probe},
            )
            try:
                observation = await observe_dual_exit(session)
                if probe_id:
                    store.record_network_observation(
                        probe_id=probe_id,
                        account_id=account_id,
                        platform=session.platform,
                        proxy_ref=observation["proxy_ref"],
                        observation=observation,
                    )
                store.verify_run_network(run_id, observation)
                epoch = store.start(run_id, binding=session.binding)
                budget = RequestBudget(store, run_id, epoch, config, session)
                task = store.next_task(run_id, epoch)
                adapter = (XHSBrowser if is_xhs_platform(session.platform) else DouyinHTTP)(
                    session,
                    budget,
                    Samples(self.root / "data/validation" / run_id),
                )
                operation_started = time.time()
                operation_clock = time.monotonic()
                error = None
                try:
                    async with asyncio.timeout(90):
                        page = await adapter.fetch(
                            TaskRequest(operation="search", keyword="咖啡", sort=config.sort)
                        )
                        budget.check()
                        if session.platform == "douyin":
                            login_state, identity = await douyin_identity(
                                adapter, budget, include_profile=True
                            )
                        else:
                            login_state = (
                                "verified" if adapter.identity_verified else "unverified"
                            )
                            identity = adapter.identity_profile
                        if login_state != "verified":
                            raise CollectionError("identity_unverified")
                        store.record_operation(
                            run_id,
                            task["id"],
                            operation="search",
                            attempt=1,
                            started_at=operation_started,
                            duration_ms=(time.monotonic() - operation_clock) * 1000,
                            outcome="success",
                            item_count=len(page.items),
                            response_has_more=page.response_has_more,
                        )
                except (CollectionError, TimeoutError) as exc:
                    error = exc if isinstance(exc, CollectionError) else CollectionError("deadline")
                    store.record_operation(
                        run_id,
                        task["id"],
                        operation="search",
                        attempt=1,
                        started_at=operation_started,
                        duration_ms=(time.monotonic() - operation_clock) * 1000,
                        outcome="error",
                        error_kind=error.kind,
                    )
                    store.fail_task(run_id, task["id"], epoch, error)
                    store.record_risk(run_id, operation="search", error=error)
                    raise error
                finally:
                    await adapter.close()
                    request_count = store.get_run(run_id)["requests"]
                    if store.get_run(run_id)["status"] == "running":
                        store.finish(
                            run_id,
                            epoch,
                            elapsed=time.monotonic() - operation_clock,
                            reason=error.kind if error else "recovery_check_complete",
                            outcome=error.outcome if error else None,
                        )
                result = {
                    "message": "低负载恢复检查成功",
                    "state": "reachable",
                    "sample_count": len(page.items),
                    "login_state": login_state,
                    "account_nickname": identity.get("nickname"),
                    "account_user_id": identity.get("user_id"),
                    "login_message": "已确认登录身份",
                    "request_count": request_count,
                }
                if probe_id:
                    store.finish_recovery_probe(
                        probe_id, success=True, request_count=request_count
                    )
                else:
                    store.finish_recovery_canary(account_id, success=True)
                return result
            except CollectionError as exc:
                request_count = (
                    store.get_run(run_id)["requests"] if run_id else request_count
                )
                if store.get_run(run_id)["status"] == "pending":
                    store.record_risk(run_id, operation=None, error=exc)
                    store.block_pending_run(run_id, exc)
                if probe_id:
                    active = next(
                        (
                            row
                            for row in store.list_recovery_probes(account_id=account_id)
                            if row["id"] == probe_id and row["status"] != "completed"
                        ),
                        None,
                    )
                    if active:
                        store.finish_recovery_probe(
                            probe_id,
                            success=False,
                            request_count=min(3, request_count),
                            error_kind=exc.kind,
                        )
                elif store.get_account(account_id)["status"] == "canary_running":
                    store.finish_recovery_canary(
                        account_id, success=False, error_kind=exc.kind
                    )
                raise
            except Exception:
                if probe_id:
                    active = next(
                        (
                            row
                            for row in store.list_recovery_probes(account_id=account_id)
                            if row["id"] == probe_id and row["status"] != "completed"
                        ),
                        None,
                    )
                    if active:
                        store.finish_recovery_probe(
                            probe_id,
                            success=False,
                            request_count=min(3, request_count),
                            error_kind="internal_error",
                        )
                elif store.get_account(account_id)["status"] == "canary_running":
                    store.finish_recovery_canary(
                        account_id, success=False, error_kind="internal_error"
                    )
                raise

    def dashboard(self, query=None):
        query = query or {}
        jobs = self.diagnostic_jobs()
        window = date_window(query)
        page = max(1, int(param(query, "page", 1)))
        run_source = (
            runs.outerjoin(run_contexts, run_contexts.c.run_id == runs.c.id)
            .outerjoin(accounts, accounts.c.id == run_contexts.c.account_id)
            .outerjoin(schedules, schedules.c.id == run_contexts.c.schedule_id)
        )
        collected_item = task_items.join(tasks, task_items.c.task_id == tasks.c.id)
        post_count = (
            select(func.count(func.distinct(task_items.c.item_id)))
            .select_from(collected_item)
            .where(
                tasks.c.run_id == runs.c.id,
                tasks.c.operation.in_(["search", "detail"]),
            )
            .correlate(runs)
            .scalar_subquery()
        )
        comment_count = (
            select(func.count(func.distinct(task_items.c.item_id)))
            .select_from(collected_item)
            .where(
                tasks.c.run_id == runs.c.id,
                tasks.c.operation.in_(["comments", "replies"]),
            )
            .correlate(runs)
            .scalar_subquery()
        )
        statement = select(
            runs.c.id,
            runs.c.platform,
            runs.c.mode,
            runs.c.status,
            runs.c.config,
            runs.c.requests,
            runs.c.elapsed,
            runs.c.cancel_requested,
            runs.c.created_at,
            run_contexts.c.account_id,
            accounts.c.name.label("account_name"),
            run_contexts.c.schedule_id,
            schedules.c.name.label("schedule_name"),
            run_contexts.c.scheduled_for,
            post_count.label("post_count"),
            comment_count.label("comment_count"),
        ).select_from(run_source)
        for key in ("platform", "status", "mode"):
            if param(query, key):
                statement = statement.where(runs.c[key] == param(query, key))
        if param(query, "source_type"):
            statement = statement.where(
                func.coalesce(runs.c.config["source_type"].as_string(), "keyword")
                == param(query, "source_type")
            )
        if param(query, "q").strip():
            pattern = contains_pattern(param(query, "q").strip())
            statement = statement.where(
                or_(
                    runs.c.id.ilike(pattern, escape="\\"),
                    select(tasks.c.id)
                    .where(tasks.c.run_id == runs.c.id, or_(tasks.c.keyword.ilike(pattern, escape="\\"), tasks.c.content_id.ilike(pattern, escape="\\")))
                    .exists(),
                    accounts.c.name.ilike(pattern, escape="\\"),
                    schedules.c.name.ilike(pattern, escape="\\"),
                )
            )
        if param(query, "time_field", "executed_at") == "created_at":
            statement = statement.where(*in_window(runs.c.created_at, window))
        elif window != (None, None):
            statement = statement.where(
                select(events.c.id)
                .where(
                    events.c.run_id == runs.c.id,
                    events.c.kind == "attempt_started",
                    *in_window(events.c.at, window),
                )
                .exists()
            )
        with self.store() as store, store.engine.connect() as conn:
            total = conn.scalar(select(func.count()).select_from(statement.subquery()))
            rows = [
                dict(r)
                for r in conn.execute(
                    statement.order_by(runs.c.created_at.desc(), runs.c.id)
                    .limit(20)
                    .offset((page - 1) * 20)
                ).mappings()
            ]
            stats = {
                "runs": conn.scalar(select(func.count()).select_from(runs)),
                "contents": conn.scalar(select(func.count()).select_from(contents)),
                "comments": conn.scalar(select(func.count()).select_from(comments)),
                "running": conn.scalar(
                    select(func.count()).select_from(runs).where(runs.c.status == "running")
                ),
            }
            active_runs = {
                j["run_id"]
                for j in jobs
                if j["status"] in {"queued", "running"} and j.get("run_id")
            }
            for row in rows:
                row["config"] = redact(row["config"])
                row["interrupted"] = False
                if row["status"] in {"running", "pending"} and row["id"] not in active_runs:
                    try:
                        with exclusive(["run:" + row["id"]]):
                            row["interrupted"] = True
                    except CollectionError:
                        pass
            return {
                "runs": rows,
                "stats": stats,
                "jobs": jobs,
                "total": total,
                "page": page,
                "page_size": 20,
            }

    def results(self, query):
        platform = query.get("platform", [""])[0]
        keyword = query.get("q", [""])[0].strip()
        run_id = query.get("run_id", [""])[0]
        page = max(1, int(query.get("page", [1])[0]))
        observed = select(tasks.c.run_id).select_from(task_items.join(tasks, task_items.c.task_id == tasks.c.id)).where(
            tasks.c.operation.in_(["search", "detail"]),
            task_items.c.item_id == contents.c.id,
        ).correlate(contents)
        legacy_hit = select(hits.c.run_id).where(
            hits.c.content_id == contents.c.id,
        ).correlate(contents)
        mode = (
            select(runs.c.mode)
            .where(runs.c.platform == contents.c.platform, runs.c.id.in_(observed.union(legacy_hit)))
            .order_by(runs.c.created_at.desc())
            .limit(1)
            .correlate(contents)
            .scalar_subquery()
        )
        time_columns = [
            content_times.c.published_at,
            content_times.c.source_updated_at,
            content_times.c.first_collected_at,
        ]
        statement = select(contents, *time_columns, mode.label("source_mode")).outerjoin(
            content_times,
            (contents.c.platform == content_times.c.platform)
            & (contents.c.id == content_times.c.id),
        )
        time_field = param(query, "time_field", "published_at")
        time_fields = {
            "published_at": content_times.c.published_at,
            "source_updated_at": content_times.c.source_updated_at,
            "first_collected_at": content_times.c.first_collected_at,
            "collected_at": contents.c.observed_at,
        }
        if time_field not in time_fields:
            raise ConsoleError("未知时间维度")
        statement = statement.where(*in_window(time_fields[time_field], date_window(query)))
        if param(query, "time_known") == "unknown":
            statement = statement.where(time_fields[time_field].is_(None))
        if param(query, "time_known") == "known":
            statement = statement.where(time_fields[time_field].is_not(None))
        if param(query, "mode"):
            statement = statement.where(mode == param(query, "mode"))
        if param(query, "author").strip():
            pattern = contains_pattern(param(query, "author").strip())
            statement = statement.where(
                or_(
                    contents.c.data["author_name"].as_string().ilike(pattern, escape="\\"),
                    contents.c.data["author_id"].as_string().ilike(pattern, escape="\\"),
                )
            )
        if param(query, "detail") in {"complete", "partial"}:
            detail = contents.c.data["detail_complete"].as_boolean()
            statement = statement.where(
                detail.is_(True)
                if param(query, "detail") == "complete"
                else or_(detail.is_(False), detail.is_(None))
            )
        sort = param(query, "sort", "collected_at")
        if sort not in time_fields:
            raise ConsoleError("未知排序方式")
        order = time_fields[sort].desc().nulls_last()
        if platform:
            statement = statement.where(contents.c.platform == platform)
        if run_id:
            statement = statement.where(
                or_(
                    contents.c.id.in_(select(hits.c.content_id).where(hits.c.run_id == run_id)),
                    contents.c.id.in_(select(task_items.c.item_id).select_from(task_items.join(tasks, task_items.c.task_id == tasks.c.id)).where(
                        tasks.c.run_id == run_id, tasks.c.operation.in_(["search", "detail"]),
                    )),
                ),
                contents.c.platform
                == select(runs.c.platform).where(runs.c.id == run_id).scalar_subquery(),
            )
        if keyword:
            pattern = (
                "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            statement = statement.where(
                or_(
                    *[
                        contents.c.data[key].as_string().ilike(pattern, escape="\\")
                        for key in ("title", "text", "author_name")
                    ]
                )
            )
        with self.store() as store, store.engine.connect() as conn:
            total = conn.scalar(select(func.count()).select_from(statement.subquery()))
            # Apply OFFSET before resolving each item's collection provenance.
            # Otherwise a deep page evaluates that correlated lookup for every
            # discarded row, which can exceed the database statement timeout.
            page_items = (
                statement.with_only_columns(contents.c.platform, contents.c.id)
                .order_by(order, contents.c.platform, contents.c.id)
                .limit(24).offset((page - 1) * 24).subquery()
            )
            page_statement = (
                select(contents, *time_columns, mode.label("source_mode"))
                .join(page_items, (contents.c.platform == page_items.c.platform)
                      & (contents.c.id == page_items.c.id))
                .outerjoin(content_times, (contents.c.platform == content_times.c.platform)
                           & (contents.c.id == content_times.c.id))
                .order_by(order, contents.c.platform, contents.c.id)
            )
            rows = [
                dict(r)
                for r in conn.execute(page_statement).mappings()
            ]
        return {"items": redact(rows), "total": total, "page": page, "page_size": 24}

    def detail(self, platform, item_id):
        with self.store() as store, store.engine.connect() as conn:
            item = (
                conn.execute(
                    select(
                        contents,
                        content_times.c.published_at,
                        content_times.c.source_updated_at,
                        content_times.c.first_collected_at,
                    )
                    .outerjoin(
                        content_times,
                        (contents.c.platform == content_times.c.platform)
                        & (contents.c.id == content_times.c.id),
                    )
                    .where(contents.c.platform == platform, contents.c.id == item_id)
                )
                .mappings()
                .one_or_none()
            )
            if item is None:
                raise ConsoleError("内容不存在")
            rows = [
                dict(r)
                for r in conn.execute(
                    select(comments)
                    .where(comments.c.platform == platform, comments.c.content_id == item_id)
                    .order_by(comments.c.observed_at)
                    .limit(500)
                ).mappings()
            ]
            return redact({"item": dict(item), "comments": rows})

    def media_asset(self, platform, item_id, media_index):
        if platform not in PLATFORMS:
            raise ConsoleError("未知平台")
        try:
            media_index = int(media_index)
        except (TypeError, ValueError):
            raise ConsoleError("媒体编号无效") from None
        with self.store() as store, store.engine.connect() as conn:
            data = conn.scalar(
                select(contents.c.data).where(
                    contents.c.platform == platform, contents.c.id == item_id
                )
            )
        if not data:
            raise ConsoleError("内容不存在")
        asset = next(
            (
                row
                for row in data.get("media_downloads", [])
                if row.get("status") == "downloaded" and row.get("media_index") == media_index
            ),
            None,
        )
        if not asset or not asset.get("path"):
            raise ConsoleError("本地媒体不存在")
        relative = Path(asset["path"])
        path = (self.artifact_root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(self.artifact_root) or not path.is_file():
            raise ConsoleError("本地媒体不存在")
        content_type = asset.get("content_type") or mimetypes.guess_type(path.name)[0]
        return path, content_type or "application/octet-stream"


async def douyin_identity(adapter, budget, *, include_profile=False):
    """Verify only a self-profile response, never a public profile or cached cookie."""

    def result(state, profile=None):
        return (state, profile or {}) if include_profile else state

    # Identity verification is part of the bounded recovery search. The quota
    # model intentionally exposes only the four collection operations, so count
    # this request against search instead of requiring an impossible fifth
    # "identity" policy.
    await budget.admit("search")
    params = adapter.parameters(TaskRequest(operation="search", keyword="咖啡"))
    try:
        signed = await adapter.security.sign("/aweme/v1/web/user/profile/self/", params)
        response = await adapter.client.get(
            signed.url,
            headers={
                "Cookie": signed.cookie_header,
                "Referer": "https://www.douyin.com/",
            }
            | signed.headers,
            timeout=15,
            allow_redirects=False,
            verify=True,
        )
        payload = response.json()
        check_response(response.status_code, payload)
        if not isinstance(payload, dict) or payload.get("status_code") != 0:
            return result("unverified")
        expected = getattr(adapter.session.config, "expected_user_id", None)
        profile = identity_from_payload("douyin", payload, expected)
        return result("verified", profile)
    except CollectionError as exc:
        if exc.kind in {"identity_mismatch", "verification_required", "access_denied", "rate_limit"}:
            raise
        return result("expired" if exc.kind == "auth_expired" else "unverified")
    except Exception:
        return result("unverified")



class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def app(self):
        return self.server.app

    def respond(
        self, status, data, content_type="application/json; charset=utf-8", attachment=None,
        headers=None,
    ):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: http: data:; media-src https: http:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(attachment, safe=''))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def respond_file(self, path, content_type):
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        status = 200
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            value = range_header.removeprefix("bytes=")
            try:
                first, last = value.split("-", 1)
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    length = int(last)
                    start = max(0, size - length)
                if start < 0 or start > end or start >= size:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self):
        host = self.headers.get("Host", "")
        try:
            hostname = urlsplit("//" + host).hostname
            loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except (TypeError, ValueError):
            loopback = False
        allowed_hosts = {
            value.strip().lower()
            for value in os.environ.get("CRAWLER_WEB_ALLOWED_HOSTS", "").split(",")
            if value.strip()
        }
        if not loopback and str(hostname or "").lower() not in allowed_hosts:
            self.respond(403, {"error": "仅允许本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self.respond(403, {"error": "跨站请求已拒绝"})
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if path.startswith('/browser/'):
                parts = self.path.split('/', 3)
                session = self.app.browser_session(parts[2]) if len(parts) == 4 else None
                if session is None:
                    self.respond(404, {'error': '浏览器窗口不存在'})
                else:
                    proxy_browser(self, session, '/' + parts[3])
            elif path == "/api/health":
                self.respond(200, self.app.runtime_status())
            elif path == "/api/bootstrap":
                self.respond(200, {
                    "token": self.app.token,
                    "settings": self.app.public_settings(include_credentials=False),
                })
            elif path == "/api/browser-profiles":
                self.respond(
                    200,
                    self.app.browser_profiles(
                        param(query, "provider"),
                        platform=param(query, "platform", "xhs"),
                        account_id=param(query, "account_id") or None,
                    ),
                )
            elif path.startswith("/api/accounts/") and len(path.strip("/").split("/")) == 3:
                self.respond(200, self.app.account_detail(path.rsplit("/", 1)[1]))
            elif path == "/api/proxies":
                self.respond(200, {"items": self.app.list_proxies()})
            elif path.startswith("/api/proxies/") and len(path.strip("/").split("/")) == 3:
                self.respond(200, self.app.proxy_detail(path.rsplit("/", 1)[1]))
            elif path == "/api/dashboard":
                self.respond(200, self.app.dashboard(query))
            elif path == "/api/jobs":
                self.respond(200, {"jobs": self.app.diagnostic_jobs()})
            elif path == "/api/risk":
                self.respond(200, self.app.risk_report(query))
            elif path == "/api/results":
                self.respond(200, self.app.results(query))
            elif path == "/api/content":
                self.respond(200, self.app.detail(query["platform"][0], query["id"][0]))
            elif path == "/api/analysis/sessions":
                self.respond(200, self.app.analysis.list())
            elif path == "/api/analysis/settings":
                self.respond(200, self.app.analysis.settings.public())
            elif path.startswith("/api/analysis/attachments/"):
                parts = path.strip("/").split("/")
                if len(parts) != 5:
                    raise AnalysisError("附件地址无效")
                meta, file = self.app.analysis.attachment(parts[3], parts[4])
                self.respond(200, file.read_bytes(),
                             meta["media_type"] if meta["is_image"] else "application/octet-stream",
                             attachment=meta["name"] if not meta["is_image"] or param(query, "download") == "1" else None)
            elif path == "/api/analysis/evidence":
                self.respond(200, self.app.analysis.evidence(
                    param(query, "session_id"), param(query, "citation")
                ))
            elif path.startswith("/api/analysis/sessions/"):
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[4] == "export":
                    self.respond(200, self.app.analysis.export(parts[3]).encode(),
                                 "text/markdown; charset=utf-8", attachment="analysis-" + parts[3] + ".md")
                elif len(parts) == 4:
                    self.respond(200, self.app.analysis.get(parts[3]))
                else:
                    raise AnalysisError("无效会话地址")
            elif path == "/api/media":
                file, content_type = self.app.media_asset(
                    query["platform"][0], query["id"][0], query["index"][0]
                )
                self.respond_file(file, content_type)
            elif path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                run_id = parts[2]
                with self.app.store() as store:
                    snapshot = store.snapshot(run_id)
                    if len(parts) == 4 and parts[3] == "export":
                        self.respond(200, redact(snapshot), attachment=f"collection-{run_id}.json")
                    else:
                        # Do not expose task cursor inputs/session-bound search tokens.
                        safe_tasks = [
                            {k: t[k] for k in ("id", "operation", "keyword", "content_id", "root_id", "status", "target")}
                            | {
                                "count": t["state"]["count"],
                                "stop_reason": t["state"].get("stop_reason"),
                            }
                            for t in snapshot["tasks"]
                        ]
                        viewer = self.app.collection_browsers.get(run_id)
                        browser_viewer = (
                            {
                                "account_id": viewer.account_id,
                                "state": viewer.state,
                                "transport": viewer.public()["transport"],
                            }
                            if viewer and viewer.active
                            else None
                        )
                        self.respond(
                            200,
                            redact(
                                {
                                    "tasks": safe_tasks,
                                    "post_sources": snapshot["post_sources"],
                                    "events": snapshot["events"][-60:],
                                    "metrics": snapshot["metrics"][-100:],
                                    "risk_events": snapshot["risk_events"][-60:],
                                    "network_observations": snapshot[
                                        "network_observations"
                                    ][-20:],
                                    "quota_reservations": snapshot[
                                        "quota_reservations"
                                    ][-100:],
                                    "context": snapshot["context"],
                                    "browser_viewer": browser_viewer,
                                    "run": {
                                        k: snapshot["run"][k]
                                        for k in (
                                            "id",
                                            "platform",
                                            "status",
                                            "mode",
                                            "config",
                                            "requests",
                                            "elapsed",
                                            "created_at",
                                        )
                                    },
                                }
                            ),
                        )
            elif path in {"/", "/account-browser.html", "/account-browser.js", "/account-browser.css", "/app.js", "/controls.js", "/console-lists.js", "/style.css", "/favicon.svg",
                          "/vendor/marked.umd.js", "/vendor/purify.min.js", "/analysis-render.js"}:
                file = STATIC / ("index.html" if path == "/" else path[1:])
                self.respond(200, file.read_bytes(), mimetypes.guess_type(file)[0] or "text/plain")
            else:
                self.respond(404, {"error": "页面不存在"})
        except Exception as exc:
            self.respond(400, {"error": error_message(exc)})

    def do_POST(self):
        if not self.trusted():
            return
        if not secrets.compare_digest(self.headers.get("X-Console-Token", ""), self.app.token):
            self.respond(403, {"error": "请刷新页面后重试"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            path = urlsplit(self.path).path
            max_size = 12 * 1024 * 1024 if path.endswith("/attachments") and path.startswith("/api/analysis/sessions/") else 1024 * 1024
            if not 0 < size <= max_size:
                raise ConsoleError("请求大小无效")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ConsoleError("请求格式无效")
            path = urlsplit(self.path).path
            if path == "/api/settings":
                result = self.app.save(body)
            elif path == "/api/check":
                result = self.app.check(body)
            elif path == "/api/runs":
                result = self.app.collect(body)
            elif path == "/api/analysis/sessions":
                result = self.app.analysis.create(body.get("scope", {}))
            elif path == "/api/analysis/settings":
                result = self.app.analysis.settings.save(body)
            elif path == "/api/analysis/settings/check":
                result = self.app.analysis.check_settings(body)
            elif path.startswith("/api/analysis/sessions/"):
                parts = path.strip("/").split("/")
                if len(parts) != 5 or parts[4] not in {"messages", "stop", "attachments", "rename"}:
                    raise ConsoleError("无效分析操作")
                if parts[4] == "messages":
                    result = self.app.analysis.send(parts[3], body.get("message"), body.get("attachments", []))
                elif parts[4] == "attachments":
                    result = self.app.analysis.upload(parts[3], body)
                elif parts[4] == "rename":
                    result = self.app.analysis.rename(parts[3], body.get("title"))
                else:
                    result = self.app.analysis.stop(parts[3])
            elif path == "/api/accounts":
                result = self.app.add_account(body)
            elif path == "/api/proxies":
                result = self.app.save_proxy(body)
            elif path.startswith("/api/proxies/") and len(path.strip("/").split("/")) == 3:
                result = self.app.save_proxy(body, path.rsplit("/", 1)[1])
            elif path == "/api/schedules":
                result = self.app.create_schedule(body)
            elif path == "/api/quota-policies":
                result = self.app.create_quota_policy(body)
            elif path == "/api/account-maintenance":
                result = self.app.account_maintenance(body)
            elif path.startswith("/api/accounts/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    result = self.app.update_account(parts[2], body)
                elif len(parts) == 4 and parts[3] == "browser":
                    result = (self.app.open_account_browser(
                        parts[2], auto_save=body.get("auto_save") is True
                    ) if body.get("kind") == "open"
                              else self.app.account_browser_command(parts[2], body))
                    session = self.app.browser_session(result["id"])
                    if session is None:
                        raise ConsoleError("浏览器窗口已关闭，请从账号列表重新打开")
                    headers = {'Set-Cookie': f'crawler_browser={session.access_token}; Path=/browser/{session.id}/; HttpOnly; SameSite=Strict'} if body.get('kind') == 'open' else None
                    self.respond(200, result, headers=headers)
                    return
                elif len(parts) == 4 and parts[3] == "copy":
                    result = self.app.copy_account(parts[2])
                elif len(parts) == 4 and parts[3] == "status":
                    account = self.app.set_account_state(parts[2], body)
                    result = {
                        key: account[key]
                        for key in (
                            "id",
                            "platform",
                            "name",
                            "status",
                            "cooldown_until",
                        )
                    }
                else:
                    raise ConsoleError("无效账号操作")
            elif path.startswith("/api/schedules/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    result = self.app.update_schedule(parts[2], body)
                elif len(parts) == 4 and parts[3] == "enabled":
                    result = self.app.set_schedule_enabled(parts[2], body)
                else:
                    raise ConsoleError("无效计划操作")
            elif path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ConsoleError("无效操作")
                if parts[3] == "cancel":
                    with self.app.store() as store:
                        store.request_cancel(parts[2])
                    result = {"message": "已请求停止；当前请求结束后保存进度"}
                elif parts[3] == "resume":
                    result = self.app.collect(body, resume_id=parts[2])
                else:
                    raise ConsoleError("未知操作")
            else:
                self.respond(404, {"error": "接口不存在"})
                return
            self.respond(200, result)
        except Exception as exc:
            self.respond(400, {"error": error_message(exc)})

    def do_DELETE(self):
        if not self.trusted():
            return
        if not secrets.compare_digest(self.headers.get("X-Console-Token", ""), self.app.token):
            self.respond(403, {"error": "请刷新页面后重试"})
            return
        try:
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "api" or parts[1] not in {"schedules", "accounts", "proxies"}:
                self.respond(404, {"error": "接口不存在"})
                return
            handlers = {"accounts": self.app.delete_account, "schedules": self.app.delete_schedule,
                        "proxies": self.app.delete_proxy}
            self.respond(200, handlers[parts[1]](parts[2]))
        except Exception as exc:
            self.respond(400, {"error": error_message(exc)})


def main(argv=None):
    from social_crawler.config import load_config

    load_config()
    parser = argparse.ArgumentParser(description="Social Crawler 本地 Web 控制台")
    parser.add_argument("--host", default=os.environ.get("CRAWLER_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRAWLER_WEB_PORT", "8765")))
    parser.add_argument(
        "--workers", type=int, default=int(os.environ.get("CRAWLER_WORKERS", "1"))
    )
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CRAWLER_ROOT", ".")))
    args = parser.parse_args(argv)
    app = Console(args.root, worker_count=args.workers)
    try:
        upgrade_database(app.settings["database_url"])
    except SQLAlchemyError:
        app.worker_pool.shutdown()
        parser.exit(1, "数据库连接或初始化失败，请检查数据库服务与 CRAWLER_DATABASE_URL。\n"
                    "使用 scripts/start_local.sh 自动启动本地 PostgreSQL 时，请将该变量留空。\n")
    # Resolve relative database paths exactly as the CLI does from the project root.
    os.chdir(app.root)
    with exclusive(["web:" + str(app.root)]):
        app.start_scheduler()
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.app = app
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def stop_on_sigterm(_signum, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop_on_sigterm)
        print(
            f"Social Crawler → http://{args.host}:{args.port} "
            f"({args.workers} collection workers)",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            app.stop_scheduler()
            app.analysis.shutdown()
            app.close_account_browsers()
            with app.lock:
                for job in app.jobs.values():
                    if job["status"] in {"queued", "running"} and job.get("run_id"):
                        with app.store() as store:
                            store.request_cancel(job["run_id"])
            for thread in app.threads:
                thread.join(timeout=5)
            app.worker_pool.shutdown(wait_seconds=5)
            server.server_close()


if __name__ == "__main__":
    main()
