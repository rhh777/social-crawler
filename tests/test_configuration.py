"""Configuration loading and launcher behavior without real services or credentials."""

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from social_crawler.config import load_config
from social_crawler.environments.session import EnvironmentConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    # Tests must not inherit the developer's .env or deployment settings.
    for key in list(os.environ):
        if key.startswith(("CRAWLER_", "ANTHROPIC_", "ADS_", "_CRAWLER_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    # Register restoration for keys added by load_config (which mutates os.environ).
    before = os.environ.copy()
    yield tmp_path
    os.environ.clear()
    os.environ.update(before)


def test_load_config_precedence_literals_and_no_shell_execution(isolated_config, monkeypatch):
    path = isolated_config / ".env"
    path.write_text(
        "CRAWLER_WORKERS=3\n"
        "export ADS_API_KEY='literal # ${HOME} $(touch leaked) `echo secret`'\n"
        "CRAWLER_AGENT_MODEL=from-file\n"
    )
    monkeypatch.setenv("CRAWLER_WORKERS", "5")
    monkeypatch.setenv("CRAWLER_AGENT_MODEL", "")
    assert load_config() == path
    assert os.environ["CRAWLER_WORKERS"] == "5"
    assert os.environ["CRAWLER_AGENT_MODEL"] == ""
    assert os.environ["ADS_API_KEY"] == 'literal # ${HOME} $(touch leaked) `echo secret`'
    assert not (isolated_config / "leaked").exists()


def test_missing_default_is_optional_explicit_file_is_required(isolated_config, monkeypatch):
    assert load_config() is None
    monkeypatch.setenv("CRAWLER_ENV_FILE", "missing.env")
    with pytest.raises(ValueError, match="CRAWLER_ENV_FILE does not exist"):
        load_config()


def test_explicit_file_replaces_default_and_errors_hide_secret(isolated_config, monkeypatch):
    (isolated_config / ".env").write_text("ADS_API_KEY=wrong-file\n")
    private = isolated_config / "private.env"
    private.write_text("ADS_API_KEY=selected-file\n")
    monkeypatch.setenv("CRAWLER_ENV_FILE", str(private))
    load_config()
    assert os.environ["ADS_API_KEY"] == "selected-file"
    private.write_text("ADS_API_KEY='never-log-this-secret\n")
    with pytest.raises(ValueError) as error:
        load_config()
    assert "never-log-this-secret" not in str(error.value)
    assert ":1" in str(error.value)


def test_adspower_environment_defaults_and_explicit_account_precedence(isolated_config):
    (isolated_config / ".env").write_text(
        "CRAWLER_ADSPOWER_API_URL=http://127.0.0.1:50326\n"
        "CRAWLER_ADSPOWER_START_TIMEOUT=120\n"
    )
    load_config()
    default = EnvironmentConfig()
    assert default.adspower_api_url == "http://127.0.0.1:50326"
    assert default.adspower_start_timeout == 120
    specific = EnvironmentConfig(adspower_api_url="http://localhost:50327", adspower_start_timeout=60)
    assert specific.adspower_api_url == "http://localhost:50327"
    assert specific.adspower_start_timeout == 60


def test_kameleo_environment_defaults_and_explicit_account_precedence(isolated_config):
    (isolated_config / ".env").write_text(
        "CRAWLER_KAMELEO_API_URL=http://127.0.0.1:5051\n"
        "CRAWLER_KAMELEO_START_TIMEOUT=120\n"
    )
    load_config()
    default = EnvironmentConfig()
    assert default.kameleo_api_url == "http://127.0.0.1:5051"
    assert default.kameleo_start_timeout == 120
    specific = EnvironmentConfig(
        kameleo_api_url="http://localhost:5052", kameleo_start_timeout=60
    )
    assert specific.kameleo_api_url == "http://localhost:5052"
    assert specific.kameleo_start_timeout == 60


@pytest.mark.parametrize("value", ["4", "901", "not-an-integer"])
def test_invalid_adspower_timeout_is_rejected(isolated_config, monkeypatch, value):
    monkeypatch.setenv("CRAWLER_ADSPOWER_START_TIMEOUT", value)
    with pytest.raises(ValueError):
        EnvironmentConfig()


@pytest.mark.parametrize("value", ["4", "901", "not-an-integer"])
def test_invalid_kameleo_timeout_is_rejected(isolated_config, monkeypatch, value):
    monkeypatch.setenv("CRAWLER_KAMELEO_START_TIMEOUT", value)
    with pytest.raises(ValueError):
        EnvironmentConfig()


def test_cli_reads_file_before_argument_defaults(isolated_config, monkeypatch):
    from social_crawler.interfaces import cli

    (isolated_config / ".env").write_text("CRAWLER_DATABASE_URL=sqlite:///configured.db\n")
    captured = []

    def stop(args):
        captured.append(args.database_url)
        raise RuntimeError("stop-before-database")

    monkeypatch.setattr(cli, "get_store", stop)
    with pytest.raises(RuntimeError, match="stop-before-database"):
        cli.main(["init-db"])
    with pytest.raises(RuntimeError, match="stop-before-database"):
        cli.main(["--database-url", "sqlite:///explicit.db", "init-db"])
    assert captured == ["sqlite:///configured.db", "sqlite:///explicit.db"]


def test_web_reads_file_before_argument_defaults(isolated_config, monkeypatch):
    from social_crawler.interfaces import web

    (isolated_config / ".env").write_text("CRAWLER_ROOT=custom-root\nCRAWLER_WORKERS=4\n")
    captured = []

    def stop(root, *, worker_count):
        captured.append((str(root), worker_count))
        raise RuntimeError("stop-before-database")

    monkeypatch.setattr(web, "Console", stop)
    with pytest.raises(RuntimeError, match="stop-before-database"):
        web.main([])
    with pytest.raises(RuntimeError, match="stop-before-database"):
        web.main(["--root", "explicit-root", "--workers", "2"])
    assert captured == [("custom-root", 4), ("explicit-root", 2)]


@pytest.mark.parametrize("external_database", [True, False, None])
def test_launcher_loads_env_and_uses_selected_database(isolated_config, external_database):
    bin_dir = isolated_config / "bin"
    bin_dir.mkdir()
    record = isolated_config / "launched.json"
    # Replace uv only; execute the actual config loader and bash launcher. Stub
    # database/Web processes, so this checks re-exec, paths and precedence safely.
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, signal, sys, time
args = sys.argv[1:]
if "--exec" in args:
    index = args.index("python")
    os.execv(sys.executable, [sys.executable, *args[index + 1:]])
if "social-crawler-web" in args:
    selected = {k: v for k, v in os.environ.items() if k.startswith("CRAWLER_")}
    pathlib.Path(os.environ["TEST_RECORD"]).write_text(json.dumps({"args": args, "env": selected}))
    sys.exit(0)
if any(value.endswith("local_postgres.py") for value in args):
    pathlib.Path(args[args.index("--url-file") + 1]).write_text("postgresql://generated-local/db")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(0.1)
raise SystemExit("Unexpected uv call")
''')
    fake_uv.chmod(0o700)
    config = isolated_config / "settings.env"
    config.write_text(
        "CRAWLER_WEB_PORT=8877\nCRAWLER_WORKERS=3\nCRAWLER_SKIP_SETUP=1\n"
        "CRAWLER_ROOT='runtime with spaces'\nCRAWLER_ARTIFACTS_DIR=custom-artifacts\n"
        + ("CRAWLER_DATABASE_URL=postgresql://external:private-password@host/db\n"
           if external_database else "CRAWLER_DATABASE_URL=\n" if external_database is None else "")
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/start_local.sh"), "--port", "8878"],
        cwd=isolated_config,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}",
                          "CRAWLER_ENV_FILE": "settings.env", "TEST_RECORD": str(record)},
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    launched = json.loads(record.read_text())
    args, env = launched["args"], launched["env"]
    assert args[args.index("--port") + 1] == "8878"
    assert args[args.index("--workers") + 1] == "3"
    assert env["CRAWLER_ARTIFACTS_DIR"] == "custom-artifacts"
    assert env["CRAWLER_ENV_FILE"] == str(config)
    assert "private-password" not in result.stdout + result.stderr
    runtime = isolated_config / "runtime with spaces"
    assert not (runtime / ".launcher.lock").exists()
    if external_database:
        assert env["CRAWLER_DATABASE_URL"].startswith("postgresql://external:")
        assert not (runtime / "data/postgres.url").exists()
    else:
        assert env["CRAWLER_DATABASE_URL"] == "postgresql://generated-local/db"
        assert (runtime / "data/postgres.url").is_file()


def load_secret_script():
    spec = importlib.util.spec_from_file_location("sync_secret", ROOT / "scripts/sync_adspower_secret.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_sync_sends_key_only_via_stdin(isolated_config, monkeypatch, capsys):
    script = load_secret_script()
    (isolated_config / ".env").write_text("ADS_API_KEY='private-test-key'\n")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    script.main(["--context", "test", "--namespace", "isolated"])
    args, kwargs = calls[0]
    assert "private-test-key" not in " ".join(args)
    manifest = json.loads(kwargs["input"])
    assert manifest["metadata"] == {"name": "adspower-api", "namespace": "isolated"}
    assert base64.b64decode(manifest["data"]["api-key"]).decode() == "private-test-key"
    assert "--server-side" in args
    assert "private-test-key" not in capsys.readouterr().out


def test_secret_sync_empty_key_never_contacts_cluster(isolated_config, monkeypatch):
    script = load_secret_script()
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: pytest.fail("contacted cluster"))
    with pytest.raises(SystemExit):
        script.main(["--context", "test"])


def test_secret_sync_failure_never_echoes_kubectl_body(isolated_config, monkeypatch):
    script = load_secret_script()
    monkeypatch.setenv("ADS_API_KEY", "private-test-key")
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stderr="request body: private-test-key",
    ))
    with pytest.raises(SystemExit) as error:
        script.main(["--context", "test"])
    assert "private-test-key" not in str(error.value)
