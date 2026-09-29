"""Local attachments: validate and persist without making any model request."""

import base64
import binascii
import io
import json
import re
import uuid
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from social_crawler.analysis.data import AnalysisError

MAX_FILE = 8 * 1024 * 1024
MAX_TURN = 20 * 1024 * 1024
MAX_FILES = 6
MAX_TEXT = 100_000
TEXT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".log": "text/plain",
}
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class Attachments:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _folder(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise AnalysisError("会话标识无效")
        return self.directory / session_id

    def load(self, session_id, attachment_id):
        if not isinstance(attachment_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attachment_id):
            raise AnalysisError("附件标识无效")
        folder = self._folder(session_id)
        try:
            meta = json.loads((folder / (attachment_id + ".json")).read_text())
            return meta, folder / (attachment_id + ".bin")
        except (FileNotFoundError, ValueError):
            raise AnalysisError("附件不存在或不属于当前会话") from None

    def save(self, session_id, body):
        name = body.get("name", "")
        encoded = body.get("content", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise AnalysisError("文件名需要为 1–200 字")
        name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip()
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE + 2) // 3 * 4:
            raise AnalysisError("单个附件不能超过 8 MB")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AnalysisError("附件编码无效") from None
        if not payload or len(payload) > MAX_FILE:
            raise AnalysisError("请选择非空文件，单个附件不能超过 8 MB")
        suffix = Path(name).suffix.lower()
        text, truncated = "", False
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            from PIL import Image

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(payload)) as img:
                        if img.format not in IMAGE_TYPES or img.width * img.height > 25_000_000:
                            raise ValueError("image dimensions")
                        media_type = IMAGE_TYPES[img.format]
                        img.verify()
            except Exception:
                raise AnalysisError(
                    "图片无效或过大，请使用不超过 2500 万像素的 PNG、JPEG、WebP、GIF"
                ) from None
        elif suffix in TEXT_TYPES:
            media_type = TEXT_TYPES[suffix]
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = payload.decode("gb18030")
                except UnicodeDecodeError:
                    raise AnalysisError("文本附件请使用 UTF-8 或 GB18030 编码") from None
            if "\x00" in text:
                raise AnalysisError("文件不是可读取的文本")
        elif suffix == ".pdf":
            from pypdf import PdfReader

            media_type = "application/pdf"
            try:
                doc = PdfReader(io.BytesIO(payload))
                if doc.is_encrypted or len(doc.pages) > 50:
                    raise AnalysisError("PDF 请使用未加密、50 页以内的文件")
                parts = []
                for page in doc.pages:
                    stream = page.get_contents()
                    if stream and len(stream.get_data()) > 10 * 1024 * 1024:
                        raise AnalysisError("PDF 页面过大，请拆分后上传")
                    parts.append(page.extract_text() or "")
                    if sum(len(t) for t in parts) > MAX_TEXT:
                        truncated = True
                        break
                text = "\n\n".join(parts)
            except AnalysisError:
                raise
            except Exception:
                raise AnalysisError("无法读取这个 PDF，请检查文件是否完整") from None
            if not text.strip():
                raise AnalysisError("PDF 没有可提取的文字；扫描件请以图片上传")
        elif suffix == ".docx":
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as doc:
                    info = doc.getinfo("word/document.xml")
                    if info.file_size > 10 * 1024 * 1024:
                        raise ValueError("document too large")
                    xml = doc.read(info)
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                        raise ValueError("unsupported XML")
                    tree = ElementTree.fromstring(xml)
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    text = "\n".join("".join(p.itertext()) for p in tree.iter(ns + "p"))
            except Exception:
                raise AnalysisError("无法读取这个 Word 文档，请使用完整的 .docx 文件") from None
        else:
            raise AnalysisError("支持图片、TXT、Markdown、CSV、JSON、LOG、PDF 和 DOCX")
        truncated = truncated or len(text) > MAX_TEXT
        folder = self._folder(session_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len(list(folder.glob("*.json"))) >= 100:
            raise AnalysisError("本会话附件已满，请新建会话")
        aid = uuid.uuid4().hex
        meta = {
            "id": aid,
            "name": name,
            "size": len(payload),
            "media_type": media_type,
            "is_image": media_type.startswith("image/"),
            "truncated": truncated,
            "url": f"/api/analysis/attachments/{session_id}/{aid}",
        }
        for path, value in [
            (folder / (aid + ".bin"), payload),
            (
                folder / (aid + ".json"),
                json.dumps(meta | {"text": text[:MAX_TEXT]}, ensure_ascii=False).encode(),
            ),
        ]:
            with path.open("xb") as f:
                path.chmod(0o600)
                f.write(value)
        return meta

    def prepare(self, session_id, ids):
        if (
            not isinstance(ids, list)
            or len(ids) > MAX_FILES
            or any(not isinstance(i, str) for i in ids)
        ):
            raise AnalysisError("每条消息最多附加 6 个文件")
        if len(set(ids)) != len(ids):
            raise AnalysisError("附件不能重复")
        attachments = [self.load(session_id, aid) for aid in ids]
        if sum(m["size"] for m, _ in attachments) > MAX_TURN:
            raise AnalysisError("每条消息的附件合计不能超过 20 MB")
        blocks, public = [], []
        for meta, path in attachments:
            public.append({k: v for k, v in meta.items() if k != "text"})
            blocks.append({"type": "text", "text": f"用户附件：{meta['name']}"})
            if meta["is_image"]:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": meta["media_type"],
                            "data": base64.b64encode(path.read_bytes()).decode(),
                        },
                    }
                )
            else:
                note = "\n[内容已截取前 100000 字]" if meta["truncated"] else ""
                blocks.append(
                    {
                        "type": "text",
                        "text": "<attachment_data>\n"
                        + meta["text"]
                        + note
                        + "\n</attachment_data>",
                    }
                )
        return public, blocks
