"""V2-P3B Managed Prompt profile (row 34): Registry -> ConfigMap -> Coordinator -> LLM, inside staging only.

The overlay (values-staging-managed-prompt.yaml) is checked with the same isolation rules as Level A
(test_v2p1_staging_chart.violations), its credentials come only from the staging inventory, and the runtime
verdict (scripts/ci/managed_prompt_verify.py) fails closed. Render-only; llm-agent-tooling environment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/llm_agent"))
import test_v2p1_staging_chart as v2p1  # noqa: E402
from ops.llm_agent import staging_credentials as creds  # noqa: E402
from scripts.ci import managed_prompt_verify as verify  # noqa: E402

OVERLAY = "values-staging-managed-prompt.yaml"
MANIFEST = json.loads((ROOT / "infra/helm/llm-agent-platform/prompts/manifest.json").read_text(encoding="utf-8"))
PINNED = verify.pinned(MANIFEST)
CONTRACT = json.loads((ROOT / "jenkins/cd/agent-staging.contract.json").read_text(encoding="utf-8"))


@unittest.skipUnless(v2p1.HELM, "helm binary unavailable")
class ManagedPromptProfileRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = v2p1.render_staging("-f", OVERLAY)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        level_a = v2p1.render_staging()
        cls.level_a = [d for d in yaml.safe_load_all(level_a.stdout) if d]

    def find(self, kind, name):
        return next(d for d in self.docs if d["kind"] == kind and d["metadata"]["name"] == name)

    def test_the_profile_is_as_isolated_as_level_a(self):
        self.assertEqual(v2p1.violations(self.docs), [])

    def test_the_coordinator_reads_the_registry_prompt_and_calls_the_staging_gateway(self):
        env = {e["name"]: e for e in self.find("Agent", "fraud-coordinator")["spec"]["byo"]["deployment"]["env"]}
        self.assertEqual(env["EWALLET_NL_INTAKE_ENABLED"]["value"], "true")
        self.assertEqual(env["EWALLET_NL_PROMPT_SOURCE"]["value"], "registry")
        self.assertEqual(env["EWALLET_NL_PROMPT_VERSION"]["value"], verify.PROMPT)
        self.assertEqual(env["EWALLET_NL_PROMPT_SHA256"]["value"], PINNED)
        self.assertEqual(env["EWALLET_LLM_BASE_URL"]["value"], verify.STAGING_GATEWAY.rsplit("/chat", 1)[0])
        self.assertEqual(env["EWALLET_LLM_API_KEY"]["valueFrom"]["secretKeyRef"]["name"], "agentgateway-client-key-staging")

    def test_only_staging_llm_credentials_are_referenced(self):
        referenced = {value["secretKeyRef"]["name"] for doc in self.docs for value in _walk(doc)
                      if isinstance(value, dict) and "secretKeyRef" in value}
        llm = {s.name for s in creds.LLM_ONLY}
        self.assertTrue(llm <= referenced)
        self.assertEqual({name for name in referenced if "agentgateway" in name or "vllm" in name}, llm)

    def test_the_llm_path_has_exactly_two_enforced_hops_and_one_vllm(self):
        names = {(d["kind"], d["metadata"]["name"]) for d in self.docs}
        for hop in ("fraud-coordinator-to-agentgateway", "agentgateway-to-vllm"):
            self.assertIn(("AuthorizationPolicy", f"allow-{hop}"), names)
            self.assertIn(("NetworkPolicy", f"allow-{hop}-ingress"), names)
            self.assertIn(("NetworkPolicy", f"allow-{hop}-egress"), names)
        self.assertEqual(self.find("Deployment", "vllm-primary")["spec"]["replicas"], 1)
        self.assertEqual(self.find("Deployment", "vllm-fallback")["spec"]["replicas"], 0)
        image = self.find("Deployment", "vllm-primary")["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertRegex(image, r"/vllm-cpu-model@sha256:[0-9a-f]{64}$")

    def test_level_a_keeps_natural_language_and_the_llm_path_off(self):
        names = {d["metadata"]["name"] for d in self.level_a}
        self.assertFalse({"agentgateway", "vllm-primary", "vllm-fallback"} & names)
        env = {e["name"]: e for d in self.level_a if d["kind"] == "Agent"
               for e in d["spec"]["byo"]["deployment"]["env"]}
        self.assertNotEqual(env.get("EWALLET_NL_INTAKE_ENABLED", {}).get("value"), "true")


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class ManagedPromptCredentialTests(unittest.TestCase):
    def test_the_contract_lists_the_profile_keys(self):
        self.assertEqual(CONTRACT["dependencies"]["llm_only_secrets"], [s.name for s in creds.LLM_ONLY])

    def test_profile_keys_are_fresh_distinct_and_never_level_a(self):
        documents = creds.llm_manifests()
        self.assertEqual([d["metadata"]["name"] for d in documents], [s.name for s in creds.LLM_ONLY])
        self.assertEqual(len({d["stringData"]["api-key"] for d in documents}), 2)
        self.assertTrue(all(d["immutable"] and d["metadata"]["namespace"] == creds.AGENTS for d in documents))
        self.assertFalse({s.name for s in creds.LLM_ONLY} & {s.name for s in creds.SECRETS})

    def test_level_a_is_the_default_profile(self):
        with mock.patch.object(creds, "existing", return_value=[]) as existing, \
                mock.patch.object(creds, "manifests", return_value=[]) as level_a, \
                mock.patch.object(creds, "llm_manifests", return_value=[]) as llm:
            self.assertEqual(creds.main(["create", "--context", "ctx"]), 0)
            existing.assert_called_with("ctx", creds.SECRETS)
            level_a.assert_called_once()
            llm.assert_not_called()
            self.assertEqual(creds.main(["create", "--context", "ctx", "--profile", "managed-prompt"]), 0)
            existing.assert_called_with("ctx", creds.LLM_ONLY)
            llm.assert_called_once()

    def test_create_refuses_when_a_profile_key_exists(self):
        with mock.patch.object(creds, "existing", return_value=["ewallet-agents-staging/vllm-api-key-staging"]):
            self.assertEqual(creds.main(["create", "--context", "ctx", "--profile", "managed-prompt"]), 2)


def _capture(sha=PINNED, source="registry", url=verify.STAGING_GATEWAY, tokens=(695, 264), namespace=None):
    body = "managed intake prompt\n"
    prompt_sha = hashlib.sha256(body.encode()).hexdigest() if sha == "body" else sha
    configmap = {"metadata": {"namespace": namespace or "ewallet-agents-staging"},
                 "data": {"system.md": body, "prompt.json": json.dumps(
                     {"agent": "fraud-coordinator-intake", "version": "v1", "sha256": prompt_sha, "source": "registry"})}}
    log = "\n".join([
        json.dumps({"event": "nl_prompt.resolved", "prompt": verify.PROMPT, "sha256": prompt_sha, "source": source}),
        f'INFO httpx HTTP Request: POST {url} "HTTP/1.1 200 OK"'])
    text = f"Trạng thái: `ok` · request_id `nl-1` · LLM chọn công cụ `fraud-evidence` ({tokens[0]}+{tokens[1]} token) · nl-intake/v1"
    answer = {"result": {"artifacts": [{"parts": [{"kind": "text", "text": text}]}]}}
    return configmap, log, answer, prompt_sha


class ManagedPromptVerdictTests(unittest.TestCase):
    def test_the_full_chain_is_verified(self):
        configmap, log, answer, sha = _capture(sha="body")
        result = verify.evaluate(configmap, log, answer, sha)
        self.assertEqual(result["verdict"], verify.VERDICT, result)
        self.assertEqual((result["tool"], result["tokens"]), ("fraud-evidence", [695, 264]))

    def test_every_break_in_the_chain_fails_closed(self):
        configmap, log, answer, sha = _capture(sha="body")
        cases = {
            "configmap_prompt": (dict(configmap, data=dict(configmap["data"], **{"system.md": "tampered\n"})), log, answer),
            "coordinator_resolved_registry_prompt": (configmap, log.replace('"source": "registry"', '"source": "builtin"'), answer),
            "llm_request_to_staging_gateway": (configmap, log.replace("ewallet-agents-staging", "ewallet-agents"), answer),
            "answer_used_llm_selection": (configmap, log, json.loads(json.dumps(answer).replace("695+264", "0+0"))),
        }
        for check, (cm, lg, ans) in cases.items():
            with self.subTest(check):
                result = verify.evaluate(cm, lg, ans, sha)
                self.assertNotEqual(result["verdict"], verify.VERDICT)
                self.assertEqual(result["checks"][check], "FAIL")

    def test_a_production_configmap_or_a_missing_request_fails(self):
        configmap, log, answer, sha = _capture(sha="body", namespace="ewallet-agents")
        self.assertEqual(verify.evaluate(configmap, log, answer, sha)["checks"]["configmap_prompt"], "FAIL")
        configmap, log, answer, sha = _capture(sha="body")
        no_request = log.splitlines()[0]
        self.assertEqual(verify.evaluate(configmap, no_request, answer, sha)["checks"]["llm_request_to_staging_gateway"], "FAIL")

    def test_the_pin_comes_from_the_chart_manifest(self):
        self.assertRegex(PINNED, r"^[0-9a-f]{64}$")
        with self.assertRaises(ValueError):
            verify.pinned({"prompts": {}})


if __name__ == "__main__":
    unittest.main()
