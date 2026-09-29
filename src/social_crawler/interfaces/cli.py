import argparse
import asyncio
import importlib.metadata
import json
import os
import signal
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from social_crawler import __version__
from social_crawler.adapters.douyin.http import DouyinHTTP
from social_crawler.adapters.samples import Samples
from social_crawler.adapters.xhs.browser import XHSBrowser
from social_crawler.adapters.xhs.http import XHSHTTP
from social_crawler.adapters.xhs.sites import is_xhs_api_adapter, is_xhs_platform
from social_crawler.config import load_config
from social_crawler.domain.models import CollectionError, RunConfig
from social_crawler.environments.browser_runtime import browser_runtime_identity
from social_crawler.environments.managed_browser import (
    is_managed_browser_provider,
    managed_browser_resource_key,
    managed_browser_transport,
)
from social_crawler.environments.network import observe_dual_exit, observe_http_transport_exit
from social_crawler.environments.session import (
    EnvironmentConfig,
    Session,
    exclusive,
    load_proxy,
)
from social_crawler.orchestration.report import export_run
from social_crawler.orchestration.worker import run_worker
from social_crawler.storage.store import Store


def parser():
    p = argparse.ArgumentParser(
        description="Collect platform data with a configured account. Requests require --online."
    )
    p.add_argument("--database-url", default=os.environ.get("CRAWLER_DATABASE_URL"))
    p.add_argument("--database-url-file", default="data/postgres.url")
    p.add_argument("--output", default="data/validation")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    create = sub.add_parser("run")
    create.add_argument("--config", required=True)
    create.add_argument(
        "--cookies",
        help="Local Cookie header or browser-cookie JSON; never pass credentials inline",
    )
    create.add_argument("--online", action="store_true")
    binding = sub.add_parser(
        "bind-network",
        help="Observe and explicitly accept an account's matching HTTP/browser exit",
    )
    binding.add_argument("--config", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument(
        "--config", help="Environment section is used; original task scope is preserved"
    )
    resume.add_argument("--cookies")
    resume.add_argument("--online", action="store_true")
    resume.add_argument("--extra-requests", type=int, default=0)
    resume.add_argument("--extra-seconds", type=int, default=0)
    for cmd in ("status", "cancel", "export"):
        action = sub.add_parser(cmd)
        action.add_argument("run_id")
    sub.add_parser(
        "doctor", help="Local dependency/config checks only; does not contact a platform"
    )
    return p


def versions():
    return {
        "social_crawler": __version__,
        "python": sys.version.split()[0],
        **{
            name: importlib.metadata.version(name)
            for name in ("playwright", "sqlalchemy", "curl-cffi", "httpx", "xhshow")
        },
        "xhs_browser_adapter": XHSBrowser.version,
        "xhs_api_adapter": XHSHTTP.version,
        "xhs_httpx_adapter": XHSHTTP.version,
        "xhs_curl_cffi_adapter": XHSHTTP.version,
        "xhs_adapter": XHSBrowser.version,
        "rednote_adapter": XHSBrowser.version,
        "douyin_adapter": DouyinHTTP.version,
    }


def cli_account_id(platform: str, account_ref: str) -> str:
    import hashlib

    value = hashlib.sha256(f"{platform}:{account_ref}".encode()).hexdigest()[:24]
    return "cli-" + value


def get_store(args):
    url = args.database_url
    if not url and Path(args.database_url_file).is_file():
        url = Path(args.database_url_file).read_text().strip()
    if not url:
        raise ValueError(
            "Configure CRAWLER_DATABASE_URL or start scripts/local_postgres.py first"
        )
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("The CLI requires PostgreSQL")
    return Store(url)


async def execute(
    store, run_id, session, *, resume=False, extra_requests=0, extra_seconds=0, output
):
    directory = Path(output) / run_id
    samples = Samples(directory)
    config = RunConfig.model_validate(store.get_run(run_id)["config"])
    platform = config.platform
    xhs_api = is_xhs_platform(platform) and is_xhs_api_adapter(config.adapter)
    online_adapter = XHSHTTP if xhs_api else XHSBrowser if is_xhs_platform(platform) else DouyinHTTP
    factory = (
        (lambda b: online_adapter(session, b, samples, transport_kind=config.adapter))
        if xhs_api
        else (lambda b: online_adapter(session, b, samples))
    )
    keys = [
        "run:" + run_id,
        "account:" + platform + ":" + session.config.account_ref,
    ]
    if not xhs_api:
        keys.append(
            managed_browser_resource_key(session.config)
            if is_managed_browser_provider(session.config.browser_provider)
            else "profile:" + str(Path(session.config.profile_dir).resolve())
        )
    with exclusive(keys):
        store.event(
            run_id,
            "environment_selected",
            {
                "account_ref": session.config.account_ref,
                "profile_dir": session.config.profile_dir
                if online_adapter is XHSBrowser
                else None,
                "session_version": session.config.session_version,
                "binding_version": session.config.binding_version,
                "user_agent": session.config.user_agent,
                "headless": session.config.headless,
                "browser_provider": session.config.browser_provider
                if online_adapter is XHSBrowser
                or is_managed_browser_provider(session.config.browser_provider)
                else None,
                "http_transport": config.adapter if xhs_api else "curl_cffi" if platform == "douyin" else None,
                "http_browser_preset": session.config.impersonate
                if platform == "douyin" or config.adapter == "curl_cffi"
                else None,
                "proxy_configured": bool(session.proxy)
                or is_managed_browser_provider(session.config.browser_provider),
                "proxy_source": (
                    "adspower_profile+project"
                    if platform == "douyin"
                    and session.config.browser_provider == "adspower"
                    else
                    managed_browser_transport(session.config)
                    if online_adapter is XHSBrowser
                    and is_managed_browser_provider(session.config.browser_provider)
                    else "project"
                    if session.proxy
                    else "direct"
                ),
                "binding_digest": session.binding,
                "adapter_version": online_adapter.version,
            },
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "output": str(directory),
                    "mode": "online",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: store.request_cancel(run_id))
        try:
            await run_worker(
                store,
                run_id,
                factory,
                session=session,
                resume=resume,
                reset_contexts=(
                    is_xhs_platform(platform) and config.adapter == "browser"
                ),
                extra_requests=extra_requests,
                extra_seconds=extra_seconds,
            )
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            report = export_run(store, run_id, directory)
    return report


def main(argv=None):
    config_file = load_config()
    args = parser().parse_args(argv)
    if args.command == "doctor":
        print(
            json.dumps(
                {
                    "configuration_file": str(config_file) if config_file else None,
                    "versions": versions(),
                    "chrome_identity": browser_runtime_identity(),
                    "postgres_url_file_present": Path(args.database_url_file).is_file(),
                    "online_requests": 0,
                    "note": "No Cookie files read and no platform requests made.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    store = None
    try:
        if args.command == "run" and not args.online:
            raise ValueError("Run requires explicit --online")
        store = get_store(args)
        store.initialize()
        if args.command == "init-db":
            print(json.dumps({"schema": "created", "dialect": store.engine.dialect.name}))
            return 0
        if args.command == "list":
            print(json.dumps(store.list_runs(args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "cancel":
            store.request_cancel(args.run_id)
            print(json.dumps({"run_id": args.run_id, "cancel_requested": True}))
            return 0
        if args.command in {"status", "export"}:
            report = export_run(store, args.run_id, Path(args.output) / args.run_id)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        env_data = {}
        if args.config:
            env_data = tomllib.loads(Path(args.config).read_text())
        env = EnvironmentConfig.model_validate(env_data.get("environment", {}))
        if args.command == "bind-network":
            config = RunConfig.model_validate(env_data.get("run", {}))
            configured_proxy = load_proxy(
                proxy_env=env.proxy_env, proxy_file=env.proxy_file, apply_relay=False
            )
            transport_proxy = load_proxy(proxy_env=env.proxy_env, proxy_file=env.proxy_file)
            observer = (
                observe_http_transport_exit
                if is_xhs_api_adapter(config.adapter)
                else observe_dual_exit
            )
            observation = asyncio.run(
                observer(
                    SimpleNamespace(
                        platform=config.platform,
                        proxy=transport_proxy,
                        configured_proxy=configured_proxy,
                        config=env,
                    )
                )
            )
            if observation["outcome"] != "success":
                print(
                    json.dumps(
                        {
                            "status": "blocked",
                            "error_kind": observation["error_kind"],
                            "proxy_ref": observation["proxy_ref"],
                        }
                    )
                )
                return 2
            account_id = cli_account_id(config.platform, env.account_ref)
            store.upsert_account(
                account_id,
                config.platform,
                env.account_ref,
                env.model_dump(),
            )
            store.confirm_account_network(
                account_id,
                proxy_ref=observation["proxy_ref"],
                egress_ip=observation["egress_ip"],
                ip_group=observation["ip_group"],
            )
            print(
                json.dumps(
                    {
                        "status": "confirmed",
                        "account_id": account_id,
                        "platform": config.platform,
                        "proxy_ref": observation["proxy_ref"],
                        "egress_ip": observation["egress_ip"],
                        "ip_group": observation["ip_group"],
                        "http_browser_match": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.cookies:
            env.cookie_file = str(Path(args.cookies).resolve())
        if args.command == "run":
            config = RunConfig.model_validate(env_data.get("run", {}))
            if config.headless is not None:
                env = env.model_copy(update={"headless": config.headless})
            session = Session(
                env,
                config.platform,
                profile_auth=(
                    is_managed_browser_provider(env.browser_provider)
                    and not is_xhs_api_adapter(config.adapter)
                ),
            )
            account_id = cli_account_id(config.platform, env.account_ref)
            store.upsert_account(
                account_id,
                config.platform,
                env.account_ref,
                env.model_dump(),
            )
            store.account_preflight(account_id)
            run_id = store.create_run(
                config,
                mode="online",
                binding=session.binding,
                versions=versions(),
                account_id=account_id,
                environment={"account_ref": env.account_ref},
            )
        else:
            run_id = args.run_id
            run = store.get_run(run_id)
            config = RunConfig.model_validate(run["config"])
            if run["mode"] != "online":
                raise ValueError("Only online runs can be resumed")
            if not args.online or not args.config:
                raise ValueError("Resume requires --online and --config with the environment")
            if config.headless is not None:
                env = env.model_copy(update={"headless": config.headless})
            session = Session(
                env,
                run["platform"],
                profile_auth=(
                    is_managed_browser_provider(env.browser_provider)
                    and not is_xhs_api_adapter(config.adapter)
                ),
            )
        report = asyncio.run(
            execute(
                store,
                run_id,
                session,
                resume=args.command == "resume",
                extra_requests=getattr(args, "extra_requests", 0),
                extra_seconds=getattr(args, "extra_seconds", 0),
                output=args.output,
            )
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "completed" else 2
    except ValidationError as exc:
        print(
            json.dumps(
                {
                    "error": "invalid_config",
                    "fields": [
                        {"location": error["loc"], "type": error["type"]}
                        for error in exc.errors(include_input=False, include_context=False)
                    ],
                }
            ),
            file=sys.stderr,
        )
        return 2
    except SQLAlchemyError as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Database operation failed; check local database setup",
                }
            ),
            file=sys.stderr,
        )
        return 2
    except (ValueError, OSError, CollectionError) as exc:
        from social_crawler.domain.redaction import redact

        print(
            json.dumps(
                {"error": type(exc).__name__, "message": redact(str(exc))}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
