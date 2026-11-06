"""Experiment specifications: what is compared, and what must stay equal.

A specification is data in ``configs/experiments``. It names the single variable that
may differ, the controls that must not, the metrics with their direction, and the
evidence a run must leave. Keeping it as data lets a reviewer read the rules of a
comparison without reading code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


LOWER_IS_BETTER = "lower"
HIGHER_IS_BETTER = "higher"


class SpecError(ValueError):
    """An experiment specification is incomplete or contradictory."""


@dataclass(frozen=True)
class Metric:
    """One compared metric.

    Attributes:
        name: Stable metric name.
        direction: ``lower`` or ``higher``, meaning which way is better.
        path: Dotted path inside a level summary.
    """

    name: str
    direction: str
    path: str

    def __post_init__(self) -> None:
        if self.direction not in {LOWER_IS_BETTER, HIGHER_IS_BETTER}:
            raise SpecError(f"{self.name}: direction must be lower or higher")


@dataclass(frozen=True)
class ExperimentSpec:
    """Rules of one comparison.

    Attributes:
        experiment_id: Stable identifier.
        hypothesis: The claim under test.
        variable: The one thing that differs.
        baseline: Label of the baseline arm.
        optimized: Label of the candidate arm.
        controls: Field names that must match between arms.
        metrics: Compared metrics.
        primary_metric: Metric that decides the verdict.
        min_runs: Runs per arm needed before a verdict can claim a change.
        significant_percent: Smallest change treated as real.
        screenshots: Evidence the owner must capture.
    """

    experiment_id: str
    hypothesis: str
    variable: str
    baseline: str
    optimized: str
    controls: tuple[str, ...]
    metrics: tuple[Metric, ...]
    primary_metric: str
    min_runs: int
    significant_percent: float
    screenshots: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.primary_metric not in {metric.name for metric in self.metrics}:
            raise SpecError(f"primary metric {self.primary_metric} is not listed")
        if self.min_runs < 2:
            raise SpecError("a single run cannot support a verdict; min_runs must be at least 2")
        if self.baseline == self.optimized:
            raise SpecError("the two arms must be distinct")


def parse_spec(value: Mapping[str, object]) -> ExperimentSpec:
    """Build a specification from its JSON form.

    Raises:
        SpecError: A field is missing or a rule is broken.
    """

    try:
        return ExperimentSpec(
            experiment_id=str(value["experiment_id"]),
            hypothesis=str(value["hypothesis"]),
            variable=str(value["variable"]),
            baseline=str(value["baseline"]),
            optimized=str(value["optimized"]),
            controls=tuple(value["controls"]),
            metrics=tuple(Metric(**item) for item in value["metrics"]),
            primary_metric=str(value["primary_metric"]),
            min_runs=int(value["min_runs"]),
            significant_percent=float(value["significant_percent"]),
            screenshots=tuple(value["screenshots"]),
        )
    except (KeyError, TypeError) as error:
        raise SpecError(f"invalid experiment specification: {error}") from error


def load_spec(path: Path) -> ExperimentSpec:
    """Read one specification file."""

    return parse_spec(json.loads(path.read_text(encoding="utf-8")))
