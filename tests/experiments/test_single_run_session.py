"""EXP_PROFILE=single sessions of vllm_bench.sh against the fake cluster.

One run per arm, every formal level, the single-run comparator, three owner checkpoints,
and the same safety paths as the multi-run session.
"""

from __future__ import annotations

import json

import pytest

from tests.experiments.test_bench_campaign_safety import run
from tests.experiments.test_vllm_bench_session import CHART


def clients(state) -> list[str]:
    """Arm labels in the order the benchmark client was started."""
    return [line.split()[0] for line in (state / "clients.log").read_text(encoding="utf-8").splitlines()]


def test_exp_v1_single_run_is_one_pair_at_every_level(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert clients(state) == ["b0-prime", "c1-thread-bind"]
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["profile"] == "single" and manifest["settings"]["methodology"] == "single-run"
    assert manifest["settings"]["levels"] == "1,2,4" and manifest["settings"]["runs_per_arm"] == "1"
    assert manifest["deviations"] == []
    for arm in ("baseline", "optimized"):
        assert sorted(p.name for p in (evidence / arm / "raw").iterdir()) == ["run-01"]
        summary = json.loads((evidence / arm / "raw/run-01/summary.json").read_text(encoding="utf-8"))
        assert sorted(summary["levels"]) == ["1", "2", "4"]
        assert (evidence / "gateway-policy" / arm / "results.json").is_file()
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["methodology"] == "single-run" and report["valid"] is True
    assert report["runs"] == {"baseline": 1, "optimized": 1}
    assert report["verdict"] == "OBSERVED_IMPROVEMENT"
    assert sorted(report["failures"]) == ["1", "2", "4"]
    assert "start exp-v1 session profile=single methodology=single-run" in (evidence / "session.log").read_text()
    assert not (state / "pool").exists() and not (state / "namespace").exists()
    assert "confirmed absent" in (evidence / "safety/cleanup.txt").read_text(encoding="utf-8")


def test_single_run_refuses_more_than_one_run(tmp_path) -> None:
    result, _, state = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="3")
    assert result.returncode != 0 and "EXP_PROFILE=single runs exactly one run per arm" in result.stderr
    assert not (state / "pool").exists()


def test_formal_default_and_smoke_are_unchanged(tmp_path) -> None:
    (tmp_path / "f").mkdir()
    (tmp_path / "s").mkdir()
    formal, evidence, state = run(tmp_path / "f", EXP_RUNS="")
    assert formal.returncode == 0, formal.stderr
    assert len(clients(state)) == 6
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["runs"] == {"baseline": 3, "optimized": 3} and "methodology" not in report
    assert json.loads((evidence / "manifest.json").read_text())["settings"]["methodology"] == "multi-run"
    smoke, evidence, state = run(tmp_path / "s", EXP_PROFILE="smoke", EXP_RUNS="")
    assert smoke.returncode == 0, smoke.stderr
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["levels"] == "1" and manifest["settings"]["methodology"] == "smoke"
    assert json.loads((evidence / "comparison/metrics.json").read_text())["verdict"] == "INCONCLUSIVE"


def test_interactive_single_run_has_three_checkpoints(tmp_path) -> None:
    result, evidence, _ = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", EXP_CAPTURE_MODE="interactive",
                              EXP_CHECKPOINT_RUNS="first", stdin="CONTINUE\n" * 3)
    assert result.returncode == 0, result.stdout + result.stderr
    err = result.stderr
    assert err.count("OWNER EVIDENCE CHECKPOINT") == 2
    assert err.index("EXP-V1 SINGLE-RUN / BASELINE run 01") < err.index("EXP-V1 SINGLE-RUN / OPTIMIZED run 01") \
        < err.index("EXP-V1 (SINGLE-RUN) — COMPARISON CHECKPOINT")
    assert "OBSERVED_IMPROVEMENT" in err and "no significance test" in err
    assert (evidence / "session.log").read_text().count("checkpoint: owner typed CONTINUE") == 3


def test_abort_at_the_first_single_run_checkpoint_cleans_up(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", EXP_CAPTURE_MODE="interactive",
                                  stdin="ABORT\n")
    assert result.returncode != 0 and "aborted by the owner" in result.stderr
    assert clients(state) == ["b0-prime"]
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("owner aborted" in item for item in manifest["deviations"])
    assert not (state / "pool").exists() and not (state / "namespace").exists()


def test_deadline_still_stops_a_single_run_session(tmp_path) -> None:
    result, evidence, state = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", SIM_STOP_AT="1")
    assert result.returncode != 0
    assert clients(state) == ["b0-prime"]
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("time limit" in item for item in manifest["deviations"])
    assert not (state / "pool").exists()


def test_failed_requests_stay_in_the_single_run_comparison(tmp_path) -> None:
    result, evidence, _ = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", SIM_PARTIAL_RUN="2")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["failures"]["1"]["optimized"]["failed"] == 1
    assert report["verdict"] == "OBSERVED_MIXED"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert any("run 01 optimized: measured" in item for item in manifest["deviations"])


def test_raw_rows_dropped_make_the_single_run_invalid(tmp_path) -> None:
    _, evidence, _ = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", SIM_DROP_RAW="1")
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INVALID" and not report["valid"]


def test_replaced_node_makes_the_single_run_invalid(tmp_path) -> None:
    _, evidence, _ = run(tmp_path, EXP_PROFILE="single", EXP_RUNS="", SIM_NODE_REPLACED="yes")
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INVALID" and any("node_names" in item for item in report["validity_problems"])


def test_plan_reports_the_single_run_methodology(tmp_path) -> None:
    result, evidence, state = run(tmp_path, action="plan", EXP_PROFILE="single", EXP_RUNS="",
                                  KUBE_CONTEXT="", EXP_APPROVED="")
    assert result.returncode == 0, result.stderr
    assert "profile=single methodology=single-run" in result.stdout and "runs_per_arm=1" in result.stdout
    assert "levels=1,2,4 " in result.stdout
    assert not (state / "calls.log").exists()


@pytest.mark.skipif(not CHART, reason="G2_ROUTER_CHART unavailable")
def test_exp_v2_single_run_routes_each_arm_once_at_every_level(tmp_path) -> None:
    result, evidence, state = run(tmp_path, experiment="exp-v2", EXP_PROFILE="single", EXP_RUNS="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert clients(state) == ["b2", "o2"]
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert calls.count("helm upgrade --install llmd-router") == 1
    assert "--epp-metrics" not in (evidence / "baseline/logs/run-01/client.log").read_text(encoding="utf-8")
    report = json.loads((evidence / "comparison/metrics.json").read_text(encoding="utf-8"))
    assert report["methodology"] == "single-run" and report["runs"] == {"baseline": 1, "optimized": 1}
    assert sorted(report["failures"], key=int) == ["1", "2", "4", "8"]
    assert report["primary_metric"] == "e2e_p95_ms" and report["verdict"] == "OBSERVED_IMPROVEMENT"
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["levels"] == "1,2,4,8" and manifest["settings"]["nodes"] == "2"
    assert not (state / "pool").exists()
