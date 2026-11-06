"""Safety rules of the bounded MCP failover and its use by the Specialist."""

from __future__ import annotations

import asyncio
import unittest

from ai_platform.llm_agent.mcp_failover import (
    READ_ONLY_TOOLS,
    FailoverPolicy,
    may_repeat,
    with_failover,
)


class Transient(Exception):
    """Stands in for a transport failure."""


class Permanent(Exception):
    """Stands in for a real answer such as a tool error."""


class Calls:
    """A call that fails a fixed number of times, then succeeds."""

    def __init__(self, failures, error=Transient):
        self.failures, self.error, self.count = failures, error, 0

    async def __call__(self):
        self.count += 1
        if self.count <= self.failures:
            raise self.error()
        return "ok"


async def no_sleep(_seconds):
    return None


def run(call, tools=("feature_lookup",), **options):
    return asyncio.run(with_failover(call, tools=tools, is_transient=lambda e: isinstance(e, Transient),
                                     sleep=no_sleep, **options))


class FailoverTest(unittest.TestCase):
    def test_a_healthy_call_runs_once(self):
        call = Calls(0)
        self.assertEqual("ok", run(call))
        self.assertEqual(1, call.count)

    def test_a_transient_failure_is_repeated(self):
        call = Calls(2)
        self.assertEqual("ok", run(call))
        self.assertEqual(3, call.count)

    def test_the_attempt_budget_is_respected(self):
        call = Calls(9)
        with self.assertRaises(Transient):
            run(call)
        self.assertEqual(3, call.count)

    def test_a_real_answer_is_not_repeated(self):
        call = Calls(1, Permanent)
        with self.assertRaises(Permanent):
            run(call)
        self.assertEqual(1, call.count)

    def test_a_tool_outside_the_read_only_set_is_never_repeated(self):
        call = Calls(1)
        with self.assertRaises(Transient):
            run(call, tools=("feature_lookup", "transfer_funds"))
        self.assertEqual(1, call.count)

    def test_an_empty_tool_list_is_not_repeated(self):
        call = Calls(1)
        with self.assertRaises(Transient):
            run(call, tools=())
        self.assertEqual(1, call.count)

    def test_each_retry_is_reported(self):
        seen = []
        run(Calls(2), on_retry=seen.append)
        self.assertEqual([1, 2], seen)

    def test_backoff_doubles_up_to_the_limit(self):
        policy = FailoverPolicy(max_attempts=5, base_delay_seconds=0.25, max_delay_seconds=0.6)
        self.assertEqual([0.25, 0.5, 0.6, 0.6], [policy.delay(attempt) for attempt in range(1, 5)])

    def test_invalid_policies_are_rejected(self):
        for options in ({"max_attempts": 0}, {"max_attempts": 9}, {"base_delay_seconds": -1},
                        {"base_delay_seconds": 2.0, "max_delay_seconds": 1.0}):
            with self.subTest(options), self.assertRaises(ValueError):
                FailoverPolicy(**options)

    def test_only_tools_without_side_effects_are_declared(self):
        self.assertEqual(
            {"feature_lookup", "fraud_scoring", "knowledge_search", "chunk_lookup", "drift_detection"},
            set(READ_ONLY_TOOLS),
        )
        self.assertFalse(may_repeat({"anything_else"}))


class SpecialistFailoverTest(unittest.TestCase):
    """The real client: a session factory that fails, then yields a working session."""

    def client(self, failures, *, session=None):
        from contextlib import asynccontextmanager

        from ai_platform.llm_agent.byo_feature_rag_specialist import FeatureRagMCPClient
        from tests.llm_agent.test_g3_byo_feature_rag_specialist import FakeSession

        working = session or FakeSession()
        self.opened = 0

        @asynccontextmanager
        async def factory(_endpoint, _headers, _timeout):
            self.opened += 1
            if self.opened <= failures:
                raise ConnectionError("backend pod is gone")
            yield working

        client = FeatureRagMCPClient(token_reader=lambda: "token", session_factory=factory,
                                     failover=FailoverPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0))
        return client, working

    def request(self):
        from tests.llm_agent.test_g3_byo_feature_rag_specialist import request

        return request()

    def test_a_lost_replica_is_survived_with_the_same_result(self):
        client, session = self.client(2)
        evidence = asyncio.run(client.run(self.request()))
        self.assertEqual("ok", evidence.status)
        self.assertEqual(3, self.opened)

    def test_each_tool_runs_once_on_the_successful_attempt(self):
        client, session = self.client(1)
        asyncio.run(client.run(self.request()))
        names = [name for name, _ in session.calls]
        self.assertEqual(len(set(names)), len(names))

    def test_a_persistent_outage_ends_with_the_transport_error(self):
        from ai_platform.llm_agent.byo_feature_rag_specialist import FeatureRagFailure

        client, _ = self.client(9)
        with self.assertRaises(FeatureRagFailure) as caught:
            asyncio.run(client.run(self.request()))
        self.assertEqual("mcp_transport_error", caught.exception.code)
        self.assertEqual(3, self.opened)
