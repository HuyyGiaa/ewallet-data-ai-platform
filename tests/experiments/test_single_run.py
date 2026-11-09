"""Single-run comparator: observed differences of one pair, never a statistical verdict."""

from __future__ import annotations

import json
from pathlib import Path

from ops.experiments import single_run as s
from ops.experiments.cli import main as cli_main
from ops.experiments.compare import Run, compare
from ops.experiments.report import write_comparison
from ops.experiments.spec import load_spec, parse_spec

ROOT = Path(__file__).resolve().parents[2]
SPEC = parse_spec({
    "experiment_id": "T", "hypothesis": "h", "variable": "v", "baseline": "a", "optimized": "b",
    "controls": ["workload"], "primary_metric": "tps", "min_runs": 3, "significant_percent": 10,
    "metrics": [{"name": "tps", "direction": "higher", "path": "output_tokens_per_second"},
                {"name": "ttft", "direction": "lower", "path": "ttft_ms.p50"},
                {"name": "cpu", "direction": "lower", "path": "derived.cpu_cores_mean"}],
    "screenshots": ["one"],
})


def level(tps: float = 10, ttft: float = 100, *, failed: int = 0, status: str = "MEASURED", **extra) -> dict:
    return {"status": status, "requests": 12, "failed": failed, "succeeded": 12 - failed,
            "output_tokens_per_second": tps, "ttft_ms": {"p50": ttft}, "error_rate": failed / 12,
            "timeout_rate": 0.0, "derived": {"cpu_cores_mean": 2.0}, **extra}


def run(*levels: dict, workload: str = "w") -> Run:
    return Run(controls={"workload": workload}, levels={str(2 ** i): item for i, item in enumerate(levels)})


def row(report: dict, level_name: str, metric: str) -> dict:
    return next(item for item in report["rows"] if item["level"] == level_name and item["metric"] == metric)


def test_one_pair_is_valid_and_reports_an_observed_improvement() -> None:
    report = s.compare_single(SPEC, [run(level(10), level(10))], [run(level(15), level(12))])
    assert report["methodology"] == "single-run" and report["statistical_inference"] == "none"
    assert report["valid"] is True and report["verdict"] == s.OBSERVED_IMPROVEMENT
    assert row(report, "1", "tps")["observed_change_percent"] == 50
    assert row(report, "2", "tps")["observed_change_percent"] == 20


def test_direction_is_respected_for_lower_is_better() -> None:
    report = s.compare_single(SPEC, [run(level(ttft=100))], [run(level(ttft=80))])
    assert row(report, "1", "ttft")["observed_change_percent"] == 20


def test_regression_mixed_and_no_clear_change() -> None:
    assert s.compare_single(SPEC, [run(level(10))], [run(level(8))])["verdict"] == s.OBSERVED_REGRESSION
    assert s.compare_single(SPEC, [run(level(10), level(10))], [run(level(13), level(8))])["verdict"] == s.OBSERVED_MIXED
    quiet = s.compare_single(SPEC, [run(level(10))], [run(level(10.5))])
    assert quiet["verdict"] == s.NO_CLEAR_OBSERVED_CHANGE and "not a statistical test" in quiet["reasons"][0]


def test_more_failed_requests_cannot_be_an_improvement() -> None:
    faster_but_failing = s.compare_single(SPEC, [run(level(10))], [run(level(20, failed=2))])
    assert faster_but_failing["verdict"] == s.OBSERVED_MIXED
    assert any("failed more requests" in item for item in faster_but_failing["reasons"])
    assert faster_but_failing["failures"]["1"]["optimized"]["failed"] == 2
    only_failing = s.compare_single(SPEC, [run(level(10))], [run(level(10, failed=1))])
    assert only_failing["verdict"] == s.OBSERVED_REGRESSION


def test_zero_and_missing_values_have_no_percentage() -> None:
    assert s.observed_change(SPEC.metrics[0], 0, 5) == (None, "baseline is 0")
    assert s.observed_change(SPEC.metrics[0], 0, 0) == (0.0, None)
    assert s.observed_change(SPEC.metrics[0], None, 5) == (None, "missing in baseline")
    report = s.compare_single(SPEC, [run(level(0))], [run(level(0))])
    assert report["verdict"] == s.NO_CLEAR_OBSERVED_CHANGE


def test_validity_is_separate_from_the_outcome() -> None:
    two = s.compare_single(SPEC, [run(level()), run(level())], [run(level())])
    assert two["verdict"] == s.INVALID and not two["valid"] and "exactly 1" in two["validity_problems"][0]
    drift = s.compare_single(SPEC, [run(level())], [run(level(), workload="other")])
    assert drift["verdict"] == s.INVALID and "workload differs" in drift["validity_problems"][0]
    blocked = s.compare_single(SPEC, [run(level())], [run(level(status="RAW_ROW_MISMATCH"))])
    assert blocked["verdict"] == s.INVALID
    uneven = s.compare_single(SPEC, [run(level(), level())], [run(level())])
    assert uneven["verdict"] == s.INVALID and "levels differ" in uneven["validity_problems"][0]
    assert s.compare_single(SPEC, [], [run(level())])["verdict"] == s.NOT_EXECUTED


def test_extra_metrics_include_cpu_efficiency_and_failures() -> None:
    report = s.compare_single(SPEC, [run(level(10))], [run(level(12))])
    used = row(report, "1", "tokens_per_second_per_used_cpu_core")
    assert used["baseline"] == 5 and used["optimized"] == 6
    assert {"itl_p50_ms", "error_rate", "timeout_rate", "tool_calls_valid"} <= {item["metric"] for item in report["rows"]}


def test_per_replica_distribution_is_reported_for_exp_v2() -> None:
    dist = {"requests_share": {"pod-aaaaa": 0.5, "pod-bbbbb": 0.5}, "requests_max_share": 0.5, "pods_serving": 2}
    report = s.compare_single(SPEC, [run(level(distribution=dist))], [run(level(distribution=dist, epp={"x": 1}))])
    assert report["distribution"]["1"]["optimized"]["epp"] == {"x": 1}
    assert "aaaaa=0.50" in s.render_markdown(report)


def test_written_artifacts_say_single_run_and_never_significance(tmp_path) -> None:
    report = s.compare_single(SPEC, [run(level(10))], [run(level(15))])
    write_comparison(report, tmp_path)
    text = (tmp_path / "comparison/comparison.md").read_text(encoding="utf-8")
    assert "SINGLE-RUN" in text and "no significance test" in text and "Within spread" not in text
    assert "single-run" in (tmp_path / "verdict.md").read_text(encoding="utf-8")
    assert (tmp_path / "comparison/comparison.csv").read_text(encoding="utf-8").startswith("methodology,")
    for verdict in ("NO_SIGNIFICANT_CHANGE", "IMPROVED`", "INCONCLUSIVE"):
        assert verdict not in text


def test_multi_run_comparator_is_unchanged_for_one_run() -> None:
    assert compare(SPEC, [run(level(10))], [run(level(20))])["verdict"] == "INCONCLUSIVE"


def write_session(directory: Path, profile: str | None) -> None:
    for arm, tps in (("baseline", 10), ("optimized", 20)):
        raw = directory / arm / "raw" / "run-01"
        raw.mkdir(parents=True)
        (raw / "summary.json").write_text(json.dumps({"workload_sha256": "w", "levels": {
            "1": {"status": "MEASURED", "requests": 12, "succeeded": 12, "output_tokens_per_second": tps,
                  "e2e_ms": {"p95": 100}}}}))
    if profile:
        (directory / "manifest.json").write_text(json.dumps({"settings": {"profile": profile}}))


def test_cli_picks_the_comparator_from_the_manifest(tmp_path) -> None:
    spec = ROOT / "configs/experiments/exp-v1-vllm-cpu.json"
    load_spec(spec)
    single, formal = tmp_path / "single", tmp_path / "formal"
    write_session(single, "single")
    write_session(formal, "formal")
    assert cli_main(["compare", "--spec", str(spec), "--dir", str(single)]) == 0
    assert cli_main(["compare", "--spec", str(spec), "--dir", str(formal)]) == 0
    single_report = json.loads((single / "comparison/metrics.json").read_text())
    formal_report = json.loads((formal / "comparison/metrics.json").read_text())
    # The fixture records no controls, so each comparator refuses it in its own vocabulary.
    assert single_report["methodology"] == "single-run" and single_report["verdict"] == s.INVALID
    assert "methodology" not in formal_report and formal_report["verdict"] == "INVALID_COMPARISON"


REHEARSAL = ROOT / "docs/evidence/edai2/llm/benchmark_rehearsal_20261109"


def test_real_client_summaries_parse_with_every_metric_present() -> None:
    # Shape check on the rehearsal's real run-01 files only: no verdict is asserted or reported.
    from ops.experiments.adapters import load_arm
    import shutil, tempfile
    for experiment, spec_name, levels in (("exp-v1", "exp-v1-vllm-cpu", 3), ("exp-v2", "exp-v2-llmd-routing", 4)):
        spec = load_spec(ROOT / f"configs/experiments/{spec_name}.json")
        with tempfile.TemporaryDirectory() as tmp:
            for arm in ("baseline", "optimized"):
                shutil.copytree(REHEARSAL / experiment / arm / "raw/run-01", Path(tmp) / arm / "raw/run-01")
            report = s.compare_single(spec, load_arm(Path(tmp) / "baseline"), load_arm(Path(tmp) / "optimized"))
        assert report["valid"], report["validity_problems"]
        assert len(report["failures"]) == levels
        assert len(report["rows"]) == levels * (len(spec.metrics) + len(s.EXTRA_METRICS))
        assert not [row for row in report["rows"] if row["note"]]
        assert bool(report["distribution"]) is (experiment == "exp-v2")
