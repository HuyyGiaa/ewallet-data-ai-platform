"""Dependency-neutral completeness checks for validation-sample exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ai_platform.features.feast.schema import FEATURE_SPECS


CANONICAL_SAMPLE_CASES = (
    "normal",
    "velocity",
    "amount_anomaly",
    "account_takeover",
    "merchant_burst",
    "cold_start",
    "multi_account_user",
    "recent_device",
    "payment_with_merchant",
    "non_payment_without_merchant",
)
CANONICAL_SAMPLE_ROW_COUNT = 10
HISTORICAL_FEATURE_COUNT = 31
CANONICAL_COMPARABLE_VALUE_COUNT = 310


def require_validation_export_manifest(
    root: Path,
    *,
    expected_lineage: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    path = root / "manifest.json"
    if not path.is_file():
        raise ValueError("validation sample export is incomplete: manifest missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": "feast-validation-samples-v1",
        "status": "COMPLETE",
        "canonical_sample_rows": CANONICAL_SAMPLE_ROW_COUNT,
        "canonical_sample_cases": list(CANONICAL_SAMPLE_CASES),
        "historical_feature_count": HISTORICAL_FEATURE_COUNT,
        "expected_comparable_values": CANONICAL_COMPARABLE_VALUE_COUNT,
        "benchmark_rows": 100,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("validation sample export is incomplete or incompatible")
    if expected_lineage is not None:
        lineage = manifest.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ValueError("validation sample export lineage is missing")
        for name, value in expected_lineage.items():
            if lineage.get(name) != value:
                raise ValueError(
                    f"validation sample export lineage mismatch: {name}"
                )
    return manifest


def require_registry_readback_evidence(
    path: Path,
    *,
    expected_registry_uri: str,
    expected_snapshot_id: str,
    expected_snapshot_root: str,
    expected_execution_git_sha: str,
    expected_registry_generation: int,
    expected_registry_sha256: str,
) -> Mapping[str, Any]:
    """Authenticate the durable readback evidence used by a PIT run."""

    if not path.is_file():
        raise ValueError("Feast registry readback evidence is missing")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": "feast-registry-operation-evidence-v1",
        "status": "READBACK_VERIFIED",
        "mode": "readback-only",
        "registry_uri": expected_registry_uri,
        "snapshot_id": expected_snapshot_id,
        "snapshot_root": expected_snapshot_root,
        "execution_git_sha": expected_execution_git_sha,
        "registry_generation": expected_registry_generation,
        "registry_sha256": expected_registry_sha256,
        "historical_feature_count": HISTORICAL_FEATURE_COUNT,
        "model_input_count": 41,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("Feast registry readback identity is incompatible")
    feature_views = evidence.get("feature_views")
    if not isinstance(feature_views, list) or len(feature_views) != 4:
        raise ValueError("Feast registry readback must contain four FeatureViews")
    names = tuple(
        item.get("name") if isinstance(item, Mapping) else None
        for item in feature_views
    )
    if names != tuple(FEATURE_SPECS):
        raise ValueError("Feast registry readback FeatureView inventory drifted")
    return evidence
