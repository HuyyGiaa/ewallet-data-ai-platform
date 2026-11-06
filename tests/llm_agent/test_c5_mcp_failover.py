"""Safety rules of the bounded MCP failover and its use by the Specialist."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import unittest

import anyio
import httpx
import pytest

from ai_platform.llm_agent.contracts import TOOL_NAMES
from ai_platform.llm_agent.mcp_failover import (
    READ_ONLY_TOOLS,
    FailoverPolicy,
    is_recoverable,
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


def test_attempts_come_from_the_environment() -> None:
    from ai_platform.llm_agent.mcp_failover import policy_from_env

    assert policy_from_env({}).max_attempts == 3
    assert policy_from_env({"EWALLET_MCP_MAX_ATTEMPTS": "1"}).max_attempts == 1


def test_bad_attempt_value_is_refused() -> None:
    from ai_platform.llm_agent.mcp_failover import policy_from_env

    for value in ("0", "9", "many"):
        try:
            policy_from_env({"EWALLET_MCP_MAX_ATTEMPTS": value})
        except ValueError:
            continue
        raise AssertionError(f"{value} was accepted")


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://mcp.test/mcp")
    return httpx.HTTPStatusError("bad", request=request, response=httpx.Response(status, request=request))


def _wrapped(cause: BaseException) -> Exception:
    """Wrap a failure as the specialist does: a task group inside its own error."""

    error = ValueError("MCP Gateway session failed")
    error.__cause__ = ExceptionGroup("task group", [cause])  # type: ignore[arg-type]
    return error


@pytest.mark.parametrize("cause", [
    httpx.ConnectError("refused"), httpx.ReadError("reset"), httpx.RemoteProtocolError("closed"),
    ConnectionError("gone"), anyio.ClosedResourceError(), _status_error(502), _status_error(503), _status_error(504),
], ids=lambda item: type(item).__name__)
def test_lost_replica_is_recoverable(cause) -> None:
    assert is_recoverable(_wrapped(cause))


@pytest.mark.parametrize("cause", [
    _status_error(400), _status_error(401), _status_error(403), _status_error(404), _status_error(429),
    _status_error(500), KeyError("field"), ValueError("bad result"), RuntimeError("unknown"),
], ids=lambda item: getattr(getattr(item, "response", None), "status_code", type(item).__name__))
def test_a_real_answer_is_not_recoverable(cause) -> None:
    assert not is_recoverable(_wrapped(cause))


def test_a_rejection_wins_over_a_closed_connection() -> None:
    assert not is_recoverable(ExceptionGroup("both", [anyio.ClosedResourceError(), _status_error(401)]))


def test_exception_cycle_ends() -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert not is_recoverable(first)


def test_every_tool_in_the_contract_is_classified() -> None:
    """A new tool fails here until someone audits it and adds it to READ_ONLY_TOOLS."""

    assert TOOL_NAMES == READ_ONLY_TOOLS


def test_read_only_tools_write_nothing() -> None:
    root = Path(__file__).resolve().parents[2] / "ai_platform/llm_agent/tools"
    pattern = re.compile(r"\.(write|put|insert|commit|delete|publish)\(|open\([^)]*['\"][wa]")
    for name in ("feature_lookup", "fraud_scoring", "knowledge_search", "chunk_lookup", "drift"):
        assert not pattern.search((root / f"{name}.py").read_text(encoding="utf-8")), name


def test_an_unaudited_tool_is_called_once_even_after_a_lost_connection() -> None:
    calls = []

    async def call() -> None:
        calls.append(1)
        raise httpx.ConnectError("gone")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(with_failover(call, tools=["feature_lookup", "refund_payment"], is_transient=is_recoverable,
                                  policy=FailoverPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0)))
    assert len(calls) == 1


def test_rejected_credential_is_not_retried_by_the_client() -> None:
    attempts = []

    @asynccontextmanager
    async def factory(endpoint, headers, timeout):
        attempts.append(1)
        raise _status_error(401)
        yield  # pragma: no cover

    from ai_platform.llm_agent.byo_feature_rag_specialist import FeatureRagFailure, FeatureRagMCPClient
    from tests.llm_agent.test_g3_byo_feature_rag_specialist import request

    client = FeatureRagMCPClient(token_reader=lambda: "token", session_factory=factory,
                                 failover=FailoverPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0))
    with pytest.raises(FeatureRagFailure):
        asyncio.run(client.run(request()))
    assert len(attempts) == 1


def test_listen_port_defaults_to_the_deployment_port() -> None:
    from ai_platform.llm_agent.byo_runtime import listen_port

    assert listen_port({}) == 8080
    assert listen_port({"EWALLET_AGENT_PORT": "80"}) == 80


@pytest.mark.parametrize("value", ["0", "70000", "http"])
def test_bad_listen_port_is_refused(value) -> None:
    from ai_platform.llm_agent.byo_runtime import listen_port

    with pytest.raises(ValueError):
        listen_port({"EWALLET_AGENT_PORT": value})
