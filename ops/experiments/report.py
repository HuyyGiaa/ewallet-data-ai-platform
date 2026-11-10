"""Write comparison artifacts in JSON, CSV and Markdown."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Mapping

from ops.experiments import single_run
from ops.experiments.spec import ExperimentSpec


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{value:,.3f}"


def render_csv(report: Mapping[str, Any]) -> str:
    """Render one line per level and metric."""

    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["level", "metric", "better_when", "baseline_median", "baseline_min", "baseline_max",
                     "optimized_median", "optimized_min", "optimized_max", "improvement_percent", "within_run_spread"])
    for row in report["rows"]:
        left, right = row["baseline"], row["optimized"]
        writer.writerow([row["level"], row["metric"], row["better_when"], left["median"], left["min"], left["max"],
                         right["median"], right["min"], right["max"], row["improvement_percent"], row["within_run_spread"]])
    return stream.getvalue()


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the comparison; a positive improvement always means better."""

    lines = [
        f"# {report['experiment_id']}: {report['baseline']} vs {report['optimized']}",
        "",
        f"**Verdict:** `{report['verdict']}`",
        "",
        *[f"- {reason}" for reason in report["reasons"]],
        "",
        f"Runs: {report['runs']['baseline']} baseline, {report['runs']['optimized']} optimized. "
        f"Variable: {report['variable']}. Positive improvement means better.",
    ]
    if report["failures"]:
        lines += ["", "| Level | Baseline failed | Optimized failed |", "|---|---:|---:|"]
        for level, arms in report["failures"].items():
            lines.append(f"| {level} | {arms['baseline']['failed']}/{arms['baseline']['requests']} | "
                         f"{arms['optimized']['failed']}/{arms['optimized']['requests']} |")
    if report["rows"]:
        lines += ["", "| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |",
                  "|---|---|---|---:|---:|---:|---|"]
        for row in report["rows"]:
            change = row["improvement_percent"]
            shown = "n/a" if change is None else f"{change:+.1f}%"
            lines.append(f"| {row['level']} | {row['metric']} | {row['better_when']} | "
                         f"{_number(row['baseline']['median'])} | {_number(row['optimized']['median'])} | "
                         f"{shown} | {row['within_run_spread']} |")
    return "\n".join(lines) + "\n"


def render_checklist(spec: ExperimentSpec) -> str:
    """Render the owner's screenshot checklist."""

    lines = [f"# {spec.experiment_id} evidence checklist", ""]
    lines += [f"- [ ] {item}" for item in spec.screenshots]
    return "\n".join(lines) + "\n"


def write_comparison(report: Mapping[str, Any], directory: Path) -> None:
    """Write ``comparison/`` and ``verdict.md`` for one experiment (multi-run or single-run report)."""

    single = report.get("methodology") == single_run.METHODOLOGY
    to_csv, to_markdown = (single_run.render_csv, single_run.render_markdown) if single else (render_csv, render_markdown)
    target = directory / "comparison"
    target.mkdir(parents=True, exist_ok=True)
    (target / "metrics.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (target / "comparison.csv").write_text(to_csv(report), encoding="utf-8")
    (target / "comparison.md").write_text(to_markdown(report), encoding="utf-8")
    title = f"# Verdict: {report['verdict']}" + (" (single-run, observed; not a statistical result)" if single else "")
    verdict = "\n".join([title, "", *[f"- {item}" for item in report["reasons"]]])
    (directory / "verdict.md").write_text(verdict + "\n", encoding="utf-8")
