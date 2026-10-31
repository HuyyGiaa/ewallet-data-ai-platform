"""Transport-neutral contracts shared by agents, MCP services and gateways."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
import re


CONTRACT_VERSION = "llm-agent-contracts/v1"
TRACEPARENT_RE = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-(?:00|01)$"
)
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
TOOL_NAMES = frozenset(
    {"feature_lookup", "fraud_scoring", "knowledge_search", "drift_detection"}
)


def _required(value: str, name: str, maximum: int = 256) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must contain 1..{maximum} characters")
    return normalized


def _revision(value: str, name: str) -> str:
    normalized = _required(value, name, 128)
    if not REVISION_RE.fullmatch(normalized):
        raise ValueError(f"{name} is not a valid revision identifier")
    return normalized


@dataclass(frozen=True)
class VersionContext:
    """Code and data provenance attached to every business response."""

    git_sha: str
    image_digest: str
    config_revision: str
    prompt_revision: str
    model_revision: str
    feature_revision: str
    corpus_revision: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.git_sha):
            raise ValueError("git_sha must be a full lowercase Git SHA")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("image_digest must be an immutable sha256 digest")
        for name in (
            "config_revision",
            "prompt_revision",
            "model_revision",
            "feature_revision",
            "corpus_revision",
        ):
            object.__setattr__(self, name, _revision(getattr(self, name), name))


@dataclass(frozen=True)
class RequestContext:
    """Correlation and bounded execution policy for one request."""

    request_id: str
    traceparent: str
    actor_id: str
    timeout_ms: int
    tool_call_budget: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "request_id", 128))
        object.__setattr__(self, "actor_id", _required(self.actor_id, "actor_id", 128))
        if not TRACEPARENT_RE.fullmatch(self.traceparent):
            raise ValueError("traceparent must be a W3C version 00 trace context")
        if not 100 <= self.timeout_ms <= 120_000:
            raise ValueError("timeout_ms must be between 100 and 120000")
        if not 1 <= self.tool_call_budget <= 32:
            raise ValueError("tool_call_budget must be between 1 and 32")


@dataclass(frozen=True)
class Citation:
    """Auditable citation returned by Feast-backed knowledge retrieval."""

    document_id: str
    chunk_id: str
    source_uri: str
    source_checksum: str
    corpus_revision: str
    embedding_revision: str
    feature_revision: str
    score: float

    def __post_init__(self) -> None:
        for name in ("document_id", "chunk_id", "source_uri"):
            object.__setattr__(self, name, _required(getattr(self, name), name, 1024))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_checksum):
            raise ValueError("source_checksum must be sha256:<64 lowercase hex>")
        for name in ("corpus_revision", "embedding_revision", "feature_revision"):
            object.__setattr__(self, name, _revision(getattr(self, name), name))
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be in [0, 1]")


@dataclass(frozen=True)
class ToolCallRequest:
    """Least-privilege request sent through the MCP policy gateway."""

    tool_name: str
    arguments: Mapping[str, Any]
    context: RequestContext
    versions: VersionContext

    def __post_init__(self) -> None:
        if self.tool_name not in TOOL_NAMES:
            raise ValueError(f"tool_name must be one of {sorted(TOOL_NAMES)}")
        if not isinstance(self.arguments, Mapping) or len(self.arguments) > 64:
            raise ValueError("arguments must be a mapping with at most 64 keys")


@dataclass(frozen=True)
class ToolCallResponse:
    """Sanitized tool result; raw prompts and hidden reasoning are excluded."""

    request_id: str
    tool_name: str
    status: str
    result: Mapping[str, Any]
    versions: VersionContext
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    fallback_used: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "request_id", 128))
        if self.tool_name not in TOOL_NAMES:
            raise ValueError(f"tool_name must be one of {sorted(TOOL_NAMES)}")
        if self.status not in {"ok", "rejected", "timeout", "unavailable", "error"}:
            raise ValueError("unsupported tool response status")
        if not isinstance(self.result, Mapping):
            raise ValueError("result must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentDelegationRequest:
    """Native A2A delegation contract between kagent SandboxAgents."""

    target_agent: str
    task: str
    context: RequestContext
    versions: VersionContext
    allowed_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_agent", _required(self.target_agent, "target_agent", 128))
        object.__setattr__(self, "task", _required(self.task, "task", 4096))
        if not self.allowed_tools or len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must be non-empty and unique")
        unknown = set(self.allowed_tools) - TOOL_NAMES
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")


@dataclass(frozen=True)
class ErrorResponse:
    """Stable error envelope shared by HTTP, MCP and A2A boundaries."""

    request_id: str
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "request_id", 128))
        object.__setattr__(self, "code", _required(self.code, "code", 64))
        object.__setattr__(self, "message", _required(self.message, "message", 512))
        if self.retry_after_ms is not None and not 0 <= self.retry_after_ms <= 60_000:
            raise ValueError("retry_after_ms must be in [0, 60000]")
