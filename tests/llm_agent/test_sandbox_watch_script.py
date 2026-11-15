"""sandbox_watch.sh gates: the real upgrade runs only after identity, exact diff and a
server-side dry-run in the same execution. Fake helm and kubectl record every call."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/llm_agent/sandbox_watch.sh"
CTX = "gke_test_ctx"
DIGEST = next(line.split("=", 1)[1] for line in (ROOT / "infra/kubernetes/kagent-pilot/versions.env").read_text().splitlines()
              if line.startswith("KAGENT_CHART_DIGEST="))

FAKE_HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$FAKE_LOG"
args=(); while (($#)); do case "$1" in --kube-context|-n|--destination|--version) [[ "$1" == --destination ]] && dest=$2; shift 2 ;; *) args+=("$1"); shift ;; esac; done
set -- "${args[@]}"
case "$1" in
  pull) touch "$dest/kagent-0.10.3.tgz"; echo "Digest: $FAKE_DIGEST" ;;
  status) echo '{"info": {"status": "deployed"}, "version": 5}' ;;
  get) [[ "$2" == values ]] && echo "a: 1" || cat "$FAKE_DIR/live.yaml" ;;
  template) if [[ "$*" == *sandbox-watch-overlay* ]]; then cat "$FAKE_DIR/candidate.yaml"; else cat "$FAKE_DIR/live.yaml"; fi ;;
  upgrade)
    if [[ "$*" == *--dry-run=server* ]]; then echo "DRY-RUN" >> "$FAKE_LOG"; exit "${FAKE_DRYRUN_RC:-0}"; fi
    echo "REAL-UPGRADE" >> "$FAKE_LOG" ;;
  *) exit 3 ;;
esac
'''
FAKE_KUBECTL = r'''#!/usr/bin/env bash
echo "kubectl $*" >> "$FAKE_LOG"
case "$*" in
  *"get namespace ewallet-sandbox"*) echo namespace/ewallet-sandbox ;;
  *"get configmap kagent-controller"*) printf ewallet-agents,kagent-pilot,ewallet-sandbox ;;
  *"rollout status"*) echo rolled out ;;
  *"get role,rolebinding"*) echo role.rbac.authorization.k8s.io/kagent-writer-role ;;
  *"get deployment kagent-controller -o json"*) echo '{"spec": {"selector": {"matchLabels": {"app": "kagent"}}}}' ;;
  *"get pods"*) printf kagent-controller-new ;;
  *" logs "*) echo '{"msg":"Watching specific namespaces at cache level","namespaces":["ewallet-agents","kagent-pilot","ewallet-sandbox"]}' ;;
  *"get sandboxagents"*) printf '' ;;
  *) echo "unexpected kubectl $*" >&2; exit 3 ;;
esac
'''


def obj(kind, namespace, name, **body):
    return {"apiVersion": "v1", "kind": kind, "metadata": {"namespace": namespace, "name": name}, **body}


LIVE = [obj("ConfigMap", "kagent", "kagent-controller", data={"WATCH_NAMESPACES": "ewallet-agents,kagent-pilot"}),
        obj("Deployment", "kagent", "kagent-controller",
            spec={"template": {"metadata": {"annotations": {"checksum/configmap": "a"}}}})]
CANDIDATE = [obj("ConfigMap", "kagent", "kagent-controller", data={"WATCH_NAMESPACES": "ewallet-agents,kagent-pilot,ewallet-sandbox"}),
             obj("Deployment", "kagent", "kagent-controller",
                 spec={"template": {"metadata": {"annotations": {"checksum/configmap": "b"}}}})] + [
    obj(kind, "ewallet-sandbox", name) for kind, name in (
        ("Role", "kagent-ate-api-env-sources"), ("RoleBinding", "kagent-ate-api-env-sources"),
        ("Role", "kagent-getter-role"), ("RoleBinding", "kagent-getter-rolebinding"),
        ("Role", "kagent-writer-role"), ("RoleBinding", "kagent-writer-rolebinding"))]


@unittest.skipUnless(shutil.which("bash"), "bash required")
class SandboxWatchScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-watch-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.write(CANDIDATE)
        self.log = self.tmp / "calls.log"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, candidate, live=LIVE):
        (self.tmp / "live.yaml").write_text(yaml.safe_dump_all(live))
        (self.tmp / "candidate.yaml").write_text(yaml.safe_dump_all(candidate))

    def run_script(self, action, **env):
        variables = {**os.environ, "KUBE_CONTEXT": CTX, "HELM": str(self.bin / "helm"), "KUBECTL": str(self.bin / "kubectl"),
                     "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.tmp), "FAKE_DIGEST": DIGEST, **env}
        return subprocess.run(["bash", str(SCRIPT), "kagent", action, str(self.tmp / "work")], capture_output=True,
                              text=True, env=variables, timeout=120)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_upgrade_runs_the_server_dry_run_before_the_real_upgrade(self):
        result = self.run_script("upgrade", WATCH_APPROVAL=f"PKG-K:{CTX}")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("DRY-RUN"), calls.index("REAL-UPGRADE"))
        self.assertEqual((self.tmp / "work/kagent/revision-before-upgrade").read_text().strip(), "5")

    def test_a_failed_server_dry_run_stops_the_upgrade(self):
        result = self.run_script("upgrade", WATCH_APPROVAL=f"PKG-K:{CTX}", FAKE_DRYRUN_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server-side dry-run failed; nothing was upgraded", result.stderr)
        self.assertIn("DRY-RUN", self.calls())
        self.assertNotIn("REAL-UPGRADE", self.calls())
        self.assertFalse((self.tmp / "work/kagent/revision-before-upgrade").exists())

    def test_an_unexpected_object_stops_before_the_dry_run(self):
        self.write(CANDIDATE + [obj("ClusterRole", "", "kagent-extra")])
        result = self.run_script("upgrade", WATCH_APPROVAL=f"PKG-K:{CTX}")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("DRY-RUN", self.calls())
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_identity_mismatch_stops_everything(self):
        # The baseline render (chart + live values) no longer reproduces the live manifest.
        (self.bin / "helm").write_text(FAKE_HELM.replace('else cat "$FAKE_DIR/live.yaml"; fi',
                                                         'else cat "$FAKE_DIR/candidate.yaml"; fi'))
        result = self.run_script("upgrade", WATCH_APPROVAL=f"PKG-K:{CTX}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not reproduce the live release", result.stderr)
        self.assertNotIn("DRY-RUN", self.calls())
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_wrong_or_missing_approval_mutates_nothing(self):
        for token in (None, f"PKG-W:{CTX}", "PKG-K:other-context"):
            with self.subTest(token=token):
                env = {} if token is None else {"WATCH_APPROVAL": token}
                result = self.run_script("upgrade", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"WATCH_APPROVAL=PKG-K:{CTX}", result.stderr)
        self.assertEqual(self.calls(), "")


if __name__ == "__main__":
    unittest.main()
