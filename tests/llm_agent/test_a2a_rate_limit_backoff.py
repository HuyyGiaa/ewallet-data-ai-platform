"""The Coordinator waits and repeats a delegation the A2A Gateway shed with 429 (AGR-06).

On GKE a burst at concurrency 8 lost tasks to the gateway's rate limit while it was healthy.
The limit stays; the Coordinator backs off within the delegation's own timeout.
"""

from __future__ import annotations

from email.message import Message
from threading import Event
import unittest
from urllib.error import HTTPError

import httpx

from ai_platform.llm_agent.business_request import a2a_message_send
from ai_platform.llm_agent.byo_coordinator import TracedGatewayTransport
from ai_platform.llm_agent.gateway_backoff import rate_limited_delay, retry_after_seconds
from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
from ai_platform.llm_agent.programmatic_coordinator import (
    CoordinatorFailure,
    GatewayCall,
    JsonHttpTransport,
    ProgrammaticCoordinator,
    RateLimitBackoff,
    RateLimitedFailure,
    encode_specialist_task,
)
from tests.llm_agent.test_agi_coordinator_workflows import fraud_evidence, fraud_request

SCOPE = IdempotencyScope("ewallet-agents", "kagent-controller")
CALL = GatewayCall(url="http://a2a-gateway.test/", headers={"x-kagent-host": "feature-rag-specialist.ewallet-agents"},
                   payload={"jsonrpc": "2.0"}, timeout_seconds=30.0)


class Shedding:
    """The A2A Gateway: refuses the first ``refusals`` calls with 429, then the Specialist answers."""

    def __init__(self, refusals: int, retry_after: float | None = None):
        self.refusals, self.retry_after, self.calls = refusals, retry_after, []

    def __call__(self, call):
        self.calls.append(call)
        if len(self.calls) <= self.refusals:
            raise RateLimitedFailure(self.retry_after)
        if "params" not in call.payload:  # a bare GatewayCall sent through _send
            return {"answered": True}
        request_id = call.payload["params"]["message"]["messageId"]
        return encode_specialist_task(request_id, fraud_evidence(request_id))


def coordinator(transport, waits, backoff=None):
    return ProgrammaticCoordinator(transport=transport, synthesizer=lambda request, evidence: "Đã xác thực.",
                                   idempotency_store=InMemoryIdempotencyStore(), rate_limit_backoff=backoff,
                                   sleep=waits.append)


class CoordinatorBackoffTests(unittest.TestCase):
    def handle(self, transport, waits, backoff=None, request_id="rl-1"):
        return coordinator(transport, waits, backoff).handle(
            a2a_message_send(fraud_request(request_id)), scope=SCOPE, session_id="s")

    def test_a_shed_delegation_is_repeated_and_completes(self):
        transport, waits = Shedding(2), []
        result = self.handle(transport, waits)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(0 < wait <= 2.0 for wait in waits))

    def test_the_attempt_budget_ends_with_the_rate_limit_code(self):
        transport, waits = Shedding(10), []
        with self.assertRaises(CoordinatorFailure) as caught:
            self.handle(transport, waits)
        self.assertEqual(caught.exception.code, "a2a_rate_limited")
        self.assertEqual(len(transport.calls), RateLimitBackoff().max_attempts)

    def test_one_attempt_turns_repeats_off(self):
        transport, waits = Shedding(1), []
        with self.assertRaises(CoordinatorFailure):
            self.handle(transport, waits, RateLimitBackoff(max_attempts=1))
        self.assertEqual((len(transport.calls), waits), (1, []))

    def test_retry_after_is_honoured(self):
        transport, waits = Shedding(1, retry_after=1.5), []
        self.handle(transport, waits)
        self.assertEqual(waits, [1.5])

    def test_repeats_stay_inside_the_delegation_timeout(self):
        transport, waits = Shedding(1), []
        coordinator(transport, waits)._send(CALL, None, _NoSpan())
        self.assertEqual(len(transport.calls), 2)
        self.assertLess(transport.calls[1].timeout_seconds, CALL.timeout_seconds)
        short = GatewayCall(url=CALL.url, headers=CALL.headers, payload=CALL.payload, timeout_seconds=0.5)
        transport, waits = Shedding(1), []
        with self.assertRaises(RateLimitedFailure):
            coordinator(transport, waits)._send(short, None, _NoSpan())
        self.assertEqual((len(transport.calls), waits), (1, []))

    def test_cancellation_during_the_wait_stops_the_repeat(self):
        transport, cancelled = Shedding(1), Event()
        cancelled.set()
        with self.assertRaises(CoordinatorFailure) as caught:
            coordinator(transport, [])._send(CALL, cancelled, _NoSpan())
        self.assertEqual((caught.exception.code, len(transport.calls)), ("cancelled", 1))

    def test_other_failures_are_not_repeated(self):
        calls = []

        def broken(call):
            calls.append(call)
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed")

        with self.assertRaises(CoordinatorFailure):
            coordinator(broken, [])._send(CALL, None, _NoSpan())
        self.assertEqual(len(calls), 1)

    def test_from_env(self):
        self.assertEqual(RateLimitBackoff.from_env({}).max_attempts, 4)
        self.assertEqual(RateLimitBackoff.from_env({"EWALLET_A2A_RATE_LIMIT_MAX_ATTEMPTS": "1"}).max_attempts, 1)
        with self.assertRaises(ValueError):
            RateLimitBackoff.from_env({"EWALLET_A2A_RATE_LIMIT_MAX_ATTEMPTS": "9"})


class _NoSpan:
    def add_event(self, *_args, **_kwargs):
        return None


class TransportTests(unittest.TestCase):
    def test_httpx_transport_maps_429_with_retry_after(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "2"}, request=request)))
        with self.assertRaises(RateLimitedFailure) as caught:
            TracedGatewayTransport(client)(CALL)
        self.assertEqual(caught.exception.retry_after_seconds, 2.0)

    def test_httpx_transport_keeps_other_statuses_as_transport_errors(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request)))
        with self.assertRaises(CoordinatorFailure) as caught:
            TracedGatewayTransport(client)(CALL)
        self.assertEqual(caught.exception.code, "a2a_transport_error")

    def test_urllib_transport_maps_429(self):
        headers = Message()
        headers["Retry-After"] = "1"

        def opener(request, timeout):
            raise HTTPError(request.full_url, 429, "Too Many Requests", headers, None)

        with self.assertRaises(RateLimitedFailure) as caught:
            JsonHttpTransport(opener)(CALL)
        self.assertEqual(caught.exception.retry_after_seconds, 1.0)


class DelayTests(unittest.TestCase):
    def test_retry_after_parsing(self):
        self.assertEqual(retry_after_seconds({"retry-after": "3"}), 3.0)
        self.assertIsNone(retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        self.assertIsNone(retry_after_seconds({"Retry-After": "-1"}))
        self.assertIsNone(retry_after_seconds({"Retry-After": "nan"}))
        self.assertIsNone(retry_after_seconds(None))
        self.assertEqual(retry_after_seconds({"Retry-After": "9999"}), 60.0)

    def test_delay_is_jittered_exponential_and_capped(self):
        self.assertEqual(rate_limited_delay(1, base_seconds=0.25, max_seconds=2.0, rand=lambda: 1.0), 0.25)
        self.assertEqual(rate_limited_delay(1, base_seconds=0.25, max_seconds=2.0, rand=lambda: 0.0), 0.125)
        self.assertEqual(rate_limited_delay(6, base_seconds=0.25, max_seconds=2.0, rand=lambda: 1.0), 2.0)
        self.assertEqual(rate_limited_delay(1, base_seconds=0.25, max_seconds=2.0, retry_after=5.0), 2.0)
        with self.assertRaises(ValueError):
            rate_limited_delay(0, base_seconds=0.25, max_seconds=2.0)


if __name__ == "__main__":
    unittest.main()


POOL_EXHAUSTED = {"jsonrpc": "2.0", "id": "x", "error": {"code": -32603, "message": (
    "failed to send HTTP request: Post \"http://feature-rag-specialist.ewallet-sandbox:8080\": substrate worker pool "
    "has no free workers; try again later or increase WorkerPool replicas")}}


class SandboxCapacityTests(unittest.TestCase):
    def test_a_full_worker_pool_is_waited_for_and_the_delegation_completes(self):
        answers = [POOL_EXHAUSTED, POOL_EXHAUSTED]

        def transport(call):
            if answers:
                return answers.pop(0)
            request_id = call.payload["params"]["message"]["messageId"]
            return encode_specialist_task(request_id, fraud_evidence(request_id))

        waits = []
        result = coordinator(transport, waits).handle(a2a_message_send(fraud_request("cap-1")), scope=SCOPE, session_id="s")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(1.0 <= wait <= 10.0 for wait in waits))

    def test_capacity_waits_end_with_their_own_code(self):
        waits = []
        with self.assertRaises(CoordinatorFailure) as caught:
            coordinator(lambda call: POOL_EXHAUSTED, waits)._send(CALL, None, _NoSpan())
        self.assertEqual(caught.exception.code, "sandbox_capacity_exhausted")
        self.assertEqual(len(waits) + 1, RateLimitBackoff().capacity_max_attempts)

    def test_other_json_rpc_errors_are_not_repeated(self):
        calls = []

        def transport(call):
            calls.append(call)
            return {"jsonrpc": "2.0", "id": "x", "error": {"code": -32603, "message": "agent crashed"}}

        response = coordinator(transport, [])._send(CALL, None, _NoSpan())
        self.assertEqual((len(calls), response["error"]["message"]), (1, "agent crashed"))

    def test_one_attempt_turns_capacity_waits_off_too(self):
        with self.assertRaises(CoordinatorFailure):
            coordinator(lambda call: POOL_EXHAUSTED, [], RateLimitBackoff(max_attempts=1))._send(CALL, None, _NoSpan())
