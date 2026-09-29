import csv
import json
from collections import Counter
from pathlib import Path

from social_crawler.domain.redaction import redact


def write_json(path, data):
    path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def export_run(store, run_id, directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = store.snapshot(run_id)
    run = snapshot["run"]
    tasks = snapshot["tasks"]
    collected = {"content": {}, "comment": {}}
    for row in snapshot["items"]:
        item = row["snapshot"]
        key = (item["content_id"], item["id"])
        existing = collected[item["kind"]].get(key)
        # Prefer detail observations over search cards when exporting the same content.
        if (
            existing
            and existing["data"].get("detail_observed")
            and not item["data"].get("detail_observed")
        ):
            continue
        collected[item["kind"]][key] = item
    coverage = [
        {
            "task_id": t["id"],
            "operation": t["operation"],
            "keyword": t["keyword"],
            "content_id": t["content_id"],
            "root_id": t["root_id"],
            "status": t["status"],
            "target": t["target"],
            "observed_unique": t["state"]["count"],
            "pages": t["state"]["pages"],
            "response_has_more": t["state"]["response_has_more"],
            "stop_reason": t["state"]["stop_reason"],
            "completeness": "scope_satisfied" if t["status"] == "completed" else "incomplete",
            "error": t["state"].get("error"),
            "samples": t["state"]["samples"],
            "skipped": t["state"]["skipped"],
        }
        for t in tasks
    ]
    operation_matrix = {}
    operations = ["search", "detail", "comments", "replies"]
    targeted = run["config"].get("source_type") == "posts"
    if targeted:
        operations = ["resolve_target", "detail", "comments", "replies"]
    for op in operations:
        rows = [r for r in coverage if r["operation"] == op and r["target"] > 0]
        operation_matrix[op] = {
            "tasks": len(rows),
            "states": dict(Counter(r["status"] for r in rows)),
            "observed_unique": sum(r["observed_unique"] for r in rows),
            "exercised": bool(rows) and any(r["pages"] for r in rows),
            "sample_observed": any(r["observed_unique"] > 0 for r in rows),
            "scope_satisfied": bool(rows) and all(r["status"] == "completed" for r in rows),
        }
    quality_gaps = [
        {
            "kind": kind,
            "id": item["id"],
            "content_id": item["content_id"],
            "missing_required": item["data"]
            .get("quality", {})
            .get("missing_required", ["unassessed"]),
        }
        for kind, records in collected.items()
        for item in records.values()
        if item["data"].get("quality", {}).get("missing_required", ["unassessed"])
    ]
    gate = not quality_gaps and all(
        v["exercised"] and v["scope_satisfied"] and v["sample_observed"]
        for v in operation_matrix.values()
    )
    if targeted:
        # Empty comment pages and disabled replies satisfy their declared scope.
        gate = (
            run["status"] == "completed" and not quality_gaps
            and bool(collected["content"])
            and all(t["status"] == "completed" for t in tasks)
            and all(s["status"] == "resolved" for s in snapshot["post_sources"])
        )
    report = {
        "run_id": run_id,
        "platform": run["platform"],
        "source_type": run["config"].get("source_type", "keyword"),
        "post_targets": len(snapshot["post_sources"]),
        "mode": run["mode"],
        "status": run["status"],
        "requests": run["requests"],
        "elapsed_seconds": run["elapsed"],
        "attempts": run["epoch"],
        "account_id": snapshot["context"].get("account_id"),
        "schedule_id": snapshot["context"].get("schedule_id"),
        "scheduled_for": snapshot["context"].get("scheduled_for"),
        "contents": len(collected["content"]),
        "comments_including_replies": len(collected["comment"]),
        "hits": len(snapshot["hits"]),
        "operation_matrix": operation_matrix,
        "automated_scope_gate": gate,
        "field_quality_gaps": len(quality_gaps),
        "human_review": "pending",
        "online_stability": "not_validated",
        "identity_check": "optional_expected_user_id; otherwise supplied_session_only",
        "notes": [
            "Synthetic results prove offline behavior only."
            if run["mode"] == "offline"
            else "Online scope results still require manual visible-sample comparison.",
            "Platform total comment counts are not completeness denominators.",
            "Local exclusive locks support one host; distributed leases are deferred.",
        ],
    }
    write_json(
        directory / "run-config.json", run["config"] | {"mode": run["mode"], "run_id": run_id}
    )
    write_json(directory / "versions.json", run["versions"] | {"binding_digest": run["binding"]})
    write_json(directory / "coverage.json", coverage)
    write_json(directory / "quality.json", quality_gaps)
    write_json(directory / "report.json", report)
    write_json(directory / "hits.json", snapshot["hits"])
    write_json(directory / "post-sources.json", snapshot["post_sources"])
    write_json(directory / "environment.json", snapshot["context"])
    write_json(directory / "operation-metrics.json", snapshot["metrics"])
    write_json(directory / "risk-events.json", snapshot["risk_events"])
    write_json(
        directory / "network-observation.json",
        snapshot.get("network_observations", []),
    )
    write_json(
        directory / "quota-reservations.json",
        snapshot.get("quota_reservations", []),
    )
    for kind in collected:
        name = "contents" if kind == "content" else "comments"
        rows = list(collected[kind].values())
        write_json(directory / (name + ".json"), rows)
        with (directory / (name + ".csv")).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["id", "content_id", "root_id", "parent_id", "data"]
            )
            writer.writeheader()
            for row in rows:
                values = {k: row.get(k) for k in writer.fieldnames}
                values["data"] = json.dumps(redact(values["data"]), ensure_ascii=False)
                writer.writerow(values)
        (directory / (name + ".csv")).chmod(0o600)
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(redact(r), ensure_ascii=False) + "\n" for r in snapshot["events"])
    )
    (directory / "events.jsonl").chmod(0o600)
    lines = [
        f"# 验证运行 {run_id}",
        "",
        f"平台：{run['platform']}；模式：{run['mode']}；状态：{run['status']}。",
        "",
        f"内容 {report['contents']} 条；评论（含回复）{report['comments_including_replies']} 条；关键词命中 {report['hits']} 条。",
        f"业务请求 {run['requests']} 次；运行 {run['elapsed']:.2f} 秒；尝试 {run['epoch']} 次。",
        "",
        "| 操作 | 任务数 | 实际条数 | 状态 |",
        "|---|---:|---:|---|",
    ]
    for op, row in operation_matrix.items():
        lines.append(f"| {op} | {row['tasks']} | {row['observed_unique']} | {row['states']} |")
    lines += [
        "",
        f"自动范围检查：{'通过' if gate else '未通过 / 未覆盖'}；人工核对：待执行。",
        "",
        ("这是历史测试数据，不代表平台内容。" if run["mode"] == "offline"
         else "本报告只覆盖本次采集范围，数量和停止原因见下方记录。"),
        "",
        "逐任务范围、停止原因与原生响应是否还有下一页见 coverage.json。",
        "",
        "## 人工核对清单",
        "",
        "- 记录账号、排序、时间与可见范围。",
        "- 每平台抽查 10 条内容，其中 5 条核对一级评论与选定父评论的回复。",
        "- 记录缺失 / 删除 / 不可见 / 预算终止，未知项保持未知。",
        "- 确认风险后停止、部分数据保留、重跑无重复业务记录。",
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines))
    return report
