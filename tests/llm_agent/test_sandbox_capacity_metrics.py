"""P2-D: Coordinator Sandbox capacity metrics.

``ewallet_sandbox_delegations_inflight{specialist}`` counts each logical Sandbox delegation once
(waiting for a worker and sending again do not count twice) and returns to 0 however the delegation
ends. ``ewallet_sandbox_admission_waits_total{specialist}`` counts genuine capacity refusals only.
"""

from __future__ import annotations

from collections import defaultdict
import threading
import time
from threading import Event
import unittest
from unittest import mock

from ai_platform.llm_agent import agent_metrics
from ai_platform.llm_agent.idempotency import InMemoryIdempotencyStore
from ai_platform.llm_agent.programmatic_coordinator import (
    CoordinatorFailure,
    GatewayCall,
    GatewayPolicy,
    ProgrammaticCoordinator,
    RateLimitBackoff,
    RateLimitedFailure,
)
from tests.llm_agent.test_a2a_rate_limit_backoff import POOL_EXHAUSTED, _NoSpan, coordinator
from tests.llm_agent.test_agent_platform_completion import drift_request

SANDBOX_CALL = GatewayCall(url="http://a2a-gateway.test/api/a2a-sandboxes/ewallet-sandbox/drift-specialist/",
                           headers={"content-type": "application/json"}, payload={"jsonrpc": "2.0"},
                           timeout_seconds=30.0, sandbox_specialist="drift-specialist")
DEPLOYMENT_CALL = GatewayCall(url="http://a2a-gateway.test/", headers={"x-kagent-host": "drift-specialist.ewallet-agents"},
                              payload={"jsonrpc": "2.0"}, timeout_seconds=30.0)
FAST = RateLimitBackoff(capacity_base_delay_seconds=0.001, capacity_max_delay_seconds=0.002,
                        base_delay_seconds=0.001, max_delay_seconds=0.002)


class Recorder:
    """Stands in for an OpenTelemetry instrument; keeps every add() per label set (thread-safe)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.adds: list[tuple[int, tuple]] = []
        self.peak: dict[tuple, int] = defaultdict(int)
        self._value: dict[tuple, int] = defaultdict(int)

    def add(self, value, attributes=None):
        key = tuple(sorted((attributes or {}).items()))
        with self.lock:
            self.adds.append((value, key))
            self._value[key] += value
            self.peak[key] = max(self.peak[key], self._value[key])

    def value(self, specialist="drift-specialist"):
        return self._value[(("specialist", specialist),)]

    def increments(self):
        return sum(1 for value, _ in self.adds if value > 0)


class MetricsCase(unittest.TestCase):
    def setUp(self):
        self.inflight, self.waits = Recorder(), Recorder()
        patches = [mock.patch.object(agent_metrics, "_sandbox_inflight", self.inflight),
                   mock.patch.object(agent_metrics, "_sandbox_waits", self.waits)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def assert_settled(self, *, increments, waits):
        self.assertEqual(self.inflight.value(), 0, "the in-flight gauge returns to 0")
        self.assertEqual(self.inflight.increments(), increments, "one increment per logical delegation")
        self.assertEqual(sum(value for value, _ in self.inflight.adds), 0, "decremented exactly once")
        self.assertEqual(self.waits.value(), waits)


class SingleDelegationTests(MetricsCase):
    def test_first_attempt_success(self):
        seen = []

        def transport(call):
            seen.append(self.inflight.value())
            return {"answered": True}

        coordinator(transport, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        self.assertEqual(seen, [1], "in flight while the Specialist works")
        self.assert_settled(increments=1, waits=0)

    def test_capacity_retries_count_once_in_flight_and_each_refusal(self):
        answers = [POOL_EXHAUSTED, POOL_EXHAUSTED]
        seen = []

        def transport(call):
            seen.append(self.inflight.value())
            return answers.pop(0) if answers else {"answered": True}

        coordinator(transport, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        self.assertEqual(seen, [1, 1, 1], "waiting for a worker does not add a second in-flight unit")
        self.assert_settled(increments=1, waits=2)

    def test_exhausted_budget_counts_every_refusal_and_settles(self):
        with self.assertRaises(CoordinatorFailure) as caught:
            coordinator(lambda call: POOL_EXHAUSTED, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        self.assertEqual(caught.exception.code, "sandbox_capacity_exhausted")
        self.assert_settled(increments=1, waits=FAST.capacity_max_attempts)

    def test_cancellation_during_the_wait_settles(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CoordinatorFailure) as caught:
            coordinator(lambda call: POOL_EXHAUSTED, [], FAST)._send(SANDBOX_CALL, cancelled, _NoSpan())
        self.assertEqual(caught.exception.code, "cancelled")
        self.assert_settled(increments=1, waits=1)

    def test_deadline_refusal_settles(self):
        short = GatewayCall(url=SANDBOX_CALL.url, headers=SANDBOX_CALL.headers, payload=SANDBOX_CALL.payload,
                            timeout_seconds=0.5, sandbox_specialist="drift-specialist")
        with self.assertRaises(RateLimitedFailure):
            coordinator(lambda call: POOL_EXHAUSTED, [])._send(short, None, _NoSpan())
        self.assert_settled(increments=1, waits=1)

    def test_non_capacity_outcomes_are_not_admission_waits(self):
        error = {"jsonrpc": "2.0", "id": "x", "error": {"code": -32603, "message": "agent crashed"}}
        coordinator(lambda call: error, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        shed = [RateLimitedFailure(None)]

        def gateway_429(call):
            if shed:
                raise shed.pop()
            return {"answered": True}

        coordinator(gateway_429, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        self.assert_settled(increments=2, waits=0)

    def test_exceptions_settle(self):
        def broken(call):
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed")

        def crashing(call):
            raise RuntimeError("unexpected")

        with self.assertRaises(CoordinatorFailure):
            coordinator(broken, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        with self.assertRaises(RuntimeError):
            coordinator(crashing, [], FAST)._send(SANDBOX_CALL, None, _NoSpan())
        self.assert_settled(increments=2, waits=0)

    def test_deployment_specialists_are_not_counted(self):
        coordinator(lambda call: {"answered": True}, [], FAST)._send(
            DEPLOYMENT_CALL, None, _NoSpan())
        self.assertEqual((self.inflight.adds, self.waits.adds), ([], []))


class ConcurrencyTests(MetricsCase):
    def run_concurrent(self, n, transport, backoff=FAST, real_sleep=False):
        engine = coordinator(transport, [], backoff)
        if real_sleep:   # the helper records waits instead of sleeping; a shared pool needs real waiting
            engine = ProgrammaticCoordinator(transport=transport, synthesizer=lambda request, evidence: "ok",
                                             idempotency_store=InMemoryIdempotencyStore(), rate_limit_backoff=backoff,
                                             sleep=time.sleep)
        errors = []

        def one():
            try:
                engine._send(SANDBOX_CALL, None, _NoSpan())
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=one) for _ in range(n)]
        for thread in threads:
            thread.start()
        return threads, errors

    def test_gauge_tracks_1_2_and_8_concurrent_delegations(self):
        for n in (1, 2, 8):
            with self.subTest(concurrency=n):
                self.setUp()
                release, arrived = Event(), threading.Semaphore(0)

                def transport(call):
                    arrived.release()
                    release.wait(5)
                    return {"answered": True}

                threads, errors = self.run_concurrent(n, transport)
                for _ in range(n):
                    self.assertTrue(arrived.acquire(timeout=5))
                self.assertEqual(self.inflight.value(), n, "every delegation in flight while the Specialists work")
                release.set()
                for thread in threads:
                    thread.join(5)
                self.assertEqual(errors, [])
                self.assertEqual(self.inflight.peak[(("specialist", "drift-specialist"),)], n)
                self.assert_settled(increments=n, waits=0)

    def test_a_two_worker_pool_at_concurrency_8(self):
        lock, state = threading.Lock(), {"busy": 0, "refused": 0}

        def transport(call):
            with lock:
                if state["busy"] >= 2:
                    state["refused"] += 1
                    return POOL_EXHAUSTED
                state["busy"] += 1
            try:
                time.sleep(0.01)
                return {"answered": True}
            finally:
                with lock:
                    state["busy"] -= 1

        # Waits long enough against 10 ms of work that no delegation runs out of attempts.
        threads, errors = self.run_concurrent(8, transport, RateLimitBackoff(capacity_base_delay_seconds=0.01,
                                                                              capacity_max_delay_seconds=0.05),
                                             real_sleep=True)
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertGreater(state["refused"], 0)
        self.assert_settled(increments=8, waits=state["refused"])


class LabelAndRoutingTests(MetricsCase):
    def test_only_sandbox_routes_carry_the_specialist(self):
        policy = GatewayPolicy(sandbox_specialists=frozenset({"drift-specialist"}), sandbox_namespace="ewallet-sandbox")
        self.assertEqual(policy.call_for(drift_request("m1")).sandbox_specialist, "drift-specialist")
        self.assertIsNone(GatewayPolicy().call_for(drift_request("m2")).sandbox_specialist)

    def test_labels_are_bounded(self):
        with self.assertRaises(ValueError):
            agent_metrics.record_sandbox_admission_wait("any-agent")
        with self.assertRaises(ValueError):
            with agent_metrics.track_sandbox_delegation("any-agent"):
                pass
        self.assertEqual(agent_metrics.SANDBOX_SPECIALISTS, frozenset({"feature-rag-specialist", "drift-specialist"}))

    def test_the_coordinator_exports_both_series_at_zero_on_start(self):
        agent_metrics.initialize("fraud-coordinator")
        for recorder in (self.inflight, self.waits):
            self.assertEqual(sorted(key for value, key in recorder.adds if value == 0),
                             [(("specialist", "drift-specialist"),), (("specialist", "feature-rag-specialist"),)])
        before = (len(self.inflight.adds), len(self.waits.adds))
        agent_metrics.initialize("feature-rag-specialist")
        self.assertEqual((len(self.inflight.adds), len(self.waits.adds)), before, "only the Coordinator exports them")


if __name__ == "__main__":
    unittest.main()
