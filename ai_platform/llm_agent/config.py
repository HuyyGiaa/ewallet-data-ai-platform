"""Fail-closed loader for the versioned LLM/Agent release inventory."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping


SCHEMA_VERSION = "llm-agent-release/v1"
ARCHITECTURE_CONTRACT = {
    "external_gateway": "istio-ambient-gateway-api",
    "a2a": "kagent-native-a2a",
    "llm_route": "modelconfig-agentgateway-vllm",
    "mcp_gateway_role": "policy-only",
}
REQUIRED_COMPONENTS = frozenset(
    {
        "vllm",
        "agentgateway",
        "mcp-gateway",
        "feature-rag-mcp",
        "drift-mcp",
        "feature-rag-specialist",
        "drift-specialist",
        "fraud-coordinator",
        "rag-pipeline",
        "streaming-to-offline",
        "streaming-to-online",
        "otel-collector",
    }
)


@dataclass(frozen=True)
class ComponentConfig:
    owner: str
    source: str
    build_definition: str
    chart: str
    tests: tuple[str, ...]
    evidence_output: str
    runtime_status: str


@dataclass(frozen=True)
class ReleaseConfig:
    schema_version: str
    release_id: str
    architecture: Mapping[str, str]
    components: Mapping[str, ComponentConfig]


def _relative_path(root: Path, raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or raw.startswith("/"):
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field} escapes the repository root") from exc
    return raw


def load_release_config(path: Path, *, repository_root: Path) -> ReleaseConfig:
    """Load and validate a release inventory without contacting a runtime."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    release_id = payload.get("release_id", "")
    if not isinstance(release_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{2,63}", release_id
    ):
        raise ValueError("release_id must be a lowercase version identifier")
    architecture = payload.get("architecture")
    if architecture != ARCHITECTURE_CONTRACT:
        raise ValueError("release architecture does not match Living Decision 21.216")
    raw_components = payload.get("components")
    if not isinstance(raw_components, dict):
        raise ValueError("components must be an object")
    missing = REQUIRED_COMPONENTS - raw_components.keys()
    extra = raw_components.keys() - REQUIRED_COMPONENTS
    if missing or extra:
        raise ValueError(
            f"component inventory mismatch missing={sorted(missing)} extra={sorted(extra)}"
        )

    components: dict[str, ComponentConfig] = {}
    for name, item in raw_components.items():
        if not isinstance(item, dict):
            raise ValueError(f"component {name} must be an object")
        owner = item.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError(f"component {name} requires an owner")
        tests = item.get("tests")
        if not isinstance(tests, list) or not tests or not all(
            isinstance(test, str) and test.strip() for test in tests
        ):
            raise ValueError(f"component {name} requires at least one test path")
        status = item.get("runtime_status")
        if status not in {"OPEN", "PARTIAL", "VERIFIED"}:
            raise ValueError(f"component {name} has invalid runtime_status")
        if status == "VERIFIED":
            raise ValueError(
                f"component {name} cannot be runtime VERIFIED in a source inventory"
            )
        components[name] = ComponentConfig(
            owner=owner.strip(),
            source=_relative_path(repository_root, item.get("source"), f"{name}.source"),
            build_definition=_relative_path(
                repository_root, item.get("build_definition"), f"{name}.build_definition"
            ),
            chart=_relative_path(repository_root, item.get("chart"), f"{name}.chart"),
            tests=tuple(tests),
            evidence_output=_relative_path(
                repository_root, item.get("evidence_output"), f"{name}.evidence_output"
            ),
            runtime_status=status,
        )
    return ReleaseConfig(
        schema_version=SCHEMA_VERSION,
        release_id=release_id,
        architecture=dict(architecture),
        components=components,
    )
