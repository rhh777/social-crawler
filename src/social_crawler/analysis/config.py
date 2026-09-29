"""Private, hot-reloadable Agent endpoint settings; never perform network calls."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from social_crawler.analysis.data import AnalysisError

DEFAULT_API_URL = "https://api.anthropic.com"
DEFAULT_MAX_BUFFER_SIZE_MB = 8
DEFAULT_RUNTIME_SETTINGS = {
    "fallback_model": "",
    "max_turns": None,
    "max_budget_usd": None,
    "max_buffer_size_mb": DEFAULT_MAX_BUFFER_SIZE_MB,
    "thinking_mode": "default",
    "thinking_budget_tokens": None,
    "effort": "",
}
STRING_FIELDS = ("api_key", "api_url", "model", "fallback_model", "thinking_mode", "effort")
OPTIONAL_INTEGER_FIELDS = ("max_turns", "thinking_budget_tokens")
THINKING_MODES = {"default", "adaptive", "enabled", "disabled"}
EFFORT_LEVELS = {"", "low", "medium", "high", "xhigh", "max"}


def sdk_base_url(api_url):
    try:
        url = urlsplit(api_url)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
        _ = url.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise AnalysisError("接口地址需要有效的 http/https URL，且不能包含凭据、查询参数或片段")
    path = url.path.rstrip("/")
    if path.endswith("/v1/chat/completions"):
        path = path[: -len("/v1/chat/completions")]
    elif path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif path.endswith("/v1/messages"):
        path = path[: -len("/v1/messages")]
    elif path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def environment_settings():
    return DEFAULT_RUNTIME_SETTINGS | {
        "api_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        "api_url": os.environ.get("CRAWLER_AGENT_API_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or DEFAULT_API_URL,
        "model": os.environ.get("CRAWLER_AGENT_MODEL", "sonnet").strip() or "sonnet",
    }


def public_api_url(api_url):
    """Keep an invalid editable URL from exposing credentials through the public GET."""
    try:
        url = urlsplit(api_url)
    except (TypeError, ValueError):
        return ""
    if url.username or url.password or url.query or url.fragment:
        return ""
    return api_url


def sdk_runtime_options(settings):
    """Translate persisted UI settings into supported ClaudeAgentOptions values."""
    thinking_mode = settings.get("thinking_mode", "default")
    thinking = None
    if thinking_mode == "adaptive":
        thinking = {"type": "adaptive"}
    elif thinking_mode == "enabled":
        thinking = {
            "type": "enabled",
            "budget_tokens": settings.get("thinking_budget_tokens"),
        }
    elif thinking_mode == "disabled":
        thinking = {"type": "disabled"}
    return {
        "fallback_model": settings.get("fallback_model") or None,
        "max_turns": settings.get("max_turns"),
        "max_budget_usd": settings.get("max_budget_usd"),
        "max_buffer_size": int(
            settings.get("max_buffer_size_mb", DEFAULT_MAX_BUFFER_SIZE_MB) * 1024 * 1024
        ),
        "thinking": thinking,
        "effort": settings.get("effort") or None,
    }


class AgentSettings:
    def __init__(self, path, lock):
        self.path = Path(path)
        self.lock = lock

    def _load_values(self):
        data = environment_settings()
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict):
                    raise ValueError
                for field in STRING_FIELDS:
                    if field in saved:
                        if not isinstance(saved[field], str):
                            raise ValueError
                        data[field] = saved[field]
                for field in OPTIONAL_INTEGER_FIELDS:
                    if field in saved:
                        if saved[field] is not None and (
                            isinstance(saved[field], bool) or not isinstance(saved[field], int)
                        ):
                            raise ValueError
                        data[field] = saved[field]
                if "max_budget_usd" in saved:
                    value = saved["max_budget_usd"]
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                    ):
                        raise ValueError
                    data["max_budget_usd"] = value
                if "max_buffer_size_mb" in saved:
                    value = saved["max_buffer_size_mb"]
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError
                    data["max_buffer_size_mb"] = value
            except (OSError, ValueError):
                raise AnalysisError("Agent 配置文件读取失败，请检查文件权限或配置格式") from None
        return data

    def load(self):
        with self.lock:
            data = self._load_values()
            data["sdk_base_url"] = sdk_base_url(data["api_url"])
            return data

    @staticmethod
    def _validate_body(body):
        if not isinstance(body, dict):
            raise AnalysisError("Agent 配置格式无效")
        for field in STRING_FIELDS:
            if field in body and not isinstance(body[field], str):
                raise AnalysisError("Agent 文本配置格式无效")

    @staticmethod
    def _optional_integer(body, field, minimum, maximum, label):
        if field not in body:
            return None, False
        value = body[field]
        if value == "" or value is None:
            return None, True
        try:
            if isinstance(value, bool) or str(value).strip() != str(int(value)):
                raise ValueError
            value = int(value)
        except (TypeError, ValueError):
            raise AnalysisError(f"{label}需要填写整数") from None
        if not minimum <= value <= maximum:
            raise AnalysisError(f"{label}需要在 {minimum}–{maximum} 之间")
        return value, True

    @staticmethod
    def _optional_float(body, field, minimum, maximum, label):
        if field not in body:
            return None, False
        value = body[field]
        if value == "" or value is None:
            return None, True
        try:
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
        except (TypeError, ValueError):
            raise AnalysisError(f"{label}需要填写数字") from None
        if not minimum <= value <= maximum:
            raise AnalysisError(f"{label}需要在 {minimum:g}–{maximum:g} 之间")
        return value, True

    def _resolve_values(self, body):
        self._validate_body(body)
        data = self._load_values()
        for field in ("api_url", "model", "fallback_model", "thinking_mode", "effort"):
            if field in body:
                data[field] = body[field].strip()
        if body.get("api_key", "").strip():
            data["api_key"] = body["api_key"].strip()
        for field, label, required in (
            ("model", "模型名称", True),
            ("fallback_model", "备用模型名称", False),
        ):
            value = data[field]
            if (
                (required and not value)
                or len(value) > 200
                or any(ch.isspace() or ord(ch) < 32 for ch in value)
            ):
                raise AnalysisError(f"请填写有效的{label}")
        if len(data["api_key"]) > 8192 or any(ord(ch) < 32 for ch in data["api_key"]):
            raise AnalysisError("API Key 格式无效")
        if len(data["api_url"]) > 2048:
            raise AnalysisError("接口地址过长")
        max_turns, changed = self._optional_integer(body, "max_turns", 1, 1000, "最大轮次")
        if changed:
            data["max_turns"] = max_turns
        budget, changed = self._optional_float(body, "max_budget_usd", 0.01, 100000, "单轮预算")
        if changed:
            data["max_budget_usd"] = budget
        buffer_size, changed = self._optional_integer(
            body, "max_buffer_size_mb", 1, 64, "消息缓冲区"
        )
        if changed:
            data["max_buffer_size_mb"] = buffer_size
        thinking_budget, changed = self._optional_integer(
            body, "thinking_budget_tokens", 1024, 128000, "思考 Token 预算"
        )
        if changed:
            data["thinking_budget_tokens"] = thinking_budget
        if data["thinking_mode"] not in THINKING_MODES:
            raise AnalysisError("推理模式无效")
        if data["thinking_mode"] == "enabled" and data["thinking_budget_tokens"] is None:
            raise AnalysisError("固定思考模式需要填写思考 Token 预算")
        if data["thinking_mode"] != "enabled":
            data["thinking_budget_tokens"] = None
        if data["effort"] not in EFFORT_LEVELS:
            raise AnalysisError("推理强度无效")
        data["sdk_base_url"] = sdk_base_url(data["api_url"])
        return data

    def resolve(self, body):
        """Resolve unsaved form values against the stored secret for a connection check."""
        with self.lock:
            return self._resolve_values(body)

    def public(self):
        with self.lock:
            data = self._load_values()
            validation_error = ""
            try:
                base_url = sdk_base_url(data["api_url"])
            except AnalysisError as exc:
                base_url = ""
                validation_error = str(exc)
        return {
            "configured": bool(data["api_key"]) and not validation_error,
            "api_key_configured": bool(data["api_key"]),
            "api_url": public_api_url(data["api_url"]),
            "sdk_base_url": base_url,
            "model": data["model"],
            **{field: data[field] for field in DEFAULT_RUNTIME_SETTINGS},
            "validation_error": validation_error,
        }

    def save(self, body):
        with self.lock:
            data = self._resolve_values(body)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as handle:
                    fields = ("api_key", "api_url", "model", *DEFAULT_RUNTIME_SETTINGS)
                    json.dump({k: data[k] for k in fields}, handle)
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self.public()
