"""Canonical, transport-independent results for executed data-quality rules.

``PASS`` and ``FAIL`` are executed results and must carry run and evidence
provenance. ``NOT_EXECUTED`` (the check never ran), ``BLOCKED`` (it could not run)
and ``UNKNOWN`` (it ran but its report holds no usable result) are never
published as an assertion run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ResultStatus = Literal["PASS", "FAIL", "NOT_EXECUTED", "BLOCKED", "UNKNOWN"]
EXECUTED_STATUSES = frozenset({"PASS", "FAIL"})
UNPUBLISHABLE_STATUSES = frozenset({"NOT_EXECUTED", "BLOCKED", "UNKNOWN"})
SCHEMA_VERSION = "datahub-quality-validation-v1"


@dataclass(frozen=True)
class ValidationOutcome:
    """One rule result bound to the dataset it checks and to its evidence."""

    assertion_id: str
    pipeline: str
    dataset_urn: str
    dataset_uri: str
    rule_name: str
    description: str
    status: ResultStatus
    expected: str
    observed: dict[str, Any]
    executed_at: str
    source_run_id: str
    source_batch_id: str
    source_git_sha: str
    source_artifact: str
    source_artifact_sha256: str

    def __post_init__(self) -> None:
        if self.status not in EXECUTED_STATUSES | UNPUBLISHABLE_STATUSES:
            raise ValueError(f"unsupported validation status: {self.status}")
        if not self.assertion_id or not self.rule_name:
            raise ValueError("assertion_id and rule_name are required")
        if not self.dataset_urn.startswith("urn:li:dataset:"):
            raise ValueError(f"invalid dataset URN: {self.dataset_urn}")
        if self.status in EXECUTED_STATUSES:
            required = (
                self.executed_at,
                self.source_run_id,
                self.source_artifact,
                self.source_artifact_sha256,
            )
            if not all(required):
                raise ValueError("executed results require timestamp, run and evidence provenance")
            parsed = datetime.fromisoformat(self.executed_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("executed_at must include a timezone")

    @property
    def assertion_urn(self) -> str:
        return f"urn:li:assertion:{self.assertion_id}"

    @property
    def run_id(self) -> str:
        return f"{self.pipeline}:{self.source_run_id}:{self.rule_name}"

    @property
    def timestamp_millis(self) -> int:
        value = datetime.fromisoformat(self.executed_at.replace("Z", "+00:00"))
        return int(value.timestamp() * 1000)


@dataclass(frozen=True)
class ValidationArtifact:
    """A versioned, credential-free set of outcomes, written as sorted JSON."""

    outcomes: tuple[ValidationOutcome, ...]
    generated_at: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported validation artifact: {self.schema_version}")
        identities = [(item.assertion_id, item.run_id) for item in self.outcomes]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate assertion/run identity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "outcomes": [asdict(item) for item in self.outcomes],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "ValidationArtifact":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            schema_version=payload["schema_version"],
            generated_at=payload["generated_at"],
            outcomes=tuple(ValidationOutcome(**item) for item in payload["outcomes"]),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
