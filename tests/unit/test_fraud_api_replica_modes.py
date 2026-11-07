"""R1: one owner for the Fraud API's spec.replicas, and a handover that keeps the count.

The renderer tests pin the two modes. The handover tests run the real script
against a fake kubectl that models client-side apply's three-way merge: a field
in the last-applied annotation but missing from the new manifest is deleted, and
a Deployment without replicas defaults to 1. That is the failure the handover
exists to prevent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

from ops.serving.fraud_api_manifest import DEPLOYMENT_NAME, NAMESPACE, TEMPLATE, RenderError, render

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/serving/fraud_api_keda_handover.sh"
SERVING_CHART = ROOT / "infra/helm/ewallet-serving"
VALUES = {
    "FRAUD_API_IMAGE": "asia-southeast1-docker.pkg.dev/p/r/fraud-api@sha256:" + "a" * 64,
    "ONLINE_REGISTRY_URI": "gs://bucket/registry.db",
    "MODEL_VERSION": "7",
    "GIT_SHA": "0123456789abcdef0123456789abcdef01234567",
}


def documents(text: str) -> dict[str, dict]:
    """Parse a manifest into {kind: object}."""
    return {doc["kind"]: doc for doc in yaml.safe_load_all(text) if doc}


class RenderModeTests(unittest.TestCase):
    def test_fixed_mode_sets_the_requested_count(self):
        deployment = documents(render("fixed", VALUES, replicas=2))["Deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 2)

    def test_keda_mode_has_no_replica_field(self):
        text = render("keda", VALUES)
        self.assertNotIn("replicas", documents(text)["Deployment"]["spec"])
        self.assertNotRegex(text, r"(?m)^\s+replicas:")

    def test_both_modes_keep_the_same_identity_and_pod_template(self):
        fixed = documents(render("fixed", VALUES, replicas=1))
        keda = documents(render("keda", VALUES))
        for kind in ("Service", "Deployment"):
            self.assertEqual(fixed[kind]["metadata"], keda[kind]["metadata"])
        fixed["Deployment"]["spec"].pop("replicas")
        self.assertEqual(fixed["Deployment"]["spec"], keda["Deployment"]["spec"])
        self.assertEqual(keda["Deployment"]["metadata"]["name"], DEPLOYMENT_NAME)
        self.assertEqual(keda["Deployment"]["metadata"]["namespace"], NAMESPACE)

    def test_fixed_mode_matches_plain_envsubst_of_the_template(self):
        expected = re.sub(
            r"\$\{([A-Z0-9_]+)\}",
            lambda m: {**VALUES, "FRAUD_API_REPLICAS": "3"}[m.group(1)],
            TEMPLATE.read_text(encoding="utf-8"),
        )
        self.assertEqual(render("fixed", VALUES, replicas=3), expected)

    def test_unsafe_requests_are_refused(self):
        cases = {
            "fixed without count": lambda: render("fixed", VALUES),
            "fixed out of range": lambda: render("fixed", VALUES, replicas=0),
            "keda with count": lambda: render("keda", VALUES, replicas=2),
            "unknown mode": lambda: render("hpa", VALUES),
            "unpinned image": lambda: render("keda", {**VALUES, "FRAUD_API_IMAGE": "repo/fraud-api:latest"}),
            "missing value": lambda: render("keda", {k: v for k, v in VALUES.items() if k != "GIT_SHA"}),
            "quote injection": lambda: render("keda", {**VALUES, "MODEL_VERSION": '7"\n  evil: x'}),
        }
        for name, call in cases.items():
            with self.subTest(name), self.assertRaises(RenderError):
                call()

    def test_no_manifest_brings_a_competing_hpa(self):
        for text in (render("fixed", VALUES, replicas=1), render("keda", VALUES)):
            self.assertNotIn("HorizontalPodAutoscaler", text)
        for template in (SERVING_CHART / "templates").glob("*.yaml"):
            self.assertNotIn("kind: HorizontalPodAutoscaler", template.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("helm"), "helm not installed")
    def test_the_scaledobject_targets_this_deployment(self):
        rendered = subprocess.run(
            ["helm", "template", "s", str(SERVING_CHART), "--set", "autoscaling.enabled=true",
             "--set", "metricsDiscovery.enabled=true", "--set", "autoscaling.targets.feature-api.enabled=false"],
            check=True, capture_output=True, text=True,
        ).stdout
        objects = [doc for doc in yaml.safe_load_all(rendered) if doc and doc["kind"] == "ScaledObject"]
        fraud = [o for o in objects if o["spec"]["scaleTargetRef"]["name"] == DEPLOYMENT_NAME]
        self.assertEqual(len(fraud), 1)
        self.assertEqual(fraud[0]["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(fraud[0]["spec"]["scaleTargetRef"]["kind"], "Deployment")
        self.assertEqual(fraud[0]["spec"]["scaleTargetRef"]["apiVersion"], "apps/v1")


FAKE_KUBECTL = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake kubectl: one Deployment, client-side apply three-way merge on spec.replicas."""
    import json, os, sys
    state_path = os.environ["FAKE_STATE"]
    state = json.load(open(state_path))
    args = sys.argv[1:]
    state["calls"].append(" ".join(a for a in args if not a.startswith("/")))

    def save():
        json.dump(state, open(state_path, "w"))

    def manifest_replicas(path):
        text = open(path).read()
        for line in text.splitlines():
            if line.startswith("  replicas: "):
                return int(line.split(":")[1])
        return None

    def merged(path):
        """Replicas after a client-side apply of path."""
        new = manifest_replicas(path)
        if new is not None:
            return new
        if state["last_applied_replicas"] is not None:
            return 1          # field removed from config -> deleted -> defaulted
        return state["replicas"]

    if "-n" in args:
        args = args[:args.index("-n")] + args[args.index("-n") + 2:]
    if args[:3] == ["get", "deployment", "fraud-api"]:
        if "jsonpath={.spec.replicas}" in " ".join(args):
            print(state["replicas"], end="")
        else:
            annotation = {"spec": {} if state["last_applied_replicas"] is None
                          else {"replicas": state["last_applied_replicas"]}}
            print(json.dumps({"spec": {"replicas": state["replicas"]}, "status": {"readyReplicas": state["replicas"]},
                              "metadata": {"annotations": {"kubectl.kubernetes.io/last-applied-configuration":
                                                           json.dumps(annotation)}, "managedFields": []}}))
    elif args[:2] == ["get", "hpa"]:
        print(json.dumps({"items": [{"metadata": {"name": n}, "spec": {"scaleTargetRef": {"name": "fraud-api"}}}
                                    for n in state["hpas"]]}))
    elif args[:2] == ["get", "scaledobjects.keda.sh"]:
        if len(args) > 2 and args[2] == "fraud-api":
            if not state["scaledobject"]:
                sys.exit(1)
            print("True", end="")
    elif args[:2] == ["apply", "set-last-applied"]:
        if not state.get("broken_set_last_applied"):
            state["last_applied_replicas"] = manifest_replicas(args[args.index("-f") + 1])
    elif args[:2] == ["apply", "--dry-run=server"]:
        print(merged(args[args.index("-f") + 1]), end="")
    elif args[:1] == ["apply"]:
        path = args[args.index("-f") + 1]
        state["replicas"] = merged(path)
        state["last_applied_replicas"] = manifest_replicas(path)
    elif args[:1] == ["diff"]:
        pass
    else:
        sys.exit(f"fake kubectl: unsupported {args}")
    save()
''')


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r1-handover-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kubectl = self.tmp / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.kubectl.chmod(0o755)
        self.state = self.tmp / "state.json"
        self.set_state(replicas=3, last_applied_replicas=3)

    def set_state(self, **changes):
        state = {"replicas": 3, "last_applied_replicas": 3, "hpas": [], "scaledobject": False, "calls": []}
        if self.state.exists():
            state = json.loads(self.state.read_text())
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def read_state(self):
        return json.loads(self.state.read_text())

    def run_script(self, action, *extra):
        env = {**os.environ, **VALUES, "KUBECTL": str(self.kubectl), "FAKE_STATE": str(self.state),
               "PYTHON": sys.executable}
        return subprocess.run(["bash", str(SCRIPT), action, str(self.tmp / "evidence"), *extra],
                              env=env, capture_output=True, text=True, cwd=ROOT)

    def test_a_plain_keda_apply_would_drop_to_one_replica(self):
        keda = self.tmp / "keda.yaml"
        keda.write_text(render("keda", VALUES))
        env = {**os.environ, "FAKE_STATE": str(self.state)}
        subprocess.run([str(self.kubectl), "apply", "-f", str(keda)], check=True, env=env)
        self.assertEqual(self.read_state()["replicas"], 1)

    def test_mutating_actions_refuse_without_execute(self):
        for action in ("handover", "rollback"):
            with self.subTest(action):
                result = self.run_script(action)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c.startswith("apply") for c in self.read_state()["calls"]))

    def test_handover_keeps_the_count_and_clears_the_annotation(self):
        result = self.run_script("handover", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_state()
        self.assertEqual(state["replicas"], 3)
        self.assertIsNone(state["last_applied_replicas"])
        applies = [c for c in state["calls"] if c.startswith("apply")]
        self.assertTrue(applies[0].startswith("apply set-last-applied"))
        self.assertTrue(applies[1].startswith("apply --dry-run=server"))

    def test_handover_stops_and_restores_when_the_dry_run_would_change_the_count(self):
        self.set_state(broken_set_last_applied=True)
        result = self.run_script("handover", "--execute")
        self.assertNotEqual(result.returncode, 0)
        state = self.read_state()
        self.assertEqual(state["replicas"], 3)
        self.assertFalse(any(c.startswith("apply -f") for c in state["calls"]))

    def test_handover_refuses_when_an_hpa_already_owns_the_deployment(self):
        self.set_state(hpas=["fraud-api"])
        result = self.run_script("handover", "--execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c.startswith("apply") for c in self.read_state()["calls"]))

    def test_verify_requires_only_the_keda_hpa(self):
        self.set_state(last_applied_replicas=None, scaledobject=True, hpas=["keda-hpa-fraud-api"])
        self.assertEqual(self.run_script("verify").returncode, 0)
        self.set_state(hpas=["keda-hpa-fraud-api", "fraud-api"])
        self.assertNotEqual(self.run_script("verify").returncode, 0)

    def test_rollback_needs_the_scaledobject_gone_then_restores_kubectl_ownership(self):
        self.set_state(replicas=2, last_applied_replicas=None, scaledobject=True)
        self.assertNotEqual(self.run_script("rollback", "--execute").returncode, 0)
        self.set_state(scaledobject=False)
        result = self.run_script("rollback", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_state()["replicas"], 2)
        self.assertEqual(self.read_state()["last_applied_replicas"], 2)


if __name__ == "__main__":
    unittest.main()
