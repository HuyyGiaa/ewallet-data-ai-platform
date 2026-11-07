"""Autoscaling timings come from samples, never from estimates."""

from __future__ import annotations

import pytest

from ops.experiments.autoscale_summary import summarise


def rows(pairs):
    return [{"ts": ts, "ready": ready, "hpa": {"metrics": {"s0-prometheus": value}}} for ts, ready, value in pairs]


def test_scale_out_and_back_are_measured_from_the_load_window():
    samples = rows([(90, 1, "0"), (100, 1, "0"), (130, 2, "86"), (160, 3, "150"), (400, 3, "170"),
                    (500, 3, "0"), (590, 2, "0"), (650, 1, "0")])
    result = summarise(samples, load_start=100, load_end=400)
    assert result["replicas_at_load_start"] == 1
    assert (result["peak_ready_replicas"], result["first_scale_out_seconds"], result["seconds_to_peak"]) == (3, 30, 60)
    assert result["scale_down_to_min_seconds"] == 250 and result["returned_to_min"]
    assert result["max_trigger_value"] == 170


def test_a_run_that_never_returns_says_so():
    result = summarise(rows([(100, 1, "0"), (200, 2, "29333m"), (300, 2, "0")]), 100, 250)
    assert result["returned_to_min"] is False and result["scale_down_to_min_seconds"] is None
    assert result["max_trigger_value"] == pytest.approx(29.333)


def test_collect_errors_are_skipped():
    with pytest.raises(ValueError):
        summarise([{"ts": 1, "collect_error": "TimeoutExpired"}], 0, 1)
