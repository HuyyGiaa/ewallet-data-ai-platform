"""Validated configuration for the F9 experiment and tracking workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _path(value: object, root: Path, name: str) -> Path:
    path = Path(_text(value, name))
    return path if path.is_absolute() else root / path


def load_experiment_config(path: Path, *, repo_root: Path | None = None) -> ExperimentConfig:
    root = Path.cwd() if repo_root is None else Path(repo_root)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    top = _mapping(raw, "F9 configuration")
    selection = _mapping(top.get("selection"), "selection")
    tracking_raw = _mapping(top.get("tracking"), "tracking")
    registry_mode = _text(tracking_raw.get("registry_mode"), "tracking.registry_mode")
    if registry_mode not in {"manual", "automatic"}:
        raise ValueError("tracking.registry_mode must be manual or automatic")
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
    return ExperimentConfig(
        dataset_path=_path(top.get("dataset_path"), root, "dataset_path"),
        output_path=_path(top.get("output_path"), root, "output_path"),
        seed=seed,
        primary_metric=_text(selection.get("primary_metric"), "selection.primary_metric"),
        tie_breaker_metric=_text(selection.get("tie_breaker_metric"), "selection.tie_breaker_metric"),
        tracking=TrackingConfig(
            uri=_text(tracking_raw.get("uri"), "tracking.uri"),
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
