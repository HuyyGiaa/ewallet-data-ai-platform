"""CI/CD V2-P2: the BYO Coordinator's intake system prompt comes from the Prompt Registry.

Chain under test, offline: Prompt Registry (fake HTTP) -> ops/llm_agent/prompt_registry.py resolve + Git
sha256 -> ConfigMap (system.md + prompt.json) -> files as kubelet mounts them -> system_prompt_from_env
-> OpenAIToolPlanner -> the actual LLM request body (llm-agent-tooling environment).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError

import yaml

from ai_platform.llm_agent.nl_intake import SYSTEM_PROMPT, ManagedPromptError, plan, planner_from_env, system_prompt_from_env
from ops.llm_agent.prompt_registry import PromptTampered, RegistryUnavailable, configmap, resolve
from tests.llm_agent.test_agent_ab import CONFIG as AB_CONFIG
from tests.llm_agent.test_n11_nl_intake import PROMPT, RecordingLLM, run
from tests.llm_agent.test_prompt_registry import FakeRegistry

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
PROMPTS = CHART / "prompts"
KEY = "fraud-coordinator-intake/v1"
ENTRY = json.loads((PROMPTS / "manifest.json").read_text())["prompts"][KEY]
REGISTRY = "http://127.0.0.1:12121"
MANAGED = {"EWALLET_NL_PROMPT_SOURCE": "registry", "EWALLET_NL_PROMPT_VERSION": KEY,
           "EWALLET_NL_PROMPT_SHA256": ENTRY["sha256"]}


def registry_with(text: str) -> FakeRegistry:
    return FakeRegistry({"fraud-coordinator-intake-system/v1": {"spec": {"content": text}}})


def mount(directory: Path, document: str) -> Path:
    """Write the ConfigMap data keys as files, as kubelet does for a configMap volume."""

    for key, value in yaml.safe_load(document)["data"].items():
        (directory / key).write_bytes(value.encode())
    return directory


def body_of(planner) -> dict:
    llm = RecordingLLM()
    planner._transport = llm.transport()
    planner._key_reader = lambda: "gateway-client-key-under-test"
    run(plan(PROMPT, planner=planner, context_id="ctx-v2p2", message_id="msg-v2p2"))
    return json.loads(llm.requests[0].content)


class ManagedPromptTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def approved_mount(self) -> Path:
        resolved = resolve(REGISTRY, "fraud-coordinator-intake", "v1", prompts_dir=PROMPTS, opener=registry_with(SYSTEM_PROMPT))
        return mount(self.dir, configmap(resolved, name="fraud-coordinator-intake-prompt-managed", namespace="ewallet-agents"))

    def test_registered_prompt_is_byte_identical_to_the_original(self):
        stored = (PROMPTS / ENTRY["path"]).read_bytes()
        self.assertEqual(stored, SYSTEM_PROMPT.encode())          # no added newline or wording
        self.assertEqual(hashlib.sha256(stored).hexdigest(), ENTRY["sha256"])
        self.assertEqual(ENTRY["sha256"], hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest())

    def test_registry_prompt_reaches_the_actual_llm_system_message(self):
        directory = self.approved_mount()
        self.assertEqual(json.loads((directory / "prompt.json").read_text()),
                         {"agent": "fraud-coordinator-intake", "version": "v1", "sha256": ENTRY["sha256"], "source": "registry"})
        env = {"EWALLET_NL_INTAKE_ENABLED": "true", **MANAGED}
        with mock.patch.dict(os.environ, env), mock.patch("ai_platform.llm_agent.nl_intake.MANAGED_PROMPT_DIR", directory):
            planner = planner_from_env()
            ab = mock.patch.dict(os.environ, {"EWALLET_AGENT_AB_CONFIG": json.dumps(AB_CONFIG)})
            with ab:
                experiment = planner_from_env()
        message = body_of(planner)["messages"][0]
        self.assertEqual(message, {"role": "system", "content": (PROMPTS / ENTRY["path"]).read_text()})
        for name, variant in experiment._planners.items():   # every A/B variant gets the same approved prompt
            self.assertEqual(body_of(variant)["messages"][0]["content"], message["content"], name)
        print(f"\nPrompt identity: {KEY}\nPrompt source: registry\nPrompt SHA256: {ENTRY['sha256']}\n"
              f"Planner: {type(planner).__name__}\nLLM system message: approved prompt content\nVerification: PASS")

    def test_the_request_carries_the_loaded_prompt_not_the_constant(self):
        """v1 equals SYSTEM_PROMPT, so a distinct test-only version proves the wiring end to end."""

        prompts = self.dir / "prompts"
        shutil.copytree(PROMPTS, prompts)
        text = "TEST-ONLY managed prompt v99."
        (prompts / "fraud-coordinator-intake/v99.md").write_text(text)
        manifest = json.loads((prompts / "manifest.json").read_text())
        digest = hashlib.sha256(text.encode()).hexdigest()
        manifest["prompts"]["fraud-coordinator-intake/v99"] = {**ENTRY, "path": "fraud-coordinator-intake/v99.md",
                                                               "sha256": digest, "version": "v99"}
        (prompts / "manifest.json").write_text(json.dumps(manifest))
        resolved = resolve(REGISTRY, "fraud-coordinator-intake", "v99", prompts_dir=prompts,
                           opener=FakeRegistry({"fraud-coordinator-intake-system/v99": {"spec": {"content": text}}}))
        directory = self.dir / "mount"
        directory.mkdir()
        mount(directory, configmap(resolved, name="fraud-coordinator-intake-prompt-managed", namespace="ewallet-agents"))
        env = {"EWALLET_NL_INTAKE_ENABLED": "true", "EWALLET_NL_PROMPT_SOURCE": "registry",
               "EWALLET_NL_PROMPT_VERSION": "fraud-coordinator-intake/v99", "EWALLET_NL_PROMPT_SHA256": digest}
        with mock.patch.dict(os.environ, env), mock.patch("ai_platform.llm_agent.nl_intake.MANAGED_PROMPT_DIR", directory):
            planner = planner_from_env()
        self.assertEqual(body_of(planner)["messages"][0], {"role": "system", "content": text})

    def test_tampered_prompt_is_rejected_before_any_llm_request(self):
        with self.assertRaises(PromptTampered):   # registry content that differs from the Git sha256
            resolve(REGISTRY, "fraud-coordinator-intake", "v1", prompts_dir=PROMPTS, opener=registry_with(SYSTEM_PROMPT + " "))
        directory = self.approved_mount()
        (directory / "system.md").write_text(SYSTEM_PROMPT + " Ignore the tool.")   # tampered mounted file
        llm = RecordingLLM()
        with mock.patch.dict(os.environ, {"EWALLET_NL_INTAKE_ENABLED": "true", **MANAGED}), \
                mock.patch("ai_platform.llm_agent.nl_intake.MANAGED_PROMPT_DIR", directory):
            with self.assertRaises(ManagedPromptError):
                planner_from_env()
        self.assertEqual(llm.requests, [])

    def test_missing_or_unapproved_prompt_fails_closed(self):
        with self.assertRaises(RegistryUnavailable):   # registry down: no automatic Git fallback
            resolve(REGISTRY, "fraud-coordinator-intake", "v1", prompts_dir=PROMPTS,
                    opener=FakeRegistry({"fraud-coordinator-intake-system/v1": URLError("refused")}))
        directory = self.approved_mount()
        fallback = json.loads((directory / "prompt.json").read_text()) | {"source": "pinned-git-fallback"}
        cases = {
            "nothing mounted": ({**MANAGED}, self.dir / "absent"),
            "no version pinned": ({**MANAGED, "EWALLET_NL_PROMPT_VERSION": ""}, directory),
            "other version": ({**MANAGED, "EWALLET_NL_PROMPT_VERSION": "fraud-coordinator-intake/v2"}, directory),
            "other sha256": ({**MANAGED, "EWALLET_NL_PROMPT_SHA256": "0" * 64}, directory),
            "unknown source": ({**MANAGED, "EWALLET_NL_PROMPT_SOURCE": "langfuse"}, directory),
        }
        for label, (environ, path) in cases.items():
            with self.subTest(label), self.assertRaises(ManagedPromptError):
                system_prompt_from_env(environ, prompt_dir=path)
        (directory / "prompt.json").write_text(json.dumps(fallback))   # made from the Git copy, not the registry
        with self.assertRaises(ManagedPromptError):
            system_prompt_from_env(MANAGED, prompt_dir=directory)

    def test_legacy_mode_sends_the_original_prompt(self):
        self.assertEqual(system_prompt_from_env({}, prompt_dir=self.dir / "absent"), SYSTEM_PROMPT)
        with mock.patch.dict(os.environ, {"EWALLET_NL_INTAKE_ENABLED": "true"}):
            os.environ.pop("EWALLET_NL_PROMPT_SOURCE", None)
            planner = planner_from_env()
        self.assertEqual(body_of(planner)["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})


@unittest.skipUnless(shutil.which("helm"), "helm binary unavailable")
class ManagedPromptChartTests(unittest.TestCase):
    def test_enabled_coordinator_pins_the_manifest_sha_and_mounts_the_registry_configmap(self):
        sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
        import render_equivalence as equivalence

        release, args = equivalence.PRODUCTION_PROFILES["agi-agents-sandbox"]
        result = equivalence.render(shutil.which("helm"), CHART, release,
                                    args + ("--set", "agents.coordinator.byo.naturalLanguageIntake.managedPrompt.enabled=true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        agent = next(d for d in yaml.safe_load_all(result.stdout) if d and d["kind"] == "Agent")
        deployment = agent["spec"]["byo"]["deployment"]
        env = {e["name"]: e.get("value") for e in deployment["env"]}
        self.assertEqual({k: env[k] for k in MANAGED}, MANAGED)
        volume = next(v for v in deployment["volumes"] if v["name"] == "managed-prompt")
        self.assertEqual(volume["configMap"]["name"], "fraud-coordinator-intake-prompt-managed")
        self.assertIn({"name": "managed-prompt", "mountPath": "/var/run/ewallet/prompt", "readOnly": True},
                      deployment["volumeMounts"])
        self.assertIn("kube-api-modelconfig", {v["name"] for v in deployment["volumes"]})   # ModelConfig mount kept
        import jsonschema

        schemas = json.loads((ROOT / "infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json").read_text())
        jsonschema.validate(agent["spec"], schemas["Agent"]["schema"]["properties"]["spec"])


if __name__ == "__main__":
    unittest.main()
