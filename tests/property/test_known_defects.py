"""Executable reports of confirmed, still unresolved defects.

Each test states the correct behaviour and is a strict ``xfail``: it passes as
"expected failure" today and turns the suite red as soon as the defect is
fixed, which is the prompt to delete the marker. Production code was left
unchanged. See docs/edai2/testing/B1_FINDINGS.md.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_platform.inference.app import create_app
from ai_platform.inference.assembler import CanonicalFeatureAssembler
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.inference.service import InferenceService
from ai_platform.inference.wire import FraudV2WireContract
from ai_platform.llm_agent.prompt_safety import screen_prompt
from tests.inference.support import MODEL_NAME, MODEL_VERSION, SilentEventLogger
from tests.inference.test_app_error_paths import tolerate_reraised_server_error
from tests.inference.test_service_and_app import (
    FakePredictor,
    RecordingTelemetry,
    asgi_request,
    payload,
)
from tests.property.fraud_api_harness import PREDICT_PATH, fraud_app


class FailingFeatureProvider:
    """Provider raising outside the sanitized error hierarchy."""

    async def get_features(self, request):
        """Raise an unexpected exception."""
        raise RuntimeError("unexpected")

    async def ready(self):
        """Report the provider as ready."""
        return True


def _internal_error_app(telemetry: RecordingTelemetry):
    service = InferenceService(
        feature_provider=FailingFeatureProvider(),
        assembler=CanonicalFeatureAssembler(),
        predictor=FakePredictor(),
        telemetry=telemetry,
        event_logger=SilentEventLogger(),
    )
    return tolerate_reraised_server_error(create_app(service=service, telemetry=telemetry))


def _decode(prediction: object):
    return FraudV2WireContract().decode_response(
        {
            "outputs": [
                {"name": "fraud_probability", "datatype": "FP64", "data": [0.1]},
                {"name": "fraud_prediction", "datatype": "INT64", "data": [prediction]},
            ]
        },
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        correlation_id="corr",
    )


@pytest.mark.xfail(
    strict=True,
    raises=OverflowError,
    reason="B1-F1: a schema-valid edge timestamp overflows astimezone() and answers 500",
)
@pytest.mark.parametrize(
    "timestamp", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"]
)
def test_edge_of_range_timestamps_are_client_errors(timestamp):
    status, _, _ = asyncio.run(
        asgi_request(
            fraud_app(), "POST", PREDICT_PATH, payload(event_timestamp=timestamp)
        )
    )
    assert 400 <= status < 500


@pytest.mark.xfail(
    strict=True, reason="B1-F2: service and exception handler both count internal_error"
)
def test_internal_error_is_counted_once_per_request():
    telemetry = RecordingTelemetry()
    asyncio.run(
        asgi_request(_internal_error_app(telemetry), "POST", PREDICT_PATH, payload())
    )
    assert telemetry.events.count(("error", "internal_error")) == 1


@pytest.mark.xfail(
    strict=True,
    reason="B1-F3: the 500 is answered outside the correlation middleware",
)
def test_internal_error_keeps_the_correlation_header():
    _, headers, _ = asyncio.run(
        asgi_request(
            _internal_error_app(RecordingTelemetry()),
            "POST",
            PREDICT_PATH,
            payload(),
            {"x-correlation-id": "corr-500"},
        )
    )
    assert headers.get("x-correlation-id") == "corr-500"


@pytest.mark.xfail(
    strict=True, reason="B1-F4: decode_response truncates or coerces fraud_prediction"
)
@pytest.mark.parametrize("prediction", [1.9, True, "1"])
def test_model_prediction_must_already_be_an_integer(prediction):
    with pytest.raises(Exception):
        _decode(prediction)


@pytest.mark.xfail(
    strict=True, reason="B1-F5: lax mode coerces booleans and strings into amount"
)
@pytest.mark.parametrize("amount", [True, "100"])
def test_amount_must_be_a_json_number(amount):
    with pytest.raises(ValueError):
        FraudPredictionRequest.model_validate(payload(amount=amount))


# B1-F7, fixed in G2: kept as a regular regression test.
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("0912345678-5", "0912345678"),
        ("0912 345 678-5", "0912 345 678"),
        ("sdt0912345678", "0912345678"),
        ("a@b.com-5", "a@b.com"),
    ],
)
def test_a_phone_or_email_next_to_a_digit_is_still_masked(text, secret):
    assert secret not in screen_prompt(text).text
