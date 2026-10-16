"""Versioned configuration boundary for the F8 Random Forest baseline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelConfig:
    name: str
    estimator: str
    strategy: str
    params: dict[str, Any]


@dataclass(frozen=True)
class F8Config:
    dataset_path: Path
    output_path: Path
    seed: int
    model: ModelConfig


def _resolved_path(value: object, repo_root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _model(raw: object) -> ModelConfig:
    if not isinstance(raw, dict):
        raise ValueError("model must be a mapping")
    name = raw.get("name")
    estimator = raw.get("estimator")
    strategy = raw.get("strategy")
    params = raw.get("params", {})
    if not isinstance(name, str) or not name:
        raise ValueError("model.name must be non-empty")
    if estimator != "random_forest":
        raise ValueError("F8 model.estimator must be random_forest")
    if strategy != "full":
        raise ValueError("F8 model.strategy must be full")
    if not isinstance(params, dict):
        raise ValueError("model.params must be a mapping")
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"model parameter must be numeric: {key}")
        if value <= 0:
            raise ValueError(f"model parameter must be positive: {key}")
    return ModelConfig(name, estimator, strategy, dict(params))


def load_config(path: Path, repo_root: Path | None = None) -> F8Config:
    """Load and validate the single-model F8 YAML configuration."""
    config_path = Path(path)
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("F8 configuration must be a mapping")

    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")

    return F8Config(
        dataset_path=_resolved_path(raw.get("dataset_path"), root, "dataset_path"),
        output_path=_resolved_path(raw.get("output_path"), root, "output_path"),
        seed=seed,
        model=_model(raw.get("model")),
    )
