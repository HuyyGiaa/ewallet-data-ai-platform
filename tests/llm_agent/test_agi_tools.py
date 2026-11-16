"""Agent integration: evidence helpers (agi_probe.py, agi_inputs.py).

``agi_probe mcp`` runs against the real ``create_drift_app`` served in-process
(``httpx2.ASGITransport``); no cluster, port-forward or Secret is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from ai_platform.llm_agent.business_request import typed_request_from_a2a
from ops.llm_agent import agi_inputs
from tests.llm_agent.agi_support import (
    ACCOUNT_BASELINE,
    ACCOUNT_FEATURE,
    CONTROLLER_TOKEN,
    CURRENT_WINDOW,
    DRIFT_TOKEN,
    FEATURE_RAG_TOKEN,
    DriftFixture,
)
from tests.llm_agent.test_agi_contracts import TRANSACTION

ROOT = Path(__file__).resolve().parents[2]
SDK = all(importlib.util.find_spec(name) is not None for name in ("mcp", "httpx2"))
spec = importlib.util.spec_from_file_location("agi_probe", ROOT / "scripts/llm_agent/agi_probe.py")
agi_probe = importlib.util.module_from_spec(spec)
sys.modules["agi_probe"] = agi_probe
spec.loader.exec_module(agi_probe)


class ProbeRequestTests(unittest.TestCase):
    def test_every_kind_writes_a_canonical_typed_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            transaction = Path(tmp) / "transaction.json"
            transaction.write_text(json.dumps(TRANSACTION))
            drift = ["--feature", ACCOUNT_FEATURE, "--baseline", ACCOUNT_BASELINE,
                     "--window-start", CURRENT_WINDOW["start"], "--window-end", CURRENT_WINDOW["end"]]
            cases = {"fraud": ["--transaction", str(transaction)], "drift": drift,
                     "combined": ["--transaction", str(transaction), *drift]}
            for kind, extra in cases.items():
                with self.subTest(kind):
                    out = Path(tmp) / f"{kind}.json"
                    self.assertEqual(agi_probe.main(["request", kind, "--request-id", f"agi-t-{kind}", *extra,
                                                     "--out", str(out)]), 0)
                    request = typed_request_from_a2a(json.loads(out.read_text()))
                    self.assertEqual(request.request_id, f"agi-t-{kind}")
                    with self.assertRaises(SystemExit):  # create-only
                        agi_probe.main(["request", kind, "--request-id", "x", *extra, "--out", str(out)])

    def test_incomplete_drift_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            agi_probe.main(["request", "drift", "--request-id", "x", "--feature", ACCOUNT_FEATURE,
                            "--out", str(Path(tmp) / "x.json")])


def intercept(app, mode):
    """ASGI wrapper that injects one failure on tools/call; everything else reaches the real server."""

    async def respond(send, status, payload):
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        messages, body, more = [], b"", True
        while more:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            more = message.get("more_body", False)
        if b'"tools/call"' in body:
            request_id = json.loads(body).get("id")
            if mode == "sleep":
                await asyncio.sleep(5)
            if mode == "http500":
                return await respond(send, 500, {"detail": "boom"})
            if mode in RPC_ERRORS:
                code, message = RPC_ERRORS[mode]
                return await respond(send, 200, {"jsonrpc": "2.0", "id": request_id,
                                                 "error": {"code": code, "message": message}})
        replay = iter(messages)

        async def again():
            return next(replay, {"type": "http.disconnect"})

        await app(scope, again, send)

    return wrapped


RPC_ERRORS = {"rpc_internal": (-32603, "Internal error"), "rpc_invalid_params": (-32602, "Invalid params"),
              "unknown_tool": (-32602, "Unknown tool")}


@unittest.skipUnless(SDK, "official MCP SDK unavailable")
class ProbeMcpTests(unittest.TestCase):
    """agi_probe against the real create_drift_app (in-process); failures injected around it."""

    @classmethod
    def setUpClass(cls):
        import httpx2

        from ai_platform.llm_agent.tools.service import create_drift_app

        cls.httpx2 = httpx2
        cls.fixture = DriftFixture()
        cls.app = create_drift_app(cls.fixture.server_env())
        broken = cls.fixture.server_env(EWALLET_DRIFT_WINDOW_MANIFEST_SHA256="0" * 64)
        cls.broken_app = create_drift_app(broken)
        # A server whose token table does not contain the Feature-RAG credential at all.
        env = cls.fixture.server_env()
        env["EWALLET_MCP_TOOL_GRANTS"] = json.dumps({"drift-specialist": ["drift_detection"]})
        tokens = json.loads(env["EWALLET_MCP_SERVICE_TOKENS"])
        tokens["credentials"] = [item for item in tokens["credentials"] if item["subject"] != "feature-rag-specialist"]
        env["EWALLET_MCP_SERVICE_TOKENS"] = json.dumps(tokens)
        cls.strict_app = create_drift_app(env)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def transport(self, app):
        return self.httpx2.ASGITransport(app=app)

    def arguments(self, **changes):
        value = {"feature_name": ACCOUNT_FEATURE, "baseline_id": ACCOUNT_BASELINE, "current_window": CURRENT_WINDOW}
        value.update(changes)
        return value

    def check(self, expect, token, app=None, *, tool="drift_detection", arguments=None, control=True,
              valid_endpoint=None, valid_app=None, timeout=10.0):
        args = argparse.Namespace(endpoint="http://127.0.0.1:1/mcp", tool=tool, expect=expect, timeout=timeout,
                                  control_token_env="AGI_TEST_CONTROL" if control else None,
                                  tested_valid_endpoint=valid_endpoint)
        transport = app if app is not None and not hasattr(app, "__call__") else self.transport(app or self.app)
        with mock.patch.dict("os.environ", {"AGI_TEST_CONTROL": DRIFT_TOKEN}):
            return asyncio.run(agi_probe.verdict(args, token, arguments or self.arguments(), transport,
                                                 self.transport(valid_app) if valid_app is not None else None))

    def assertVerdict(self, report, verdict, outcome):
        self.assertEqual((report["verdict"], report["outcome"]), (verdict, outcome), report.get("reasons"))

    def test_authorized_call_succeeds(self):
        self.assertVerdict(self.check("ok", DRIFT_TOKEN), "PASS", "ok")

    def test_hidden_ungranted_tool_is_a_proven_denial(self):
        report = self.check("denied", FEATURE_RAG_TOKEN)
        self.assertVerdict(report, "PASS", "hidden_tool")
        self.assertEqual(report["jsonrpc_error"], {"code": -32602, "message": "Unknown tool"})
        self.assertEqual(report["control"]["outcome"], "listed")

    def test_credential_of_another_server_is_a_proven_authentication_denial(self):
        report = self.check("unauthenticated", FEATURE_RAG_TOKEN, self.strict_app,
                            valid_endpoint="http://127.0.0.1:2/mcp", valid_app=self.app)
        self.assertVerdict(report, "PASS", "unauthenticated")
        self.assertEqual(report["tested_credential_home"]["outcome"], "listed")

    def test_unproven_or_unrelated_failures_never_pass_as_denied(self):
        cases = {
            "invalid arguments": dict(token=DRIFT_TOKEN, arguments=self.arguments(baseline_id="latest"), outcome="rejected"),
            "backend unavailable": dict(token=DRIFT_TOKEN, app=self.broken_app, outcome="unavailable"),
            "generic JSON-RPC error": dict(token=DRIFT_TOKEN, app=intercept(self.app, "rpc_internal"), outcome="protocol_error"),
            "invalid params uses -32602 too": dict(token=DRIFT_TOKEN, app=intercept(self.app, "rpc_invalid_params"),
                                                   outcome="protocol_error"),
            "server error": dict(token=DRIFT_TOKEN, app=intercept(self.app, "http500"), outcome="server_error"),
            "timeout": dict(token=DRIFT_TOKEN, app=intercept(self.app, "sleep"), outcome="client_timeout", timeout=0.5),
            "authentication failure is not a grant denial": dict(token="x" * 40, outcome="unauthenticated"),
            "unknown tool that nobody serves": dict(token=DRIFT_TOKEN, tool="drift_detectionx", outcome="hidden_tool"),
            "Unknown tool for a principal that lists the tool": dict(token=DRIFT_TOKEN, app=intercept(self.app, "unknown_tool"),
                                                                    outcome="hidden_tool"),
            "no control principal": dict(token=FEATURE_RAG_TOKEN, control=False, outcome="hidden_tool"),
        }
        for label, case in cases.items():
            outcome = case.pop("outcome")
            with self.subTest(label):
                self.assertVerdict(self.check("denied", **case), "FAIL", outcome)

    def test_list_only_subject_is_a_proven_call_denial(self):
        report = self.check("call-denied", CONTROLLER_TOKEN)   # kagent-controller: discovery-only
        self.assertVerdict(report, "PASS", "hidden_tool")
        self.assertIn("drift_detection", report["tools"])
        self.assertEqual(report["control"]["outcome"], "listed")
        self.assertVerdict(self.check("call-denied", DRIFT_TOKEN), "FAIL", "ok")
        self.assertVerdict(self.check("call-denied", FEATURE_RAG_TOKEN), "FAIL", "hidden_tool")   # does not list it
        self.assertVerdict(self.check("call-denied", CONTROLLER_TOKEN, control=False),
                           "FAIL", "hidden_tool")
        self.assertVerdict(self.check("call-denied", CONTROLLER_TOKEN, tool="drift_detectionx"),
                           "FAIL", "hidden_tool")

    def test_retired_credential_is_a_proven_revocation_only_with_a_working_control(self):
        report = self.check("revoked", FEATURE_RAG_TOKEN, self.strict_app)
        self.assertVerdict(report, "PASS", "unauthenticated")
        self.assertEqual(report["control"]["outcome"], "listed")
        self.assertVerdict(self.check("revoked", DRIFT_TOKEN), "FAIL", "ok")   # still accepted
        self.assertVerdict(self.check("revoked", FEATURE_RAG_TOKEN, self.strict_app, control=False), "FAIL",
                           "unauthenticated")
        self.assertNotIn(FEATURE_RAG_TOKEN, json.dumps(report, default=str))

    def test_transport_failure_never_passes(self):
        httpx2 = self.httpx2

        class Refused(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx2.ConnectError("connection refused")

        report = self.check("denied", FEATURE_RAG_TOKEN, Refused())
        self.assertVerdict(report, "FAIL", "transport_error")

    def test_authentication_expectation_needs_a_valid_home_for_the_credential(self):
        self.assertVerdict(self.check("unauthenticated", "x" * 40, self.strict_app), "FAIL", "unauthenticated")
        report = self.check("unauthenticated", "x" * 40, self.strict_app,
                            valid_endpoint="http://127.0.0.1:2/mcp", valid_app=self.app)
        self.assertVerdict(report, "FAIL", "unauthenticated")

    def test_evidence_never_contains_a_token(self):
        report = self.check("denied", FEATURE_RAG_TOKEN)
        self.assertNotIn(FEATURE_RAG_TOKEN, json.dumps(report, default=str))
        self.assertNotIn(DRIFT_TOKEN, json.dumps(report, default=str))


class ProbeA2ATests(unittest.TestCase):
    class Response:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return self.body

    def route(self, answers):
        from urllib.error import HTTPError

        def opener(request, timeout):
            status, body = answers[request.get_header("X-kagent-host")]
            if isinstance(status, Exception):
                raise status
            if status >= 400:
                raise HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
            return self.Response(status, body)

        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            request.write_text("{}")
            args = argparse.Namespace(endpoint="http://127.0.0.1:18083/", kagent_host="payments-agent.ewallet-agents",
                                      control_kagent_host="drift-specialist.ewallet-agents", request=request,
                                      expect="no-route", timeout=5)
            return agi_probe.a2a_route(args, opener)

    def test_no_route_passes_only_with_404_and_a_routed_control(self):
        ok = (200, b'{"jsonrpc":"2.0","result":{}}')
        self.assertEqual(self.route({"payments-agent.ewallet-agents": (404, b"route not found"),
                                     "drift-specialist.ewallet-agents": ok})["verdict"], "PASS")
        failures = {
            "gateway down": {"payments-agent.ewallet-agents": (OSError("refused"), b""),
                             "drift-specialist.ewallet-agents": (OSError("refused"), b"")},
            "routed by mistake": {"payments-agent.ewallet-agents": ok, "drift-specialist.ewallet-agents": ok},
            "control not routed": {"payments-agent.ewallet-agents": (404, b""),
                                   "drift-specialist.ewallet-agents": (404, b"")},
            "server error is not a missing route": {"payments-agent.ewallet-agents": (502, b"bad gateway"),
                                                    "drift-specialist.ewallet-agents": ok},
        }
        for label, answers in failures.items():
            with self.subTest(label):
                self.assertEqual(self.route(answers)["verdict"], "FAIL")


class SandboxControlRouteTests(unittest.TestCase):
    """After the legacy Specialist cleanup the AGI-08 control is a declared Sandbox route."""

    def run_probe(self, tested, control, path="/api/a2a-sandboxes/ewallet-sandbox/drift-specialist/"):
        from urllib.error import HTTPError
        seen = {}

        def opener(request, timeout):
            if request.get_header("X-kagent-host"):
                status, body = tested
            else:
                seen["url"], seen["body"] = request.full_url, json.loads(request.data)
                status, body = control
            if isinstance(status, Exception):
                raise status
            if status >= 400:
                raise HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
            return ProbeA2ATests.Response(status, body)

        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(json.dumps({"jsonrpc": "2.0", "params": {"message": {"messageId": "m-1"}}}))
            args = argparse.Namespace(endpoint="http://127.0.0.1:18083/", kagent_host="payments-agent.ewallet-agents",
                                      control_kagent_host=None, control_path=path, request=request,
                                      expect="no-route", timeout=5)
            return agi_probe.a2a_route(args, opener), seen

    def test_undeclared_host_404_with_a_working_sandbox_route_passes(self):
        report, seen = self.run_probe((404, b"route not found"), (200, b'{"jsonrpc":"2.0","result":{}}'))
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(seen["url"], "http://127.0.0.1:18083/api/a2a-sandboxes/ewallet-sandbox/drift-specialist/")
        self.assertEqual(seen["body"]["params"]["message"]["contextId"], "m-1", "kagent selects the actor by contextId")

    def test_the_security_assertion_is_unchanged(self):
        ok = (200, b'{"jsonrpc":"2.0","result":{}}')
        self.assertEqual(self.run_probe(ok, ok)[0]["verdict"], "FAIL", "an undeclared host must not be routed")
        self.assertEqual(self.run_probe((404, b""), (404, b""))[0]["verdict"], "FAIL", "the control must be routed")
        with self.assertRaises(SystemExit):
            self.run_probe((404, b""), ok, path="/api/a2a/ewallet-agents/drift-specialist/")


class InputsTests(unittest.TestCase):
    def test_database_is_made_explicit_in_both_connection_forms(self):
        self.assertEqual(agi_inputs.with_database("redis.fs.svc:6379,password=p,db=0", 3),
                         "redis.fs.svc:6379,password=p,db=3")
        self.assertEqual(agi_inputs.with_database("redis.fs.svc:6379,password=p", 0), "redis.fs.svc:6379,password=p,db=0")
        self.assertEqual(agi_inputs.with_database("redis://:p@redis.fs.svc:6379", 3), "redis://:p@redis.fs.svc:6379/3")
        for bad in ("", "not a connection"):
            with self.subTest(bad), self.assertRaises(agi_inputs.InputError):
                agi_inputs.with_database(bad, 3)
        with self.assertRaises(agi_inputs.InputError):
            agi_inputs.with_database("redis.fs.svc:6379", 16)

    def test_secret_plan_never_contains_the_connection(self):
        document = agi_inputs.derived_secret("feast-online-redis-agi-rag", "ewallet-mcp", "connection_string",
                                             "redis.fs.svc:6379,password=s3cret,db=3", "feast-online-redis", 3)
        self.assertEqual(base64.b64decode(document["data"]["connection_string"]).decode(),
                         "redis.fs.svc:6379,password=s3cret,db=3")
        self.assertNotIn("s3cret", json.dumps(agi_inputs._plan(document)))
        self.assertTrue(document["immutable"])

    def test_cli_refuses_db0_and_unapproved_copies_before_reading_a_secret(self):
        base = ["redis-secret", "--context", "ctx", "--source", "feast-online-redis", "--name",
                "feast-online-redis-agi-fraud"]
        for extra in (["--db", "0", "--approve-sensitive-copy"], ["--db", "2"]):
            with self.subTest(extra), self.assertRaises(SystemExit):
                agi_inputs.main([*base, *extra])

    def test_synthetic_drift_artifacts_are_refused_for_the_cluster(self):
        fixture = DriftFixture()
        try:
            with self.assertRaises(agi_inputs.InputError):
                agi_inputs.drift_configmaps(fixture.baselines, fixture.window_manifest, "ewallet-mcp", "agi")
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
