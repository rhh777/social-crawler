from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Operation(StrEnum):
    RESOLVE_TARGET = "resolve_target"
    SEARCH = "search"
    DETAIL = "detail"
    COMMENTS = "comments"
    REPLIES = "replies"


class Item(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["content", "comment"]
    content_id: str = Field(min_length=1)
    root_id: str | None = None
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    children: list["Item"] = Field(default_factory=list)


class CollectionError(Exception):
    def __init__(self, kind: str, message: str = "", *, retry_after: float | None = None):
        self.kind = kind
        self.message = message or kind
        self.retry_after = retry_after
        self.sample_ref = None
        super().__init__(self.message)

    @property
    def blocks_run(self) -> bool:
        """Whether continuing the remaining task graph would be unsafe or misleading."""
        return self.kind in {
            "rate_limit",
            "auth_expired",
            "verification_required",
            "access_denied",
            "schema_changed",
            "abnormal_empty",
            "no_progress",
            "session_changed",
            "proxy_unavailable",
            "egress_unconfirmed",
            "egress_changed",
            "egress_mismatch",
            "quota_exhausted",
            "quota_policy_missing",
            "identity_mismatch",
            "identity_unverified",
            "environment_mismatch",
            "environment_in_use",
            "environment_snapshot_missing",
            "missing_cursor",
            "missing_search_context",
        }

    @property
    def outcome(self) -> Literal["blocked", "failed", "partial"]:
        """Classify why an attempt stopped without conflating risk and code failures."""
        if self.kind == "schema_changed":
            return "failed"
        if self.kind.startswith("account_") or self.kind in {
            "rate_limit",
            "auth_expired",
            "verification_required",
            "access_denied",
            "session_changed",
            "proxy_unavailable",
            "identity_mismatch",
            "identity_unverified",
            "environment_mismatch",
            "environment_in_use",
            "environment_snapshot_missing",
            "egress_unconfirmed",
            "egress_changed",
            "egress_mismatch",
            "quota_exhausted",
            "quota_policy_missing",
        }:
            return "blocked"
        return "partial"


class PageResult(BaseModel):
    resolved_target: dict[str, str] | None = None
    items: list[Item] = Field(default_factory=list)
    next_context: dict[str, Any] = Field(default_factory=dict)
    response_has_more: bool | None = None
    stop_reason: str | None = None
    sample_ref: str | None = None
    skipped: list[dict[str, str]] = Field(default_factory=list)


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["xhs", "rednote", "douyin"]
    source_type: Literal["keyword", "posts"] = "keyword"
    keywords: list[str] = Field(default_factory=list, max_length=100)
    post_targets: list[str] = Field(default_factory=list, max_length=100)
    sort: Literal["general", "latest"] = "latest"
    content_limit: int = Field(default=3, ge=1, le=1000)
    comment_limit: int = Field(default=5, ge=0, le=10000)
    unlimited_comments: bool = False
    reply_parents: int = Field(default=1, ge=0, le=100)
    reply_limit: int = Field(default=3, ge=0, le=2000)
    max_requests: int = Field(default=200, ge=1, le=100000)
    max_seconds: int = Field(default=900, ge=1, le=86400)
    max_pages: int = Field(default=100, ge=1, le=10000)
    max_no_progress: int = Field(default=2, ge=1, le=10)
    min_interval: float = Field(default=5, ge=0, le=300)
    request_timeout: float = Field(default=20, ge=1, le=120)
    network_retries: int = Field(default=2, ge=0, le=2)
    download_media: bool = False
    # None inherits the account default; bool values are per-run overrides.
    headless: bool | None = None
    # XHS-family sites can use the page-driven adapter or either direct HTTP transport.
    # Existing XHS/RedNote runs default to browser when this field is absent.
    adapter: Literal["browser", "httpx", "curl_cffi"] | None = None
    seed_contents: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_keywords(self):
        self.keywords = list(dict.fromkeys(k.strip() for k in self.keywords))
        if self.source_type == "keyword":
            if not self.keywords or self.post_targets:
                raise ValueError("关键词来源需要 keywords，且不能包含 post_targets")
        else:
            from social_crawler.domain.targets import parse_target

            if self.keywords or self.seed_contents:
                raise ValueError("指定帖子来源不能同时配置关键词或 seed_contents")
            self.post_targets = list(dict.fromkeys(t.strip() for t in self.post_targets if t.strip()))
            if not self.post_targets or any(len(t) > 4096 for t in self.post_targets):
                raise ValueError("请提供 1–100 个帖子目标，每行最多 4096 字符")
            valid = False
            for target in self.post_targets:
                try:
                    parse_target(self.platform, target)
                    valid = True
                except CollectionError:
                    pass
            if not valid:
                raise ValueError("没有有效帖子目标，请检查平台、帖子链接或 ID")
        if any(not k for k in self.keywords):
            raise ValueError("keywords must not be blank")
        from social_crawler.adapters.xhs.sites import is_xhs_platform

        if is_xhs_platform(self.platform) and self.adapter is None:
            self.adapter = "browser"
        if not is_xhs_platform(self.platform) and self.adapter is not None:
            raise ValueError("adapter selection is supported only for XHS / RedNote")
        return self


class TaskRequest(BaseModel):
    operation: Operation
    keyword: str = ""
    sort: str = "general"
    content_id: str = ""
    root_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
