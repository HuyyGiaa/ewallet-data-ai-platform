"""Helm source contract for the CPU vLLM runtime fixes (render only, no deploy)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ops.llm_agent.policy_lint import baseline_violations

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
HELM = shutil.which("helm")


REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"


@unittest.skipUnless(HELM, "helm binary unavailable")
class VllmRuntimeFixTests(unittest.TestCase):
    """Defects found by running the chart on kind (local, 2026-10-31)."""

    def render(self, override=None, check=True):
        with tempfile.TemporaryDirectory() as folder:
            args = [HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml")]
            if override is not None:
                path = Path(folder) / "override.yaml"
                path.write_text(yaml.safe_dump(override))
                args += ["-f", str(path)]
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if not check:
            return proc
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item for item in yaml.safe_load_all(proc.stdout) if item]

    def vllm(self, items, name="vllm-primary"):
        return next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == name)["spec"]["template"]["spec"]

    def test_runtime_fixes_in_every_overlay(self):
        for overlay in ("values-gcp.yaml", "values-local.yaml"):
            with self.subTest(overlay):
                proc = subprocess.run([HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / overlay)],
                                      capture_output=True, text=True, check=True)
                items = [i for i in yaml.safe_load_all(proc.stdout) if i]
                pod = self.vllm(items)
                container = pod["containers"][0]
                args, env = container["args"], {e["name"]: e.get("value") for e in container["env"]}
                # 64 MiB default /dev/shm made the engine fail; VLLM_* service links polluted its config.
                shm = next(v for v in pod["volumes"] if v["name"] == "dshm")
                self.assertEqual(shm["emptyDir"]["medium"], "Memory")
                self.assertIn({"name": "dshm", "mountPath": "/dev/shm"}, container["volumeMounts"])
                self.assertIs(pod["enableServiceLinks"], False)
                self.assertTrue(env["OMP_NUM_THREADS"].isdigit())
                self.assertEqual(args[args.index("--reasoning-parser") + 1], "qwen3")
                self.assertEqual(json.loads(args[args.index("--default-chat-template-kwargs") + 1]),
                                 {"enable_thinking": False})
                probe = container["startupProbe"]["exec"]["command"]
                compile(probe[2], "warmup-probe", "exec")
                self.assertIn("/v1/chat/completions", probe[2])
                self.assertGreater(container["startupProbe"]["timeoutSeconds"], 60)
                self.assertEqual(baseline_violations(items), [])

    def test_staged_model_is_offline_and_checksum_gated(self):
        proc = subprocess.run([HELM, "template", "llm-agent", str(CHART), "-f", str(CHART / "values-local.yaml")],
                              capture_output=True, text=True, check=True)
        items = [i for i in yaml.safe_load_all(proc.stdout) if i]
        pod = self.vllm(items)
        container = pod["containers"][0]
        args, env = container["args"], {e["name"]: e.get("value") for e in container["env"]}
        self.assertNotIn("--revision", args)
        self.assertEqual(args[args.index("--model") + 1], f"/models/Qwen3-0.6B/{REVISION}")
        self.assertEqual((env["HF_HUB_OFFLINE"], env["TRANSFORMERS_OFFLINE"]), ("1", "1"))
        init = pod["initContainers"][0]
        self.assertIn("sha256sum --check --strict", init["command"][2])
        self.assertEqual(init["securityContext"]["capabilities"], {"drop": ["ALL"]})
        model = next(v for v in pod["volumes"] if v["name"] == "model")
        self.assertTrue(model["persistentVolumeClaim"]["readOnly"])
        sums = next(i for i in items if i["kind"] == "ConfigMap" and i["metadata"]["name"] == "vllm-model-checksums")
        self.assertIn("./model.safetensors", sums["data"]["SHA256SUMS"])

    def test_staged_mode_refuses_unpinned_inputs(self):
        local = yaml.safe_load((CHART / "values-local.yaml").read_text())["vllm"]["modelSource"]["staged"]
        cases = {
            "path without the pinned revision": {"subPath": "Qwen3-0.6B/main"},
            "missing claim": {"claimName": ""},
            "malformed checksums": {"sha256sums": "abc  ./model.safetensors\n"},
        }
        for label, change in cases.items():
            with self.subTest(label):
                override = {"vllm": {"modelSource": {"mode": "staged", "staged": {**local, **change}}}}
                self.assertNotEqual(self.render(override, check=False).returncode, 0)
        bad_mode = {"vllm": {"modelSource": {"mode": "s3"}}}
        self.assertNotEqual(self.render(bad_mode, check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
