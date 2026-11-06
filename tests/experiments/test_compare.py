"""Verdict rules: the comparison must refuse to overclaim."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.experiments import compare as c
from ops.experiments.adapters import load_arm, run_from_summary
from ops.experiments.compare import Run, compare
from ops.experiments.report import render_csv, render_markdown, write_comparison
from ops.experiments.spec import SpecError, load_spec, parse_spec

ROOT = Path(__file__).resolve().parents[2]
SPEC = parse_spec({
    "experiment_id": "T", "hypothesis": "h", "variable": "v", "baseline": "a", "optimized": "b",
    "controls": ["workload"], "primary_metric": "e2e", "min_runs": 2, "significant_percent": 10,
    "metrics": [{"name": "e2e", "direction": "lower", "path": "e2e_ms.p95"},
                {"name": "tps", "direction": "higher", "path": "tps"}],
    "screenshots": ["one"],
})


def run(e2e: float, tps: float = 10, *, failed: int = 0, workload: str = "w", level: str = "1", **extra) -> Run:
    summary = {"requests": 12, "failed": failed, "e2e_ms": {"p95": e2e}, "tps": tps, **extra}
    return Run(controls={"workload": workload}, levels={level: summary})


def test_faster_with_separated_runs_is_improved() -> None:
    assert compare(SPEC, [run(100), run(104)], [run(70), run(72)])["verdict"] == c.IMPROVED


def test_slower_is_regressed_and_improvement_is_negative() -> None:
    report = compare(SPEC, [run(70), run(72)], [run(100), run(104)])
    assert report["verdict"] == c.REGRESSED
    assert report["rows"][0]["improvement_percent"] < 0


def test_higher_is_better_metric_points_the_same_way() -> None:
    report = compare(SPEC, [run(100, 10), run(100, 10)], [run(100, 20), run(100, 20)])
    assert next(row for row in report["rows"] if row["metric"] == "tps")["improvement_percent"] == 100


def test_change_inside_run_spread_is_not_significant() -> None:
    assert compare(SPEC, [run(60), run(140)], [run(70), run(100)])["verdict"] == c.NO_SIGNIFICANT_CHANGE


def test_change_below_threshold_is_not_significant() -> None:
    assert compare(SPEC, [run(100), run(100)], [run(95), run(95)])["verdict"] == c.NO_SIGNIFICANT_CHANGE


def test_single_run_is_inconclusive() -> None:
    assert compare(SPEC, [run(100)], [run(50)])["verdict"] == c.INCONCLUSIVE


def test_faster_but_failing_more_is_regressed() -> None:
    report = compare(SPEC, [run(100), run(100)], [run(50, failed=1), run(50)])
    assert report["verdict"] == c.REGRESSED
    assert report["failures"]["1"]["optimized"] == {"requests": 24, "failed": 1}


def test_different_workload_is_invalid_and_reports_no_numbers() -> None:
    report = compare(SPEC, [run(100), run(100)], [run(50, workload="x"), run(50, workload="x")])
    assert report["verdict"] == c.INVALID_COMPARISON
    assert report["rows"] == []


def test_unrecorded_control_is_invalid() -> None:
    report = compare(SPEC, [run(100), run(100)], [Run(controls={}, levels={}), run(50)])
    assert report["verdict"] == c.INVALID_COMPARISON


def test_missing_arm_is_not_executed() -> None:
    assert compare(SPEC, [run(100)], [])["verdict"] == c.NOT_EXECUTED


def test_blocked_level_blocks_the_verdict() -> None:
    blocked = Run(controls={"workload": "w"}, levels={"1": {"status": "BLOCKED_RESOURCE_GUARD"}})
    report = compare(SPEC, [run(100), run(100)], [run(50), blocked])
    assert report["verdict"] == c.INCONCLUSIVE
    assert "BLOCKED_RESOURCE_GUARD" in report["reasons"][0]


def test_better_at_one_level_worse_at_another_is_inconclusive() -> None:
    def both(first: float, second: float) -> Run:
        return Run(controls={"workload": "w"}, levels={
            "1": {"requests": 12, "failed": 0, "e2e_ms": {"p95": first}},
            "8": {"requests": 12, "failed": 0, "e2e_ms": {"p95": second}}})
    report = compare(SPEC, [both(100, 100), both(100, 100)], [both(130, 70), both(130, 70)])
    assert report["verdict"] == c.INCONCLUSIVE


def test_single_run_spec_is_rejected() -> None:
    with pytest.raises(SpecError):
        parse_spec({"experiment_id": "x", "hypothesis": "h", "variable": "v", "baseline": "a", "optimized": "b",
                    "controls": [], "metrics": [{"name": "m", "direction": "lower", "path": "p"}],
                    "primary_metric": "m", "min_runs": 1, "significant_percent": 5, "screenshots": []})


def test_adapter_counts_failures_from_success_count() -> None:
    converted = run_from_summary({"workload_sha256": "s", "levels": {"1": {"status": "MEASURED", "requests": 12,
                                                                           "succeeded": 9}}}, {})
    assert converted.levels["1"]["failed"] == 3
    assert converted.controls["workload_sha256"] == "s"


@pytest.mark.parametrize("path", sorted((ROOT / "configs/experiments").glob("*.json")), ids=lambda p: p.stem)
def test_shipped_specs_load(path: Path) -> None:
    spec = load_spec(path)
    assert spec.screenshots and spec.controls


def historical(path: str, spec) -> Run:
    summary = json.loads((ROOT / "docs/evidence/llm_agent" / path / "summary.json").read_text(encoding="utf-8"))
    return run_from_summary(summary, {name: "same" for name in spec.controls if name != "workload_sha256"})


SPEC_V2 = load_spec(ROOT / "configs/experiments/exp-v2-llmd-routing.json")
SPEC_V1 = load_spec(ROOT / "configs/experiments/exp-v1-vllm-cpu.json")


def test_real_g2_output_fills_every_metric_but_one_run_stays_inconclusive() -> None:
    base = "g2_20261101T124444Z/g2"
    report = compare(SPEC_V2, [historical(f"{base}/b2", SPEC_V2)], [historical(f"{base}/o2", SPEC_V2)])
    assert report["verdict"] == c.INCONCLUSIVE
    empty = {row["metric"] for row in report["rows"] if row["improvement_percent"] is None}
    assert empty <= {"tpot_ms"}, empty


def test_real_o1_output_fills_every_metric() -> None:
    base = "o1_20261101T052939Z/o1"
    report = compare(SPEC_V1, [historical(f"{base}/b0-prime", SPEC_V1)], [historical(f"{base}/c1-thread-bind", SPEC_V1)])
    assert report["verdict"] == c.INCONCLUSIVE
    assert not [row for row in report["rows"] if row["improvement_percent"] is None]


def test_reports_are_written_and_never_hide_failures(tmp_path) -> None:
    report = compare(SPEC, [run(100, failed=2), run(100)], [run(50, failed=3), run(50)])
    write_comparison(report, tmp_path)
    assert "3/24" in (tmp_path / "comparison/comparison.md").read_text(encoding="utf-8")
    assert report["verdict"] in (tmp_path / "verdict.md").read_text(encoding="utf-8")
    assert render_csv(report).count("\n") == 3
    assert report["verdict"] in render_markdown(report)


def test_load_arm_reads_raw_runs(tmp_path) -> None:
    run_dir = tmp_path / "raw/run-01"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text('{"levels": {}, "workload_sha256": "s"}', encoding="utf-8")
    (run_dir / "controls.json").write_text('{"image_digest": "d"}', encoding="utf-8")
    assert load_arm(tmp_path)[0].controls == {"workload_sha256": "s", "image_digest": "d"}
