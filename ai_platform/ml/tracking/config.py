"""Validated configuration for the F9 experiment and tracking workflow."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from ai_platform.ml.modeling.config import ModelConfig


SUPPORTED_ESTIMATORS = {
    "dummy",
    "logistic_regression",
    "random_forest",
    "hist_gradient_boosting",
}


@dataclass(frozen=True)
class TrackingConfig:
    uri: str
    experiment_name: str
    registered_model_name: str
    registry_mode: str


@dataclass(frozen=True)
class SnapshotConfig:
    training_snapshot_uri: str
    dataset_version: str
    silver_run_id: str
    feature_run_id: str
    snapshot_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "training_snapshot_uri": self.training_snapshot_uri,
            "dataset_version": self.dataset_version,
            "silver_run_id": self.silver_run_id,
            "feature_run_id": self.feature_run_id,
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True)
class CandidateConfig:
    model: ModelConfig
    selection_eligible: bool


@dataclass(frozen=True)
class ExperimentConfig:
    dataset_path: Path
    output_path: Path
    seed: int
    primary_metric: str
    tie_breaker_metric: str
    snapshot: SnapshotConfig
    tracking: TrackingConfig
    candidates: tuple[CandidateConfig, ...]


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


_ENVIRONMENT_VALUE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _runtime_text(
    value: object,
    name: str,
    environment: Mapping[str, str],
) -> str:
    raw = _text(value, name)

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        resolved = environment.get(variable)
        if resolved is None or not resolved.strip():
            raise ValueError(f"{name} requires environment variable {variable}")
        return resolved.strip()

    rendered = _ENVIRONMENT_VALUE.sub(replace, raw)
    if "${" in rendered:
        raise ValueError(f"{name} contains an unresolved environment variable")
    return rendered


def _path(
    value: object,
    root: Path,
    name: str,
    environment: Mapping[str, str],
) -> Path:
    path = Path(_runtime_text(value, name, environment))
    return path if path.is_absolute() else root / path


def load_experiment_config(
    path: Path,
    *,
    repo_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> ExperimentConfig:
    root = Path.cwd() if repo_root is None else Path(repo_root)
    runtime = os.environ if environment is None else environment
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    top = _mapping(raw, "F9 configuration")
    selection = _mapping(top.get("selection"), "selection")
    snapshot_raw = _mapping(top.get("snapshot"), "snapshot")
    tracking_raw = _mapping(top.get("tracking"), "tracking")
    registry_mode = _text(tracking_raw.get("registry_mode"), "tracking.registry_mode")
    if registry_mode not in {"manual", "winner_only"}:
        raise ValueError("tracking.registry_mode must be manual or winner_only")
    seed = top.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    raw_candidates = top.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidates must be a non-empty list")
    candidates: list[CandidateConfig] = []
    names: set[str] = set()
    for index, value in enumerate(raw_candidates):
        item = _mapping(value, f"candidates[{index}]")
        name = _text(item.get("name"), f"candidates[{index}].name")
        estimator = _text(item.get("estimator"), f"candidates[{index}].estimator")
        strategy = _text(item.get("strategy"), f"candidates[{index}].strategy")
        if estimator not in SUPPORTED_ESTIMATORS:
            raise ValueError(f"Unsupported estimator: {estimator}")
        if strategy != "full":
            raise ValueError("Only the full feature strategy is supported")
        if name in names:
            raise ValueError(f"Duplicate candidate name: {name}")
        names.add(name)
        params = _mapping(item.get("params", {}), f"candidates[{index}].params")
        eligible = item.get("selection_eligible")
        if not isinstance(eligible, bool):
            raise ValueError("selection_eligible must be boolean")
        candidates.append(
            CandidateConfig(ModelConfig(name, estimator, strategy, dict(params)), eligible)
        )
    if not any(candidate.selection_eligible for candidate in candidates):
        raise ValueError("At least one candidate must be selection eligible")
    expected_candidates = (
        ("dummy_baseline", "dummy", False),
        ("logistic_regression", "logistic_regression", True),
        ("random_forest", "random_forest", True),
        ("hist_gradient_boosting", "hist_gradient_boosting", True),
    )
    actual_candidates = tuple(
        (
            candidate.model.name,
            candidate.model.estimator,
            candidate.selection_eligible,
        )
        for candidate in candidates
    )
    if actual_candidates != expected_candidates:
        raise ValueError(f"Canonical F9 candidates must be exactly {expected_candidates}")
    primary_metric = _text(selection.get("primary_metric"), "selection.primary_metric")
    tie_breaker_metric = _text(
        selection.get("tie_breaker_metric"), "selection.tie_breaker_metric"
    )
    if (primary_metric, tie_breaker_metric) != ("pr_auc", "f1"):
        raise ValueError("Canonical F9 selection must be validation PR-AUC then F1")
    snapshot = SnapshotConfig(
        training_snapshot_uri=_runtime_text(
            snapshot_raw.get("training_snapshot_uri"),
            "snapshot.training_snapshot_uri",
            runtime,
        ),
        dataset_version=_runtime_text(
            snapshot_raw.get("dataset_version"), "snapshot.dataset_version", runtime
        ),
        silver_run_id=_runtime_text(
            snapshot_raw.get("silver_run_id"), "snapshot.silver_run_id", runtime
        ),
        feature_run_id=_runtime_text(
            snapshot_raw.get("feature_run_id"), "snapshot.feature_run_id", runtime
        ),
        snapshot_id=_runtime_text(
            snapshot_raw.get("snapshot_id"), "snapshot.snapshot_id", runtime
        ),
    )
    expected_suffix = f"/{snapshot.dataset_version}/{snapshot.snapshot_id}"
    if not snapshot.training_snapshot_uri.startswith("gs://") or not (
        snapshot.training_snapshot_uri.rstrip("/").endswith(expected_suffix)
    ):
        raise ValueError(
            "snapshot.training_snapshot_uri must be the immutable GCS training "
            "snapshot for dataset_version/snapshot_id"
        )
    return ExperimentConfig(
        dataset_path=_path(top.get("dataset_path"), root, "dataset_path", runtime),
        output_path=_path(top.get("output_path"), root, "output_path", runtime),
        seed=seed,
        primary_metric=primary_metric,
        tie_breaker_metric=tie_breaker_metric,
        snapshot=snapshot,
        tracking=TrackingConfig(
            uri=_runtime_text(tracking_raw.get("uri"), "tracking.uri", runtime),
            experiment_name=_text(
                tracking_raw.get("experiment_name"), "tracking.experiment_name"
            ),
            registered_model_name=_text(
                tracking_raw.get("registered_model_name"),
                "tracking.registered_model_name",
            ),
            registry_mode=registry_mode,
        ),
        candidates=tuple(candidates),
    )
