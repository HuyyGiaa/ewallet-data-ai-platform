"""Compare baseline and optimized runs and decide an honest verdict.

Rules that keep a report truthful:

* A comparison runs only when both arms ran the same controls; otherwise the verdict is
  ``INVALID_COMPARISON`` and no percentage is reported as a result.
* Failed requests stay in the counts. An arm that fails more requests cannot be
  ``IMPROVED``, however fast its successful requests were.
* One run is not a result: below ``min_runs`` the verdict is ``INCONCLUSIVE``.
* A difference smaller than the run-to-run spread, or than the configured threshold, is
  ``NO_SIGNIFICANT_CHANGE``.
* Every level is reported, so a good level cannot hide a bad one.

The improvement column always points the same way: positive means better, whichever
direction the metric improves in.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ops.experiments.spec import HIGHER_IS_BETTER, ExperimentSpec, Metric


IMPROVED = "IMPROVED"
REGRESSED = "REGRESSED"
NO_SIGNIFICANT_CHANGE = "NO_SIGNIFICANT_CHANGE"
INCONCLUSIVE = "INCONCLUSIVE"
INVALID_COMPARISON = "INVALID_COMPARISON"
NOT_EXECUTED = "NOT_EXECUTED"


@dataclass(frozen=True)
class Run:
    """One measured run of one arm.

    Attributes:
        controls: Values of the experiment's control fields.
        levels: Level name to summary; a summary needs ``requests`` and ``failed``.
    """

    controls: Mapping[str, Any]
    levels: Mapping[str, Mapping[str, Any]]


def read_path(value: Mapping[str, Any], path: str) -> float | None:
    """Follow a dotted path and return a number, or ``None`` when it is absent."""

    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return float(current) if isinstance(current, (int, float)) and not isinstance(current, bool) else None


def control_mismatches(spec: ExperimentSpec, baseline: Sequence[Run], optimized: Sequence[Run]) -> list[str]:
    """Return every control that differs between or within the arms."""

    problems = []
    runs = [*baseline, *optimized]
    for name in spec.controls:
        missing = [index for index, run in enumerate(runs) if run.controls.get(name) in (None, "")]
        if missing:
            problems.append(f"{name} is not recorded")
            continue
        values = {str(run.controls[name]) for run in runs}
        if len(values) > 1:
            problems.append(f"{name} differs: {sorted(values)}")
    return problems


def improvement_percent(metric: Metric, baseline: float, optimized: float) -> float | None:
    """Return the change in percent, positive when the candidate is better."""

    if baseline == 0:
        return None
    change = 100 * (optimized - baseline) / abs(baseline)
    return change if metric.direction == HIGHER_IS_BETTER else -change


def _summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"median": None, "min": None, "max": None, "runs": 0}
    return {"median": statistics.median(values), "min": min(values), "max": max(values), "runs": len(values)}


def _ranges_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left["min"] <= right["max"] and right["min"] <= left["max"]


def compare_metric(metric: Metric, level: str, baseline: Sequence[Run], optimized: Sequence[Run]) -> dict[str, Any]:
    """Summarize one metric at one level across both arms."""

    def values(runs: Sequence[Run]) -> list[float]:
        found = (read_path(run.levels.get(level, {}), metric.path) for run in runs)
        return [item for item in found if item is not None]

    left, right = _summary(values(baseline)), _summary(values(optimized))
    row: dict[str, Any] = {
        "level": level, "metric": metric.name, "better_when": metric.direction,
        "baseline": left, "optimized": right, "improvement_percent": None, "within_run_spread": None,
    }
    if left["runs"] and right["runs"]:
        row["improvement_percent"] = improvement_percent(metric, left["median"], right["median"])
        row["within_run_spread"] = _ranges_overlap(left, right)
    return row


def failure_counts(runs: Sequence[Run], level: str) -> dict[str, int]:
    """Total requests and failures of an arm at one level; nothing is excluded."""

    summaries = [run.levels.get(level, {}) for run in runs]
    return {
        "requests": int(sum(item.get("requests", 0) for item in summaries)),
        "failed": int(sum(item.get("failed", 0) for item in summaries)),
    }


def unmeasured_levels(runs: Sequence[Run]) -> list[str]:
    """Levels whose client status is anything but ``MEASURED``, such as a blocked level."""

    found = {f"{level} ({summary['status']})" for run in runs for level, summary in run.levels.items()
             if summary.get("status", "MEASURED") != "MEASURED"}
    return sorted(found)


def decide(spec: ExperimentSpec, rows: Sequence[Mapping[str, Any]], failures: Mapping[str, Mapping[str, Mapping[str, int]]],
           run_counts: tuple[int, int], unmeasured: Sequence[str] = ()) -> tuple[str, list[str]]:
    """Return the verdict and the reasons behind it."""

    def rate(item: Mapping[str, int]) -> float:
        return item["failed"] / item["requests"] if item["requests"] else 0.0

    if unmeasured:
        return INCONCLUSIVE, [f"level was not measured: {item}" for item in unmeasured]
    worse = [level for level, arms in failures.items() if rate(arms["optimized"]) > rate(arms["baseline"])]
    if worse:
        return REGRESSED, [f"more failed requests at level {level}" for level in worse]
    if min(run_counts) < spec.min_runs:
        return INCONCLUSIVE, [f"needs {spec.min_runs} runs per arm, has {run_counts[0]} and {run_counts[1]}"]
    primary = [row for row in rows if row["metric"] == spec.primary_metric and row["improvement_percent"] is not None]
    if not primary:
        return INCONCLUSIVE, [f"{spec.primary_metric} has no comparable values"]
    real = [row for row in primary if abs(row["improvement_percent"]) >= spec.significant_percent
            and not row["within_run_spread"]]
    better = [row for row in real if row["improvement_percent"] > 0]
    worse_rows = [row for row in real if row["improvement_percent"] < 0]

    def levels(items: Sequence[Mapping[str, Any]]) -> str:
        return ", ".join(str(row["level"]) for row in items)

    if better and worse_rows:
        return INCONCLUSIVE, [f"better at {levels(better)}, worse at {levels(worse_rows)}"]
    if worse_rows:
        return REGRESSED, [f"{spec.primary_metric} worse at {levels(worse_rows)}"]
    if better:
        return IMPROVED, [f"{spec.primary_metric} better at {levels(better)}"]
    return NO_SIGNIFICANT_CHANGE, ["no level changed beyond the threshold and the run-to-run spread"]


def compare(spec: ExperimentSpec, baseline: Sequence[Run], optimized: Sequence[Run]) -> dict[str, Any]:
    """Compare two arms.

    Args:
        spec: Rules of the comparison.
        baseline: Runs of the baseline arm.
        optimized: Runs of the candidate arm.

    Returns:
        Metrics per level, failure counts, the verdict and its reasons.
    """

    report: dict[str, Any] = {
        "experiment_id": spec.experiment_id, "variable": spec.variable,
        "baseline": spec.baseline, "optimized": spec.optimized,
        "runs": {"baseline": len(baseline), "optimized": len(optimized)},
        "rows": [], "failures": {},
    }
    if not baseline or not optimized:
        return {**report, "verdict": NOT_EXECUTED, "reasons": ["both arms need evidence"]}
    mismatches = control_mismatches(spec, baseline, optimized)
    if mismatches:
        return {**report, "verdict": INVALID_COMPARISON, "reasons": mismatches}
    levels = sorted({level for run in [*baseline, *optimized] for level in run.levels},
                    key=lambda item: (len(item), item))
    report["rows"] = [compare_metric(metric, level, baseline, optimized) for level in levels for metric in spec.metrics]
    report["failures"] = {
        level: {"baseline": failure_counts(baseline, level), "optimized": failure_counts(optimized, level)}
        for level in levels
    }
    verdict, reasons = decide(spec, report["rows"], report["failures"], (len(baseline), len(optimized)),
                              unmeasured_levels([*baseline, *optimized]))
    return {**report, "verdict": verdict, "reasons": reasons}
