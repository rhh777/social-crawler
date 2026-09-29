"""Read-only, scope-enforced queries. Never expose raw crawler payloads to the model."""

import base64
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import time
import uuid
import warnings
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import func, insert, or_, select

from social_crawler.storage.store import (
    analysis_sessions,
    analysis_sources,
    comments,
    contents,
    runs,
    task_items,
    tasks,
)


class AnalysisError(ValueError):
    """Safe user-facing analysis error."""


def safe_record(platform, item):
    data = item.get("data", {})
    fields = (
        "title",
        "text",
        "author_name",
        "published_at",
        "source_updated_at",
        "like_count",
        "detail_complete",
        "detail_observed",
        "observed_at",
    )
    clean = {k: data[k] for k in fields if isinstance(data.get(k), (str, int, float, bool))}
    clean["metrics"] = (
        {
            k: v
            for k, v in data.get("metrics", {}).items()
            if k
            in {
                "digg_count",
                "liked_count",
                "likedCount",
                "comment_count",
                "share_count",
                "collect_count",
                "collected_count",
            }
            and isinstance(v, (int, float))
        }
        if isinstance(data.get("metrics"), dict)
        else {}
    )
    kind = item["kind"]
    content_id = item["content_id"]
    item_id = item["id"]
    record = {
        "platform": platform,
        "kind": kind,
        "id": item_id,
        "content_id": content_id,
        "root_id": item.get("root_id"),
        "parent_id": item.get("parent_id"),
        "data": clean,
    }
    version = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]
    record["citation"] = f"{platform}/{kind}/{content_id}/{item_id}/v/{version}"
    return record


def create_session(conn, scope):
    if not isinstance(scope, dict) or scope.get("kind") not in {"all", "post", "run"}:
        raise AnalysisError("请选择帖子、采集任务或自由分析")
    kind = scope["kind"]
    mode = scope.get("mode", "readonly")
    if mode not in {"readonly", "workspace"}:
        raise AnalysisError("Agent 执行模式无效")
    scope = {"kind": kind, "mode": mode} | {
        k: str(scope.get(k, ""))
        for k in ({"post": ("platform", "content_id"), "run": ("run_id",)}.get(kind, ()))
    }
    frozen = {}
    coverage = {}
    title = "自由分析"
    if kind == "post":
        item = (
            conn.execute(
                select(contents).where(
                    contents.c.platform == scope["platform"], contents.c.id == scope["content_id"]
                )
            )
            .mappings()
            .first()
        )
        if item is None:
            raise AnalysisError("帖子不存在")
        title = str(item["data"].get("title") or item["data"].get("text") or item["id"])[:80]
    elif kind == "run":
        run = conn.execute(select(runs).where(runs.c.id == scope["run_id"])).mappings().first()
        if run is None:
            raise AnalysisError("采集任务不存在")
        scope["platform"] = run["platform"]
        title = "任务分析 · " + run["id"][:12]
        coverage = run_coverage(conn, run)
        coverage["snapshot_at"] = time.time()
        rows = conn.execute(
            select(task_items.c.snapshot)
            .join(tasks, tasks.c.id == task_items.c.task_id)
            .where(tasks.c.run_id == run["id"])
            .order_by(tasks.c.sequence, tasks.c.id)
        )
        for (snapshot,) in rows:
            record = safe_record(run["platform"], snapshot)
            key = (record["kind"], record["content_id"], record["id"])
            old = frozen.get(key)
            if (
                old
                and old["data"].get("detail_observed")
                and not record["data"].get("detail_observed")
            ):
                continue
            frozen[key] = record
    session_id = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        insert(analysis_sessions).values(
            id=session_id,
            scope=scope,
            coverage=coverage,
            title=title,
            status="idle",
            created_at=now,
            updated_at=now,
        )
    )
    if frozen:
        conn.execute(
            insert(analysis_sources),
            [
                {
                    "session_id": session_id,
                    "kind": key[0],
                    "content_id": key[1],
                    "item_id": key[2],
                    "record": value,
                }
                for key, value in frozen.items()
            ],
        )
    return session_id


def run_coverage(conn, run):
    config_fields = (
        "source_type",
        "keywords",
        "sort",
        "content_limit",
        "comment_limit",
        "unlimited_comments",
        "reply_parents",
        "reply_limit",
    )
    rows = conn.execute(select(tasks).where(tasks.c.run_id == run["id"])).mappings()
    return {
        "run_id": run["id"],
        "platform": run["platform"],
        "status": run["status"],
        "mode": run["mode"],
        "created_at": run["created_at"],
        "config": {k: run["config"][k] for k in config_fields if k in run["config"]},
        "tasks": [
            {
                "operation": t["operation"],
                "content_id": t["content_id"],
                "status": t["status"],
                "target": t["target"],
                "observed": t["state"].get("count", 0),
                "stop_reason": t["state"].get("stop_reason"),
                "response_has_more": t["state"].get("response_has_more"),
            }
            for t in rows
        ],
        "note": ("这是历史测试数据，不代表平台内容。" if run["mode"] == "offline"
                 else "分析范围为本次已采集的数据。"),
    }


def get_session(conn, session_id):
    row = (
        conn.execute(select(analysis_sessions).where(analysis_sessions.c.id == session_id))
        .mappings()
        .first()
    )
    if row is None:
        raise AnalysisError("分析会话不存在")
    return dict(row)


class AnalysisReader:
    def __init__(self, engine, session, artifact_root=None):
        self.engine = engine
        self.session = session
        self.scope = session["scope"]
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else None

    def _require_content_scope(self, platform, content_id):
        if not isinstance(platform, str) or not isinstance(content_id, str):
            raise AnalysisError("平台和帖子 ID 格式无效")
        if self.scope["kind"] == "post" and (
            platform != self.scope["platform"] or content_id != self.scope["content_id"]
        ):
            raise AnalysisError("媒体不在当前帖子会话范围内")
        if self.scope["kind"] == "run":
            if platform != self.scope["platform"]:
                raise AnalysisError("媒体不在当前任务会话范围内")
            with self.engine.connect() as conn:
                exists = conn.execute(
                    select(analysis_sources.c.item_id).where(
                        analysis_sources.c.session_id == self.session["id"],
                        analysis_sources.c.kind == "content",
                        analysis_sources.c.content_id == content_id,
                    )
                ).first()
            if not exists:
                raise AnalysisError("媒体不在当前任务会话范围内")

    def _media_assets(self, platform, content_id):
        self._require_content_scope(platform, content_id)
        with self.engine.connect() as conn:
            data = conn.scalar(
                select(contents.c.data).where(
                    contents.c.platform == platform, contents.c.id == content_id
                )
            )
        if data is None:
            raise AnalysisError("帖子不存在或不在当前会话范围内")
        assets = data.get("media_downloads", [])
        if not isinstance(assets, list):
            return []
        return [asset for asset in assets if isinstance(asset, dict)]

    @staticmethod
    def _public_media(asset):
        return {
            key: asset.get(key)
            for key in ("media_index", "kind", "status", "content_type", "bytes", "sha256", "error")
            if asset.get(key) is not None
        }

    def list_media(self, platform, content_id):
        assets = self._media_assets(platform, content_id)
        return {
            "platform": platform,
            "content_id": content_id,
            "items": [self._public_media(asset) for asset in assets],
            "downloaded": sum(asset.get("status") == "downloaded" for asset in assets),
            "note": "只能检查采集任务已下载到本地的媒体。",
        }

    def _media_path(self, platform, content_id, media_index):
        if self.artifact_root is None:
            raise AnalysisError("媒体存储目录未配置")
        try:
            media_index = int(media_index)
        except (TypeError, ValueError):
            raise AnalysisError("媒体编号无效") from None
        asset = next(
            (
                row
                for row in self._media_assets(platform, content_id)
                if row.get("status") == "downloaded" and row.get("media_index") == media_index
            ),
            None,
        )
        if not asset or not asset.get("path"):
            raise AnalysisError("这个媒体尚未下载到本地")
        relative = Path(asset["path"])
        path = (self.artifact_root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(self.artifact_root) or not path.is_file():
            raise AnalysisError("本地媒体文件不存在")
        if path.stat().st_size > 2 * 1024 * 1024 * 1024:
            raise AnalysisError("媒体文件过大，无法分析")
        return asset, path

    @staticmethod
    def _image_block(path):
        from PIL import Image, ImageOps

        if path.stat().st_size > 100 * 1024 * 1024:
            raise AnalysisError("图片超过 100 MB，无法分析")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as source:
                    if source.width * source.height > 40_000_000:
                        raise AnalysisError("图片超过 4000 万像素，无法分析")
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((1568, 1568))
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=88, optimize=True)
        except AnalysisError:
            raise
        except Exception:
            raise AnalysisError("无法读取这个图片文件") from None
        return {
            "type": "image",
            "data": base64.b64encode(output.getvalue()).decode(),
            "mimeType": "image/jpeg",
        }

    @staticmethod
    def _video_blocks(path, max_frames):
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        if not ffprobe or not ffmpeg:
            raise AnalysisError("视频分析需要安装 ffmpeg；可改用工作区 Agent 安装或处理视频")
        try:
            max_frames = max(1, min(12, int(max_frames)))
        except (TypeError, ValueError):
            raise AnalysisError("视频抽帧数量无效") from None
        try:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,duration:format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                check=True,
                timeout=15,
                text=True,
            )
            metadata = json.loads(probe.stdout)
            stream = (metadata.get("streams") or [{}])[0]
            duration = float(stream.get("duration") or metadata.get("format", {}).get("duration") or 0)
            if duration <= 0 or duration > 6 * 60 * 60:
                raise AnalysisError("视频时长无效或超过 6 小时")
            rate = max_frames / duration
            with tempfile.TemporaryDirectory(prefix="analysis-video-") as temporary:
                pattern = str(Path(temporary) / "frame-%02d.jpg")
                subprocess.run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-vf",
                        f"fps={rate:.8f},scale=1280:-2:force_original_aspect_ratio=decrease",
                        "-frames:v",
                        str(max_frames),
                        "-q:v",
                        "3",
                        pattern,
                    ],
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                frames = [
                    {
                        "type": "image",
                        "data": base64.b64encode(frame.read_bytes()).decode(),
                        "mimeType": "image/jpeg",
                    }
                    for frame in sorted(Path(temporary).glob("frame-*.jpg"))
                ]
        except AnalysisError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            raise AnalysisError("视频探测或抽帧失败") from None
        if not frames:
            raise AnalysisError("视频没有生成可分析的画面")
        return {
            "duration_seconds": round(duration, 3),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "sampled_frames": len(frames),
        }, frames

    def inspect_media(self, platform, content_id, media_index, max_frames=6):
        asset, path = self._media_path(platform, content_id, media_index)
        kind = asset.get("kind")
        result = self._public_media(asset) | {
            "platform": platform,
            "content_id": content_id,
            "note": "媒体来自本地已下载文件；视频结果为均匀抽取的画面，不含音频转写。",
        }
        if kind == "image" or str(asset.get("content_type", "")).startswith("image/"):
            result["_media_blocks"] = [self._image_block(path)]
            return result
        if kind == "video" or str(asset.get("content_type", "")).startswith("video/"):
            metadata, frames = self._video_blocks(path, max_frames)
            result.update(metadata)
            result["_media_blocks"] = frames
            return result
        raise AnalysisError("目前只支持分析已下载的图片和视频")

    def query(
        self, *, kind="content", platform="", content_id="", item_id="", q="", offset=0, limit=30
    ):
        if kind not in {"content", "comment"}:
            raise AnalysisError("数据类型无效")
        offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
        if self.scope["kind"] == "run":
            table = analysis_sources
            record = table.c.record
            statement = select(record).where(
                table.c.session_id == self.session["id"], table.c.kind == kind
            )
            if content_id:
                statement = statement.where(table.c.content_id == content_id)
            if item_id:
                statement = statement.where(table.c.item_id == item_id)
            if platform and platform != self.scope["platform"]:
                return {"items": [], "total": 0, "next_offset": None}
            data = record["data"]
            order = [table.c.content_id, table.c.item_id]
        else:
            table = contents if kind == "content" else comments
            statement = select(table)
            data = table.c.data
            if platform:
                statement = statement.where(table.c.platform == platform)
            if content_id:
                statement = statement.where(
                    (table.c.id if kind == "content" else table.c.content_id) == content_id
                )
            if item_id:
                statement = statement.where(table.c.id == item_id)
            if self.scope["kind"] == "post":
                statement = statement.where(
                    table.c.platform == self.scope["platform"],
                    (table.c.id if kind == "content" else table.c.content_id)
                    == self.scope["content_id"],
                )
            order = [table.c.platform]
            if kind == "comment":
                order.append(table.c.content_id)
            order.append(table.c.id)
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            statement = statement.where(
                or_(
                    data["text"].as_string().ilike(pattern, escape="\\"),
                    data["title"].as_string().ilike(pattern, escape="\\"),
                )
            )
        with self.engine.connect() as conn:
            total = conn.scalar(select(func.count()).select_from(statement.subquery()))
            rows = conn.execute(statement.order_by(*order).offset(offset).limit(limit)).mappings()
            records = [
                dict(r["record"])
                if self.scope["kind"] == "run"
                else safe_record(
                    r["platform"],
                    {
                        "kind": kind,
                        "id": r["id"],
                        "content_id": r["id"] if kind == "content" else r["content_id"],
                        "root_id": r.get("root_id"),
                        "parent_id": r.get("parent_id"),
                        "data": r["data"],
                    },
                )
                for r in rows
            ]
        for record in records:
            record["evidence_url"] = "/api/analysis/evidence?" + urlencode(
                {
                    "session_id": self.session["id"],
                    "citation": record["citation"],
                }
            )
        return {
            "items": records,
            "total": total,
            "next_offset": offset + len(records) if offset + len(records) < total else None,
        }

    def coverage(self):
        return {
            "scope": self.scope,
            "collection": self.session["coverage"],
            "posts": self.query(kind="content", limit=1)["total"],
            "comments": self.query(kind="comment", limit=1)["total"],
            "note": "统计只代表当前会话范围内已保存样本，评论和回复可能未采全。",
        }

    def list_runs(self, limit=20, offset=0):
        if self.scope["kind"] == "post":
            raise AnalysisError("帖子会话仅可查询当前帖子及评论")
        if self.scope["kind"] == "run":
            return {"items": [self.session["coverage"]], "next_offset": None}
        limit, offset = max(1, min(50, int(limit))), max(0, int(offset))
        with self.engine.connect() as conn:
            total = conn.scalar(select(func.count()).select_from(runs))
            rows = conn.execute(
                select(runs)
                .order_by(runs.c.created_at.desc(), runs.c.id)
                .offset(offset)
                .limit(limit)
            ).mappings()
            items = [
                {k: r[k] for k in ("id", "platform", "status", "mode", "created_at")}
                | {"keywords": r["config"].get("keywords", [])}
                for r in rows
            ]
            return {
                "items": items,
                "total": total,
                "next_offset": offset + len(items) if offset + len(items) < total else None,
            }

    def run_data(self, run_id, offset=0, limit=30):
        if self.scope["kind"] == "post":
            raise AnalysisError("帖子会话仅可查询当前帖子及评论")
        if self.scope["kind"] == "run":
            if run_id != self.scope["run_id"]:
                raise AnalysisError("该任务不在当前会话范围内")
            return {
                "coverage": self.session["coverage"],
                "posts": self.query(offset=offset, limit=limit),
            }
        # Free chat can inspect task snapshots without falling back to latest content.
        with self.engine.connect() as conn:
            run = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
            if run is None:
                raise AnalysisError("采集任务不存在")
            statement = (
                select(task_items.c.snapshot)
                .join(tasks, tasks.c.id == task_items.c.task_id)
                .where(tasks.c.run_id == run_id)
            )
            total = conn.scalar(select(func.count()).select_from(statement.subquery()))
            offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
            rows = conn.execute(
                statement.order_by(tasks.c.id, task_items.c.item_id).offset(offset).limit(limit)
            )
            records = [safe_record(run["platform"], row.snapshot) for row in rows]
            for record in records:
                record["citation"] += "/run/" + run_id
                record["evidence_url"] = "/api/analysis/evidence?" + urlencode(
                    {
                        "session_id": self.session["id"],
                        "citation": record["citation"],
                    }
                )
            return {
                "coverage": run_coverage(conn, run),
                "items": records,
                "total_observations": total,
                "next_offset": offset + len(records) if offset + len(records) < total else None,
                "note": "同一内容可能出现在不同阶段，按 platform、kind、content_id、id 去重。",
            }

    def evidence(self, citation):
        parts = citation.split("/")
        if len(parts) not in {4, 6}:
            raise AnalysisError("证据标识无效")
        result = self.query(platform=parts[0], kind=parts[1], content_id=parts[2], item_id=parts[3])
        if not result["items"]:
            raise AnalysisError("证据不存在或超出当前会话范围")
        if len(parts) == 6 and result["items"][0]["citation"] != citation:
            raise AnalysisError("该版本的证据不存在，请重新查询")
        return result["items"][0]

    def call(self, operation, arguments):
        if operation == "coverage":
            return self.coverage()
        if operation == "search":
            return self.query(**arguments)
        if operation == "list_runs":
            return self.list_runs(**arguments)
        if operation == "run_data":
            return self.run_data(**arguments)
        if operation == "list_media":
            return self.list_media(**arguments)
        if operation == "inspect_media":
            return self.inspect_media(**arguments)
        raise AnalysisError("未知分析工具")


def tool_result(value):
    if isinstance(value, dict):
        media = value.get("_media_blocks", [])
        value = {key: item for key, item in value.items() if key != "_media_blocks"}
    else:
        media = []
    return {
        "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)},
            *media,
        ]
    }
