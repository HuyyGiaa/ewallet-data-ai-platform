"""Versioned configuration boundary for the F8 local modeling run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_ESTIMATORS = frozenset(
    {"dummy", "logistic", "random_forest", "hist_gradient_boosting"}
)
SUPPORTED_STRATEGIES = frozenset({"full", "selected", "pca"})


@dataclass(frozen=True)
class CandidateConfig:
    name: str
    estimator: str
    strategy: str
    params: dict[str, Any]


@dataclass(frozen=True)
class F8Config:
    dataset_path: Path
    output_path: Path
    seed: int
    pca_variance: float
    selector_c: float
    candidates: tuple[CandidateConfig, ...]


def _resolved_path(value: object, repo_root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _candidate(raw: object) -> CandidateConfig:
    if not isinstance(raw, dict):
        raise ValueError("Each candidate must be a mapping")
    name = raw.get("name")
    estimator = raw.get("estimator")
    strategy = raw.get("strategy")
    params = raw.get("params", {})
    if not isinstance(name, str) or not name:
        raise ValueError("Candidate name must be non-empty")
    if estimator not in SUPPORTED_ESTIMATORS:
        raise ValueError(f"Unsupported estimator for {name}: {estimator}")
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(f"Unsupported feature strategy for {name}: {strategy}")
    if not isinstance(params, dict):
        raise ValueError(f"Candidate params must be a mapping: {name}")
    for key, value in params.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value <= 0:
            raise ValueError(f"Candidate parameter must be positive: {name}.{key}")
    return CandidateConfig(name, estimator, strategy, dict(params))


def load_config(path: Path, repo_root: Path | None = None) -> F8Config:
    """Load and validate an F8 YAML configuration."""
    config_path = Path(path)
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("F8 configuration must be a mapping")

    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    pca_variance = raw.get("pca_variance")
    if not isinstance(pca_variance, (int, float)) or not 0 < pca_variance <= 1:
        raise ValueError("pca_variance must be in (0, 1]")
    selector_c = raw.get("selector_c")
    if not isinstance(selector_c, (int, float)) or selector_c <= 0:
        raise ValueError("selector_c must be positive")

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidates must be a non-empty list")
    candidates = tuple(_candidate(item) for item in raw_candidates)
    names = [candidate.name for candidate in candidates]
    if len(names) != len(set(names)):
        raise ValueError("Candidate names must be unique")

    return F8Config(
        dataset_path=_resolved_path(raw.get("dataset_path"), root, "dataset_path"),
        output_path=_resolved_path(raw.get("output_path"), root, "output_path"),
        seed=seed,
        pca_variance=float(pca_variance),
        selector_c=float(selector_c),
        candidates=candidates,
    )
