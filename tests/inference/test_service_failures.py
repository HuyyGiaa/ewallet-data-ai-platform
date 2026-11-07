"""Service orchestration when a stage fails, and model schema self-checks."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from ai_platform.inference import contracts
from ai_platform.inference.assembler import CanonicalFeatureAssembler
from ai_platform.inference.errors import ModelBackendError
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.inference.service import InferenceService
from tests.inference.test_service_and_app import (
    FakePredictor,
    FakeProvider,
    RecordingLogger,
    RecordingTelemetry,
    payload,
)


def service(*, provider=None, predictor=None, telemetry=None, logger=None):
    """Compose a service from the given doubles, or the defaults."""
    return InferenceService(
        feature_provider=provider or FakeProvider(),
        assembler=CanonicalFeatureAssembler(),
        predictor=predictor or FakePredictor(),
        telemetry=telemetry or RecordingTelemetry(),
        event_logger=logger or RecordingLogger(),
    )


def predict(inference_service):
    """Run one prediction for the default payload."""
    request = FraudPredictionRequest.model_validate(payload())
    return asyncio.run(inference_service.predict(request, correlation_id="corr-1"))


class StageFailureTest(unittest.TestCase):
    def test_model_failure_is_recorded_and_logged_without_request_data(self):
        telemetry = RecordingTelemetry()
        logger = RecordingLogger()
        with self.assertRaises(ModelBackendError):
            predict(
                service(
                    predictor=FakePredictor(error=ModelBackendError()),
                    telemetry=telemetry,
                    logger=logger,
                )
            )
        self.assertIn(("error", "model_backend_error"), telemetry.events)
        self.assertEqual(
            [
                {
                    "event": "prediction_failed",
                    "correlation_id": "corr-1",
                    "error_code": "model_backend_error",
                    "retryable": True,
                }
            ],
            logger.events,
        )
        self.assertNotIn("user-sensitive-1", json.dumps(logger.events))

    def test_unexpected_failure_is_classified_as_internal(self):
        telemetry = RecordingTelemetry()
        logger = RecordingLogger()
        with self.assertRaises(RuntimeError):
            predict(
                service(
                    provider=FakeProvider(error=RuntimeError("secret")),
                    telemetry=telemetry,
                    logger=logger,
                )
            )
        self.assertIn(("error", "internal_error"), telemetry.events)
        self.assertEqual("internal_error", logger.events[0]["error_code"])
        self.assertFalse(logger.events[0]["retryable"])
        self.assertNotIn("secret", json.dumps(logger.events))

    def test_failed_feature_stage_never_calls_the_model(self):
        predictor = FakePredictor()
        with self.assertRaises(RuntimeError):
            predict(
                service(
                    provider=FakeProvider(error=RuntimeError("boom")),
                    predictor=predictor,
                )
            )
        self.assertEqual([], predictor.calls)

    def test_success_logs_the_serving_model_identity(self):
        logger = RecordingLogger()
        predict(service(logger=logger))
        self.assertEqual(
            [
                {
                    "event": "prediction_completed",
                    "correlation_id": "corr-1",
                    "model_name": "ewallet-fraud-classifier",
                    "model_version": "7",
                }
            ],
            logger.events,
        )


class ModelSchemaSelfCheckTest(unittest.TestCase):
    def test_current_contract_is_valid(self):
        self.assertIsNone(contracts.validate_model_schema())

    def test_a_drifted_contract_stops_the_service(self):
        cases = (
            ("REQUEST_TIME_FEATURE_COLUMNS", ("amount",)),
            ("HISTORICAL_FEATURE_COLUMNS", ("one",)),
            ("DERIVED_FEATURE_COLUMNS", ("one",)),
            ("MODEL_INPUT_COLUMNS", ("one",)),
            ("MODEL_INPUT_COLUMNS", tuple(reversed(contracts.MODEL_INPUT_COLUMNS))),
        )
        for name, value in cases:
            with self.subTest(name=name, size=len(value)):
                with patch.object(contracts, name, value):
                    with self.assertRaises(RuntimeError):
                        contracts.validate_model_schema()


if __name__ == "__main__":
    unittest.main()
