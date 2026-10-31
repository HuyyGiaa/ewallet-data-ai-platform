"""Phase D MCP security and Streamable HTTP hardening (source-level, in-process ASGI).

Authentication here uses the TEST FIXTURE token table from test_phase_d_tools:
these tests prove the boundary fails closed and authorizes on the verified
principal; they are not evidence of any deployed authentication mechanism.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from tests.llm_agent.test_phase_d_tools import (
    ACTOR,
    ENV,
    FASTAPI,
    SUBJECT,
    FixtureAuthenticator,
    FixtureFeatureProvider,
    asgi,
    drift_args,
    executor,
    feature_rag,
    rpc,
    run,
    tool_app,
    transaction,
)

if FASTAPI:
    from ai_platform.inference.errors import ConfigurationError
    from ai_platform.llm_agent.tools.security import (
        AuthenticationError,
        DenyAllAuthenticator,
        HttpSecuritySettings,
        Principal,
        ToolGrants,
    )
    from ai_platform.llm_agent.tools.drift import DriftDetectionTool, UnavailableBaselineStore, UnavailableWindowSampler
    from ai_platform.llm_agent.tools.service import create_tool_app

OTHER = {"authorization": "Bearer fixture-token-other"}
CALL = rpc("tools/call", {"name": "knowledge_search", "arguments": {"query": "takeover"}})


async def chunked(app, path, chunks, headers, *, disconnect_after=None, delay=0.0, stall_after=None):
    """Send a body in several ASGI messages without Content-Length.

    With ``disconnect_after=n`` the client disconnects after n chunks, mid-body.
    ``delay`` waits before each chunk (a trickling client); with
    ``stall_after=n`` the client sends n chunks and then never sends again.
    Returns (status, body), or (None, None) when no response was sent.
    """

    sent, pending = [], [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                         for i, c in enumerate(chunks)]
    if disconnect_after is not None:
        pending = [dict(m, more_body=True) for m in pending[:disconnect_after]] + [{"type": "http.disconnect"}]
    received = 0

    async def receive():
        nonlocal received
        if stall_after is not None and received >= stall_after:
            await asyncio.Event().wait()
        received += 1
        await asyncio.sleep(delay)
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
               "client": ("127.0.0.1", 1), "server": ("testserver", 80), "root_path": ""}, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    if start is None:
        return None, None
    return start["status"], json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class SecurityConfigurationTests(unittest.TestCase):
    def test_http_settings_from_env(self):
        self.assertEqual(HttpSecuritySettings.from_env({}), HttpSecuritySettings())
        settings = HttpSecuritySettings.from_env({
            "EWALLET_MCP_ALLOWED_ORIGINS": "https://agents.example.invalid, http://localhost:8080",
            "EWALLET_MCP_MAX_BODY_BYTES": "2048", "EWALLET_MCP_MAX_INFLIGHT": "4"})
        self.assertEqual(settings.allowed_origins, ("https://agents.example.invalid", "http://localhost:8080"))
        self.assertEqual((settings.max_body_bytes, settings.max_inflight_requests), (2048, 4))
        self.assertEqual(HttpSecuritySettings.from_env({"EWALLET_MCP_BODY_TIMEOUT_SECONDS": "2.5"}).body_read_timeout_seconds, 2.5)
        for name, value in (("EWALLET_MCP_ALLOWED_ORIGINS", "*"), ("EWALLET_MCP_ALLOWED_ORIGINS", "https://a.invalid/*"),
                            ("EWALLET_MCP_ALLOWED_ORIGINS", "null"), ("EWALLET_MCP_MAX_BODY_BYTES", "1023"),
                            ("EWALLET_MCP_MAX_BODY_BYTES", str(4 * 1024 * 1024 + 1)), ("EWALLET_MCP_MAX_BODY_BYTES", "big"),
                            ("EWALLET_MCP_MAX_INFLIGHT", "0"), ("EWALLET_MCP_MAX_INFLIGHT", "1025"),
                            ("EWALLET_MCP_BODY_TIMEOUT_SECONDS", "0"), ("EWALLET_MCP_BODY_TIMEOUT_SECONDS", "61"),
                            ("EWALLET_MCP_BODY_TIMEOUT_SECONDS", "soon")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                HttpSecuritySettings.from_env({name: value})

    def test_tool_grants_deny_by_default_and_validate(self):
        principal = Principal(subject=SUBJECT, mechanism="test-fixture")
        self.assertFalse(ToolGrants.from_env({}).allows(principal, "feature_lookup"))
        grants = ToolGrants.from_env({"EWALLET_MCP_TOOL_GRANTS": json.dumps({SUBJECT: ["feature_lookup"]})})
        self.assertTrue(grants.allows(principal, "feature_lookup"))
        self.assertFalse(grants.allows(principal, "fraud_scoring"))
        for raw in ("not json", "[]", json.dumps({SUBJECT: "feature_lookup"}), json.dumps({SUBJECT: ["shell"]}),
                    json.dumps({SUBJECT: []}), json.dumps({"bad subject!": ["feature_lookup"]})):
            with self.subTest(raw), self.assertRaises(ConfigurationError):
                ToolGrants.from_env({"EWALLET_MCP_TOOL_GRANTS": raw})

    def test_principal_and_errors_are_bounded(self):
        for subject in ("", "x" * 129, "spaces not allowed"):
            with self.subTest(subject), self.assertRaises(ValueError):
                Principal(subject=subject, mechanism="m")
        with self.assertRaises(ValueError):
            AuthenticationError("origin_not_allowed")
        with self.assertRaises(AuthenticationError) as caught:
            run(DenyAllAuthenticator().authenticate({"authorization": "Bearer anything"}))
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("authentication_not_configured", 401))


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class AuthenticationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = tool_app(feature_rag())

    def call(self, *args, **kwargs):
        return run(asgi(self.app, *args, **kwargs))

    def test_missing_and_invalid_credentials_fail_closed_before_parsing(self):
        cases = {
            "no credentials": {},
            "forged actor header only": {"x-ewallet-actor": SUBJECT},
            "wrong scheme": {"authorization": "Basic Zm9vOmJhcg=="},
            "unknown token": {"authorization": "Bearer stolen-or-guessed"},
        }
        for label, headers in cases.items():
            for path, raw in (("/mcp", b"{not json"), ("/v1/tools/feature_lookup", b'{"transaction": 1}')):
                with self.subTest(label=label, path=path):
                    status, body = self.call("POST", path, raw=raw, headers=headers)
                    self.assertEqual(status, 401)
                    self.assertIn(body["code"], {"authentication_required", "authentication_failed"})
                    self.assertNotIn("stolen-or-guessed", json.dumps(body))

    def test_forged_actor_with_valid_credentials_is_rejected(self):
        forged = {**ACTOR, "x-ewallet-actor": "spiffe-test/fraud-coordinator"}
        status, body = self.call("POST", "/mcp", CALL, headers=forged)
        self.assertEqual((status, body["code"]), (403, "actor_mismatch"))
        status, body = self.call("POST", "/v1/tools/feature_lookup", {"transaction": transaction()}, headers=forged)
        self.assertEqual((status, body["code"]), (403, "actor_mismatch"))
        status, body = self.call("POST", "/mcp", CALL, headers={**ACTOR, "x-ewallet-actor": SUBJECT})
        self.assertFalse(body["result"]["isError"])

    def test_tool_authorization_uses_the_verified_principal(self):
        grants = ToolGrants({SUBJECT: frozenset({"knowledge_search"}),
                             "spiffe-test/drift-specialist": frozenset({"fraud_scoring"})})
        app = tool_app(feature_rag(), grants=grants)
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/list"), headers=OTHER))
        self.assertEqual([tool["name"] for tool in body["result"]["tools"]], ["fraud_scoring"])
        status, body = run(asgi(app, "POST", "/mcp", CALL, headers=OTHER))
        self.assertEqual((status, body["error"]["code"], body["error"]["message"]), (200, -32602, "Unknown tool"))
        status, body = run(asgi(app, "POST", "/v1/tools/feature_lookup", {"transaction": transaction()}, headers=ACTOR))
        self.assertEqual((status, body["code"]), (403, "tool_forbidden"))
        status, body = run(asgi(app, "POST", "/mcp", CALL, headers=ACTOR))
        self.assertFalse(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["tool_name"], "knowledge_search")

    def test_no_grants_means_no_tools(self):
        app = tool_app(feature_rag(), grants=ToolGrants({"spiffe-test/nobody": frozenset({"fraud_scoring"})}))
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/list"), headers=ACTOR))
        self.assertEqual(body["result"]["tools"], [])

    def test_verifier_faults_deny_without_leaking(self):
        class Broken:
            async def authenticate(self, headers):
                raise RuntimeError("jwks fetch failed: secret-internal-url")

        app = create_tool_app(service_name="feature-rag-mcp", executor=feature_rag(), authenticator=Broken(),
                              grants=ToolGrants({SUBJECT: frozenset({"knowledge_search"})}))
        status, body = run(asgi(app, "POST", "/mcp", CALL, headers=ACTOR))
        self.assertEqual((status, body["code"], body["retryable"]), (503, "authentication_unavailable", True))
        self.assertNotIn("secret-internal-url", json.dumps(body))

    def test_health_probes_need_no_credentials(self):
        self.assertEqual(self.call("GET", "/health/live")[0], 200)
        self.assertEqual(self.call("GET", "/health/ready")[0], 200)


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class TransportHardeningTests(unittest.TestCase):
    def setUp(self):
        settings = HttpSecuritySettings(allowed_origins=("https://agents.example.invalid",), max_body_bytes=2048,
                                        max_inflight_requests=4)
        self.app = tool_app(feature_rag(), http_settings=settings)

    def call(self, *args, **kwargs):
        return run(asgi(self.app, *args, **kwargs))

    def test_origin_validation(self):
        self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "origin": "https://agents.example.invalid"})[0], 200)
        self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers=ACTOR)[0], 200)
        for origin in ("https://evil.example", "null", "https://agents.example.invalid.evil", "http://agents.example.invalid"):
            with self.subTest(origin):
                status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "origin": origin})
                self.assertEqual((status, body["code"]), (403, "origin_not_allowed"))
        # Origin is checked before credentials: a hostile page learns nothing about auth.
        self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers={"origin": "https://evil.example"})[0], 403)

    def test_body_size_limit(self):
        big = rpc("tools/call", {"name": "knowledge_search", "arguments": {"query": "x" * 4096}})
        status, body = self.call("POST", "/mcp", big, headers=ACTOR)
        self.assertEqual((status, body["code"]), (413, "request_too_large"))
        status, body = self.call("POST", "/mcp", big, headers={**ACTOR, "content-length": "10"})
        self.assertEqual(status, 413)  # declared length is not trusted
        status, body = self.call("POST", "/mcp", big, headers={**ACTOR, "content-length": "4000000"})
        self.assertEqual((status, body["code"]), (413, "request_too_large"))
        status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "content-length": "-1"})
        self.assertEqual((status, body["code"]), (400, "invalid_content_length"))
        headers = {**ACTOR, "content-type": "application/json", "accept": "application/json"}
        status, body = run(chunked(self.app, "/mcp", [b" " * 1500, b" " * 1500, json.dumps(rpc("ping")).encode()], headers))
        self.assertEqual((status, body["code"]), (413, "request_too_large"))
        status, body = run(chunked(self.app, "/mcp", [b'{"jsonrpc": "2.0", ', b'"method": "ping", "id": 7}'], headers))
        self.assertEqual((status, body["result"]), (200, {}))

    def test_in_flight_limit(self):
        app = tool_app(feature_rag(provider=FixtureFeatureProvider(delay=0.2)),
                       http_settings=HttpSecuritySettings(max_inflight_requests=2))

        async def burst():
            return await asyncio.gather(*(asgi(app, "POST", "/v1/tools/feature_lookup", {"transaction": transaction()},
                                               headers=ACTOR) for _ in range(5)))

        statuses = sorted(status for status, _ in run(burst()))
        self.assertEqual(statuses, [200, 200, 503, 503, 503])
        self.assertEqual(run(asgi(app, "GET", "/health/live"))[0], 200)

    def test_media_types(self):
        for accept in ("text/html", "text/event-stream", "image/*", ""):
            with self.subTest(accept=accept):
                status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "accept": accept})
                self.assertEqual((status, body["code"]), (406, "not_acceptable"))
        for accept in ("application/json", "*/*", "application/*;q=0.9", "text/event-stream, application/json"):
            with self.subTest(accept=accept):
                self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "accept": accept})[0], 200)
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            with self.subTest(content_type=content_type):
                status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "content-type": content_type})
                self.assertEqual((status, body["code"]), (415, "unsupported_media_type"))
        self.assertEqual(self.call("POST", "/mcp", rpc("ping"),
                                   headers={**ACTOR, "content-type": "application/json; charset=utf-8"})[0], 200)

    def test_unsupported_http_methods(self):
        for method in ("GET", "DELETE", "PUT"):
            with self.subTest(method):
                self.assertEqual(self.call(method, "/mcp", headers=ACTOR)[0], 405)
                self.assertEqual(self.call(method, "/mcp")[0], 401)

    def test_json_rpc_message_shapes(self):
        cases = (
            ({"jsonrpc": "2.0", "id": 5, "result": {}}, 202, None),
            ({"jsonrpc": "2.0", "id": 5, "error": {"code": -1, "message": "x"}}, 202, None),
            ({"jsonrpc": "2.0", "id": 5, "result": {}, "error": {"code": -1, "message": "x"}}, 400, -32600),
            ({"jsonrpc": "2.0", "id": 5}, 400, -32600),
            ({"jsonrpc": "2.0", "method": 7, "id": 5}, 400, -32600),
            ({"jsonrpc": "2.0", "method": "ping", "id": None}, 400, -32600),
            ({"jsonrpc": "2.0", "method": "ping", "id": True}, 400, -32600),
            ({"jsonrpc": "2.0", "method": "ping", "id": 1.5}, 400, -32600),
            ({"jsonrpc": "2.0", "method": "ping", "id": {"a": 1}}, 400, -32600),
            ({"jsonrpc": "2.0", "method": "ping", "id": "s-1"}, 200, None),
            (rpc("tools/list", []), 200, -32602),
            (rpc("tools/call", {"name": 42}), 200, -32602),
            (rpc("tools/call", {"name": "knowledge_search", "arguments": ["takeover"]}), 200, -32602),
            ({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}, 202, None),
        )
        for message, status, code in cases:
            with self.subTest(message=message):
                actual, body = self.call("POST", "/mcp", message, headers=ACTOR)
                self.assertEqual(actual, status)
                if code is None:
                    self.assertTrue(body is None or "result" in body)
                else:
                    self.assertEqual(body["error"]["code"], code)

    def test_tool_failures_stay_in_band_and_redacted(self):
        app = tool_app(feature_rag(provider=FixtureFeatureProvider(error=RuntimeError("redis://secret@host"))))
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/call", {
            "name": "feature_lookup", "arguments": {"transaction": transaction()}}), headers=ACTOR))
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["result"]["error"]["code"], "internal_error")
        self.assertNotIn("secret", json.dumps(body))



class RecordingDriftTool:
    """Wraps the real drift tool to count executions (TEST FIXTURE)."""

    def __init__(self):
        self.inner = DriftDetectionTool(UnavailableBaselineStore(), UnavailableWindowSampler())
        self.name, self.description, self.input_model = self.inner.name, self.inner.description, self.inner.input_model
        self.calls = 0

    async def run(self, arguments, context):
        self.calls += 1
        return await self.inner.run(arguments, context)

    async def ready(self):
        return await self.inner.ready()


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class ReadinessContractTests(unittest.TestCase):
    """Transport readiness vs per-tool capability (contract changed 2026-10-31)."""

    def drift_app(self, authenticator=None):
        grants = ToolGrants({SUBJECT: frozenset({"drift_detection"})})
        return create_tool_app(service_name="drift-mcp", authenticator=authenticator or FixtureAuthenticator(),
                               executor=executor(DriftDetectionTool(UnavailableBaselineStore(), UnavailableWindowSampler())),
                               grants=grants)

    def test_missing_baseline_keeps_transport_ready_and_tool_fail_closed(self):
        app = self.drift_app()
        status, body = run(asgi(app, "GET", "/health/ready"))
        self.assertEqual((status, body), (200, {"status": "ready", "service": "drift-mcp", "scope": "transport"}))
        status, body = run(asgi(app, "GET", "/v1/capabilities", headers=ACTOR))
        self.assertEqual(body["tools"], {"drift_detection": {"status": "not_ready", "code": "drift_baseline_unavailable"}})
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/call", {"name": "drift_detection", "arguments": drift_args()}),
                                headers=ACTOR))
        self.assertTrue(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["result"]["error"]["code"], "drift_baseline_unavailable")
        status, body = run(asgi(app, "POST", "/v1/tools/drift_detection", drift_args(), headers=ACTOR))
        self.assertEqual((status, body["status"]), (503, "unavailable"))

    def test_backend_outage_is_a_capability_not_a_readiness_failure(self):
        app = tool_app(feature_rag(provider=FixtureFeatureProvider(ready=False)))
        self.assertEqual(run(asgi(app, "GET", "/health/ready"))[0], 200)
        _, body = run(asgi(app, "GET", "/v1/capabilities", headers=ACTOR))
        self.assertEqual(body["tools"]["feature_lookup"], {"status": "not_ready", "code": "feature_store_not_ready"})
        self.assertEqual(body["tools"]["knowledge_search"], {"status": "ready", "code": None})

    def test_authentication_failures_are_not_concealed(self):
        class Configurable(FixtureAuthenticator):
            def __init__(self, outcome):
                self.outcome = outcome

            async def readiness(self):
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome

        cases = {
            "deny-all default": (DenyAllAuthenticator(), "authentication_not_configured"),
            "verifier down": (Configurable("authentication_unavailable"), "authentication_unavailable"),
            "verifier raises": (Configurable(RuntimeError("jwks: secret-url")), "authentication_unavailable"),
            "unknown code": (Configurable("totally_fine"), "authentication_unavailable"),
        }
        for label, (authenticator, reason) in cases.items():
            with self.subTest(label):
                status, body = run(asgi(self.drift_app(authenticator), "GET", "/health/ready"))
                self.assertEqual((status, body["status"], body["reason"]), (503, "not_ready", reason))
                self.assertNotIn("secret-url", json.dumps(body))
        self.assertEqual(run(asgi(self.drift_app(Configurable(None)), "GET", "/health/ready"))[0], 200)
        # Liveness stays independent of authentication and tool backends.
        self.assertEqual(run(asgi(self.drift_app(DenyAllAuthenticator()), "GET", "/health/live"))[0], 200)

    def test_health_endpoints_expose_no_tool_details(self):
        for path in ("/health/live", "/health/ready"):
            _, body = run(asgi(self.drift_app(), "GET", path))
            self.assertNotIn("drift", json.dumps(body).replace("drift-mcp", ""))

    def test_capabilities_require_authentication_and_grants(self):
        app = tool_app(feature_rag(), grants=ToolGrants({SUBJECT: frozenset({"fraud_scoring"})}))
        status, body = run(asgi(app, "GET", "/v1/capabilities"))
        self.assertEqual((status, body["code"]), (401, "authentication_required"))
        _, body = run(asgi(app, "GET", "/v1/capabilities", headers=ACTOR))
        self.assertEqual(set(body["tools"]), {"fraud_scoring"})

    def test_schema_endpoints_are_not_exposed(self):
        app = tool_app(feature_rag())
        for path in ("/docs", "/redoc", "/openapi.json", "/unknown"):
            with self.subTest(path):
                self.assertEqual(run(asgi(app, "GET", path))[0], 401)
                self.assertEqual(run(asgi(app, "GET", path, headers=ACTOR))[0], 404)


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class MediaTypeAndDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.app = tool_app(feature_rag())

    def call(self, *args, **kwargs):
        return run(asgi(self.app, *args, **kwargs))

    def test_content_type_is_parsed_not_prefix_matched(self):
        for value in ("application/jsonp", "application/json-seq", "application/json5", "application/json; charset=latin-1",
                      "application/json; foo=bar", "application/json, text/plain", "application/json;",
                      "application/json; charset=utf-8; charset=utf-8", "application /json"):
            with self.subTest(value):
                status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "content-type": value})
                self.assertEqual((status, body["code"]), (415, "unsupported_media_type"))
        for value in ("Application/JSON", "application/json;charset=UTF-8", 'application/json; charset="utf-8"'):
            with self.subTest(value):
                self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "content-type": value})[0], 200)

    def test_accept_quality_values(self):
        for value in ("application/json;q=0", "*/*;q=0", "*/*, application/json;q=0", "application/json;q=1.5",
                      "application/*;q=0"):
            with self.subTest(value):
                status, body = self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "accept": value})
                self.assertEqual((status, body["code"]), (406, "not_acceptable"))
        for value in ("application/json;q=0.001", "application/*;q=0, application/json", "text/event-stream;q=1, */*;q=0.1"):
            with self.subTest(value):
                self.assertEqual(self.call("POST", "/mcp", rpc("ping"), headers={**ACTOR, "accept": value})[0], 200)

    def test_client_disconnect_mid_body_never_runs_the_tool(self):
        tool = RecordingDriftTool()
        app = create_tool_app(service_name="drift-mcp", executor=executor(tool), authenticator=FixtureAuthenticator(),
                              grants=ToolGrants({SUBJECT: frozenset({"drift_detection"})}))
        body = json.dumps(rpc("tools/call", {"name": "drift_detection", "arguments": drift_args()})).encode()
        headers = {**ACTOR, "content-type": "application/json", "accept": "application/json"}
        status, payload = run(chunked(app, "/mcp", [body[:20], body[20:]], headers, disconnect_after=1))
        self.assertEqual((status, payload, tool.calls), (None, None, 0))
        status, payload = run(chunked(app, "/mcp", [body[:20], body[20:]], headers))
        self.assertEqual((status, tool.calls), (200, 1))


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable: run with the MCP project environment")
class BodyDeadlineTests(unittest.TestCase):
    """An authenticated client that stalls or trickles its body gets 408; no tool runs."""

    def setUp(self):
        self.tool = RecordingDriftTool()
        self.app = create_tool_app(
            service_name="drift-mcp", executor=executor(self.tool), authenticator=FixtureAuthenticator(),
            grants=ToolGrants({SUBJECT: frozenset({"drift_detection"})}),
            http_settings=HttpSecuritySettings(body_read_timeout_seconds=0.1, max_inflight_requests=1))
        self.body = json.dumps(rpc("tools/call", {"name": "drift_detection", "arguments": drift_args()})).encode()
        self.headers = {**ACTOR, "content-type": "application/json", "accept": "application/json"}

    def send(self, **kwargs):
        return run(asyncio.wait_for(chunked(self.app, "/mcp", [self.body[:20], self.body[20:]], self.headers, **kwargs), 5))

    def test_stalled_body_times_out_without_running_the_tool(self):
        for stall_after in (0, 1):
            with self.subTest(stall_after=stall_after):
                status, payload = self.send(stall_after=stall_after)
                self.assertEqual((status, payload["code"], payload["retryable"]), (408, "request_timeout", False))
        self.assertEqual(self.tool.calls, 0)

    def test_deadline_covers_the_whole_body_not_each_chunk(self):
        # Each chunk arrives within the deadline (0.06 s < 0.1 s); the body as a whole does not.
        status, payload = self.send(delay=0.06)
        self.assertEqual((status, payload["code"], self.tool.calls), (408, "request_timeout", 0))

    def test_timeout_releases_the_inflight_slot(self):
        self.assertEqual(self.send(stall_after=1)[0], 408)
        status, _ = self.send()
        self.assertEqual((status, self.tool.calls), (200, 1))

    def test_timeout_is_checked_after_authentication(self):
        status, payload = run(asyncio.wait_for(chunked(
            self.app, "/mcp", [self.body], {**self.headers, "authorization": "Bearer wrong"}, stall_after=0), 5))
        self.assertEqual((status, payload["code"], self.tool.calls), (401, "authentication_failed", 0))


if __name__ == "__main__":
    unittest.main()
