"""Safety and correctness of the load harness, without starting Locust."""

from __future__ import annotations

import asyncio
from pathlib import Path
import random
import sys
import tempfile
import unittest

from pydantic import ValidationError

from ai_platform.inference.schemas import FraudPredictionRequest
from scripts.testing import locust_report
from tests.inference.test_service_and_app import asgi_request
from tests.load.mock_server import build_mock_app
from tests.load.payloads import (
    invalid_payload,
    replay_seed,
    shared_payload,
    valid_payload,
)
from tests.load.scenarios import (
    DEFAULT_MIX,
    DEPENDENCY_FAULTS,
    FEATURE_BACKEND,
    INTEGRATION_MIX,
)
from tests.load.settings import (
    REMOTE_ACKNOWLEDGEMENT,
    LoadConfigurationError,
    LoadSettings,
    parse_mix,
)


LOCAL_HOST = "http://127.0.0.1:18080"
REMOTE_HOST = "https://fraud-api.example.invalid"


class TargetGuardTest(unittest.TestCase):
    def test_loopback_hosts_are_the_default_allowance(self):
        for host in (LOCAL_HOST, "http://localhost:8080", "http://[::1]:8080"):
            with self.subTest(host=host):
                self.assertEqual("local", LoadSettings.from_env(host, {}).profile)

    def test_remote_host_is_refused_by_the_default_profile(self):
        with self.assertRaises(LoadConfigurationError):
            LoadSettings.from_env(REMOTE_HOST, {})

    def test_acknowledgement_alone_does_not_enable_a_remote_host(self):
        with self.assertRaises(LoadConfigurationError):
            LoadSettings.from_env(
                REMOTE_HOST,
                {"EWALLET_LOAD_ACKNOWLEDGE_REMOTE": REMOTE_ACKNOWLEDGEMENT},
            )

    def test_integration_profile_requires_the_exact_acknowledgement(self):
        for value in (None, "yes"):
            environment = {"EWALLET_LOAD_PROFILE": "integration"}
            if value:
                environment["EWALLET_LOAD_ACKNOWLEDGE_REMOTE"] = value
            with self.subTest(value=value):
                with self.assertRaises(LoadConfigurationError):
                    LoadSettings.from_env(REMOTE_HOST, environment)

    def test_acknowledged_integration_profile_excludes_fault_injection(self):
        environment = {
            "EWALLET_LOAD_PROFILE": "integration",
            "EWALLET_LOAD_ACKNOWLEDGE_REMOTE": REMOTE_ACKNOWLEDGEMENT,
        }
        settings = LoadSettings.from_env(REMOTE_HOST, environment)
        self.assertEqual(parse_mix(INTEGRATION_MIX), dict(settings.mix))
        with self.assertRaises(LoadConfigurationError):
            LoadSettings.from_env(
                REMOTE_HOST,
                {**environment, "EWALLET_LOAD_MIX": "valid=1,dependency_failure=1"},
            )

    def test_malformed_settings_are_rejected(self):
        for host, environment in (
            ("", {}),
            ("ftp://127.0.0.1", {}),
            (LOCAL_HOST, {"EWALLET_LOAD_PROFILE": "stress"}),
            (LOCAL_HOST, {"EWALLET_LOAD_SEED": "abc"}),
        ):
            with self.subTest(host=host, environment=environment):
                with self.assertRaises(LoadConfigurationError):
                    LoadSettings.from_env(host, environment)


class ScenarioMixTest(unittest.TestCase):
    def test_default_mix_uses_every_scenario(self):
        self.assertEqual(5, len(parse_mix(DEFAULT_MIX)))

    def test_invalid_mixes_are_rejected(self):
        for text in (
            "",
            "valid",
            "unknown=1",
            "valid=x",
            "valid=-1",
            "valid=0",
            "valid=1,valid=2",
        ):
            with self.subTest(text=text):
                with self.assertRaises(LoadConfigurationError):
                    parse_mix(text)


class PayloadTest(unittest.TestCase):
    def test_generated_valid_payloads_satisfy_the_schema(self):
        rng = random.Random(1)
        for sequence in range(300):
            FraudPredictionRequest.model_validate(valid_payload(rng, sequence))
        FraudPredictionRequest.model_validate(shared_payload())

    def test_generated_invalid_payloads_violate_the_schema(self):
        rng = random.Random(2)
        for sequence in range(300):
            with self.assertRaises(ValidationError):
                FraudPredictionRequest.model_validate(invalid_payload(rng, sequence))

    def test_a_seed_fixes_the_sequence_and_users_differ(self):
        def sequence(seed):
            rng = random.Random(seed)
            return [valid_payload(rng, index) for index in range(5)]

        first, second = replay_seed(7, 0), replay_seed(7, 1)
        self.assertEqual(sequence(first), sequence(first))
        self.assertNotEqual(sequence(first), sequence(second))

    def test_payloads_are_marked_synthetic(self):
        payload = valid_payload(random.Random(3), 1)
        for field in ("transaction_id", "user_id", "account_id", "device_id"):
            self.assertTrue(str(payload[field]).startswith("synthetic-"))


class MockServerTest(unittest.TestCase):
    def request(self, app, body, headers=None):
        return asyncio.run(
            asgi_request(app, "POST", "/v1/fraud/predict", body, headers)
        )

    def test_valid_and_invalid_requests_behave_as_the_scenarios_expect(self):
        app = build_mock_app()
        status, _, body = self.request(app, shared_payload())
        self.assertEqual(200, status)
        self.assertTrue(0.0 <= body["fraud_probability"] <= 1.0)
        status, _, body = self.request(app, {**shared_payload(), "amount": 0})
        self.assertEqual(422, status)

    def test_every_catalogued_fault_produces_its_declared_response(self):
        app = build_mock_app()
        for fault in DEPENDENCY_FAULTS:
            with self.subTest(fault=fault.label):
                payload = shared_payload()
                headers = None
                if fault.backend == FEATURE_BACKEND:
                    payload["user_id"] = fault.user_id
                else:
                    headers = {"x-correlation-id": fault.correlation_id}
                status, _, body = self.request(app, payload, headers)
                self.assertEqual(fault.expected_status, status)
                self.assertEqual(fault.expected_code, body["error"]["code"])

    def test_health_endpoints_are_ready(self):
        app = build_mock_app()
        for path in ("/health/live", "/health/ready"):
            with self.subTest(path=path):
                status, _, _ = asyncio.run(asgi_request(app, "GET", path))
                self.assertEqual(200, status)


STATS = """Type,Name,Request Count,Failure Count,Requests/s,50%,95%,99%
POST,POST predict [valid],90,1,9.0,10,20,30
GET,GET /health/live,10,0,1.0,1,2,3
,Aggregated,100,1,10.0,9,19,29
"""
FAILURES = """Method,Name,Error,Occurrences
POST,POST predict [valid],unexpected status 500,1
"""


class LocustReportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        (self.directory / "locust_stats.csv").write_text(STATS)

    def summary(self, profile="local"):
        return locust_report.build_summary(
            locust_report.read_stats(self.directory / "locust_stats.csv"),
            locust_report.read_failures(self.directory / "locust_failures.csv"),
            profile=profile,
        )

    def test_numbers_are_carried_over_unchanged(self):
        (self.directory / "locust_failures.csv").write_text(FAILURES)
        summary = self.summary()
        self.assertEqual(100, summary["total_requests"])
        self.assertEqual(0.01, summary["failure_rate"])
        self.assertEqual(10.0, summary["requests_per_second"])
        self.assertEqual((9.0, 19.0, 29.0), (
            summary["p50_ms"], summary["p95_ms"], summary["p99_ms"]
        ))
        self.assertEqual(2, len(summary["endpoints"]))
        self.assertEqual(1, summary["errors"][0]["occurrences"])

    def test_local_results_are_labelled_as_mock_backed(self):
        summary = self.summary()
        self.assertIn("not a production measurement", summary["measurement_kind"])
        self.assertIn("not a production measurement", locust_report.render_markdown(summary))
        self.assertIn("No unexpected outcomes", locust_report.render_markdown(summary))

    def test_integration_results_are_labelled_separately(self):
        self.assertIn("real dependencies", self.summary("integration")["measurement_kind"])

    def test_missing_aggregate_row_is_an_error(self):
        (self.directory / "locust_stats.csv").write_text(STATS.rsplit("\n", 2)[0] + "\n")
        with self.assertRaises(ValueError):
            self.summary()


if __name__ == "__main__":
    unittest.main()
