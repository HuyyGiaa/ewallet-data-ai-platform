"""Compare Feast retrieval with direct Gold values for canonical cases."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Mapping

import pandas as pd

from ai_platform.features.feast import FEATURE_REPO, RUNTIME_DATA_ROOT
from ai_platform.features.feast.retrieval.historical import (
    get_store,
    retrieve_combined,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.validation.contracts import (
    CANONICAL_COMPARABLE_VALUE_COUNT,
    CANONICAL_SAMPLE_CASES,
    CANONICAL_SAMPLE_ROW_COUNT,
    HISTORICAL_FEATURE_COUNT,
    require_registry_readback_evidence,
    require_validation_export_manifest,
)


ENTITY_COLUMNS = (
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "event_timestamp",
)
SAMPLE_METADATA_COLUMNS = (
    "sample_case",
    "transaction_id",
    "type",
    "label",
    "fraud_type",
)
FORBIDDEN_RETRIEVAL_COLUMNS = frozenset(("label", "fraud_type"))


def expected_feature_columns() -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected = tuple(
        f"expected__{view_name}__{feature}"
        for view_name, spec in FEATURE_SPECS.items()
        for feature in spec["features"]
    )
    actual = tuple(
        f"{view_name}__{feature}"
        for view_name, spec in FEATURE_SPECS.items()
        for feature in spec["features"]
    )
    if len(expected) != HISTORICAL_FEATURE_COUNT or len(actual) != HISTORICAL_FEATURE_COUNT:
        raise ValueError("frozen 31-feature historical contract drifted")
    return expected, actual


def values_equal(expected, actual) -> bool:
    if pd.isna(expected) and pd.isna(actual):
        return True
    if isinstance(expected, float) or isinstance(actual, float):
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-9)
    return expected == actual


def _require_utc_timestamp(frame: pd.DataFrame, *, source: str) -> None:
    dtype = frame["event_timestamp"].dtype
    if not isinstance(dtype, pd.DatetimeTZDtype) or str(dtype.tz) != "UTC":
        raise ValueError(f"{source} event_timestamp must be timezone-aware UTC")


def validate_sample_frame(sample: pd.DataFrame) -> pd.DataFrame:
    """Validate the direct-Gold oracle before any Feast retrieval occurs."""

    if not sample.columns.is_unique:
        raise ValueError("canonical sample contains duplicate columns")
    if len(sample) != CANONICAL_SAMPLE_ROW_COUNT:
        raise ValueError(
            f"canonical sample must contain exactly {CANONICAL_SAMPLE_ROW_COUNT} rows"
        )
    required = {*ENTITY_COLUMNS, *SAMPLE_METADATA_COLUMNS}
    missing = sorted(required - set(sample.columns))
    if missing:
        raise ValueError(f"canonical sample columns missing: {missing}")
    expected_columns, _ = expected_feature_columns()
    missing_expected = sorted(set(expected_columns) - set(sample.columns))
    if missing_expected:
        raise ValueError(
            f"canonical sample expected feature columns missing: {missing_expected}"
        )
    observed_expected = {
        name for name in sample.columns if name.startswith("expected__")
    }
    unexpected_expected = sorted(observed_expected - set(expected_columns))
    if unexpected_expected:
        raise ValueError(
            "canonical sample contains unexpected expected-feature columns: "
            f"{unexpected_expected}"
        )
    cases = sample["sample_case"].tolist()
    if len(set(cases)) != CANONICAL_SAMPLE_ROW_COUNT or set(cases) != set(
        CANONICAL_SAMPLE_CASES
    ):
        raise ValueError(
            "canonical sample case coverage mismatch: "
            f"actual={sorted(set(cases))}, expected={sorted(CANONICAL_SAMPLE_CASES)}"
        )
    _require_utc_timestamp(sample, source="canonical sample")
    indexed = sample.set_index("sample_case", drop=False)
    payment = indexed.loc["payment_with_merchant"]
    non_payment = indexed.loc["non_payment_without_merchant"]
    if payment["type"] != "payment" or pd.isna(payment["merchant_id"]):
        raise ValueError("payment_with_merchant case has invalid merchant semantics")
    if non_payment["type"] == "payment" or not pd.isna(non_payment["merchant_id"]):
        raise ValueError(
            "non_payment_without_merchant case has invalid merchant semantics"
        )
    return sample.sort_values("sample_case").reset_index(drop=True)


def validate_retrieved_frame(
    sample: pd.DataFrame,
    retrieved: pd.DataFrame,
) -> tuple[str, ...]:
    """Validate Feast output shape and row alignment before value comparison."""

    if not retrieved.columns.is_unique:
        raise ValueError("Feast retrieval contains duplicate columns")
    if len(retrieved) != CANONICAL_SAMPLE_ROW_COUNT:
        raise ValueError(
            f"Feast retrieval must contain exactly {CANONICAL_SAMPLE_ROW_COUNT} rows"
        )
    missing_entities = sorted(set(ENTITY_COLUMNS) - set(retrieved.columns))
    if missing_entities:
        raise ValueError(f"Feast retrieval entity columns missing: {missing_entities}")
    _, actual_columns = expected_feature_columns()
    missing_actual = sorted(set(actual_columns) - set(retrieved.columns))
    if missing_actual:
        raise ValueError(f"Feast retrieval feature columns missing: {missing_actual}")
    feature_prefixes = tuple(f"{view_name}__" for view_name in FEATURE_SPECS)
    observed_actual = {
        name for name in retrieved.columns if name.startswith(feature_prefixes)
    }
    unexpected_actual = sorted(observed_actual - set(actual_columns))
    if unexpected_actual:
        raise ValueError(
            f"Feast retrieval contains unexpected feature columns: {unexpected_actual}"
        )
    leaked = sorted(FORBIDDEN_RETRIEVAL_COLUMNS & set(retrieved.columns))
    if leaked:
        raise ValueError(f"labels entered Feast retrieval: {leaked}")
    _require_utc_timestamp(retrieved, source="Feast retrieval")
    for row_index in range(CANONICAL_SAMPLE_ROW_COUNT):
        for column in ENTITY_COLUMNS:
            if not values_equal(
                sample.iloc[row_index][column], retrieved.iloc[row_index][column]
            ):
                raise ValueError(
                    f"Feast retrieval row alignment mismatch: row={row_index}, "
                    f"column={column}"
                )
    return actual_columns


def validate_canonical_sample(
    sample_path: Path = RUNTIME_DATA_ROOT / "validation" / "canonical_sample",
    *,
    environment: str = "local",
    expected_lineage: Mapping[str, str] | None = None,
    registry_readback_evidence: Path | None = None,
    expected_registry_generation: int | None = None,
    expected_registry_sha256: str | None = None,
) -> dict:
    manifest = None
    registry_evidence = None
    if environment == "gcp":
        if expected_lineage is None:
            raise ValueError("GCP PIT validation requires expected export lineage")
        if (
            registry_readback_evidence is None
            or expected_registry_generation is None
            or expected_registry_sha256 is None
        ):
            raise ValueError("GCP PIT validation requires trusted registry readback identity")
        snapshot_root = os.environ.get("EWALLET_FEAST_OFFLINE_DATA_ROOT")
        registry_uri = os.environ.get("EWALLET_FEAST_REGISTRY")
        if not snapshot_root or not registry_uri:
            raise ValueError("GCP PIT validation requires explicit Feast snapshot and registry")
        manifest = require_validation_export_manifest(
            sample_path.parent,
            expected_lineage=expected_lineage,
        )
        registry_evidence = require_registry_readback_evidence(
            registry_readback_evidence,
            expected_registry_uri=registry_uri,
            expected_snapshot_id=expected_lineage["snapshot_id"],
            expected_snapshot_root=snapshot_root,
            expected_execution_git_sha=expected_lineage["execution_git_sha"],
            expected_registry_generation=expected_registry_generation,
            expected_registry_sha256=expected_registry_sha256,
        )
    sample = validate_sample_frame(pd.read_parquet(sample_path))
    entity_df = sample[list(ENTITY_COLUMNS)].copy()
    if tuple(entity_df.columns) != ENTITY_COLUMNS:
        raise ValueError("Feast retrieval entity projection drifted")
    retrieved = retrieve_combined(
        get_store(FEATURE_REPO, environment=environment), entity_df
    )
    actual_columns = validate_retrieved_frame(sample, retrieved)
    expected_columns, _ = expected_feature_columns()
    mismatches = []
    comparable = 0
    for row_index, expected_row in sample.iterrows():
        actual_row = retrieved.iloc[row_index]
        for expected_column, actual_column in zip(
            expected_columns, actual_columns, strict=True
        ):
            expected = expected_row[expected_column]
            actual = actual_row[actual_column]
            comparable += 1
            if not values_equal(expected, actual):
                mismatches.append(
                    {
                        "case": expected_row["sample_case"],
                        "feature": actual_column,
                        "expected": None if pd.isna(expected) else expected,
                        "actual": None if pd.isna(actual) else actual,
                    }
                )
    if comparable != CANONICAL_COMPARABLE_VALUE_COUNT:
        raise ValueError(
            "canonical comparison count drifted: "
            f"actual={comparable}, expected={CANONICAL_COMPARABLE_VALUE_COUNT}"
        )
    result = {
        "schema_version": "feast-pit-validation-evidence-v1",
        "sample_rows": len(sample),
        "sample_cases": sample["sample_case"].tolist(),
        "distinct_sample_cases": sample["sample_case"].nunique(),
        "historical_features_per_case": len(actual_columns),
        "expected_comparable_values": CANONICAL_COMPARABLE_VALUE_COUNT,
        "comparable_values": comparable,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "status": (
            "PASS"
            if comparable == CANONICAL_COMPARABLE_VALUE_COUNT and not mismatches
            else "FAIL"
        ),
        "labels_passed_to_feast": False,
        "retrieval_columns": list(ENTITY_COLUMNS),
        "environment": environment,
        "validation_export_lineage": (
            None if manifest is None else manifest.get("lineage")
        ),
        "registry_readback": (
            None
            if registry_evidence is None
            else {
                "status": registry_evidence["status"],
                "registry_uri": registry_evidence["registry_uri"],
                "registry_generation": registry_evidence["registry_generation"],
                "registry_sha256": registry_evidence["registry_sha256"],
            }
        ),
    }
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("local", "dev", "gcp"), default="local")
    parser.add_argument(
        "--sample-path",
        type=Path,
        default=RUNTIME_DATA_ROOT / "validation" / "canonical_sample",
    )
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--registry-readback-evidence", type=Path)
    parser.add_argument("--expected-registry-generation", type=int)
    parser.add_argument("--expected-registry-sha256")
    parser.add_argument("--expected-dataset-version")
    parser.add_argument("--expected-source-silver-run-id")
    parser.add_argument("--expected-source-feature-run-id")
    parser.add_argument("--expected-source-silver-root")
    parser.add_argument("--expected-source-feature-root")
    parser.add_argument("--expected-snapshot-id")
    parser.add_argument("--expected-source-git-sha")
    parser.add_argument("--expected-execution-git-sha")
    parser.add_argument("--expected-feature-selection-digest")
    return parser.parse_args(argv)


def _expected_lineage(args: argparse.Namespace) -> dict[str, str] | None:
    values = {
        "dataset_version": args.expected_dataset_version,
        "source_silver_run_id": args.expected_source_silver_run_id,
        "source_feature_run_id": args.expected_source_feature_run_id,
        "source_silver_root": args.expected_source_silver_root,
        "source_feature_root": args.expected_source_feature_root,
        "snapshot_id": args.expected_snapshot_id,
        "source_git_sha": args.expected_source_git_sha,
        "execution_git_sha": args.expected_execution_git_sha,
        "feature_selection_digest": args.expected_feature_selection_digest,
    }
    if args.environment != "gcp" and all(value is None for value in values.values()):
        return None
    missing = sorted(name for name, value in values.items() if value is None)
    if missing:
        raise ValueError(f"expected validation export lineage is incomplete: {missing}")
    return {name: str(value) for name, value in values.items()}


def _write_evidence(path: Path, result: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_canonical_sample(
            args.sample_path,
            environment=args.environment,
            expected_lineage=_expected_lineage(args),
            registry_readback_evidence=args.registry_readback_evidence,
            expected_registry_generation=args.expected_registry_generation,
            expected_registry_sha256=args.expected_registry_sha256,
        )
    except Exception as exc:
        result = {
            "schema_version": "feast-pit-validation-evidence-v1",
            "status": "FAIL",
            "error": str(exc),
            "environment": args.environment,
        }
    if args.evidence_output is not None:
        _write_evidence(args.evidence_output, result)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
