"""Environment-aware paths and evidence helpers for the Feast repository."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from data_platform.config import RuntimeConfig
from data_platform.config.spark_profiles import require_optimized_lineage


FEATURE_REPO_ROOT = Path(__file__).resolve().parent / "feature_repo"
_CONFIG_BY_ENVIRONMENT = {
    "local": FEATURE_REPO_ROOT / "feature_store.yaml",
    "dev": FEATURE_REPO_ROOT / "feature_store.gcp.yaml",
    "gcp": FEATURE_REPO_ROOT / "feature_store.gcp.yaml",
}


def feature_store_config_path(environment: str) -> Path:
    """Return the tracked Feast config for one supported runtime profile."""
    normalized = environment.strip().lower()
    try:
        return _CONFIG_BY_ENVIRONMENT[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_CONFIG_BY_ENVIRONMENT))
        raise ValueError(
            f"unsupported environment {environment!r}; expected one of: {supported}"
        ) from exc


def feature_data_uri(config: RuntimeConfig, table_name: str) -> str:
    """Build a Feast snapshot URI without coercing object storage to Path."""
    return config.storage.table_uri("feast", table_name)


def join_data_root(root: str | Path, table_name: str) -> str:
    """Join a local path or object-store URI to one feature table name."""
    root_text = str(root).rstrip("/")
    if "://" in root_text:
        return f"{root_text}/{table_name}"
    return str(Path(root_text) / table_name)


def build_export_manifest(
    *,
    environment: str,
    dataset_version: str,
    tables: Mapping[str, object],
    exported_at: str | None = None,
    spark_profile: str = "optimized",
    source_feature_run_id: str | None = None,
    allow_baseline_for_test: bool = False,
) -> dict[str, object]:
    """Build the versioned manifest shared by local and cloud exports."""
    require_optimized_lineage(
        spark_profile,
        allow_baseline_for_test=allow_baseline_for_test,
    )
    return {
        "exported_at": exported_at or datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "dataset_version": dataset_version,
        "spark_profile": spark_profile,
        "source_feature_run_id": source_feature_run_id,
        "source_format": "Delta Lake active snapshot",
        "output_format": "Parquet",
        "timestamp_precision": "microsecond",
        "tables": dict(tables),
    }
