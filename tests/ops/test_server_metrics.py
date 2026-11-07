"""Server-side histogram deltas for load runs."""

from __future__ import annotations

from ops.experiments.server_metrics import delta, quantile

FINE_BEFORE = """# HELP request_predict_seconds x
request_predict_seconds_bucket{le="0.005"} 0
request_predict_seconds_bucket{le="0.01"} 10
request_predict_seconds_bucket{le="0.025"} 10
request_predict_seconds_bucket{le="+Inf"} 10
request_predict_seconds_sum 0.08
request_predict_seconds_count 10
"""
FINE_AFTER = """request_predict_seconds_bucket{le="0.005"} 0
request_predict_seconds_bucket{le="0.01"} 60
request_predict_seconds_bucket{le="0.025"} 110
request_predict_seconds_bucket{le="+Inf"} 110
request_predict_seconds_sum 1.38
request_predict_seconds_count 110
"""
COARSE = """stage_latency_seconds_bucket{le="0.0",stage="model_inference",otel_scope_name="s"} 0
stage_latency_seconds_bucket{le="5.0",stage="model_inference",otel_scope_name="s"} {n}
stage_latency_seconds_bucket{le="+Inf",stage="model_inference",otel_scope_name="s"} {n}
stage_latency_seconds_sum{stage="model_inference",otel_scope_name="s"} {s}
stage_latency_seconds_count{stage="model_inference",otel_scope_name="s"} {n}
stage_latency_seconds_count{stage="feature_retrieval",otel_scope_name="s"} {n}
stage_latency_seconds_sum{stage="feature_retrieval",otel_scope_name="s"} {s}
"""


def test_only_the_run_traffic_is_counted():
    [row] = delta(FINE_BEFORE, FINE_AFTER, "request_predict_seconds")
    assert row["count"] == 100
    assert row["mean_ms"] == 13.0
    assert row["quantiles_resolved"] is True
    assert 5 < row["p50_ms"] <= 10 < row["p95_ms"] <= 25


def test_coarse_buckets_give_a_mean_but_no_quantiles():
    before = COARSE.replace("{n}", "5").replace("{s}", "0.1")
    after = COARSE.replace("{n}", "105").replace("{s}", "2.1")
    rows = delta(before, after, "stage_latency_seconds", {"stage": "model_inference"})
    assert rows == [{"labels": {"stage": "model_inference"}, "count": 100, "mean_ms": 20.0,
                     "quantiles_resolved": False}]


def test_quantile_in_the_overflow_bucket_is_unknown():
    assert quantile({0.01: 1, float("inf"): 100}, 0.95) is None
