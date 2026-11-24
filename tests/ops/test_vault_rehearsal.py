"""scripts/vault/gke_rehearsal.sh and its Vault fixture: isolation, approvals and refusals, without a cluster."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/vault/gke_rehearsal.sh"
TEXT = SCRIPT.read_text()
REHEARSAL = json.loads((ROOT / "scripts/vault/rehearsal/bootstrap.json").read_text())
OFFICIAL = json.loads((ROOT / "infra/kubernetes/vault/bootstrap.json").read_text())
GKE = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
ID = "vault-rehearsal-20261125T010203Z"


def grants(policy_path: str) -> dict[str, str]:
    text = (ROOT / policy_path).read_text()
    return dict(re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text))


# ---- the rehearsal's Vault role and policy

def test_rehearsal_role_is_bound_only_to_the_temporary_namespace():
    (name, role), = REHEARSAL["roles"].items()
    assert name == "eso-vault-rehearsal" and name not in OFFICIAL["roles"]
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == ["vault-rehearsal"]
    assert role["audience"] == "vault" and role["token_no_default_policy"] is True
    assert role["token_policies"] == ["eso-vault-rehearsal"]


def test_rehearsal_policy_reads_one_synthetic_path_never_the_real_credential():
    paths = grants(REHEARSAL["policies"]["eso-vault-rehearsal"])
    reads = [p for p, caps in paths.items() if not p.startswith("auth/token/")]
    assert reads == ["ewallet/data/feature-api/rehearsal-redis"] and paths[reads[0]].strip() == '"read"'
    assert "ewallet/data/feature-api/feast-redis" not in paths


def test_the_existing_operator_token_can_seed_the_rehearsal_path():
    """The rehearsal uses the official put --token-file workflow; no operator policy change is needed."""
    operator = grants(OFFICIAL["policies"]["secret-operator"])
    assert "create" in operator["ewallet/data/feature-api/*"] and "update" in operator["ewallet/data/feature-api/*"]


def test_rehearsal_bootstrap_reuses_the_official_mounts():
    for key in ("kv_mount", "audit_file", "kubernetes_auth_mount", "token_audience"):
        assert REHEARSAL[key] == OFFICIAL[key]


# ---- the script refuses before touching any cluster

def run(phase: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "REHEARSAL_CONTEXT": "kind-test",
            "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(tmp_path / "ev")}
    base.update(env)
    return subprocess.run(["bash", str(SCRIPT), phase], env=base, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("change,reason", [
    ({"REHEARSAL_CONTEXT": "gke_other-project_zone_cluster"}, "neither kind-* nor"),
    ({"REHEARSAL_ID": "my-test"}, "REHEARSAL_ID must look like"),
    ({"REHEARSAL_CONTEXT": GKE, "KUBECONFIG": "/home/x/.kube/config"}, "KUBECONFIG must be"),
    ({"REHEARSAL_DIR": str(ROOT / "tmp-rehearsal")}, "outside the repository"),
], ids=["other-context", "bad-id", "gke-default-kubeconfig", "dir-in-repo"])
def test_refusals(tmp_path, change, reason):
    done = run("preflight", tmp_path, **change)
    assert done.returncode == 2 and reason in done.stderr
    assert not (ROOT / "tmp-rehearsal").exists()


@pytest.mark.parametrize("phase", ["vault", "init", "configure", "eso", "consumer", "negative", "lifecycle"])
def test_every_gke_mutation_needs_the_approval_for_this_id(tmp_path, phase):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    common = {"REHEARSAL_CONTEXT": GKE, "KUBECONFIG": str(kubeconfig)}
    assert "needs VAULT_REHEARSAL_APPROVED" in run(phase, tmp_path, **common).stderr
    wrong = run(phase, tmp_path, **common, VAULT_REHEARSAL_APPROVED="vault-rehearsal-20260101T000000Z")
    assert wrong.returncode == 2 and "needs VAULT_REHEARSAL_APPROVED" in wrong.stderr


def test_gke_cleanup_needs_its_own_approval(tmp_path):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    done = run("cleanup", tmp_path, REHEARSAL_CONTEXT=GKE, KUBECONFIG=str(kubeconfig), VAULT_REHEARSAL_APPROVED=ID)
    assert done.returncode == 2 and "VAULT_REHEARSAL_CLEANUP_APPROVED" in done.stderr


def test_xtrace_is_refused(tmp_path):
    base = {"PATH": os.environ["PATH"], "REHEARSAL_CONTEXT": "kind-test", "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(tmp_path)}
    done = subprocess.run(["bash", "-x", str(SCRIPT), "preflight"], env=base, capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "xtrace" in done.stderr


# ---- static properties of the procedure

def test_objects_are_created_never_applied_and_every_call_is_pinned():
    assert "kubectl apply" not in TEXT and " k apply" not in TEXT and "set -x" not in TEXT
    code = [line for line in TEXT.splitlines() if not line.lstrip().startswith("#")]
    # kubectl or helm in command position (line start, after ( ; | & or $( ): always pinned to the context
    calls = [line for line in code if re.search(r"(^|[(;|&]|\$\()\s*(kubectl|helm)\s", line)]
    assert calls and all("--context" in line or "--kube-context" in line for line in calls), calls


def test_nothing_is_created_in_an_existing_production_namespace():
    for namespace in ("ml-platform", "ewallet-agents", "feature-store", "kagent", "ewallet-ingress"):
        for line in TEXT.splitlines():
            if re.search(rf"(-n\s+|namespace:\s*){namespace}\b", line) and not line.lstrip().startswith("#"):
                assert re.search(r"\bget\b", line), f"{namespace} used outside a read: {line.strip()}"


def test_cleanup_deletes_only_namespaces_proven_to_be_this_rehearsals():
    cleanup = TEXT.split("phase_cleanup()")[1].split("phase_verify_clean()")[0]
    assert "without $LABEL=$ID" in cleanup
    for deletion in re.findall(r"k delete ns \"\$(\w+)\"", cleanup):
        assert f'if mine "${deletion}"' in cleanup


def test_consumer_side_uses_the_platform_redis_image_and_restricted_pods():
    platform = (ROOT / "infra/kubernetes/feast-online/redis.yaml").read_text()
    digest = re.search(r"REDIS_IMAGE=\S+@(sha256:[0-9a-f]{64})", TEXT).group(1)
    assert digest in platform
    assert "pod-security.kubernetes.io/enforce: restricted" in TEXT and "runAsNonRoot: true" in TEXT


# ---- regression: the real cleanup and verify-clean code against stub kubectl/helm/gcloud (no cluster)

STUB = r'''#!/usr/bin/env python3
import json, os, sys
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
s = json.load(open(os.environ["STUB_SCENARIO"]))
state_file = os.environ["STUB_STATE"]
state = json.load(open(state_file)) if os.path.exists(state_file) else {"pvc_deleted": False}
open(os.environ["STUB_LOG"], "a").write(tool + " " + " ".join(args) + "\n")
text = " ".join(args)
def out(obj, rc=0):
    sys.stdout.write(obj if isinstance(obj, str) else json.dumps(obj)); sys.exit(rc)
if tool == "sleep":
    sys.exit(0)
if tool == "gcloud":                       # compute disks describe NAME --zone Z --project P ...
    name, zone, project = args[3], args[args.index("--zone") + 1], args[args.index("--project") + 1]
    mode = s["gcloud_after"] if state["pvc_deleted"] else "present"
    if mode == "present":
        out(name + "\n")
    if mode == "absent":
        sys.stderr.write(f"ERROR: (gcloud.compute.disks.describe) HTTPError 404: The resource 'projects/{project}/zones/{zone}/disks/{name}' was not found. This command is authenticated as someone\n"); sys.exit(1)
    sys.stderr.write("ERROR: (gcloud.compute.disks.describe) There was a problem refreshing your current auth tokens\n"); sys.exit(1)
if tool == "helm":
    if args[0] == "list" or " list " in f" {text} ":
        out(s["helm_releases"])
    sys.exit(0)
# kubectl
if "delete" in args:
    if "pvc" in args:
        state["pvc_deleted"] = True; json.dump(state, open(state_file, "w"))
    sys.exit(0)
if "get" in args and "ns" in args and "jsonpath" in text:
    out(s["id"])
if "get" in args and "ns" in args and "-o" not in args:
    sys.exit(0)
if "externalsecret,secretstore,pushsecret" in text:
    out({"items": s["eso_objects"]})
if "clusterrole," in text:
    out({"items": [{"kind": o.split("/")[0], "metadata": {"name": o.split("/")[1]}} for o in s["cluster_objects"]]})
if "get pvc -o name" in text:
    out("" if state["pvc_deleted"] else "persistentvolumeclaim/audit-vault-0\npersistentvolumeclaim/data-vault-0\n")
if "get pvc -o json" in text:
    out({"items": [] if state["pvc_deleted"] else [{"metadata": {"name": v["claim"]}, "spec": {"volumeName": v["name"]}} for v in s["live_volumes"]]})
if "get pv" in text and "--ignore-not-found" in args:
    out("" if state["pvc_deleted"] else "persistentvolume/" + args[args.index("pv") + 1])
if "get pv" in text and "-o json" in text and args[args.index("pv") + 1] != "-o":
    v = next(x for x in s["live_volumes"] if x["name"] == args[args.index("pv") + 1])
    out({"spec": {"persistentVolumeReclaimPolicy": s["reclaim"], "claimRef": {"namespace": "ewallet-vault", "name": v["claim"]},
                  "csi": {"volumeHandle": v["disk"]}}})
if "get ns -o json" in text or "get crd -o json" in text or "get pv -o json" in text:
    out({"items": []})
if "get svc -A" in text:
    out({"items": []})
if "get deploy feature-api" in text:
    out("")
sys.exit(0)
'''

DISK = "projects/ewallet-data-ai-platform-hg26/zones/asia-southeast1-b/disks/pvc-{}"
VOLUMES = [{"name": "pvc-a", "reclaim": "Delete", "disk": DISK.format("a"), "claim": "audit-vault-0"},
           {"name": "pvc-d", "reclaim": "Delete", "disk": DISK.format("d"), "claim": "data-vault-0"}]
ESO_OBJECTS = ["ClusterRole/external-secrets-controller", "ValidatingWebhookConfiguration/secretstore-validate"]


def stubbed(tmp_path, phase, *, context=GKE, gcloud_after="absent", reclaim="Delete", eso_objects=None, live_volumes=None,
            helm_releases=None, recorded_eso=None, cluster_objects=ESO_OBJECTS):
    """Run one phase with stub binaries; ``live_volumes`` is what the cluster reports, VOLUMES what R3 recorded."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    stub = bin_dir / "stub.py"; stub.write_text(STUB); stub.chmod(0o755)
    for tool in ("kubectl", "helm", "gcloud", "sleep"):
        (bin_dir / tool).symlink_to(stub)
    ev = tmp_path / "ev"; (ev / "private").mkdir(parents=True)
    (ev / "volumes.json").write_text(json.dumps(VOLUMES))
    (ev / "eso-cluster-objects.json").write_text(json.dumps({"cluster_objects": recorded_eso or ESO_OBJECTS, "crds": []}))
    releases = [{"name": "ewallet-serving", "namespace": "ml-platform", "revision": "3", "status": "deployed"}]
    baseline = {"helm_releases": releases, "feast_online_redis_resource_version": "",
                "feature_api": None, "loadbalancer_services": [], "edge_gateway_service_type": ""}
    (ev / "baseline.json").write_text(json.dumps(baseline))
    if phase == "verify-clean":
        os.rmdir(ev / "private")
    scenario = {"id": ID, "gcloud_after": gcloud_after, "reclaim": reclaim, "live_volumes": live_volumes or VOLUMES,
                "eso_objects": eso_objects if eso_objects is not None else [
                    {"kind": "ExternalSecret", "metadata": {"namespace": "vault-rehearsal", "name": "rehearsal-redis-vault"}}],
                "cluster_objects": cluster_objects, "helm_releases": helm_releases or releases}
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "REHEARSAL_CONTEXT": context,
           "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(ev), "KUBECONFIG": str(kubeconfig),
           "VAULT_REHEARSAL_CLEANUP_APPROVED": ID, "STUB_SCENARIO": str(tmp_path / "scenario.json"),
           "STUB_STATE": str(tmp_path / "state.json"), "STUB_LOG": str(tmp_path / "calls.log")}
    done = subprocess.run(["bash", str(SCRIPT), phase], env=env, capture_output=True, text=True, timeout=60)
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    results = [json.loads(line) for line in (ev / "results.jsonl").read_text().splitlines()] if (ev / "results.jsonl").exists() else []
    return done, calls, results


def test_cleanup_passes_only_when_the_recorded_disks_are_confirmed_absent(tmp_path):
    done, calls, results = stubbed(tmp_path, "cleanup", gcloud_after="absent")
    assert done.returncode == 0, done.stderr
    assert results[-1]["step"] == "cleanup" and results[-1]["result"] == "PASS"
    assert "delete pvc audit-vault-0 data-vault-0" in calls and "--all" not in calls.split("delete pvc")[1].splitlines()[0]
    assert "someone" not in json.dumps(results)                       # the gcloud account text is never recorded


@pytest.mark.parametrize("after,reason", [("unknown", "state unknown"), ("present", "still present")])
def test_cleanup_never_passes_when_disk_absence_is_not_proven(tmp_path, after, reason):
    done, _, results = stubbed(tmp_path, "cleanup", gcloud_after=after)
    assert done.returncode != 0 and not any(r["step"] == "cleanup" and r.get("result") == "PASS" for r in results)
    assert results[-1]["result"] == "FAIL" and reason in results[-1]["detail"]


@pytest.mark.parametrize("change,reason", [
    ({"reclaim": "Retain"}, "not reclaim Delete"),
    ({"live_volumes": [dict(VOLUMES[0], disk=DISK.format("other")), VOLUMES[1]]}, "claim or disk differs from the record"),
    ({"live_volumes": [dict(VOLUMES[0], name="pvc-x"), VOLUMES[1]]}, "differ from the recorded"),
    ({"eso_objects": [{"kind": "ExternalSecret", "metadata": {"namespace": "ml-platform", "name": "feature-api-redis"}}]},
     "ESO objects outside vault-rehearsal"),
    ({"recorded_eso": ["ClusterRole/external-secrets-controller"]}, "differ from the recorded inventory"),
], ids=["retain-policy", "disk-handle-changed", "pvc-bound-to-other-pv", "eso-used-elsewhere", "eso-inventory-drift"])
def test_cleanup_refuses_before_any_deletion(tmp_path, change, reason):
    done, calls, results = stubbed(tmp_path, "cleanup", **change)
    assert done.returncode != 0 and " delete " not in f" {calls} "
    assert results[-1]["result"] == "FAIL" and reason in results[-1]["detail"]


def test_production_drift_fails_the_phase_and_records_what_changed(tmp_path):
    moved = [{"name": "ewallet-serving", "namespace": "ml-platform", "revision": "4", "status": "deployed"}]
    done, calls, results = stubbed(tmp_path, "verify-clean", context="kind-test", helm_releases=moved, cluster_objects=[])
    assert done.returncode != 0
    assert results[-1]["step"] == "production-unchanged" and results[-1]["result"] == "FAIL"
    assert "helm_releases" in results[-1]["detail"] and (tmp_path / "ev/snapshot-differs-production-unchanged.json").exists()
    assert " delete " not in f" {calls} "                                # drift never triggers any cleanup


def test_lifecycle_and_verify_clean_share_the_failing_production_comparison():
    lifecycle = TEXT.split("phase_lifecycle()")[1].split("phase_cleanup()")[0]
    verify = TEXT.split("phase_verify_clean()")[1]
    assert "compare_production production-unchanged" in lifecycle and "compare_production production-unchanged" in verify
    assert "DIFFERS" not in TEXT                                       # drift is a FAIL, never a soft status
    assert "|| true" not in TEXT.split("disk_state()")[1].split("check_volumes_before_delete()")[0].split("else")[0]


def test_the_consumer_reports_auth_failure_only_for_a_rejected_password():
    """A connection race must never pass as the expected AUTH_FAILED of the lifecycle phase."""
    job = TEXT.split("consume() {")[1].split("collisions() {")[0]
    assert "*WRONGPASS*|*NOAUTH*) echo AUTH_FAILED" in job and "echo UNREACHABLE" in job
    assert job.index("WRONGPASS") < job.index("sleep 1")              # an auth failure is reported at once, not retried
