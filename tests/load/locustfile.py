"""Locust scenarios for ``POST /v1/fraud/predict`` and the health endpoints.

Each simulated user draws its next scenario from the configured mix with a
seeded generator. A response counts as a Locust failure only when it differs
from what the scenario expects, so 422 and injected dependency errors are
successes of those scenarios, and the failure rate means "unexpected outcome".

Run through ``scripts/testing/run_locust.sh``. ``tests/load/settings.py`` refuses
non-loopback hosts unless the integration profile is explicitly acknowledged.
"""

from __future__ import annotations

from itertools import count
import random
import sys

from locust import HttpUser, constant, events, task
from locust.env import Environment

from tests.load.payloads import (
    invalid_payload,
    replay_seed,
    shared_payload,
    valid_payload,
)
from tests.load.scenarios import (
    CONCURRENT,
    DEPENDENCY_FAILURE,
    DEPENDENCY_FAULTS,
    FEATURE_BACKEND,
    HEALTH,
    INTEGRATION_PROFILE,
    INVALID,
    VALID,
    DependencyFault,
)
from tests.load.settings import LoadConfigurationError, LoadSettings


PREDICT_PATH = "/v1/fraud/predict"
_user_sequence = count()
_settings: LoadSettings | None = None
_shared_score: list[float] = []


@events.init.add_listener
def configure_run(environment: Environment, **_kwargs: object) -> None:
    """Validate the target and stop before any user can start.

    Locust logs and ignores ``Exception`` raised by listeners, so a refusal has
    to leave as ``SystemExit`` or the run would continue.
    """
    global _settings
    try:
        _settings = LoadSettings.from_env(environment.host or "")
    except LoadConfigurationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    kind = (
        "REAL DEPENDENCIES"
        if _settings.profile == INTEGRATION_PROFILE
        else "MOCK-BACKED, NOT A PRODUCTION MEASUREMENT"
    )
    print(f"ewallet load profile: {_settings.profile} ({kind}); mix={dict(_settings.mix)}")


class FraudApiUser(HttpUser):
    """One client drawing scenarios from the configured mix."""

    wait_time = constant(0)

    def on_start(self) -> None:
        """Seed this user's generator and bind the scenario handlers."""
        self._index = next(_user_sequence)
        self._rng = random.Random(replay_seed(_settings.seed, self._index))
        self._sequence = count(self._index * 10_000_000)
        handlers = {
            VALID: self._predict_valid,
            INVALID: self._predict_invalid,
            CONCURRENT: self._predict_shared,
            DEPENDENCY_FAILURE: self._predict_with_fault,
            HEALTH: self._check_health,
        }
        weighted = [(name, weight) for name, weight in _settings.mix.items() if weight]
        self._handlers = [handlers[name] for name, _ in weighted]
        self._weights = [weight for _, weight in weighted]

    @task
    def run_next_scenario(self) -> None:
        """Execute one scenario chosen by weight."""
        self._rng.choices(self._handlers, self._weights)[0]()

    def _post(self, name: str, payload: dict, headers: dict | None = None):
        return self.client.post(
            PREDICT_PATH,
            json=payload,
            headers=headers,
            name=name,
            catch_response=True,
        )

    def _predict_valid(self) -> None:
        payload = valid_payload(self._rng, next(self._sequence))
        with self._post("POST predict [valid]", payload) as response:
            self._require_prediction(response, payload["transaction_id"])

    def _predict_invalid(self) -> None:
        payload = invalid_payload(self._rng, next(self._sequence))
        with self._post("POST predict [invalid 422]", payload) as response:
            self._require_error(response, 422, "invalid_request")

    def _predict_shared(self) -> None:
        payload = shared_payload()
        with self._post("POST predict [concurrent shared]", payload) as response:
            if not self._require_prediction(response, payload["transaction_id"]):
                return
            score = response.json()["fraud_probability"]
            if not _shared_score:
                _shared_score.append(score)
            elif score != _shared_score[0]:
                response.failure("identical concurrent requests disagreed")

    def _predict_with_fault(self) -> None:
        fault = self._rng.choice(DEPENDENCY_FAULTS)
        payload = valid_payload(self._rng, next(self._sequence))
        headers = None
        if fault.backend == FEATURE_BACKEND:
            payload["user_id"] = fault.user_id
        else:
            headers = {"x-correlation-id": fault.correlation_id}
        name = f"POST predict [fault {fault.label}]"
        with self._post(name, payload, headers) as response:
            self._require_fault(response, fault)

    def _check_health(self) -> None:
        path = "/health/live" if self._rng.random() < 0.5 else "/health/ready"
        with self.client.get(path, name=f"GET {path}", catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"unexpected status {response.status_code}")
            else:
                response.success()

    @staticmethod
    def _require_prediction(response, transaction_id: str) -> bool:
        if response.status_code != 200:
            response.failure(f"unexpected status {response.status_code}")
            return False
        body = response.json()
        if not 0.0 <= body.get("fraud_probability", -1.0) <= 1.0:
            response.failure("fraud_probability outside [0, 1]")
            return False
        if body.get("transaction_id") != transaction_id:
            response.failure("transaction_id was not preserved")
            return False
        response.success()
        return True

    @staticmethod
    def _require_error(response, status: int, code: str) -> None:
        if response.status_code != status:
            response.failure(f"expected {status}, got {response.status_code}")
            return
        observed = response.json().get("error", {}).get("code")
        if observed != code:
            response.failure(f"expected {code}, got {observed}")
            return
        response.success()

    def _require_fault(self, response, fault: DependencyFault) -> None:
        self._require_error(response, fault.expected_status, fault.expected_code)
