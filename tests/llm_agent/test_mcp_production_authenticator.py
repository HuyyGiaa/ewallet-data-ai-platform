"""Offline production-authentication contracts using synthetic credentials."""

from __future__ import annotations

import io
import json
import logging
import unittest

from ai_platform.llm_agent.tools.security import (
    AuthenticationError,
    DenyAllAuthenticator,
    SERVICE_TOKEN_ENV,
    SERVICE_TOKEN_HEADER,
    ServiceTokenAuthenticator,
    ToolGrants,
    authenticator_readiness,
    configured_authenticator,
)
from tests.llm_agent.test_phase_d_tools import (
    ENV,
    FASTAPI,
    asgi,
    drift_args,
    feature_rag,
    rpc,
    run,
    transaction,
)

if FASTAPI:
    from ai_platform.llm_agent.tools.service import create_drift_app, create_tool_app


FEATURE_RAG = "feature-rag-specialist"
DRIFT = "drift-specialist"
FEATURE_TOKEN = "test-feature-rag-token-" + "a" * 40
DRIFT_FEATURE_TOKEN = "test-drift-feature-token-" + "b" * 40
DRIFT_SERVICE_TOKEN = "test-drift-service-token-" + "c" * 40
REVOKED_TOKEN = "test-revoked-token-" + "d" * 40
UNKNOWN_TOKEN = "test-unknown-token-" + "e" * 40


def credential_table(*entries):
    return json.dumps({"version": 1, "credentials": list(entries)})


def credential(credential_id, subject, token, status="active"):
    return {"id": credential_id, "subject": subject, "token": token, "status": status}


def feature_table():
    return credential_table(
        credential("feature-v1", FEATURE_RAG, FEATURE_TOKEN),
        credential("drift-feature-v1", DRIFT, DRIFT_FEATURE_TOKEN),
        credential("feature-revoked", FEATURE_RAG, REVOKED_TOKEN, "revoked"),
    )


def authenticator(raw=None, subjects=frozenset({FEATURE_RAG, DRIFT})):
    return ServiceTokenAuthenticator.from_env(
        {SERVICE_TOKEN_ENV: raw or feature_table()}, known_subjects=subjects
    )


def headers(token):
    return {SERVICE_TOKEN_HEADER: token}


class ServiceTokenConfigurationTests(unittest.TestCase):
    def test_missing_empty_and_malformed_configuration_fail_closed(self):
        for raw in (None, "", 7, "{}", "not-json", json.dumps({"version": 2, "credentials": []})):
            with self.subTest(raw=raw):
                environment = {} if raw is None else {SERVICE_TOKEN_ENV: raw}
                selected = configured_authenticator(
                    environment, known_subjects=frozenset({FEATURE_RAG})
                )
                self.assertIsInstance(selected, DenyAllAuthenticator)
                self.assertEqual(run(authenticator_readiness(selected)), "authentication_not_configured")

    def test_unknown_subject_duplicate_weak_and_all_revoked_fail_closed(self):
        tables = (
            credential_table(credential("ghost", "ghost-specialist", UNKNOWN_TOKEN)),
            credential_table(
                credential("one", FEATURE_RAG, FEATURE_TOKEN),
                credential("two", DRIFT, FEATURE_TOKEN),
            ),
            credential_table(credential("weak", FEATURE_RAG, "short")),
            credential_table(credential("old", FEATURE_RAG, REVOKED_TOKEN, "revoked")),
        )
        for raw in tables:
            with self.subTest(raw=raw):
                selected = configured_authenticator(
                    {SERVICE_TOKEN_ENV: raw}, known_subjects=frozenset({FEATURE_RAG, DRIFT})
                )
                self.assertIsInstance(selected, DenyAllAuthenticator)

    def test_service_scoping_rejects_a_subject_without_grants_for_that_service(self):
        # drift-mcp grants only drift-specialist. Supplying the feature-rag
        # table must fail closed rather than silently accepting a cross-service identity.
        selected = configured_authenticator(
            {SERVICE_TOKEN_ENV: feature_table()}, known_subjects=frozenset({DRIFT})
        )
        self.assertIsInstance(selected, DenyAllAuthenticator)
        drift_only = credential_table(credential("drift-v1", DRIFT, DRIFT_SERVICE_TOKEN))
        self.assertIsNone(run(authenticator_readiness(authenticator(drift_only, frozenset({DRIFT})))))

    def test_rejection_log_never_contains_configuration_or_tokens(self):
        stream = io.StringIO()
        logger = logging.getLogger("test.g3.auth.config")
        logger.handlers = [logging.StreamHandler(stream)]
        logger.propagate = False
        bad = credential_table(credential("feature", FEATURE_RAG, FEATURE_TOKEN, "invalid"))
        configured_authenticator(
            {SERVICE_TOKEN_ENV: bad}, known_subjects=frozenset({FEATURE_RAG}), logger=logger
        )
        self.assertIn("authentication_configuration_rejected", stream.getvalue())
        self.assertNotIn(FEATURE_TOKEN, stream.getvalue())


class ServiceTokenAuthenticationTests(unittest.TestCase):
    def assert_auth_error(self, expected, supplied_headers):
        with self.assertRaises(AuthenticationError) as caught:
            run(authenticator().authenticate(supplied_headers))
        self.assertEqual(caught.exception.code, expected)

    def test_missing_invalid_unknown_and_revoked_tokens_are_denied(self):
        self.assert_auth_error("authentication_required", {})
        self.assert_auth_error("authentication_failed", headers("too-short"))
        self.assert_auth_error("authentication_failed", headers(UNKNOWN_TOKEN))
        self.assert_auth_error("authentication_failed", headers(REVOKED_TOKEN))

    def test_server_mapping_is_the_only_source_of_principal_identity(self):
        principal = run(authenticator().authenticate({
            **headers(FEATURE_TOKEN),
            "x-ewallet-actor": DRIFT,
            "x-ewallet-caller": DRIFT,
        }))
        self.assertEqual((principal.subject, principal.mechanism), (FEATURE_RAG, "service-token"))

    def test_rotation_overlap_accepts_new_and_old_active_then_rejects_revoked(self):
        replacement = "test-feature-rag-token-v2-" + "f" * 40
        overlap = credential_table(
            credential("feature-v1", FEATURE_RAG, FEATURE_TOKEN),
            credential("feature-v2", FEATURE_RAG, replacement),
        )
        rotating = authenticator(overlap, frozenset({FEATURE_RAG}))
        self.assertEqual(run(rotating.authenticate(headers(FEATURE_TOKEN))).subject, FEATURE_RAG)
        self.assertEqual(run(rotating.authenticate(headers(replacement))).subject, FEATURE_RAG)
        revoked = authenticator(credential_table(
            credential("feature-v1", FEATURE_RAG, FEATURE_TOKEN, "revoked"),
            credential("feature-v2", FEATURE_RAG, replacement),
        ), frozenset({FEATURE_RAG}))
        with self.assertRaises(AuthenticationError):
            run(revoked.authenticate(headers(FEATURE_TOKEN)))


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable")
class ProductionAuthorizationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.grants = ToolGrants({
            FEATURE_RAG: frozenset({"feature_lookup", "fraud_scoring", "knowledge_search"}),
            DRIFT: frozenset({"feature_lookup"}),
        })
        self.app = create_tool_app(
            service_name="feature-rag-mcp",
            executor=feature_rag(),
            authenticator=authenticator(),
            grants=self.grants,
        )

    def test_existing_specialist_roles_are_preserved_for_rest(self):
        status, body = run(asgi(
            self.app, "POST", "/v1/tools/fraud_scoring",
            {"transaction": transaction()}, headers=headers(FEATURE_TOKEN),
        ))
        self.assertIn(status, (200, 503))
        self.assertIn(body["status"], ("ok", "unavailable"))

        status, body = run(asgi(
            self.app, "POST", "/v1/tools/fraud_scoring",
            {"transaction": transaction()}, headers=headers(DRIFT_FEATURE_TOKEN),
        ))
        self.assertEqual((status, body["code"]), (403, "tool_forbidden"))

    def test_mcp_tools_list_and_direct_call_are_filtered_by_toolgrants(self):
        status, body = run(asgi(
            self.app, "POST", "/mcp", rpc("tools/list"), headers=headers(DRIFT_FEATURE_TOKEN),
        ))
        self.assertEqual(status, 200)
        self.assertEqual({item["name"] for item in body["result"]["tools"]}, {"feature_lookup"})
        status, body = run(asgi(
            self.app, "POST", "/mcp", rpc("tools/call", {
                "name": "fraud_scoring", "arguments": {"transaction": transaction()},
            }), headers=headers(DRIFT_FEATURE_TOKEN),
        ))
        self.assertEqual((status, body["error"]["code"]), (200, -32602))

    def test_discovery_subject_lists_every_tool_but_cannot_call_one(self):
        # The kagent controller reconciling a RemoteMCPServer: list only.
        controller_token = "kagent-controller-discovery-token-0123456789abcdef"
        table = credential_table(credential("controller-v1", "kagent-controller", controller_token))
        grants = ToolGrants(dict(self.grants.grants), frozenset({"kagent-controller"}))
        app = create_tool_app(
            service_name="feature-rag-mcp", executor=feature_rag(),
            authenticator=authenticator(table, grants.subjects), grants=grants,
        )
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/list"), headers=headers(controller_token)))
        self.assertEqual(status, 200)
        self.assertEqual({item["name"] for item in body["result"]["tools"]},
                         {"feature_lookup", "fraud_scoring", "knowledge_search", "chunk_lookup"} & set(feature_rag().handlers))
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/call", {
            "name": "feature_lookup", "arguments": {"transaction": transaction()},
        }), headers=headers(controller_token)))
        self.assertEqual((status, body["error"]["code"]), (200, -32602))
        status, body = run(asgi(app, "POST", "/v1/tools/feature_lookup", {"transaction": transaction()},
                                headers=headers(controller_token)))
        self.assertEqual((status, body["code"]), (403, "tool_forbidden"))

    def test_discovery_and_granted_subjects_must_be_disjoint(self):
        with self.assertRaises(ValueError):
            ToolGrants({DRIFT: frozenset({"feature_lookup"})}, frozenset({DRIFT}))
        self.assertEqual(ToolGrants.from_env({"EWALLET_MCP_DISCOVERY_SUBJECTS": '["kagent-controller"]'}).discovery,
                         frozenset({"kagent-controller"}))

    def test_authentication_failure_is_redacted_and_tool_never_runs(self):
        stream = io.StringIO()
        logger = logging.getLogger("test.g3.auth.request")
        logger.handlers = [logging.StreamHandler(stream)]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        app = create_tool_app(
            service_name="feature-rag-mcp", executor=feature_rag(),
            authenticator=authenticator(), grants=self.grants, logger=logger,
        )
        status, body = run(asgi(
            app, "POST", "/mcp", rpc("ping"), headers=headers(UNKNOWN_TOKEN),
        ))
        self.assertEqual((status, body["code"]), (401, "authentication_failed"))
        self.assertIn("request_rejected", stream.getvalue())
        self.assertNotIn(UNKNOWN_TOKEN, stream.getvalue() + json.dumps(body))

    def test_production_factory_uses_service_scoped_configuration(self):
        drift_table = credential_table(credential("drift-v1", DRIFT, DRIFT_SERVICE_TOKEN))
        environment = {
            **ENV,
            SERVICE_TOKEN_ENV: drift_table,
            "EWALLET_MCP_TOOL_GRANTS": json.dumps({DRIFT: ["drift_detection"]}),
        }
        app = create_drift_app(environment)
        self.assertEqual(run(asgi(app, "GET", "/health/ready"))[0], 200)
        status, body = run(asgi(
            app, "POST", "/v1/tools/drift_detection", drift_args(),
            headers=headers(DRIFT_SERVICE_TOKEN),
        ))
        self.assertEqual((status, body["status"]), (503, "unavailable"))
        status, body = run(asgi(
            app, "POST", "/v1/tools/drift_detection", drift_args(),
            headers=headers(UNKNOWN_TOKEN),
        ))
        self.assertEqual((status, body["code"]), (401, "authentication_failed"))


if __name__ == "__main__":
    unittest.main()
