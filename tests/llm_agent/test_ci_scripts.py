"""CI/CD scripts called by Jenkinsfile.llm-agent, run against fake docker/helm/kubectl.

The fakes record their arguments and return canned output, so the real script
logic (input validation, image selection, digest-only deploys, rollback target)
runs without touching Docker, a registry or a cluster.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts/llm_agent"
SHA = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "asia-southeast1-docker.pkg.dev/ewallet-proj/llm-agent"
DIGEST = "sha256:" + "e" * 64

FAKE = """#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $*" >> "$FAKE_LOG"
case "$(basename "$0") $1" in
  "docker image") echo "{ref_base}@{digest}";;
  "helm history") cat "$FAKE_HISTORY" 2>/dev/null || echo '[]';;
  "kubectl --context") echo "deployment.apps/agentgateway";;
esac
exit "${{FAKE_EXIT:-0}}"
"""


class ScriptHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name in ("docker", "helm", "kubectl"):
            path = self.bin / name
            path.write_text(FAKE.format(ref_base=f"{REGISTRY}/mcp-tools", digest=DIGEST))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "calls.log"
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_LOG": str(self.log), "HOME": str(self.dir)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, name, *args, **env):
        return subprocess.run([str(SCRIPTS / name), *map(str, args)], capture_output=True, text=True,
                              env={**self.env, **env}, check=False)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def components(self, *names):
        path = self.dir / "components.json"
        path.write_text(json.dumps({"components": list(names)}))
        return path


class BuildTests(ScriptHarness):
    def test_builds_only_changed_images_tagged_by_sha(self):
        out = self.dir / "digests.json"
        proc = self.run_script("build_changed_images.sh", self.components("drift-mcp", "fraud-coordinator"), SHA, out,
                               ARTIFACT_REGISTRY=REGISTRY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(out.read_text())
        self.assertEqual(list(data["images"]), ["mcp-tools"])
        self.assertEqual(data["images"]["mcp-tools"]["tag_ref"], f"{REGISTRY}/mcp-tools:{SHA}")
        self.assertIsNone(data["images"]["mcp-tools"]["digest_ref"])
        builds = [c for c in self.calls() if c.startswith("docker build")]
        self.assertEqual(len(builds), 1)
        self.assertIn("--target mcp-server", builds[0])
        self.assertIn(f"org.opencontainers.image.revision={SHA}", builds[0])

    def test_config_only_changes_build_nothing(self):
        out = self.dir / "digests.json"
        proc = self.run_script("build_changed_images.sh", self.components("llm-d-router", "agent-registry"), SHA, out,
                               ARTIFACT_REGISTRY=REGISTRY)
        self.assertEqual((proc.returncode, json.loads(out.read_text())["images"], self.calls()), (0, {}, []))

    def test_rejects_bad_inputs(self):
        out = self.dir / "digests.json"
        for args, env in (((SHA,), {}), (("abc123",), {"ARTIFACT_REGISTRY": REGISTRY}),
                          ((SHA,), {"ARTIFACT_REGISTRY": "docker.io/library"})):
            with self.subTest(args=args, env=env):
                proc = self.run_script("build_changed_images.sh", self.components("drift-mcp"), *args, out, **env)
                self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.calls(), [])


class ScanPushTests(ScriptHarness):
    def digests(self):
        path = self.dir / "digests.json"
        path.write_text(json.dumps({"git_sha": SHA, "images": {"mcp-tools": {
            "tag_ref": f"{REGISTRY}/mcp-tools:{SHA}", "target": "mcp-server",
            "components": ["drift-mcp"], "digest_ref": None}}}))
        return path

    def test_scans_with_pinned_trivy_then_records_the_pushed_digest(self):
        path = self.digests()
        proc = self.run_script("scan_and_push.sh", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        scan = next(i for i, c in enumerate(calls) if "aquasec/trivy" in c)
        push = next(i for i, c in enumerate(calls) if c.startswith("docker push"))
        self.assertLess(scan, push)
        self.assertRegex(calls[scan], r"trivy:0\.75\.0@sha256:[0-9a-f]{64}")
        self.assertIn("--exit-code 1 --severity CRITICAL,HIGH", calls[scan])
        self.assertEqual(json.loads(path.read_text())["images"]["mcp-tools"]["digest_ref"], f"{REGISTRY}/mcp-tools@{DIGEST}")

    def test_vulnerable_image_is_never_pushed(self):
        proc = self.run_script("scan_and_push.sh", self.digests(), FAKE_EXIT="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("docker push")])


class DeployRollbackTests(ScriptHarness):
    def digests(self, digest_ref, name="digests.json"):
        path = self.dir / name
        images = {} if digest_ref is None else {"mcp-tools": {"tag_ref": "x", "digest_ref": digest_ref}}
        path.write_text(json.dumps({"git_sha": SHA, "images": images}))
        return path

    def test_atomic_deploy_pins_the_digest_and_explicit_context(self):
        ref = f"{REGISTRY}/mcp-tools@{DIGEST}"
        proc = self.run_script("deploy_atomic.sh", "gcp", self.digests(ref), self.dir / "ev", KUBE_CONTEXT="ctx-test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        upgrade = next(c for c in self.calls() if c.startswith("helm upgrade"))
        for fragment in ("--atomic", "--wait", "--kube-context ctx-test", "values-gcp.yaml",
                         f"mcpServices.image={ref}", f"mcpServices.identity.imageDigest={DIGEST}",
                         f"mcpServices.identity.gitSha={SHA}"):
            self.assertIn(fragment, upgrade)
        self.assertTrue(any(c.startswith("kubectl --context ctx-test") and "rollout status" in c for c in self.calls()))

    def test_deploy_refuses_tags_missing_context_and_unknown_environment(self):
        cases = (("gcp", self.digests(f"{REGISTRY}/mcp-tools:{SHA}", "tag.json"), {"KUBE_CONTEXT": "ctx"}),
                 ("gcp", self.digests(None, "none.json"), {}),
                 ("prod", self.digests(None, "none.json"), {"KUBE_CONTEXT": "ctx"}))
        for environment, digests, env in cases:
            with self.subTest(environment=environment, env=env):
                proc = self.run_script("deploy_atomic.sh", environment, digests, self.dir / "ev", **env)
                self.assertNotEqual(proc.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("helm upgrade")])

    def test_config_only_deploy_sets_no_image(self):
        proc = self.run_script("deploy_atomic.sh", "dev", self.digests(None), self.dir / "ev", KUBE_CONTEXT="ctx")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("mcpServices.image", next(c for c in self.calls() if c.startswith("helm upgrade")))

    def test_rollback_targets_the_last_good_revision(self):
        history = self.dir / "history.json"
        history.write_text(json.dumps([
            {"revision": 3, "status": "superseded"}, {"revision": 4, "status": "superseded"},
            {"revision": 5, "status": "failed"}, {"revision": 6, "status": "deployed"}]))
        proc = self.run_script("rollback.sh", "gcp", self.dir / "ev", KUBE_CONTEXT="ctx", FAKE_HISTORY=str(history))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rollback = next(c for c in self.calls() if c.startswith("helm rollback"))
        self.assertRegex(rollback, r"^helm rollback llm-agent 4 ")

    def test_rollback_without_history_fails_loudly(self):
        history = self.dir / "history.json"
        history.write_text(json.dumps([{"revision": 1, "status": "deployed"}]))
        proc = self.run_script("rollback.sh", "gcp", self.dir / "ev", KUBE_CONTEXT="ctx", FAKE_HISTORY=str(history))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("helm rollback")])


class JenkinsfileTests(unittest.TestCase):
    def test_every_script_the_pipeline_calls_exists_and_is_executable(self):
        text = (ROOT / "Jenkinsfile.llm-agent").read_text()
        scripts = set(re.findall(r"(scripts/[\w/.-]+\.sh)", text))
        self.assertTrue({"scripts/llm_agent/build_changed_images.sh", "scripts/llm_agent/scan_and_push.sh",
                         "scripts/llm_agent/deploy_atomic.sh", "scripts/llm_agent/rollback.sh"} <= scripts)
        for script in scripts:
            path = ROOT / script
            self.assertTrue(path.exists(), script)
            self.assertTrue(os.access(path, os.X_OK) or script.startswith("scripts/validation/"), script)


if __name__ == "__main__":
    unittest.main()
