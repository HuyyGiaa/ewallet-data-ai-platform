"""PKG-T runtime-fix package: exact diffs, the ate-api post-renderer override, and the pinned build spec."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/llm_agent"))
import runtime_fix_diff as rfd  # noqa: E402

RENDERER = ROOT / "infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py"
PATCHES = ROOT / "infra/kubernetes/kagent-pilot/patches"
AR = "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform"
DIGEST = "sha256:" + "a" * 64


def deployment(ns, name, container, image, env=None):
    c = {"name": container, "image": image}
    if env is not None:
        c["env"] = env
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"template": {"metadata": {}, "spec": {"containers": [c]}}}}


def kagent_base():
    return {
        ("Deployment", "kagent", "kagent-controller"): deployment(
            "kagent", "kagent-controller", "controller", "ghcr.io/kagent-dev/kagent/controller:0.10.3",
            [{"name": "SSL_CERT_FILE", "value": "/etc/substrate-ca/ca.crt"}]),
        ("ConfigMap", "kagent", "kagent-controller"): {"kind": "ConfigMap", "data": {"WATCH_NAMESPACES": "a,b"}},
    }


class KagentDiffTests(unittest.TestCase):
    image = f"{AR}/kagent-controller:0.10.3-ewallet.177af5ca@{DIGEST}"

    def cand(self):
        c = copy.deepcopy(kagent_base())
        ctr = c[("Deployment", "kagent", "kagent-controller")]["spec"]["template"]["spec"]["containers"][0]
        ctr["image"] = self.image
        ctr["env"].append({"name": "SUBSTRATE_CALL_TIMEOUT", "value": "90s"})
        return c

    def test_reviewed_change_passes(self):
        self.assertEqual(rfd.candidate("kagent", kagent_base(), self.cand(), self.image, "90s"), [])

    def test_image_only_step_after_the_env_is_live_passes(self):
        live = self.cand()
        ctr = live[("Deployment", "kagent", "kagent-controller")]["spec"]["template"]["spec"]["containers"][0]
        ctr["image"] = "x/kagent-controller:old@sha256:" + "d" * 64
        self.assertEqual(rfd.candidate("kagent", live, self.cand(), self.image, "90s"), [])

    def test_other_object_changes_fail(self):
        c = self.cand()
        c[("ConfigMap", "kagent", "kagent-controller")]["data"]["WATCH_NAMESPACES"] = "a"
        self.assertTrue(rfd.candidate("kagent", kagent_base(), c, self.image, "90s"))

    def test_dropped_env_entry_fails(self):
        c = self.cand()
        ctr = c[("Deployment", "kagent", "kagent-controller")]["spec"]["template"]["spec"]["containers"][0]
        ctr["env"] = [e for e in ctr["env"] if e["name"] != "SSL_CERT_FILE"]
        self.assertTrue(rfd.candidate("kagent", kagent_base(), c, self.image, "90s"))

    def test_wrong_image_or_timeout_fails(self):
        self.assertTrue(rfd.candidate("kagent", kagent_base(), self.cand(), self.image.replace("a" * 64, "b" * 64), "90s"))
        self.assertTrue(rfd.candidate("kagent", kagent_base(), self.cand(), self.image, "120s"))


class SubstrateOverrideTests(unittest.TestCase):
    image = f"{AR}/substrate-ateapi:0.0.9-ewallet.c1bcc768@{DIGEST}"

    def render(self, docs, *args):
        out = subprocess.run([sys.executable, str(RENDERER), *args], input=yaml.safe_dump_all(docs),
                             capture_output=True, text=True)
        return out.returncode, [d for d in yaml.safe_load_all(out.stdout) if d] if out.returncode == 0 else out.stderr

    def docs(self):
        ateapi = deployment("ate-system", "ate-api-server-deployment", "ate-api-server", "ghcr.io/kagent-dev/substrate/ateapi:v0.0.9")
        atelet = {"kind": "DaemonSet", "metadata": {"name": "atelet", "namespace": "ate-system"},
                  "spec": {"template": {"spec": {"containers": [{"name": "atelet", "image": "ghcr.io/kagent-dev/substrate/atelet:v0.0.9"}]}}}}
        return [ateapi, atelet]

    def test_without_argument_the_output_is_unchanged(self):
        code, plain = self.render(self.docs())
        self.assertEqual(code, 0)
        self.assertTrue(plain[0]["spec"]["template"]["spec"]["containers"][0]["image"].startswith("ghcr.io/kagent-dev/substrate/ateapi:v0.0.9@sha256:"))

    def test_override_changes_only_the_ateapi_container(self):
        _, plain = self.render(self.docs())
        code, patched = self.render(self.docs(), f"--ateapi-image={self.image}")
        self.assertEqual(code, 0)
        self.assertEqual(patched[0]["spec"]["template"]["spec"]["containers"][0]["image"], self.image)
        self.assertEqual(patched[1], plain[1])

    def test_override_rejects_other_images(self):
        for bad in ("ghcr.io/x/ateapi:v1", f"{AR}/kagent-controller@{DIGEST}", f"{AR}/substrate-ateapi:tag"):
            code, err = self.render(self.docs(), f"--ateapi-image={bad}")
            self.assertNotEqual(code, 0, bad)

    def test_atelet_memory_is_set_only_on_atelet(self):
        docs = self.docs()
        code, out = self.render(docs, "--atelet-memory=512Mi:1536Mi")
        self.assertEqual(code, 0)
        res = out[1]["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual((res["requests"]["memory"], res["limits"]["memory"]), ("512Mi", "1536Mi"))
        for bad in ("--atelet-memory=2Gi:3Gi", "--atelet-memory=2048Mi:1024Mi", "--atelet-memory=512Mi"):
            self.assertNotEqual(self.render(self.docs(), bad)[0], 0, bad)

    def test_override_fails_closed_without_the_deployment(self):
        code, _ = self.render(self.docs()[1:], f"--ateapi-image={self.image}")
        self.assertNotEqual(code, 0)


class SubstrateDiffTests(unittest.TestCase):
    image = f"{AR}/substrate-ateapi:0.0.9-ewallet.0291af52@{DIGEST}"
    api = ("Deployment", "ate-system", "ate-api-server-deployment")
    atelet = ("DaemonSet", "ate-system", "atelet")

    def objs(self, image, memory):
        api = deployment("ate-system", "ate-api-server-deployment", "ate-api-server", image)
        ds = {"kind": "DaemonSet", "metadata": {"name": "atelet", "namespace": "ate-system"}, "spec": {"template": {"spec": {
            "containers": [{"name": "atelet", "image": "x", "resources": {"requests": {"memory": memory[0], "cpu": "100m"},
                                                                          "limits": {"memory": memory[1], "cpu": "500m"}}}]}}}}
        return {self.api: api, self.atelet: ds}

    def test_full_step_and_incremental_step_pass(self):
        release = self.objs("ghcr.io/kagent-dev/substrate/ateapi:v0.0.9@sha256:" + "c" * 64, ("128Mi", "512Mi"))
        half = self.objs(self.image, ("128Mi", "512Mi"))
        done = self.objs(self.image, ("512Mi", "1536Mi"))
        self.assertEqual(rfd.candidate("substrate", release, done, self.image, None, "512Mi:1536Mi"), [])
        self.assertEqual(rfd.candidate("substrate", half, done, self.image, None, "512Mi:1536Mi"), [])

    def test_wrong_memory_or_extra_field_fails(self):
        half = self.objs(self.image, ("128Mi", "512Mi"))
        self.assertTrue(rfd.candidate("substrate", half, self.objs(self.image, ("512Mi", "1024Mi")), self.image, None, "512Mi:1536Mi"))
        cand = self.objs(self.image, ("512Mi", "1536Mi"))
        cand[self.atelet]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "2"
        self.assertTrue(rfd.candidate("substrate", half, cand, self.image, None, "512Mi:1536Mi"))

    def test_no_change_fails(self):
        done = self.objs(self.image, ("512Mi", "1536Mi"))
        self.assertTrue(rfd.candidate("substrate", done, copy.deepcopy(done), self.image, None, "512Mi:1536Mi"))


class BuildSpecTests(unittest.TestCase):
    def test_patch_hashes_match_the_spec(self):
        spec = json.loads((PATCHES / "runtime-fix.json").read_text())
        for component in ("kagent", "substrate"):
            patch = PATCHES / spec[component]["patch"]
            self.assertEqual(hashlib.sha256(patch.read_bytes()).hexdigest(), spec[component]["patch_sha256"])
            for image in spec[component]["images"].values():
                self.assertRegex(image["base"], r"@sha256:[0-9a-f]{64}$")
                self.assertRegex(image["go_image"], r"@sha256:[0-9a-f]{64}$")

    def test_patches_touch_only_the_reviewed_files(self):
        allowed = {
            "kagent-0.10.3-session-turns.patch": {
                "go/core/internal/a2a/substrate_sandbox_transport.go", "go/core/internal/a2a/substrate_sandbox_transport_test.go",
                "go/core/internal/a2a/substrate_session_turns_test.go", "go/core/pkg/sandboxbackend/substrate/agent_actor.go",
                "go/core/pkg/sandboxbackend/substrate/session_turns.go", "go/core/pkg/sandboxbackend/substrate/session_turns_test.go"},
            "substrate-0.0.9-resume-recovery.patch": {
                "cmd/ateapi/internal/controlapi/workflow.go", "cmd/ateapi/internal/controlapi/workflow_resume.go",
                "cmd/ateapi/internal/controlapi/functional_test.go", "cmd/ateapi/internal/controlapi/functional_recovery_test.go",
                "cmd/ateom-gvisor/main.go", "cmd/ateom-gvisor/sandbox_teardown.go", "cmd/ateom-gvisor/sandbox_teardown_test.go"},
        }
        for name, files in allowed.items():
            touched = {line.split(" b/", 1)[1] for line in (PATCHES / name).read_text().splitlines() if line.startswith("diff --git ")}
            self.assertEqual(touched, files, name)

    def test_package_gates_exist(self):
        script = (ROOT / "scripts/llm_agent/runtime_fix.sh").read_text()
        for gate in ('approval "$package:$KUBE_CONTEXT"', 'approval "$package-ROLLBACK:$KUBE_CONTEXT"', "--dry-run=server",
                     "--atomic", "diff_tool identity", "runtime_fix_diff.py"):
            self.assertIn(gate, script)
        self.assertNotIn("kubectl set image", script)


if __name__ == "__main__":
    unittest.main()
