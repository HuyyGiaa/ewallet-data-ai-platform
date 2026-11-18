"""CI/CD V2-P1: Gateway endpoints are configurable for production and staging only, production defaults are
unchanged, and staging can never route to production (g3-byo-coordinator environment: A2A and MCP SDKs)."""

from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock
from urllib.error import URLError

from ai_platform.llm_agent import gateway_environment as ge
from ai_platform.llm_agent.byo_coordinator import agent_card as coordinator_card
from ai_platform.llm_agent.byo_drift_specialist import DRIFT_ROUTE, DriftMCPClient
from ai_platform.llm_agent.byo_feature_rag_specialist import FeatureRagMCPClient
from ai_platform.llm_agent.model_config import validate_base_url
from ai_platform.llm_agent.nl_intake import SYSTEM_PROMPT, OpenAIToolPlanner
from ai_platform.llm_agent.programmatic_coordinator import CoordinatorFailure, GatewayPolicy, JsonHttpTransport
from tests.llm_agent.test_g3_programmatic_coordinator import request

# Production literals as they were before V2-P1 (0fff5625); the defaults must still equal them.
PROD_A2A = "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/"
PROD_FEATURE_RAG = "http://mcp-gateway.ewallet-agents.svc:8080/feature-rag/mcp"
PROD_DRIFT = "http://mcp-gateway.ewallet-agents.svc:8080/drift/mcp"
PROD_LLM = "http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1"
STG_A2A = "http://a2a-gateway.ewallet-agents-staging.svc.cluster.local:8083/"
STG_DRIFT = "http://mcp-gateway.ewallet-agents-staging.svc:8080/drift/mcp"
STG_LLM = "http://agentgateway.ewallet-agents-staging.svc.cluster.local:3000/v1"
STAGING = {"EWALLET_GATEWAY_NAMESPACE": "ewallet-agents-staging", "EWALLET_RUNTIME_NAMESPACE": "ewallet-agents-staging"}
SANDBOX = {"EWALLET_SANDBOX_SPECIALISTS": "drift-specialist,feature-rag-specialist"}


def env(**values: str):
    """os.environ without any Gateway selector, plus ``values``."""

    base = {k: v for k, v in os.environ.items() if k not in (ge.GATEWAY_NAMESPACE_ENV, ge.RUNTIME_NAMESPACE_ENV)}
    return mock.patch.dict(os.environ, {**base, **values}, clear=True)


class ProductionUnchangedTests(unittest.TestCase):
    def test_default_endpoints_are_the_original_production_ones(self):
        with env():
            policy = GatewayPolicy.from_env(os.environ, timeout_seconds=30.0)
            call = policy.call_for(request())
            self.assertEqual((call.url, call.headers["x-kagent-host"]), (PROD_A2A, "feature-rag-specialist.ewallet-agents"))
            self.assertEqual(policy, GatewayPolicy())
            self.assertEqual(FeatureRagMCPClient()._endpoint, PROD_FEATURE_RAG)
            self.assertEqual(DriftMCPClient()._endpoint, PROD_DRIFT)
            self.assertEqual(OpenAIToolPlanner(key_reader=lambda: "k")._url, PROD_LLM + "/chat/completions")
            self.assertEqual(coordinator_card().url, "http://fraud-coordinator.ewallet-agents:8080")
            sandbox = GatewayPolicy.from_env({**SANDBOX, "EWALLET_SANDBOX_NAMESPACE": "ewallet-sandbox"}, timeout_seconds=30.0)
            self.assertEqual(sandbox.call_for(request()).url,
                             PROD_A2A + "api/a2a-sandboxes/ewallet-sandbox/feature-rag-specialist/")

    def test_prompt_identity_system_prompt_bytes_are_unchanged(self):
        self.assertEqual(hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                         "cbb0c51b0edad83bd5640a3d03e542c44e5070018c2b83a5565ee82c17244380")


class StagingTests(unittest.TestCase):
    def test_staging_endpoints_resolve_to_the_staging_gateways(self):
        with env(**STAGING):
            policy = GatewayPolicy.from_env({**STAGING, **SANDBOX, "EWALLET_SANDBOX_NAMESPACE": "ewallet-sandbox-staging"},
                                            timeout_seconds=30.0)
            self.assertEqual(policy.call_for(request()).url,
                             STG_A2A + "api/a2a-sandboxes/ewallet-sandbox-staging/feature-rag-specialist/")
            direct = GatewayPolicy.from_env(STAGING, timeout_seconds=30.0).call_for(request())
            self.assertEqual(direct.headers["x-kagent-host"], "feature-rag-specialist.ewallet-agents-staging")
            self.assertEqual(DriftMCPClient()._endpoint, STG_DRIFT)
            self.assertEqual(OpenAIToolPlanner(key_reader=lambda: "k")._url, STG_LLM + "/chat/completions")
            self.assertEqual(coordinator_card().url, "http://fraud-coordinator.ewallet-agents-staging:8080")

    def test_unknown_or_unauthorized_namespaces_are_rejected(self):
        for value in ("", "ewallet-agents ", "EWALLET-AGENTS", "ewallet-sandbox", "kagent", "ewallet-agents-dev",
                      "evil.example.com", "10.0.0.1", "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/"):
            with self.subTest(value=value), self.assertRaises(ge.GatewayEnvironmentError):
                ge.resolve_gateway_environment({"EWALLET_GATEWAY_NAMESPACE": value})

    def test_staging_pod_without_or_with_a_wrong_selector_fails_instead_of_using_production(self):
        cases = [{"EWALLET_RUNTIME_NAMESPACE": "ewallet-agents-staging"},
                 {"EWALLET_RUNTIME_NAMESPACE": "ewallet-sandbox-staging"},
                 {"EWALLET_GATEWAY_NAMESPACE": "ewallet-agents", "EWALLET_RUNTIME_NAMESPACE": "ewallet-agents-staging"},
                 {"EWALLET_GATEWAY_NAMESPACE": "ewallet-agents-staging", "EWALLET_RUNTIME_NAMESPACE": "ewallet-agents"}]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ge.GatewayEnvironmentError):
                    GatewayPolicy.from_env(case, timeout_seconds=30.0)
                with env(**case), self.assertRaises(ge.GatewayEnvironmentError):
                    DriftMCPClient()

    def test_staging_cannot_route_to_production(self):
        with env(**STAGING):
            for build in (lambda: FeatureRagMCPClient(endpoint=PROD_FEATURE_RAG), lambda: DriftMCPClient(endpoint=PROD_DRIFT),
                          lambda: validate_base_url(PROD_LLM), lambda: OpenAIToolPlanner(base_url=PROD_LLM, key_reader=lambda: "k"),
                          lambda: GatewayPolicy.from_env({**STAGING, **SANDBOX, "EWALLET_SANDBOX_NAMESPACE": "ewallet-sandbox"},
                                                         timeout_seconds=30.0)):
                with self.assertRaises(ValueError):
                    build()
        with env():   # and production never accepts a staging destination
            for build in (lambda: DriftMCPClient(endpoint=STG_DRIFT), lambda: validate_base_url(STG_LLM),
                          lambda: GatewayPolicy(endpoint=STG_A2A),
                          lambda: GatewayPolicy(sandbox_specialists=frozenset({"drift-specialist"}),
                                                sandbox_namespace="ewallet-sandbox-staging")):
                with self.assertRaises(ValueError):
                    build()

    def test_endpoint_validation_stays_exact(self):
        host = ge.STAGING_GATEWAY.mcp_host
        for value in (f"http://{host}:8081/drift/mcp", f"http://{host}:8080/feature-rag/mcp", f"https://{host}:8080/drift/mcp",
                      f"http://{host}:8080/drift/mcp?x=1", f"http://{host}:8080/drift/mcp#f", f"http://u:p@{host}:8080/drift/mcp",
                      f"http://{host}:8080/drift%2Fmcp", f"http://{host.upper()}:8080/drift/mcp",
                      f"http://{host}.cluster.local:8080/drift/mcp", "http://evil.example.com:8080/drift/mcp",
                      "http://drift-mcp.ewallet-mcp.svc.cluster.local:8080/drift/mcp"):   # direct server bypass
            with self.subTest(value=value), self.assertRaises(ValueError):
                DriftMCPClient(endpoint=value, gateway=ge.STAGING_GATEWAY)
        with self.assertRaises(ValueError):   # a Specialist cannot use the other Specialist's route
            FeatureRagMCPClient(endpoint=ge.STAGING_GATEWAY.mcp_route(DRIFT_ROUTE), gateway=ge.STAGING_GATEWAY)

    def test_unavailable_staging_gateway_is_an_error_without_production_fallback(self):
        attempted = []

        def opener(http_request, *, timeout):
            attempted.append(http_request.full_url)
            raise URLError("connection refused")

        with self.assertRaises(CoordinatorFailure):
            JsonHttpTransport(opener)(GatewayPolicy.from_env(STAGING, timeout_seconds=5.0).call_for(request()))
        self.assertEqual(attempted, [STG_A2A])


if __name__ == "__main__":
    unittest.main()
