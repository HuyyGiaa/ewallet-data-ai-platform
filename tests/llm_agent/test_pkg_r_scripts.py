"""PKG-R gates: substrate_pull_auth.sh (Helm), substrate_pull_iam.sh (IAM) and
substrate_golden_residue.sh (residual golden actors). Fakes record every call; no cloud."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

import importlib.util

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "scripts/llm_agent/substrate_pull_auth.sh"
IAM = ROOT / "scripts/llm_agent/substrate_pull_iam.sh"
RESIDUE = ROOT / "scripts/llm_agent/substrate_golden_residue.sh"
CTX = "gke_test_ctx"
PROJECT = "ewallet-data-ai-platform-hg26"
PRINCIPAL = ("principal://iam.googleapis.com/projects/198818809719/locations/global/workloadIdentityPools/"
             f"{PROJECT}.svc.id.goog/subject/ns/ate-system/sa/atelet")
DIGEST = next(line.split("=", 1)[1] for line in (ROOT / "infra/kubernetes/kagent-pilot/versions.env").read_text().splitlines()
              if line.startswith("SUBSTRATE_CHART_DIGEST="))

FAKE_HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$FAKE_LOG"
args=(); while (($#)); do case "$1" in --kube-context|-n|--version) shift 2 ;; --destination) dest=$2; shift 2 ;; *) args+=("$1"); shift ;; esac; done
set -- "${args[@]}"
case "$1" in
  pull) touch "$dest/substrate-0.0.9.tgz"; echo "Digest: $FAKE_DIGEST" ;;
  status) echo '{"info": {"status": "deployed"}, "version": 2}' ;;
  get) [[ "$2" == values ]] && echo "a: 1" || cat "$FAKE_DIR/live.yaml" ;;
  template) if [[ "$*" == *substrate-ar-pull-overlay* ]]; then cat "$FAKE_DIR/candidate.yaml"; else cat "$FAKE_DIR/${FAKE_BASELINE:-live}.yaml"; fi ;;
  upgrade)
    if [[ "$*" == *--dry-run=server* ]]; then echo "DRY-RUN" >> "$FAKE_LOG"; exit "${FAKE_DRYRUN_RC:-0}"; fi
    echo "REAL-UPGRADE" >> "$FAKE_LOG"; echo "--gcp-auth-for-image-pulls=true" > "$FAKE_DIR/flag" ;;
  rollback) echo "REAL-ROLLBACK $2" >> "$FAKE_LOG"; echo "--gcp-auth-for-image-pulls=false" > "$FAKE_DIR/flag" ;;
  *) exit 3 ;;
esac
'''
FAKE_KUBECTL = r'''#!/usr/bin/env bash
echo "kubectl $*" >> "$FAKE_LOG"
case "$*" in
  *"get daemonset atelet"*"args[0]"*) cat "$FAKE_DIR/flag" | tr -d '\n' ;;
  *"get daemonset atelet"*serviceAccountName*) printf %s "${FAKE_SA:-atelet}" ;;
  *"get daemonset atelet"*hostNetwork*) printf %s "${FAKE_HOSTNETWORK:-}" ;;
  *"get serviceaccount atelet"*) printf %s "${FAKE_GSA:-}" ;;
  *"get pods -l app=atelet"*nodeName*) printf 'node-a node-b' ;;
  *"get pods -l app=atelet"*restartCount*) printf 'atelet-a=%s atelet-b=0 ' "${FAKE_RESTARTS:-0}" ;;
  *"get pods -l app=atelet"*metadata.name*) printf 'atelet-a atelet-b' ;;
  *"get node "*) printf %s "${FAKE_MDS_LABEL:-true}" ;;
  *"get pods -l k8s-app=gke-metadata-server"*) echo pod/gke-metadata-server-x ;;
  *"rollout status"*) echo rolled out ;;
  *" logs "*) [[ -n "${FAKE_AUTH_FAIL:-}" ]] && echo "Failed to create GCP registry authenticator"; echo ok ;;
  *"get actortemplate"*) [[ -n "${FAKE_TEMPLATE_EXISTS:-}" ]] && echo actortemplate/x || exit 1 ;;
  *) echo "unexpected kubectl $*" >&2; exit 3 ;;
esac
'''
FAKE_GCLOUD = r'''#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$*" in
  "projects describe"*) echo 198818809719 ;;
  "container clusters describe"*) echo "${FAKE_POOL:-ewallet-data-ai-platform-hg26.svc.id.goog}" ;;
  "container node-pools describe"*) echo "${FAKE_MODE:-GKE_METADATA}" ;;
  "artifacts repositories get-iam-policy"*) cat "$FAKE_DIR/policy.json" ;;
  "artifacts repositories add-iam-policy-binding"*|"artifacts repositories remove-iam-policy-binding"*)
    python3 - "$FAKE_DIR/policy.json" "$*" <<'PY'
import json, re, sys, os
path, text = sys.argv[1], sys.argv[2]
member = re.search(r"--member (\S+)", text).group(1); role = re.search(r"--role (\S+)", text).group(1)
policy = json.load(open(path)); bindings = policy.setdefault("bindings", [])
binding = next((b for b in bindings if b["role"] == role), None)
if "add-iam" in text:
    if binding is None: bindings.append({"role": role, "members": [member]})
    elif member not in binding["members"]: binding["members"].append(member)
else:
    binding["members"].remove(member)
    if not binding["members"]: bindings.remove(binding)
if os.environ.get("FAKE_IAM_CLOBBER"): policy["bindings"] = [b for b in bindings if b["role"] == role]
json.dump(policy, open(path, "w"))
print("{}")
PY
    ;;
  *) echo "unexpected gcloud $*" >&2; exit 3 ;;
esac
'''
FAKE_ATE = r'''#!/usr/bin/env bash
echo "ate $*" >> "$FAKE_LOG"
case "$*" in
  *"get actors"*) cat "$FAKE_DIR/actors.json" ;;
  *"delete actor"*) id=$(echo "$*" | sed -E 's/.*delete actor ([^ ]+).*/\1/')
    python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["actors"]=[a for a in d["actors"] if a["metadata"]["name"]!=sys.argv[2]]; json.dump(d,open(p,"w"))' "$FAKE_DIR/actors.json" "$id"
    echo "actor \"$id\" deleted" ;;
  *) exit 3 ;;
esac
'''


def obj(kind, namespace, name, **body):
    return {"apiVersion": "v1", "kind": kind, "metadata": {"namespace": namespace, "name": name}, **body}


def atelet(flag):
    return obj("DaemonSet", "ate-system", "atelet", spec={"template": {"spec": {"containers": [
        {"name": "atelet", "args": [f"--gcp-auth-for-image-pulls={flag}", "--other=1"]}]}}})


SECRET = obj("Secret", "ate-system", "ateapi-tls", data={"tls.key": "c2VjcmV0"})
LIVE = [atelet("false"), SECRET, obj("ConfigMap", "ate-system", "ateapi-ca", data={"ca.crt": "\nCERT"})]
CANDIDATE = [atelet("true"), SECRET, obj("ConfigMap", "ate-system", "ateapi-ca", data={"ca.crt": "\nCERT"})]


class Fakes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pkg-r-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD), ("kubectl-ate", FAKE_ATE)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.tmp / "calls.log"
        (self.tmp / "flag").write_text("--gcp-auth-for-image-pulls=false\n")
        self.write(CANDIDATE)
        self.policy([("roles/artifactregistry.writer", "serviceAccount:ewallet-cloudbuild@x.iam.gserviceaccount.com")])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, candidate, live=LIVE, baseline=None):
        (self.tmp / "live.yaml").write_text(yaml.safe_dump_all(live))
        (self.tmp / "candidate.yaml").write_text(yaml.safe_dump_all(candidate))
        if baseline is not None:
            (self.tmp / "other.yaml").write_text(yaml.safe_dump_all(baseline))

    def policy(self, pairs):
        bindings = {}
        for role, member in pairs:
            bindings.setdefault(role, []).append(member)
        (self.tmp / "policy.json").write_text(json.dumps({"bindings": [{"role": r, "members": m} for r, m in bindings.items()]}))

    def env(self, **extra):
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "KUBE_CONTEXT": CTX, "HELM": str(self.bin / "helm"),
                "KUBECTL": str(self.bin / "kubectl"), "GCLOUD": str(self.bin / "gcloud"), "KUBECTL_ATE": str(self.bin / "kubectl-ate"),
                "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.tmp), "FAKE_DIGEST": DIGEST, **extra}

    def run_script(self, script, *args, **extra):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=self.env(**extra), timeout=120)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""


class SubstratePullAuthTests(Fakes):
    def grant(self):
        self.policy([("roles/artifactregistry.writer", "serviceAccount:ewallet-cloudbuild@x.iam.gserviceaccount.com"),
                     ("roles/artifactregistry.reader", PRINCIPAL)])

    def upgrade(self, **extra):
        return self.run_script(AUTH, "upgrade", str(self.tmp / "work"), PKG_R_APPROVAL=f"PKG-R:{CTX}", **extra)

    def test_upgrade_order_identity_iam_metadata_diff_dry_run_then_upgrade(self):
        self.grant()
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        order = [calls.index(marker) for marker in (" template substrate", "artifacts repositories get-iam-policy",
                                                    "gke-metadata-server", "DRY-RUN", "REAL-UPGRADE")]
        self.assertEqual(order, sorted(order))
        self.assertEqual((self.tmp / "work/substrate/revision-before-upgrade").read_text().strip(), "2")
        self.assertIn("--dry-run=server", calls.split(" template substrate")[1].splitlines()[0], "the baseline render uses lookup")

    def test_missing_iam_binding_or_metadata_server_blocks_the_upgrade(self):
        for extra in ({}, {"FAKE_MDS_LABEL": "false"}):
            with self.subTest(extra=extra):
                if extra:
                    self.grant()
                self.log.unlink(missing_ok=True)
                result = self.upgrade(**extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("REAL-UPGRADE", self.calls())
                self.assertNotIn("DRY-RUN", self.calls())

    def test_failed_server_dry_run_stops_the_upgrade(self):
        self.grant()
        result = self.upgrade(FAKE_DRYRUN_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was upgraded", result.stderr)
        self.assertNotIn("REAL-UPGRADE", self.calls())

    def test_identity_mismatch_and_unexpected_diffs_stop_before_the_dry_run(self):
        self.grant()
        cases = {
            "regenerated secret in the candidate": dict(candidate=[atelet("true"), obj("Secret", "ate-system", "ateapi-tls", data={"tls.key": "bmV3"}), LIVE[2]]),
            "a second atelet argument": dict(candidate=[obj("DaemonSet", "ate-system", "atelet", spec={"template": {"spec": {"containers": [
                {"name": "atelet", "args": ["--gcp-auth-for-image-pulls=true", "--other=2"]}]}}}), SECRET, LIVE[2]]),
            "baseline does not reproduce live": dict(candidate=CANDIDATE, baseline=[atelet("false"), obj("Secret", "ate-system", "ateapi-tls", data={"tls.key": "bmV3"}), LIVE[2]]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.write(kwargs["candidate"], baseline=kwargs.get("baseline"))
                self.log.unlink(missing_ok=True)
                result = self.upgrade(**({"FAKE_BASELINE": "other"} if "baseline" in kwargs else {}))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("DRY-RUN", self.calls())
                self.assertNotIn("REAL-UPGRADE", self.calls())
                self.assertNotIn("c2VjcmV0", result.stderr + result.stdout, "no Secret value is ever printed")
                self.assertNotIn("bmV3", result.stderr + result.stdout)

    def test_identity_preconditions(self):
        self.grant()
        for extra, message in (({"FAKE_SA": "default"}, "not atelet"), ({"FAKE_HOSTNETWORK": "true"}, "hostNetwork"),
                               ({"FAKE_GSA": "x@y.iam.gserviceaccount.com"}, "bound to"), ({"FAKE_MODE": "GCE_METADATA"}, "GKE_METADATA"),
                               ({"FAKE_POOL": "other.svc.id.goog"}, "Workload Identity pool")):
            with self.subTest(extra=extra):
                result = self.run_script(AUTH, "diff", str(self.tmp / "work"), **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_wrong_approval_mutates_nothing(self):
        for token in (None, f"PKG-K:{CTX}", "PKG-R:other"):
            with self.subTest(token=token):
                extra = {} if token is None else {"PKG_R_APPROVAL": token}
                result = self.run_script(AUTH, "upgrade", str(self.tmp / "work"), **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"PKG_R_APPROVAL=PKG-R:{CTX}", result.stderr)
        self.assertEqual(self.calls(), "")

    def test_verify_catches_restarts_and_token_failures(self):
        self.grant()
        self.assertEqual(self.upgrade().returncode, 0)
        for extra, message in (({"FAKE_RESTARTS": "2"}, "restarted"), ({"FAKE_AUTH_FAIL": "1"}, "could not obtain a Google token")):
            with self.subTest(extra=extra):
                result = self.run_script(AUTH, "verify", str(self.tmp / "work"), **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_rollback_needs_its_token_and_restores_the_flag_and_manifest(self):
        self.grant()
        self.assertEqual(self.upgrade().returncode, 0)
        refused = self.run_script(AUTH, "rollback", str(self.tmp / "work"), PKG_R_APPROVAL=f"PKG-R:{CTX}")
        self.assertNotEqual(refused.returncode, 0)
        result = self.run_script(AUTH, "rollback", str(self.tmp / "work"), PKG_R_APPROVAL=f"PKG-R-ROLLBACK:{CTX}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REAL-ROLLBACK substrate", self.calls())
        self.assertEqual((self.tmp / "flag").read_text().strip(), "--gcp-auth-for-image-pulls=false")
        cleanup = self.run_script(AUTH, "cleanup", str(self.tmp / "work"))
        self.assertEqual(cleanup.returncode, 0)
        self.assertFalse(list((self.tmp / "work/substrate").glob("render-*.yaml")))

    def test_diff_module_accepts_only_the_atelet_flag(self):
        spec = importlib.util.spec_from_file_location("substrate_pull_diff", ROOT / "scripts/llm_agent/substrate_pull_diff.py")
        diff = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diff)
        base = {("DaemonSet", "ate-system", "atelet"): atelet("false"), ("Secret", "ate-system", "ateapi-tls"): SECRET}
        good = {**base, ("DaemonSet", "ate-system", "atelet"): atelet("true")}
        self.assertEqual(diff.candidate(base, good), [])
        for bad in ({**base, ("DaemonSet", "ate-system", "atelet"): atelet("yes")},
                    {**good, ("Secret", "ate-system", "ateapi-tls"): {**SECRET, "data": {"tls.key": "bmV3"}}},
                    {**good, ("ConfigMap", "ate-system", "extra"): obj("ConfigMap", "ate-system", "extra")}):
            with self.subTest(bad=sorted(bad)):
                problems = diff.candidate(base, bad)
                self.assertTrue(problems)
                self.assertNotIn("bmV3", " ".join(problems), "problems never carry values")


class SubstratePullIamTests(Fakes):
    def test_plan_and_verify_are_read_only(self):
        plan = self.run_script(IAM, "plan", str(self.tmp / "ev"))
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn(PRINCIPAL, plan.stdout)
        self.assertIn("absent", plan.stdout)
        self.assertNotEqual(self.run_script(IAM, "verify").returncode, 0)
        self.assertNotIn("add-iam-policy-binding", self.calls())

    def test_grant_needs_its_token_adds_one_binding_and_keeps_the_rest(self):
        self.assertNotEqual(self.run_script(IAM, "grant", IAM_APPROVAL=f"PKG-R:{CTX}").returncode, 0)
        self.assertNotIn("add-iam-policy-binding", self.calls())
        result = self.run_script(IAM, "grant", str(self.tmp / "ev"), IAM_APPROVAL=f"PKG-R-IAM:{PROJECT}")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads((self.tmp / "policy.json").read_text())
        pairs = {(b["role"], m) for b in policy["bindings"] for m in b["members"]}
        self.assertEqual(pairs, {("roles/artifactregistry.writer", "serviceAccount:ewallet-cloudbuild@x.iam.gserviceaccount.com"),
                                 ("roles/artifactregistry.reader", PRINCIPAL)})
        self.assertIn("--role roles/artifactregistry.reader", self.calls())
        self.assertNotIn("projects add-iam-policy-binding", self.calls(), "never project-wide")
        self.assertEqual(self.run_script(IAM, "verify").returncode, 0)

    def test_a_grant_that_loses_another_binding_fails(self):
        result = self.run_script(IAM, "grant", str(self.tmp / "ev"), IAM_APPROVAL=f"PKG-R-IAM:{PROJECT}", FAKE_IAM_CLOBBER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs beyond the one binding", result.stderr)

    def test_revoke_removes_only_the_principal(self):
        self.run_script(IAM, "grant", IAM_APPROVAL=f"PKG-R-IAM:{PROJECT}")
        self.assertNotEqual(self.run_script(IAM, "revoke", IAM_APPROVAL=f"PKG-R-IAM:{PROJECT}").returncode, 0)
        result = self.run_script(IAM, "revoke", str(self.tmp / "ev2"), IAM_APPROVAL=f"PKG-R-IAM-REVOKE:{PROJECT}")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads((self.tmp / "policy.json").read_text())
        self.assertEqual([(b["role"], b["members"]) for b in policy["bindings"]],
                         [("roles/artifactregistry.writer", ["serviceAccount:ewallet-cloudbuild@x.iam.gserviceaccount.com"])])


class GoldenResidueTests(Fakes):
    REVIEWED = ROOT / "docs/workplans/edai2/agent-platform-campaign/golden-residue.json"

    def actors(self, **changes):
        reviewed = json.loads(self.REVIEWED.read_text())
        items = [{"metadata": {"name": a["id"], "atespace": "ate-golden"}, "actorTemplateNamespace": a["template_namespace"],
                  "actorTemplateName": a["template"], "status": "STATUS_SUSPENDED"} for a in reviewed["actors"]]
        items[0].update(changes)
        items.append({"metadata": {"name": "pilot-golden", "atespace": "ate-golden"}, "actorTemplateNamespace": "kagent-pilot",
                      "actorTemplateName": "pilot-sandbox-agent-6f000f880e9fe909", "status": "STATUS_SUSPENDED"})
        (self.tmp / "actors.json").write_text(json.dumps({"actors": items}))

    def test_list_is_read_only_and_classifies(self):
        self.actors()
        result = self.run_script(RESIDUE, "list", str(self.REVIEWED))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("DELETABLE"), len(json.loads(self.REVIEWED.read_text())["actors"]))
        self.assertNotIn("delete actor", self.calls())

    def test_delete_needs_approval_and_removes_only_reviewed_actors(self):
        self.actors()
        self.assertNotEqual(self.run_script(RESIDUE, "delete", str(self.REVIEWED)).returncode, 0)
        self.assertNotIn("delete actor", self.calls())
        result = self.run_script(RESIDUE, "delete", str(self.REVIEWED), RESIDUE_APPROVAL=f"GOLDEN-RESIDUE:{CTX}")
        self.assertEqual(result.returncode, 0, result.stderr)
        left = [a["metadata"]["name"] for a in json.loads((self.tmp / "actors.json").read_text())["actors"]]
        self.assertEqual(left, ["pilot-golden"], "the pilot's golden actor is never touched")

    def test_delete_refuses_anything_not_safely_deletable(self):
        for changes in ({"status": "STATUS_RUNNING"}, {"ateomPodName": "worker-1"}, {"latestSnapshotInfo": {"x": 1}}):
            with self.subTest(changes=changes):
                self.actors(**changes)
                self.log.unlink(missing_ok=True)
                result = self.run_script(RESIDUE, "delete", str(self.REVIEWED), RESIDUE_APPROVAL=f"GOLDEN-RESIDUE:{CTX}")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("delete actor", self.calls())
        self.actors()
        result = self.run_script(RESIDUE, "delete", str(self.REVIEWED), RESIDUE_APPROVAL=f"GOLDEN-RESIDUE:{CTX}", FAKE_TEMPLATE_EXISTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("template-still-exists", result.stderr)


if __name__ == "__main__":
    unittest.main()
