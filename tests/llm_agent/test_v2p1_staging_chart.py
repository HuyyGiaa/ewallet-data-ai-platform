"""CI/CD V2-P1: the staging Helm profile cannot render production resources or destinations, and the
production renders keep their configuration (render-only; llm-agent-tooling environment: helm, PyYAML)."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
import render_equivalence as equivalence  # noqa: E402

HELM = shutil.which("helm")
STAGING = "ewallet-agents-staging"
# A production namespace as a whole value (NetworkPolicy selector) or as a DNS/URL label.
PRODUCTION_REF = re.compile(r"(?:^|[./@])(ewallet-agents|ewallet-sandbox|ewallet-mcp|feature-store|fraud-serving|"
                            r"ml-platform|kubeflow)(?=[.:/]|$)")
FORBIDDEN_KINDS = {"Namespace", "ClusterRole", "ClusterRoleBinding", "PodDisruptionBudget", "HorizontalPodAutoscaler",
                   "ScaledObject"}
# Release identities are supplied at deploy time; these are non-deployable test digests.
SETS = ("--set", "agents.coordinator.byo.image=asia-southeast1-docker.pkg.dev/p/r/agent@sha256:" + "b" * 64,
        "--set", f"mcpServices.image=r.example/p/mcp@{equivalence.DIGEST}",
        "--set", f"mcpServices.identity.imageDigest={equivalence.DIGEST}", "--set", "mcpServices.identity.gitSha=" + "c" * 40,
        "--set", "mcpServices.identity.configRevision=staging-offline",
        "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_BASELINES_SHA256=" + "d" * 64,
        "--set", "mcpServices.services.drift-mcp.env.EWALLET_DRIFT_WINDOW_MANIFEST_SHA256=" + "e" * 64)


def render_staging(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([HELM, "template", "llm-agent-staging", str(CHART), "-f", "values-staging.yaml", *SETS, *extra],
                          cwd=CHART, capture_output=True, text=True)


def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node


def violations(docs: list[dict]) -> list[str]:
    """Check both ownership (where each object lives) and effective configuration (where it points)."""

    found = []
    for doc in docs:
        ident = f"{doc['kind']}/{doc['metadata'].get('namespace')}/{doc['metadata']['name']}"
        if doc["metadata"].get("namespace") != STAGING:
            found.append(f"{ident}: not in {STAGING}")   # also catches cluster-scoped objects
        if doc["kind"] in FORBIDDEN_KINDS:
            found.append(f"{ident}: forbidden kind")
        found += [f"{ident}: production reference {value!r}" for value in strings(doc) if PRODUCTION_REF.search(value)]
        if doc["kind"] == "Agent":
            byo = {e["name"]: e for e in doc["spec"]["byo"]["deployment"]["env"]}
            if byo.get("EWALLET_GATEWAY_NAMESPACE", {}).get("value") != STAGING:
                found.append(f"{ident}: Gateway selector is not staging")
            if byo.get("EWALLET_IDEMPOTENCY_TENANT", {}).get("value") != STAGING:
                found.append(f"{ident}: idempotency tenant is not staging")
    return found


@unittest.skipUnless(HELM, "helm binary unavailable")
class StagingChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = render_staging()
        assert result.returncode == 0, result.stderr
        cls.docs = [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_staging_render_is_isolated_and_minimal(self):
        self.assertEqual(violations(self.docs), [])
        names = {(d["kind"], d["metadata"]["name"]) for d in self.docs}
        for expected in (("Agent", "fraud-coordinator"), ("Deployment", "a2a-gateway"), ("Deployment", "mcp-gateway"),
                         ("Deployment", "feature-rag-mcp"), ("Deployment", "drift-mcp"),
                         ("StatefulSet", "coordinator-idempotency-redis")):
            self.assertIn(expected, names)
        for absent in ("agentgateway", "vllm-primary", "otel-collector"):
            self.assertNotIn(("Deployment", absent), names)

    def test_the_check_detects_production_contamination(self):
        def agent_env(docs, name, value):
            agent = next(d for d in docs if d["kind"] == "Agent")
            for item in agent["spec"]["byo"]["deployment"]["env"]:
                if item["name"] == name:
                    item["value"] = value

        mutations = {
            "object in production": lambda docs: docs.append(
                {"kind": "ConfigMap", "metadata": {"name": "x", "namespace": "ewallet-agents"}}),
            "policy into feature-store": lambda docs: docs.append(
                {"kind": "NetworkPolicy", "metadata": {"name": "x", "namespace": STAGING},
                 "spec": {"egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "feature-store"}}}]}]}}),
            "production MCP URL": lambda docs: next(d for d in docs if d["kind"] == "RemoteMCPServer")["spec"].update(
                url="http://mcp-gateway.ewallet-agents.svc:8080/drift/mcp"),
            "production Gateway selector": lambda docs: agent_env(docs, "EWALLET_GATEWAY_NAMESPACE", "ewallet-agents"),
            "production Redis tenant": lambda docs: agent_env(docs, "EWALLET_IDEMPOTENCY_TENANT", "ewallet-agents"),
            "cluster-scoped object": lambda docs: docs.append({"kind": "ClusterRole", "metadata": {"name": "x"}}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                docs = copy.deepcopy(self.docs)
                mutate(docs)
                self.assertTrue(violations(docs))

    def test_the_chart_guard_refuses_unsafe_staging_values(self):
        cases = {
            "production namespace": ("--set", "namespace.name=ewallet-agents"),
            "cross-namespace policy into feature-store": ("--set", "crossNamespace.flows[0].peer.namespace=feature-store"),
            "write into the kagent namespace": ("--set", "crossNamespace.flows[0].peerIngress=true"),
            "production MCP Gateway": ("--set", "agents.mcpGatewayUrl=http://mcp-gateway.ewallet-agents.svc:8080"),
            "production sandbox routes": ("--set", "a2aGateway.sandboxRoutes.namespace=ewallet-sandbox"),
            "production MCP backends": ("--set", "mcpGateway.backendNamespace=ewallet-mcp"),
            "production Redis tenant": ("--set", "idempotencyRedis.coordinator.tenant=ewallet-agents"),
            "production KServe": ("--set", "mcpServices.services.feature-rag-mcp.env.EWALLET_KSERVE_BASE_URL="
                                  "http://fraud-detector-predictor.fraud-serving.svc.cluster.local"),
            "production Feast database": ("--set-string", "mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true"),
            "autoscaling": ("--set", "agents.autoscaling.enabled=true"),
            "PDB": ("--set", "mcpServices.services.drift-mcp.pdb.enabled=true"),
            "kubeflow policy": ("--set", "networkPolicy.kfpUiIngress.enabled=true"),
            "staging namespace without the staging profile": ("--set", "deploymentProfile=production"),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                result = render_staging(*extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("staging-guard.yaml", result.stderr)


@unittest.skipUnless(HELM, "helm binary unavailable")
class ProductionRenderTests(unittest.TestCase):
    def test_production_agents_keep_their_configuration(self):
        for name, (release, args) in equivalence.PRODUCTION_PROFILES.items():
            with self.subTest(name):
                result = equivalence.render(HELM, CHART, release, args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("staging", result.stdout)
                for agent in (d for d in yaml.safe_load_all(result.stdout) if d and d["kind"] == "Agent" and "byo" in d["spec"]):
                    byo = {e["name"]: e.get("value") for e in agent["spec"]["byo"]["deployment"]["env"]}
                    self.assertEqual(byo["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], "otel-collector.ewallet-agents.svc.cluster.local:4317")
                    self.assertNotIn("EWALLET_GATEWAY_NAMESPACE", byo)

    @unittest.skipUnless(os.environ.get("EWALLET_RENDER_BASELINE_REF"), "set EWALLET_RENDER_BASELINE_REF (V2-P1: 0fff5625)")
    def test_production_render_is_byte_identical_to_the_baseline_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "src").mkdir()
            chart = equivalence.chart_from_ref(os.environ["EWALLET_RENDER_BASELINE_REF"], Path(tmp) / "src")
            equivalence.snapshot(Path(tmp) / "base", chart, equivalence.PRODUCTION_PROFILES, "baseline")
            equivalence.snapshot(Path(tmp) / "cur", CHART, equivalence.PRODUCTION_PROFILES, "current")
            self.assertEqual(equivalence.compare(Path(tmp) / "base", Path(tmp) / "cur"), 0)


if __name__ == "__main__":
    unittest.main()
