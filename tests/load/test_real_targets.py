"""Target selection, response contracts and fixture fitting; no network is used."""

from __future__ import annotations

import pytest

from ops.experiments.fraud_entities import fit_timestamp, prepare
from scripts.testing.real_load_report import summarise
from tests.load.real_targets import (
    DEFAULT_FEATURES,
    LoadConfigurationError,
    check_feature,
    check_inference,
    feature_request,
    inference_request,
    select_target,
    validate_host,
)

ENTITY = {"transaction_id": "t1", "event_timestamp": "2026-06-01T00:00:00Z", "user_id": "u", "account_id": "a",
          "device_id": "d", "merchant_id": "m", "type": "payment", "amount": 10.0, "currency": "VND", "channel": "app"}
GOOD_PREDICTION = {"fraud_probability": 0.2, "fraud_prediction": 0, "model_version": "1"}


def test_targets_are_separate_and_unknown_ones_refused():
    assert select_target("feature").path != select_target("inference").path
    with pytest.raises(LoadConfigurationError):
        select_target("both")


def test_remote_target_needs_acknowledgement():
    validate_host("http://127.0.0.1:18080", None)
    validate_host("https://api.example.com", "I_OWN_THIS_TARGET")
    with pytest.raises(LoadConfigurationError):
        validate_host("https://api.example.com", None)


def test_merchant_features_are_only_requested_when_a_merchant_exists():
    assert len(feature_request(ENTITY)["feature_names"]) == 4
    assert all("merchant" not in name for name in feature_request({**ENTITY, "merchant_id": None})["feature_names"])


def test_every_inference_request_has_its_own_transaction_id():
    assert inference_request(ENTITY)["transaction_id"] != inference_request(ENTITY)["transaction_id"]


def test_feature_contract():
    sent = {"feature_names": list(DEFAULT_FEATURES)}
    good = {"features": {name: 1 for name in DEFAULT_FEATURES}, "oldest_feature_timestamp": "t", "correlation_id": "c"}
    assert check_feature(sent, good, {}) is None
    assert check_feature(sent, {**good, "features": {}}, {})
    assert check_feature(sent, {**good, "oldest_feature_timestamp": None}, {})


@pytest.mark.parametrize(
    "change", [{"fraud_probability": 1.5}, {"fraud_probability": True}, {"fraud_prediction": 2}, {"model_version": "2"}, {"transaction_id": "x"}]
)
def test_inference_contract_rejects_a_bad_response(change):
    sent = {"transaction_id": "t"}
    assert check_inference(sent, {"transaction_id": "t", **GOOD_PREDICTION, **change}, {"model_version": "1"})


def test_inference_contract_accepts_a_good_response():
    assert check_inference({"transaction_id": "t"}, {"transaction_id": "t", **GOOD_PREDICTION}, {"model_version": "1"}) is None


VIEWS = {"user_behavior": "2026-05-31T23:50:00Z", "account_behavior": "2026-05-31T23:52:00Z",
         "device_behavior": "2026-05-31T23:51:00Z", "merchant_behavior": "2026-05-31T23:45:00Z"}


def test_timestamp_is_fitted_just_after_the_newest_view():
    row, outcome = fit_timestamp(ENTITY, VIEWS)
    assert outcome == "fitted"
    assert row["event_timestamp"] == "2026-05-31T23:52:01Z"


def test_views_further_apart_than_the_policy_are_dropped_not_relaxed():
    row, outcome = fit_timestamp(ENTITY, {**VIEWS, "merchant_behavior": "2026-05-31T22:00:00Z"})
    assert (row, outcome) == (None, "views_too_far_apart")


def test_merchant_view_is_ignored_when_the_entity_has_no_merchant():
    row, _ = fit_timestamp({**ENTITY, "merchant_id": None}, {**VIEWS, "merchant_behavior": None})
    assert row is not None


def test_missing_view_is_dropped():
    assert fit_timestamp(ENTITY, {**VIEWS, "user_behavior": None}) == (None, "missing_view")


def test_prepare_keeps_every_outcome():
    answers = iter([VIEWS, None, {**VIEWS, "user_behavior": None}])
    rows, outcomes = prepare([ENTITY, ENTITY, ENTITY], lambda _: next(answers))
    assert len(rows) == 1
    assert outcomes == {"fitted": 1, "preflight_failed": 1, "missing_view": 1}


def test_report_keeps_failures_in_the_totals():
    row = {"Request Count": "100", "Failure Count": "5", "Requests/s": "12.5", "Average Response Time": "20.5",
           "50%": "18", "95%": "40", "99%": "60"}
    summary = summarise(row)
    assert (summary["successful_requests"], summary["failure_rate"]) == (95, 0.05)
