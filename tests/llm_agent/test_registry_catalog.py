"""agentregistry catalog (governance + prompt versions) source contract."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
import urllib.request

import yaml

from ops.llm_agent.registry_catalog import build_catalog, publish, schema_errors

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
OPENAPI = json.loads((ROOT / "infra/helm/agentregistry/openapi-subset-v0.4.0.json").read_text())
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())["agents"]
SHA = "a" * 40


def catalog():
    return build_catalog(VALUES, CHART / "prompts", SHA)


class FakeRegistry:
    """Records requests; answers /v0/apply and readbacks like the real server."""

    def __init__(self, *, apply_error=None, tamper=None):
        self.requests: list[urllib.request.Request] = []
        self.stored: dict[str, dict] = {}
        self.apply_error = apply_error
        self.tamper = tamper

    def __call__(self, request):
        self.requests.append(request)
        if request.get_method() == "POST":
            results = []
            for doc in yaml.safe_load_all(request.data):
                results.append({"kind": doc["kind"], "name": doc["metadata"]["name"], "tag": doc["metadata"]["tag"],
                                "status": "failed" if self.apply_error else "created", "error": self.apply_error})
                if "dryRun=false" in request.full_url and not self.apply_error:
                    stored = json.loads(json.dumps(doc))
                    if self.tamper and doc["metadata"]["name"] == self.tamper:
                        stored["spec"]["content"] = "tampered"
                    self.stored[f"{doc['metadata']['name']}/{doc['metadata']['tag']}"] = stored
            return 200, json.dumps({"results": results}).encode()
        name, tag = request.full_url.rsplit("/", 2)[-2:]
        item = self.stored.get(f"{name}/{tag}")
        return (200, json.dumps(item).encode()) if item else (404, b"{}")


class CatalogTests(unittest.TestCase):
    def test_documents_match_the_registry_openapi_schema(self):
        schemas = OPENAPI["schemas"]
        for doc in catalog():
            with self.subTest(kind=doc["kind"], name=doc["metadata"]["name"]):
                self.assertEqual(doc["apiVersion"], "ar.dev/v1alpha1")
                self.assertEqual(schema_errors(doc, schemas[doc["kind"]], schemas), [])
                for value in doc["metadata"].get("labels", {}).values():
                    self.assertRegex(value, r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")

    def test_catalog_mirrors_the_deployed_agents_and_prompts(self):
        docs = {(d["kind"], d["metadata"]["name"]): d for d in catalog()}
        agents = {n for k, n in docs if k == "Agent"}
        self.assertEqual(agents, {"fraud-coordinator", "feature-rag-specialist", "drift-specialist"})
        manifest = json.loads((CHART / "prompts/manifest.json").read_text())["prompts"]
        for name in agents:
            agent = docs[("Agent", name)]
            ref = agent["spec"]["instructions"]
            prompt = docs[("Prompt", ref["name"])]
            self.assertEqual(prompt["metadata"]["tag"], ref["tag"])
            entry = manifest[f"{name}/{ref['tag']}"]
            self.assertEqual(prompt["spec"]["content"], (CHART / "prompts" / entry["path"]).read_text())
            self.assertIn(entry["sha256"], prompt["spec"]["description"])
            self.assertEqual(agent["spec"]["compatibleHarnesses"], [{"type": "kagent"}])
            self.assertEqual(agent["metadata"]["labels"]["ewallet.io/git-sha"], SHA)
        servers = {r["name"] for r in docs[("Agent", "feature-rag-specialist")]["spec"]["mcpServers"]}
        self.assertEqual(servers, {"feature-rag-mcp"})
        self.assertEqual(docs[("Agent", "fraud-coordinator")]["spec"]["mcpServers"], [])
        for server in ("feature-rag-mcp", "drift-mcp"):
            self.assertTrue(docs[("MCPServer", server)]["spec"]["remote"]["url"].startswith("http://mcp-gateway."))

    def test_tampered_prompt_is_refused(self):
        values = json.loads(json.dumps(VALUES))
        values["coordinator"]["promptVersion"] = "v1"
        bad = CHART / "prompts/fraud-coordinator/v1.md"
        original = bad.read_text()
        try:
            bad.write_text(original + "x")
            with self.assertRaises(ValueError):
                build_catalog(values, CHART / "prompts", SHA)
        finally:
            bad.write_text(original)


class PublishTests(unittest.TestCase):
    def test_publish_then_read_back_every_entry(self):
        fake = FakeRegistry()
        report = publish(catalog(), "http://127.0.0.1:12121", dry_run=False, opener=fake, token="secret-token")
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(len(report["readback"]), len(catalog()))
        self.assertEqual(fake.requests[0].get_header("Content-type"), "application/yaml")
        self.assertNotIn("secret-token", json.dumps(report))

    def test_dry_run_reads_nothing_back(self):
        fake = FakeRegistry()
        report = publish(catalog(), "http://localhost:12121", dry_run=True, opener=fake)
        self.assertEqual((report["verdict"], report["readback"], len(fake.requests)), ("PASS", [], 1))
        self.assertIn("dryRun=true", fake.requests[0].full_url)

    def test_rejected_apply_or_changed_prompt_fails(self):
        self.assertEqual(publish(catalog(), "http://127.0.0.1:1", dry_run=False,
                                 opener=FakeRegistry(apply_error="validation"))["verdict"], "FAIL")
        report = publish(catalog(), "http://127.0.0.1:1", dry_run=False,
                         opener=FakeRegistry(tamper="drift-specialist-system"))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual([r["name"] for r in report["readback"] if not r["match"]], ["drift-specialist-system"])

    def test_only_a_local_port_forward_is_accepted(self):
        for url in ("https://registry.example.com", "http://10.0.0.5:12121", "file:///etc/passwd"):
            with self.subTest(url), self.assertRaises(ValueError):
                publish(catalog(), url, dry_run=True, opener=FakeRegistry())


class LocalRegistryEvidenceTests(unittest.TestCase):
    def test_real_registry_accepted_and_returned_every_entry(self):
        evidence = json.loads((ROOT / "docs/evidence/llm_agent/source_review/gateways/registry_local_smoke.json").read_text())
        self.assertEqual(evidence["verdict"], "PASS")
        self.assertEqual(evidence["registry_version"]["version"], "v0.4.0")
        self.assertEqual(len(evidence["apply"]), 8)
        self.assertTrue(all(r["status"] == "created" and not r.get("error") for r in evidence["apply"]))
        self.assertTrue(all(r["match"] for r in evidence["readback"]))
        self.assertEqual({n for n, _ in evidence["agents_listing"]},
                         {"fraud-coordinator", "feature-rag-specialist", "drift-specialist"})


if __name__ == "__main__":
    unittest.main()
