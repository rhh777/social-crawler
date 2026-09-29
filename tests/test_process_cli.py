import json
import os
import signal
import subprocess
import sys

import pytest

from social_crawler.domain.models import Item, PageResult, RunConfig
from social_crawler.domain.quality import assess
from social_crawler.interfaces.cli import main
from social_crawler.orchestration.report import export_run

KILLED_WORKER = """
import os,signal
from sqlalchemy import create_engine
from social_crawler.domain.models import Item,PageResult
from social_crawler.storage.store import Store
store=Store(os.environ['PROCESS_TEST_DB'])
if schema:=os.environ.get('PROCESS_TEST_SCHEMA'):
    store.engine.dispose()
    store.engine=create_engine(os.environ['PROCESS_TEST_DB'],connect_args={'options':f'-csearch_path={schema}'})
run_id=os.environ['PROCESS_TEST_RUN']
epoch=store.get_run(run_id)['epoch']
task=store.next_task(run_id,epoch)
def kill(at):
    if at==os.environ['PROCESS_TEST_STAGE']:
        os.kill(os.getpid(),signal.SIGKILL)
store.commit_page(run_id,task['id'],epoch,PageResult(items=[Item(id='n',content_id='n',kind='content')],next_context={'page':2},response_has_more=True),fault=kill)
"""


@pytest.mark.parametrize("stage,expected", [("before_commit", 0), ("after_commit", 1)])
def test_actual_killed_process_recovers_database(store, tmp_path, stage, expected):
    config = RunConfig(platform="xhs", keywords=["one"], content_limit=3, min_interval=0)
    run_id = store.create_run(config, mode="offline")
    store.start(run_id, binding="offline")
    script = tmp_path / "kill_worker.py"
    script.write_text(KILLED_WORKER)
    env = os.environ | {
        "PROCESS_TEST_DB": store.engine.url.render_as_string(hide_password=False),
        "PROCESS_TEST_SCHEMA": getattr(store, "test_schema", ""),
        "PROCESS_TEST_RUN": run_id,
        "PROCESS_TEST_STAGE": stage,
    }
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=10)
    assert result.returncode == -signal.SIGKILL
    snapshot = store.snapshot(run_id)
    assert len(snapshot["items"]) == len(snapshot["hits"]) == expected
    assert snapshot["tasks"][0]["state"]["context"] == ({"page": 2} if expected else {})
    epoch = store.start(run_id, binding="offline", resume=True)
    assert epoch == 2 and store.get_run(run_id)["elapsed"] > 0
    assert store.next_task(run_id, epoch)["state"]["count"] == expected


def test_partial_detail_can_be_completed_on_resume_without_duplicates(store, tmp_path):
    config = RunConfig(platform="xhs", keywords=["one"], content_limit=1, comment_limit=0)
    run_id = store.create_run(config, mode="offline")
    epoch = store.start(run_id, binding="offline")
    search = store.next_task(run_id, epoch)
    incomplete = assess(
        Item(id="n", content_id="n", kind="content", data={"text": "body"}), detail=True
    )
    store.commit_page(
        run_id, search["id"], epoch, PageResult(items=[incomplete], response_has_more=False)
    )
    detail = store.next_task(run_id, epoch)
    result = store.commit_page(
        run_id,
        detail["id"],
        epoch,
        PageResult(items=[incomplete], response_has_more=False, stop_reason="incomplete_fields"),
    )
    assert result["status"] == "partial"
    store.finish(run_id, epoch, elapsed=1)
    assert export_run(store, run_id, tmp_path)["field_quality_gaps"] == 1
    epoch = store.start(run_id, binding="offline", resume=True)
    detail = store.next_task(run_id, epoch)
    complete = assess(
        Item(
            id="n",
            content_id="n",
            kind="content",
            data={"text": "body", "author_id": "a", "published_at": 1700000000000},
        ),
        detail=True,
    )
    result = store.commit_page(
        run_id, detail["id"], epoch, PageResult(items=[complete], response_has_more=False)
    )
    assert result["status"] == "completed"
    assert result["state"]["count"] == 1
    store.finish(run_id, epoch, elapsed=1)
    report = export_run(store, run_id, tmp_path)
    assert report["field_quality_gaps"] == 0
    assert report["automated_scope_gate"] is False  # comments / replies never exercised
    assert len(store.snapshot(run_id)["items"]) == 2  # search + detail observations
    assert (
        json.loads((tmp_path / "contents.json").read_text())[0]["data"]["published_at"]
        == 1700000000
    )


def test_cli_has_no_demo_command_and_requires_explicit_online(capsys):
    from social_crawler.interfaces.cli import parser

    assert "demo" not in parser().format_help()
    with pytest.raises(SystemExit) as error:
        main(["demo"])
    assert error.value.code == 2
    assert main(["run", "--config", "does-not-exist.toml"]) == 2
    assert "--online" in capsys.readouterr().err


def test_cli_rejects_sqlite_for_collection(tmp_path, capsys):
    assert main([
        "--database-url", f"sqlite:///{tmp_path / 'test.db'}",
        "run", "--config", "does-not-exist.toml", "--online",
    ]) == 2
    assert "PostgreSQL" in capsys.readouterr().err
