"""A/B statistics and the traffic summary."""

from __future__ import annotations

import pytest

from ops.experiments.ab_stats import average_precision, classification_report, percentile, split_check
from ops.experiments.ml_ab_traffic import run_traffic, summarise


def test_split_check_accepts_noise_and_rejects_a_real_skew():
    assert split_check({"a": 905, "b": 95}, {"a": 90, "b": 10}).within_tolerance
    assert not split_check({"a": 700, "b": 300}, {"a": 90, "b": 10}).within_tolerance


def test_any_traffic_on_a_zero_weight_arm_is_a_leak():
    assert not split_check({"a": 999, "b": 1}, {"a": 100, "b": 0}).within_tolerance


def test_average_precision_matches_a_hand_computed_case():
    assert average_precision([1, 0, 1, 0], [0.9, 0.8, 0.7, 0.1]) == pytest.approx((1 + 2 / 3) / 2)


def test_average_precision_treats_ties_as_one_step():
    assert average_precision([1, 0], [0.5, 0.5]) == pytest.approx(0.5)


def test_report_counts_errors_exactly():
    report = classification_report([1, 1, 0, 0], [0.9, 0.2, 0.8, 0.1], [1, 0, 1, 0])
    assert (report["precision"], report["recall"], report["false_positives"]) == (0.5, 0.5, 1)


def test_percentile_is_nearest_rank():
    assert percentile([1, 2, 3, 4], 0.5) == 2


def fake_send(payload):
    arm = "model-b" if payload["user_id"] in {"u1"} else "model-a"
    version = {"model-a": "1", "model-b": "2"}[arm]
    return 200, {"model_version": version, "fraud_probability": 0.2,
                 "experiment": {"assigned_arm": arm, "served_arm": arm, "fallback": False}}


def test_summary_keeps_failures_and_detects_sticky_users():
    entities = [{"user_id": f"u{i}", "transaction_id": f"t{i}"} for i in range(4)]
    records = run_traffic(fake_send, entities, 2)
    records.append({"user_id": "u9", "status": 503, "latency_seconds": 0.1, "assigned_arm": None,
                    "served_arm": None, "model_version": None, "fallback": None, "fraud_probability": None})
    summary = summarise(records, {"model-a": 75, "model-b": 25})
    assert summary["total_requests"] == 9
    assert summary["failed_requests"] == 1
    assert summary["non_sticky_users"] == 0
    assert summary["version_leaks"] == 0
    assert summary["arms"]["model-b"]["requests"] == 2


def test_user_on_two_arms_is_reported():
    rows = [{"user_id": "u", "status": 200, "latency_seconds": 0.1, "assigned_arm": arm, "served_arm": arm,
             "model_version": version, "fallback": False, "fraud_probability": 0.1}
            for arm, version in (("model-a", "1"), ("model-b", "2"))]
    assert summarise(rows, {"model-a": 50, "model-b": 50})["non_sticky_users"] == 1


def test_ml_ab_script_refuses_cloud_actions_without_approval():
    import subprocess

    result = subprocess.run(["scripts/experiments/ml_ab.sh", "deploy"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "EXP_APPROVED" in result.stderr
