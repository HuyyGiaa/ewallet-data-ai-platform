"""Single-run comparison: one baseline run against one optimized run (``EXP_PROFILE=single``).

This is a separate methodology from :mod:`ops.experiments.compare`. It does not reuse that
module's run-to-run spread gate, and it never states statistical significance:

* With one run per arm there is no spread, no confidence interval and no significance
  test. The report shows the *observed* difference at every level, and nothing more.
* **Execution validity and observed outcome are kept apart.** ``valid`` says whether the
  pair can be compared:
  - exactly one run per arm;
  - equal controls;
  - every level measured in both arms;
  - raw rows matching the summary.

  ``verdict`` says what was observed. ``INVALID`` is used only when ``valid`` is false.
  A valid comparison does not need the optimized arm to win.
* The verdict uses the experiment's primary metric and a **fixed threshold**: a level
  counts as changed when the observed difference is at least ``significant_percent`` of
  the spec. This is a reporting rule, not a statistical test, and the report says so.
* **Failed requests and timeouts stay in the counts.** An arm that fails more requests at
  a level cannot be an observed improvement.
* A metric whose baseline is 0 or that is missing in one arm has no percentage
  (``None``), with the reason; it is never treated as 0 % or as a win.

Verdicts (``methodology: single-run``):
- ``OBSERVED_IMPROVEMENT``
- ``OBSERVED_REGRESSION``
- ``OBSERVED_MIXED``
- ``NO_CLEAR_OBSERVED_CHANGE``
- ``INVALID``
- ``NOT_EXECUTED``
"""

from __future__ import annotations

import csv
import io
from typing import Any, Mapping, Sequence

from ops.experiments.compare import Run, control_mismatches, read_path, unmeasured_levels
from ops.experiments.spec import HIGHER_IS_BETTER, LOWER_IS_BETTER, ExperimentSpec, Metric

METHODOLOGY = "single-run"
SCHEMA = "ewallet-single-run-comparison-v1"
OBSERVED_IMPROVEMENT = "OBSERVED_IMPROVEMENT"
OBSERVED_REGRESSION = "OBSERVED_REGRESSION"
OBSERVED_MIXED = "OBSERVED_MIXED"
NO_CLEAR_OBSERVED_CHANGE = "NO_CLEAR_OBSERVED_CHANGE"
INVALID = "INVALID"
NOT_EXECUTED = "NOT_EXECUTED"
VERDICTS = (OBSERVED_IMPROVEMENT, OBSERVED_REGRESSION, OBSERVED_MIXED, NO_CLEAR_OBSERVED_CHANGE, INVALID, NOT_EXECUTED)
INTERPRETATION = ("One run per arm: the percentages are observed differences between two runs. There is no "
                  "repetition, no confidence interval and no significance test, so this is not a statistically "
                  "proven or stable improvement.")

# Fields every client summary already holds, compared in addition to the spec's metrics.
EXTRA_METRICS = (
    Metric("itl_p50_ms", LOWER_IS_BETTER, "itl_ms.p50"),
    Metric("error_rate", LOWER_IS_BETTER, "error_rate"),
    Metric("timeout_rate", LOWER_IS_BETTER, "timeout_rate"),
    Metric("tool_calls_valid", HIGHER_IS_BETTER, "tool_calls_valid"),
    Metric("tokens_per_second_per_allocated_cpu", HIGHER_IS_BETTER, "output_tokens_per_second_per_allocated_cpu"),
    Metric("tokens_per_second_per_used_cpu_core", HIGHER_IS_BETTER, "derived.tokens_per_used_cpu_core"),
)


def observed_change(metric: Metric, baseline: float | None, optimized: float | None) -> tuple[float | None, str | None]:
    """Observed difference in percent, positive when the optimized run is better.

    Returns the percentage and, when there is none, why.
    """

    if baseline is None or optimized is None:
        return None, "missing in " + ("both arms" if baseline is None and optimized is None
                                      else "baseline" if baseline is None else "optimized")
    if baseline == 0:
        return (0.0, None) if optimized == 0 else (None, "baseline is 0")
    change = 100 * (optimized - baseline) / abs(baseline)
    return (change if metric.direction == HIGHER_IS_BETTER else -change) + 0.0, None  # + 0.0: never "-0.0 %"


def _with_used_cpu(level: Mapping[str, Any]) -> Mapping[str, Any]:
    derived = dict(level.get("derived") or {})
    cores, tokens = derived.get("cpu_cores_mean"), level.get("output_tokens_per_second")
    derived["tokens_per_used_cpu_core"] = tokens / cores if isinstance(tokens, (int, float)) and cores else None
    return {**level, "derived": derived}


def _failures(level: Mapping[str, Any]) -> dict[str, Any]:
    requests = int(level.get("requests", 0))
    return {"requests": requests, "failed": int(level.get("failed", 0)), "succeeded": int(level.get("succeeded", 0)),
            "timeout_rate": level.get("timeout_rate"), "error_rate": level.get("error_rate")}


def _distribution(level: Mapping[str, Any]) -> dict[str, Any] | None:
    dist = level.get("distribution")
    if not dist:
        return None
    return {"requests_share": dist.get("requests_share"), "requests_max_share": dist.get("requests_max_share"),
            "pods_serving": dist.get("pods_serving"), "epp": level.get("epp"),
            "prefix_cache_hit_rate": (level.get("derived") or {}).get("prefix_cache_hit_rate")}


def validity_problems(spec: ExperimentSpec, baseline: Sequence[Run], optimized: Sequence[Run]) -> list[str]:
    """Reasons the pair cannot be compared; empty when it can."""

    problems = [f"{arm} has {len(runs)} runs; single-run mode needs exactly 1"
                for arm, runs in (("baseline", baseline), ("optimized", optimized)) if len(runs) != 1]
    if problems:
        return problems
    problems += control_mismatches(spec, baseline, optimized)
    problems += [f"level not measured: {item}" for item in unmeasured_levels([*baseline, *optimized])]
    left, right = set(baseline[0].levels), set(optimized[0].levels)
    if left != right:
        problems.append(f"levels differ: baseline {sorted(left)}, optimized {sorted(right)}")
    return problems


def _levels(runs: Sequence[Run]) -> list[str]:
    return sorted({level for run in runs for level in run.levels}, key=lambda item: (len(item), item))


def decide(spec: ExperimentSpec, rows: Sequence[Mapping[str, Any]],
           failures: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> tuple[str, list[str]]:
    """Observed outcome from the primary metric and the failure counts (pair already valid)."""

    def rate(item: Mapping[str, Any]) -> float:
        return item["failed"] / item["requests"] if item["requests"] else 0.0

    primary = [row for row in rows if row["metric"] == spec.primary_metric]
    comparable = [row for row in primary if row["observed_change_percent"] is not None]
    if not comparable:
        return INVALID, [f"{spec.primary_metric} has no comparable value at any level"]
    threshold = spec.significant_percent
    better = [row["level"] for row in comparable if row["observed_change_percent"] >= threshold]
    worse = [row["level"] for row in comparable if row["observed_change_percent"] <= -threshold]
    more_failed = [level for level, arms in failures.items() if rate(arms["optimized"]) > rate(arms["baseline"])]
    reasons = [f"{spec.primary_metric} observed better by >= {threshold:g} % at {', '.join(better)}"] if better else []
    reasons += [f"{spec.primary_metric} observed worse by >= {threshold:g} % at {', '.join(worse)}"] if worse else []
    reasons += [f"optimized failed more requests at level {level}" for level in more_failed]
    reasons += [f"{spec.primary_metric} not comparable at level {row['level']}: {row['note']}"
                for row in primary if row["observed_change_percent"] is None]
    negative = bool(worse or more_failed)
    if better and negative:
        return OBSERVED_MIXED, reasons
    if negative:
        return OBSERVED_REGRESSION, reasons
    if better:
        return OBSERVED_IMPROVEMENT, reasons
    return NO_CLEAR_OBSERVED_CHANGE, reasons or [
        f"{spec.primary_metric} differs by less than {threshold:g} % at every level (fixed rule, not a statistical test)"]


def compare_single(spec: ExperimentSpec, baseline: Sequence[Run], optimized: Sequence[Run]) -> dict[str, Any]:
    """Compare one baseline run with one optimized run.

    Returns:
        A report with ``methodology: single-run``, the execution validity, the observed
        outcome, one row per level and metric, failure counts and, for EXP-V2, the
        per-replica distribution of each arm.
    """

    report: dict[str, Any] = {
        "schema": SCHEMA, "methodology": METHODOLOGY, "statistical_inference": "none",
        "interpretation": INTERPRETATION, "experiment_id": spec.experiment_id, "variable": spec.variable,
        "baseline": spec.baseline, "optimized": spec.optimized, "primary_metric": spec.primary_metric,
        "threshold_percent": spec.significant_percent,
        "runs": {"baseline": len(baseline), "optimized": len(optimized)},
        "rows": [], "failures": {}, "distribution": {},
    }
    if not baseline or not optimized:
        return {**report, "valid": False, "validity_problems": ["both arms need one run"],
                "verdict": NOT_EXECUTED, "reasons": ["both arms need evidence"]}
    problems = validity_problems(spec, baseline, optimized)
    if problems:
        return {**report, "valid": False, "validity_problems": problems, "verdict": INVALID, "reasons": problems}
    left, right = baseline[0], optimized[0]
    for level in _levels([left, right]):
        b, o = _with_used_cpu(left.levels[level]), _with_used_cpu(right.levels[level])
        for metric in (*spec.metrics, *EXTRA_METRICS):
            bv, ov = read_path(b, metric.path), read_path(o, metric.path)
            change, note = observed_change(metric, bv, ov)
            report["rows"].append({"level": level, "metric": metric.name, "better_when": metric.direction,
                                   "baseline": bv, "optimized": ov, "observed_change_percent": change, "note": note})
        report["failures"][level] = {"baseline": _failures(b), "optimized": _failures(o)}
        dist = {arm: _distribution(item) for arm, item in (("baseline", b), ("optimized", o))}
        if any(dist.values()):
            report["distribution"][level] = dist
    verdict, reasons = decide(spec, report["rows"], report["failures"])
    return {**report, "valid": True, "validity_problems": [], "verdict": verdict, "reasons": reasons}


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{value:,.3f}"


def render_csv(report: Mapping[str, Any]) -> str:
    """One line per level and metric."""

    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["methodology", "level", "metric", "better_when", "baseline", "optimized",
                     "observed_change_percent", "note"])
    for row in report["rows"]:
        writer.writerow([METHODOLOGY, row["level"], row["metric"], row["better_when"], row["baseline"],
                         row["optimized"], row["observed_change_percent"], row["note"] or ""])
    return stream.getvalue()


def render_markdown(report: Mapping[str, Any]) -> str:
    """Human-readable single-run comparison."""

    lines = [
        f"# {report['experiment_id']} SINGLE-RUN comparison: {report['baseline']} vs {report['optimized']}",
        "",
        f"**Methodology:** `single-run` (EXP_PROFILE=single): {report['runs']['baseline']} baseline run, "
        f"{report['runs']['optimized']} optimized run. {report['interpretation']}",
        "",
        f"**Execution validity:** {'VALID' if report.get('valid') else 'NOT VALID'}",
        *[f"- {item}" for item in report.get("validity_problems", [])],
        "",
        f"**Observed outcome:** `{report['verdict']}`",
        *[f"- {reason}" for reason in report["reasons"]],
        "",
        f"Primary metric: `{report['primary_metric']}`. A level counts as changed when the observed difference is "
        f"at least {report['threshold_percent']:g} % (a fixed reporting rule, not a statistical test). "
        f"Variable: {report['variable']}. Positive change means better.",
    ]
    if report["failures"]:
        lines += ["", "| Level | Baseline failed | Optimized failed | Baseline timeout rate | Optimized timeout rate |",
                  "|---|---:|---:|---:|---:|"]
        for level, arms in report["failures"].items():
            b, o = arms["baseline"], arms["optimized"]
            lines.append(f"| {level} | {b['failed']}/{b['requests']} | {o['failed']}/{o['requests']} | "
                         f"{_number(b['timeout_rate'])} | {_number(o['timeout_rate'])} |")
    if report["rows"]:
        lines += ["", "| Level | Metric | Better when | Baseline | Optimized | Observed change |", "|---|---|---|---:|---:|---:|"]
        for row in report["rows"]:
            change = row["observed_change_percent"]
            shown = f"n/a ({row['note']})" if change is None else f"{change:+.1f}%"
            lines.append(f"| {row['level']} | {row['metric']} | {row['better_when']} | {_number(row['baseline'])} | "
                         f"{_number(row['optimized'])} | {shown} |")
    if report["distribution"]:
        lines += ["", "Per-replica request share (observed, not compared):", "",
                  "| Level | Arm | Requests share per pod | Max share | EPP |", "|---|---|---|---:|---|"]
        for level, arms in report["distribution"].items():
            for arm, dist in arms.items():
                if dist:
                    shares = ", ".join(f"{pod[-5:]}={share:.2f}" for pod, share in (dist["requests_share"] or {}).items())
                    lines.append(f"| {level} | {arm} | {shares} | {_number(dist['requests_max_share'])} | "
                                 f"{dist['epp'] or ''} |")
    return "\n".join(lines) + "\n"
