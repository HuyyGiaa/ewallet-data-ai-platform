"""Locust user for one real target, selected by ``EWALLET_LOAD_TARGET``.

Every request comes from a verified entity fixture and every response is checked against
the target's contract. Run through ``scripts/testing/run_real_load.sh`` (laptop)
or ``scripts/testing/run_incluster_load.sh`` (temporary pod in the cluster).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from locust import HttpUser, constant, events, task

from tests.load.real_targets import LoadConfigurationError, entity_order, select_target, validate_host

_state: dict = {}


@events.init.add_listener
def configure(environment, **_kwargs) -> None:
    """Validate the run before any user starts; Locust ignores plain exceptions here."""
    try:
        validate_host(environment.host or "", os.environ.get("EWALLET_LOAD_ACKNOWLEDGE_REMOTE"))
        target = select_target(os.environ.get("EWALLET_LOAD_TARGET", ""))
        path = Path(os.environ.get("EWALLET_LOAD_ENTITIES", ""))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise LoadConfigurationError("the entity fixture is empty")
    except (OSError, LoadConfigurationError) as error:
        print(f"refused: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    _state.update(
        target=target,
        rows=rows,
        options={"model_version": os.environ.get("EWALLET_LOAD_MODEL_VERSION", "")},
        order=entity_order(len(rows), int(os.environ.get("EWALLET_LOAD_SEED", "20261107"))),
    )
    print(f"real load: target={target.name} entities={len(rows)} host={environment.host}")


class RealApiUser(HttpUser):
    """One client sending valid requests; think time comes from ``EWALLET_LOAD_WAIT_SECONDS`` (default 0)."""

    wait_time = constant(float(os.environ.get("EWALLET_LOAD_WAIT_SECONDS", "0")))

    def on_start(self) -> None:
        """Attach gateway credentials and the route's Host header when configured."""
        self.client.verify = os.environ.get("EWALLET_LOAD_CA_FILE") or True
        credentials = os.environ.get("EWALLET_LOAD_BASIC_AUTH_FILE")
        if credentials:
            user, _, password = Path(credentials).read_text(encoding="utf-8").strip().partition(":")
            self.client.auth = (user, password)
        host_header = os.environ.get("EWALLET_LOAD_HOST_HEADER")
        if host_header:
            self.client.headers["Host"] = host_header

    @task
    def send(self) -> None:
        """Send one valid request and judge it by the target's contract."""
        target = _state["target"]
        body = target.build(_state["rows"][next(_state["order"])])
        with self.client.post(target.path, json=body, name=target.name, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            try:
                reason = target.check(body, response.json(), _state["options"])
            except ValueError:
                reason = "response is not JSON"
            if reason:
                response.failure(reason)
            else:
                response.success()
