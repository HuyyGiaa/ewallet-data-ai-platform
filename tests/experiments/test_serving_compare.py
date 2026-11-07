"""Replica statistics and the fixed-versus-KEDA comparison."""

from __future__ import annotations

import subprocess

from ops.experiments.serving_compare import compare, replica_stats


def rows(*ready):
    return [{"ts": index * 10.0, "ready": value} for index, value in enumerate(ready)]


def test_reaction_times_and_pod_seconds():
    stats = replica_stats(rows(1, 1, 2, 3, 3, 2, 1, 1))
    assert (stats["peak"], stats["final"]) == (3, 1)
    assert stats["seconds_to_peak"] == 30
    assert stats["seconds_from_peak_to_final"] == 20
    assert stats["pod_seconds"] == 130


def test_collection_gaps_do_not_become_numbers():
    assert replica_stats([{"ts": 1.0, "collect_error": "x"}])["pod_seconds"] is None


def load(fixture="a", source="s", rps=10.0):
    return {"fixture_sha256": fixture, "source_sha": source, "requests_per_second": rps, "average_ms": 1.0, "p50_ms": 1.0,
            "p95_ms": 2.0, "p99_ms": 3.0, "failure_rate": 0.0}


def arm(**changes):
    return {name: {"load": load(**changes), "replicas": {"pod_seconds": 100.0}} for name in ("low", "medium", "high")}


def test_the_cost_is_reported_with_the_difference():
    result = compare(arm(), arm(rps=15.0))["low"]
    assert result["requests_per_second_delta"] == 5.0
    assert result["pod_seconds"] == {"fixed": 100.0, "keda": 100.0}


def test_different_fixtures_are_not_compared():
    assert compare(arm(), arm(fixture="b"))["low"]["status"] == "NOT_COMPARABLE"


def test_a_missing_run_is_not_compared():
    missing = arm()
    missing["high"]["load"] = None
    assert compare(arm(), missing)["high"]["status"] == "NOT_COMPARABLE"


def test_run_refuses_without_approval():
    done = subprocess.run(["scripts/experiments/serving_keda.sh", "run", "fixed", "feature-api", "/tmp/unused"],
                          capture_output=True, text=True)
    assert done.returncode == 1
    assert "EXP_APPROVED" in done.stderr
