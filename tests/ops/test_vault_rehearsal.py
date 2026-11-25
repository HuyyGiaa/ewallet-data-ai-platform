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

def test_rehearsal_role_binds_the_dedicated_reader_and_only_its_own_policy():
    """V2: the rehearsal store uses the same reader identity as ewallet-vault, through its own narrower role."""
    (name, role), = REHEARSAL["roles"].items()
    assert name == "eso-vault-rehearsal" and name not in OFFICIAL["roles"]
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == ["external-secrets"]
    assert role["audience"] == "vault" and role["token_no_default_policy"] is True
    assert role["token_policies"] == ["eso-vault-rehearsal"]


SYNTHETIC = ["ewallet/data/agent-platform/rehearsal-agentgateway-client-key", "ewallet/data/agent-platform/rehearsal-vllm-api-key",
             "ewallet/data/ml-platform/rehearsal-redis"]


def test_rehearsal_policy_reads_three_synthetic_paths_never_the_real_credentials():
    paths = grants(REHEARSAL["policies"]["eso-vault-rehearsal"])
    reads = sorted(p for p, caps in paths.items() if not p.startswith("auth/token/"))
    assert reads == SYNTHETIC and all(paths[p].strip() == '"read"' for p in reads)
    for real in ("ml-platform/feast-online-redis", "agent-platform/vllm-api-key", "agent-platform/agentgateway-client-key"):
        assert f"ewallet/data/{real}" not in paths
    assert not any("*" in p for p in paths)


def test_the_existing_operator_token_can_seed_the_rehearsal_paths():
    """The rehearsal uses the official put --token-file workflow; no operator policy change is needed."""
    operator = grants(OFFICIAL["policies"]["secret-operator"])
    for prefix in ("ewallet/data/ml-platform/*", "ewallet/data/agent-platform/*"):
        assert "create" in operator[prefix] and "update" in operator[prefix]
    assert all(p.rsplit("/", 1)[0] + "/*" in operator for p in SYNTHETIC)


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
    assert "pod-security.kubernetes.io/enforce: %s" in TEXT and '"${2:-restricted}"' in TEXT and "runAsNonRoot: true" in TEXT
    # Only the agents namespace enforces baseline (as the chart's own ewallet-agents: the vLLM CPU image runs as root).
    assert TEXT.count("new_namespace \"$ANS\" baseline") == 1 and "baseline" not in TEXT.split("new_namespace \"$ANS\" baseline")[0].split("new_namespace() {")[1].split("\n}")[1]


# ---- regression: the real cleanup and verify-clean code against stub kubectl/helm/gcloud (no cluster)

STUB = r'''#!/usr/bin/env python3
import json, os, sys
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
s = json.load(open(os.environ["STUB_SCENARIO"]))
state_file = os.environ["STUB_STATE"]
def initial():
    if "pvs" in s:
        return {"pvs": s["pvs"], "pvcs": s["pvcs"], "deleted_disks": s.get("deleted_disks", [])}
    pvs, pvcs = {}, {}
    for v in s["live_volumes"]:
        uid = "uid-" + v["claim"]
        pvs[v["name"]] = {"metadata": {"name": v["name"]}, "status": {"phase": "Bound"},
                          "spec": {"persistentVolumeReclaimPolicy": s["reclaim"], "csi": {"volumeHandle": v["disk"]},
                                   "claimRef": {"namespace": "ewallet-vault", "name": v["claim"], "uid": uid},
                                   "storageClassName": "standard-rwo", "capacity": {"storage": "1Gi"}}}
        pvcs[v["claim"]] = {"metadata": {"name": v["claim"], "uid": uid}, "spec": {"volumeName": v["name"]}, "status": {"phase": "Bound"}}
    return {"pvs": pvs, "pvcs": pvcs, "deleted_disks": []}
state = json.load(open(state_file)) if os.path.exists(state_file) else initial()
def save():
    json.dump(state, open(state_file, "w"))
def reclaim(name):                         # the provisioner: a Released PV on Delete goes, with its disk
    pv = state["pvs"][name]
    if pv["status"]["phase"] == "Released" and pv["spec"]["persistentVolumeReclaimPolicy"] == "Delete":
        state["deleted_disks"].append(pv["spec"]["csi"]["volumeHandle"]); del state["pvs"][name]
def after(word):
    return args[args.index(word) + 1] if word in args and args.index(word) + 1 < len(args) else None
text = " ".join(args)
open(os.environ["STUB_LOG"], "a").write(tool + " " + text + "\n")
def out(obj, rc=0):
    sys.stdout.write(obj if isinstance(obj, str) else json.dumps(obj)); sys.exit(rc)
if tool == "sleep":
    sys.exit(0)
if tool == "gcloud":                       # compute disks describe NAME --zone Z --project P ...
    name, zone, project = args[3], args[args.index("--zone") + 1], args[args.index("--project") + 1]
    path = f"projects/{project}/zones/{zone}/disks/{name}"
    mode = s["gcloud_after"] if path in state["deleted_disks"] else "present"
    if mode == "present":
        out(name + "\n")
    if mode == "absent":
        sys.stderr.write(f"ERROR: (gcloud.compute.disks.describe) HTTPError 404: The resource '{path}' was not found. This command is authenticated as someone\n"); sys.exit(1)
    sys.stderr.write("ERROR: (gcloud.compute.disks.describe) There was a problem refreshing your current auth tokens\n"); sys.exit(1)
if tool == "helm":
    if args[0] == "list" or " list " in f" {text} ":
        out(s["helm_releases"])
    sys.exit(0)
# kubectl: PVs and PVCs as a small state machine (bind, release, reclaim)
if "get" in args and "ns" in args and s.get("ns_absent"):
    sys.exit(0 if "--ignore-not-found" in args else 1)
pv_name, pvc_name = after("pv"), after("pvc")
if "patch" in args and "pv" in args:
    if pv_name not in state["pvs"]:
        sys.exit(1)
    pv = state["pvs"][pv_name]
    if "--type" in args:                   # remove /spec/claimRef
        pv["spec"].pop("claimRef", None); pv["status"]["phase"] = "Available"
    else:
        pv["spec"]["persistentVolumeReclaimPolicy"] = json.loads(after("-p"))["spec"]["persistentVolumeReclaimPolicy"]
        reclaim(pv_name)
    save(); sys.exit(0)
if "create" in args:
    import yaml
    for d in yaml.safe_load_all(sys.stdin.read()):
        if d and d["kind"] == "PersistentVolumeClaim":
            name, vol = d["metadata"]["name"], d["spec"]["volumeName"]
            uid = "new-" + name
            state["pvcs"][name] = {"metadata": {"name": name, "uid": uid}, "spec": {"volumeName": vol}, "status": {"phase": "Pending"}}
            pv = state["pvs"].get(vol)
            if pv and "claimRef" not in pv["spec"] and not s.get("never_bind"):
                pv["spec"]["claimRef"] = {"namespace": "ewallet-vault", "name": name, "uid": uid}
                pv["status"]["phase"] = "Bound"; state["pvcs"][name]["status"]["phase"] = "Bound"
    save(); sys.exit(0)
if "delete" in args and "pvc" in args:
    for name in args[args.index("pvc") + 1:]:
        if name.startswith("-") or name not in state["pvcs"]:
            continue
        vol = state["pvcs"].pop(name)["spec"]["volumeName"]
        if vol in state["pvs"]:
            state["pvs"][vol]["status"]["phase"] = "Released"; reclaim(vol)
    save(); sys.exit(0)
if "delete" in args:
    sys.exit(0)
if "get" in args and "pv" in args:
    if pv_name in (None, "-o"):
        out({"items": list(state["pvs"].values())})
    pv = state["pvs"].get(pv_name)
    if pv is None:
        out("", 0 if "--ignore-not-found" in args else 1)
    if "persistentVolumeReclaimPolicy" in text:
        out(pv["spec"]["persistentVolumeReclaimPolicy"])
    out("persistentvolume/" + pv_name if after("-o") == "name" else pv)
if "get" in args and "pvc" in args:
    if pvc_name in (None, "-o"):
        out({"items": list(state["pvcs"].values())})
    pvc = state["pvcs"].get(pvc_name)
    if pvc is None:
        out("", 0 if "--ignore-not-found" in args else 1)
    pv = state["pvs"].get(pvc["spec"]["volumeName"])
    if pvc["status"]["phase"] == "Pending" and pv and "claimRef" not in pv["spec"] and not s.get("never_bind"):
        pv["spec"]["claimRef"] = {"namespace": "ewallet-vault", "name": pvc_name, "uid": pvc["metadata"]["uid"]}   # the PV controller
        pv["status"]["phase"] = pvc["status"]["phase"] = "Bound"; save()
    if "jsonpath" in text:
        out(f'{pvc["status"]["phase"]}/{pvc["spec"]["volumeName"]}')
    out("persistentvolumeclaim/" + pvc_name if after("-o") == "name" else pvc)
if "get" in args and "ns" in args and "jsonpath" in text:
    out(s["id"])
if "get" in args and "ns" in args and "-o" not in args:
    sys.exit(0)
if "externalsecret,secretstore,pushsecret" in text:
    out({"items": s["eso_objects"]})
if "get clustersecretstore" in text:
    out({"items": s.get("cluster_stores", [{"metadata": {"name": "ewallet-vault-rehearsal", "labels": {"ewallet.io/vault-rehearsal": s["id"]}}}])})
if "clusterrole," in text:
    out({"items": [{"kind": o.split("/")[0], "metadata": {"name": o.split("/")[1]}} for o in s["cluster_objects"]]})
if "get ns -o json" in text or "get crd -o json" in text:
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
            helm_releases=None, recorded_eso=None, cluster_objects=ESO_OBJECTS, volumes=None, results_before=(), extra=(), **scenario_extra):
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
        scenario_extra.setdefault("pvs", {}); scenario_extra.setdefault("pvcs", {})
    if volumes is not None:                  # {"pvs": ..., "pvcs": ..., "deleted_disks": ...}: a state mid-way through the drill
        scenario_extra.update(volumes)
    if scenario_extra.pop("remove_record", False):
        (ev / "volumes.json").unlink()
    if results_before:
        (ev / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results_before))
    scenario = {"id": ID, "gcloud_after": gcloud_after, "reclaim": reclaim, "live_volumes": live_volumes or VOLUMES,
                "eso_objects": eso_objects if eso_objects is not None else [
                    {"kind": "ExternalSecret", "metadata": {"namespace": "vault-rehearsal", "name": "rehearsal-redis-vault"}}],
                "cluster_objects": cluster_objects, "helm_releases": helm_releases or releases, **scenario_extra}
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "REHEARSAL_CONTEXT": context,
           "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(ev), "KUBECONFIG": str(kubeconfig),
           "VAULT_REHEARSAL_CLEANUP_APPROVED": ID, "STUB_SCENARIO": str(tmp_path / "scenario.json"),
           "STUB_STATE": str(tmp_path / "state.json"), "STUB_LOG": str(tmp_path / "calls.log")}
    done = subprocess.run(["bash", str(SCRIPT), phase, *extra], env=env, capture_output=True, text=True, timeout=60)
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
    ({"reclaim": "Recycle"}, "unexpected reclaim policy Recycle"),
    ({"live_volumes": [dict(VOLUMES[0], disk=DISK.format("other")), VOLUMES[1]]}, "differs from the recorded"),
    ({"live_volumes": [dict(VOLUMES[0], name="pvc-x"), VOLUMES[1]]}, "bound to pvc-x, not the recorded pvc-a"),
    ({"eso_objects": [{"kind": "ExternalSecret", "metadata": {"namespace": "ml-platform", "name": "feature-api-redis"}}]},
     "ESO objects outside the rehearsal namespaces"),
    ({"recorded_eso": ["ClusterRole/external-secrets-controller"]}, "differ from the recorded inventory"),
], ids=["unexpected-reclaim-policy", "disk-handle-changed", "pvc-bound-to-other-pv", "eso-used-elsewhere", "eso-inventory-drift"])
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


def test_cleanup_returns_retained_rehearsal_pvs_to_delete_and_only_those(tmp_path):
    """A+ leaves the Vault PVs on Retain; cleanup patches exactly the two recorded PVs, then deletes."""
    done, calls, results = stubbed(tmp_path, "cleanup", reclaim="Retain")
    assert done.returncode == 0, done.stdout
    patched = [line for line in calls.splitlines() if line.startswith("kubectl") and " patch pv " in line]
    assert sorted(line.split(" patch pv ")[1].split()[0] for line in patched) == ["pvc-a", "pvc-d"]
    assert all('"Delete"' in line for line in patched)
    steps = [r["step"] for r in results]
    assert steps.index("volumes-identity") < steps.index("reclaim-delete-restored") < steps.index("cleanup")
    assert calls.index(" patch pv ") < calls.index("delete pvc")


# ---- fault injection: cleanup after the Retain drill stopped at any point (stub cluster, real script)

def _pv(name, phase, claim=None, reclaim="Retain", uid=None):
    spec = {"persistentVolumeReclaimPolicy": reclaim, "csi": {"volumeHandle": DISK.format(name[-1])},
            "storageClassName": "standard-rwo", "capacity": {"storage": "1Gi"}}
    if claim:
        spec["claimRef"] = {"namespace": "ewallet-vault", "name": claim, "uid": uid or "uid-" + claim}
    return {"metadata": {"name": name}, "spec": spec, "status": {"phase": phase}}


def _pvc(claim, volume, phase="Bound", uid=None):
    return {"metadata": {"name": claim, "uid": uid or "uid-" + claim}, "spec": {"volumeName": volume}, "status": {"phase": phase}}


A, D = ("pvc-a", "audit-vault-0"), ("pvc-d", "data-vault-0")
BOUND = lambda v: ({v[0]: _pv(v[0], "Bound", v[1])}, {v[1]: _pvc(v[1], v[0])})
RELEASED = lambda v: ({v[0]: _pv(v[0], "Released", v[1])}, {})
UNCLAIMED = lambda v: ({v[0]: _pv(v[0], "Available")}, {})
BINDING = lambda v: ({v[0]: _pv(v[0], "Available")}, {v[1]: _pvc(v[1], v[0], "Pending", "new-" + v[1])})
GONE = lambda v: ({}, {})


def _state(a, d, **more):
    (pa, ca), (pd, cd) = a(A), d(D)
    return {"pvs": {**pa, **pd}, "pvcs": {**ca, **cd}, **more}


FAULTS = {   # REHEARSAL_FAULT point in phase_retain (or a cleanup retry) -> the cluster state it leaves
    "retain-set": _state(BOUND, BOUND),
    "pvc-deleted:audit-vault-0": _state(RELEASED, BOUND),
    "pvcs-released": _state(RELEASED, RELEASED),
    "claimref-removed:audit-vault-0": _state(UNCLAIMED, RELEASED),
    "pvc-recreated-not-bound": _state(BINDING, RELEASED),
    "rebound:audit-vault-0": _state(BOUND, RELEASED),
    "rebound:data-vault-0": _state(BOUND, BOUND),
    "cleanup-retry-one-gone": _state(GONE, RELEASED, deleted_disks=[DISK.format("a")]),
    "cleanup-retry-all-gone": _state(GONE, GONE, deleted_disks=[DISK.format("a"), DISK.format("d")]),
}


@pytest.mark.parametrize("point", FAULTS)
def test_cleanup_finishes_from_every_interruption_point_of_the_retain_drill(tmp_path, point):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=FAULTS[point])
    assert done.returncode == 0, done.stdout + done.stderr
    assert results[-1]["step"] == "cleanup" and results[-1]["result"] == "PASS"
    state = json.loads((tmp_path / "state.json").read_text()) if (tmp_path / "state.json").exists() else {"pvs": {}}
    assert state["pvs"] == {} and set(state.get("deleted_disks", FAULTS[point].get("deleted_disks"))) == {DISK.format("a"), DISK.format("d")}
    lines = calls.splitlines()
    assert not any("disks delete" in line or " delete pv " in line for line in lines)       # no direct PV or disk deletion
    assert not any(" delete " in line and (" -l " in line or "--all" in line) and " pvc" in line for line in lines)
    patched = sorted(line.split(" patch pv ")[1].split()[0] for line in lines if " patch pv " in line and '"Delete"' in line)
    assert set(patched) <= {"pvc-a", "pvc-d"}
    steps = [r["step"] for r in results]
    assert steps.index("volumes-identity") < steps.index("reclaim-delete-restored")
    if point in ("claimref-removed:audit-vault-0", "pvc-recreated-not-bound"):
        assert "recovery-completed" in steps and steps.index("recovery-completed") < steps.index("reclaim-delete-restored")


@pytest.mark.parametrize("volumes,reason", [
    (_state(BOUND, BOUND, pvs={**_state(BOUND, BOUND)["pvs"], "pvc-z": _pv("pvc-z", "Bound", "data-vault-1")}), "unrecorded PV pvc-z"),
    (dict(_state(RELEASED, BOUND), pvs={"pvc-a": _pv("pvc-a", "Bound", "someone-else"), "pvc-d": _pv("pvc-d", "Bound", "data-vault-0")}),
     "claimed by ewallet-vault/someone-else"),
    (dict(_state(BOUND, BOUND), pvs={"pvc-d": _pv("pvc-d", "Bound", "data-vault-0")}), "its PV pvc-a is missing"),
    (dict(_state(RELEASED, BOUND), pvs={"pvc-a": _pv("pvc-a", "Failed", "audit-vault-0"), "pvc-d": _pv("pvc-d", "Bound", "data-vault-0")}),
     "Failed without its PVC"),
    (dict(_state(RELEASED, BOUND), pvcs={"audit-vault-0": _pvc("audit-vault-0", "pvc-a", "Pending", "new-audit-vault-0"),
                                         "data-vault-0": _pvc("data-vault-0", "pvc-d")}), "still names an older claim"),
], ids=["unrecorded-pv", "pv-claimed-by-other", "pvc-lost", "pv-failed", "stale-claimref"])
def test_cleanup_refuses_an_unidentified_volume_state_before_any_deletion(tmp_path, volumes, reason):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=volumes)
    assert done.returncode != 0 and " delete " not in f" {calls} " and " patch " not in f" {calls} "
    assert results[-1]["result"] == "FAIL" and reason in results[-1]["detail"]


def test_cleanup_never_finishes_a_recovery_without_its_namespace(tmp_path):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=_state(UNCLAIMED, RELEASED), ns_absent=True)
    assert done.returncode != 0 and "needs ewallet-vault" in results[-1]["detail"]
    assert " patch " not in f" {calls} " and " create " not in f" {calls} "


def test_cleanup_refuses_a_recovered_pvc_that_never_binds(tmp_path):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=_state(UNCLAIMED, RELEASED), never_bind=True)
    assert done.returncode != 0 and results[-1]["step"] == "rebind" and "not bound" in results[-1]["detail"]
    assert '"Delete"' not in calls                                     # nothing returned to Delete


def test_cleanup_refuses_a_missing_record_while_vault_volumes_exist(tmp_path):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=_state(BOUND, BOUND), remove_record=True)
    assert done.returncode != 0 and "no volumes.json" in results[-1]["detail"] and " delete " not in f" {calls} "


def test_an_orphaned_disk_is_reported_never_deleted(tmp_path):
    """The PV is gone (deleted by hand while Retain) but its disk is present: cleanup fails and never deletes it."""
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=_state(GONE, BOUND, deleted_disks=[]))
    assert done.returncode != 0 and "never deletes a disk itself" in results[-1]["detail"]
    assert "disks delete" not in calls


def test_disk_unreadable_before_deletion_refuses(tmp_path):
    done, calls, results = stubbed(tmp_path, "cleanup", volumes=_state(RELEASED, BOUND), deleted_disks=[DISK.format("a")], gcloud_after="unknown")
    assert done.returncode != 0 and "not confirmed present before deletion" in results[-1]["detail"] and " patch " not in f" {calls} "


def test_fault_injection_is_refused_on_gke(tmp_path):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    done = run("retain", tmp_path, REHEARSAL_CONTEXT=GKE, KUBECONFIG=str(kubeconfig), REHEARSAL_FAULT="retain-set", VAULT_REHEARSAL_APPROVED=ID)
    assert done.returncode == 2 and "kind fault-injection tests only" in done.stderr


def test_every_fault_point_exists_in_the_script():
    for point in ("retain-set", "pvcs-released", "cleanup-reclaim-restored"):
        assert f"fault {point}" in TEXT
    assert 'fault "pvc-deleted:$claim"' in TEXT and 'fault "claimref-removed:$claim"' in TEXT and 'fault "rebound:$claim"' in TEXT


# ---- fail-fast: a failed or interrupted phase stops every later normal phase

def _rec(phase, step, result):
    return {"phase": phase, "step": step, "result": result}


@pytest.mark.parametrize("before,why", [
    ([_rec("switch", "phase-start", "STARTED"), _rec("switch", "lookup-g9", "FAIL")], "switch failed at lookup-g9"),
    ([_rec("retain", "phase-start", "STARTED")], "retain was interrupted"),
    ([_rec("cleanup", "phase-start", "STARTED")], "cleanup has started"),
], ids=["failed", "interrupted", "cleanup-started"])
def test_a_stopped_run_refuses_every_normal_phase(tmp_path, before, why):
    done, calls, _ = stubbed(tmp_path, "after-wake", context="kind-test", results_before=before)
    assert done.returncode == 2 and why in done.stderr and "only inspect, cleanup and verify-clean" in done.stderr
    assert calls == ""                                                 # refused before any cluster call


def test_a_failed_read_only_preflight_does_not_stop_the_run(tmp_path):
    """The first preflight after a wake fails until cert-manager is up; that rerun is by design."""
    before = [_rec("preflight", "cert-manager-ready", "FAIL"), _rec("retain", "phase-start", "STARTED"), _rec("retain", "phase-done", "PASS")]
    done, _, results = stubbed(tmp_path, "gate", context="kind-test", results_before=before, extra=("retain",))
    assert done.returncode == 0 and results[-1]["step"] == "gate-retain" and results[-1]["result"] == "PASS"


def test_gate_refuses_an_unknown_step(tmp_path):
    done, _, _ = stubbed(tmp_path, "gate", context="kind-test", extra=("vault",))
    assert done.returncode == 2 and "gate needs retain, after-wake or clean" in done.stderr


@pytest.mark.parametrize("before,gate,ok", [
    ([_rec("retain", "phase-start", "STARTED"), _rec("retain", "phase-done", "PASS")], "retain", True),
    ([_rec("retain", "phase-start", "STARTED"), _rec("retain", "kv", "FAIL")], "retain", False),
    ([_rec("lifecycle", "phase-start", "STARTED"), _rec("lifecycle", "phase-done", "PASS")], "retain", False),
    ([_rec("after-wake", "phase-start", "STARTED"), _rec("after-wake", "phase-done", "PASS")], "after-wake", True),
    ([_rec("cleanup", "phase-start", "STARTED"), _rec("cleanup", "cleanup", "PASS"), _rec("verify-clean", "clean-verified", "PASS")], "clean", True),
    ([_rec("cleanup", "phase-start", "STARTED"), _rec("cleanup", "disks", "FAIL"), _rec("verify-clean", "clean-verified", "PASS")], "clean", False),
    ([_rec("cleanup", "phase-start", "STARTED"), _rec("cleanup", "cleanup", "PASS"), _rec("verify-clean", "clean", "FAIL")], "clean", False),
    ([_rec("switch", "lookup", "FAIL"), _rec("cleanup", "cleanup", "PASS"), _rec("verify-clean", "clean-verified", "PASS")], "clean", True),
], ids=["retain-passed", "retain-failed", "retain-not-reached", "after-wake-passed", "clean", "cleanup-failed", "verify-failed",
        "clean-after-failed-run"])
def test_gates_open_only_on_a_passed_step(tmp_path, before, gate, ok):
    done, calls, results = stubbed(tmp_path, "gate", context="kind-test", results_before=before, extra=(gate,))
    assert (done.returncode == 0) is ok and results[-1]["step"] == f"gate-{gate}"
    assert calls == ""                                                 # a gate reads only the run's own record
    if not ok:
        assert "do not continue" in results[-1]["detail"]


# ---- Campaign A+: the serving chart as a rehearsal Helm release

import importlib.util
import shutil
import yaml

POST = ROOT / "scripts/vault/rehearsal/serving_post_render.py"
_spec = importlib.util.spec_from_file_location("serving_post_render", POST)
post_render_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_render_module)
IMAGE = "localhost:5001/ewallet-inference@sha256:" + "0" * 64


def _doc(kind, namespace, name, **spec):
    return {"kind": kind, "metadata": {"name": name, **({"namespace": namespace} if namespace else {})}, "spec": spec}


def _deployment():
    return _doc("Deployment", "vault-rehearsal", "feature-api", template={"spec": {"containers": [{"name": "feature-api"}]}})


def test_post_renderer_drops_the_feature_store_policy_mounts_the_registry_and_labels_everything():
    out = post_render_module.post_render(
        [_deployment(), _doc("Service", "vault-rehearsal", "feature-api"),
         _doc("NetworkPolicy", "feature-store", "redis-clients-feature-api")], "vault-rehearsal", ID)
    assert [(d["kind"], d["metadata"]["name"]) for d in out] == [("Deployment", "feature-api"), ("Service", "feature-api")]
    assert all(d["metadata"]["labels"]["ewallet.io/vault-rehearsal"] == ID for d in out)
    pod = out[0]["spec"]["template"]["spec"]
    assert pod["volumes"] == [{"name": "registry", "configMap": {"name": "feast-registry"}}]
    assert pod["containers"][0]["volumeMounts"] == [{"name": "registry", "mountPath": "/registry", "readOnly": True}]


@pytest.mark.parametrize("stray", [
    _doc("NetworkPolicy", "feature-store", "something-else"),
    _doc("ServiceAccount", "ml-platform", "feature-api"),
    _doc("ClusterRole", None, "anything"),
], ids=["other-feature-store-object", "production-namespace", "cluster-scoped"])
def test_post_renderer_refuses_anything_outside_the_rehearsal_namespace(stray):
    with pytest.raises(SystemExit):
        post_render_module.post_render([_deployment(), stray], "vault-rehearsal", ID)


def test_post_renderer_requires_its_environment():
    done = subprocess.run([str(POST)], input="", env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "REHEARSAL_NAMESPACE and REHEARSAL_ID are required" in done.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize("overlay,secret", [(False, "rehearsal-feast-online-redis"), (True, "feast-online-redis-vault")],
                         ids=["before-switch", "official-overlay"])
def test_the_real_chart_renders_only_rehearsal_objects_and_the_official_overlay_switches_the_secret(overlay, secret):
    argv = ["helm", "template", "rehearsal-serving", str(ROOT / "infra/helm/ewallet-serving"), "-n", "vault-rehearsal",
            "-f", str(ROOT / "scripts/vault/rehearsal/serving-values.yaml"), "--set", f"featureApi.image={IMAGE}",
            "--post-renderer", str(POST)]
    if overlay:
        argv += ["-f", str(ROOT / "infra/helm/ewallet-serving/values-vault-eso.yaml")]
    env = {**os.environ, "REHEARSAL_NAMESPACE": "vault-rehearsal", "REHEARSAL_ID": ID}
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    docs = [d for d in yaml.safe_load_all(done.stdout) if d]
    assert sorted((d["kind"], d["metadata"]["namespace"]) for d in docs) == [("Deployment", "vault-rehearsal"), ("Service", "vault-rehearsal")]
    container = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
    env_vars = {e["name"]: e for e in container["env"]}
    assert env_vars["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"]["name"] == secret
    assert not any(name.startswith("EWALLET_VAULT_") for name in env_vars)
    assert env_vars["EWALLET_FEAST_REGISTRY"]["value"] == "/registry/registry.db"


@pytest.mark.parametrize("phase", ["feature-api", "switch", "rollback", "retain", "after-wake"])
def test_every_a_plus_phase_needs_the_gke_approval(tmp_path, phase):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    done = run(phase, tmp_path, REHEARSAL_CONTEXT=GKE, KUBECONFIG=str(kubeconfig))
    assert done.returncode == 2 and "needs VAULT_REHEARSAL_APPROVED" in done.stderr


def test_a_plus_lookups_pass_only_on_a_real_business_result_and_drift_ignores_sleep_scaling():
    lookup = TEXT.split("lookup_class() {")[1].split("seed_registry() {")[0]
    assert '"BASELINE_OK"' in lookup and "[ \"$cls\" = BASELINE_OK ] || fail" in lookup and "--max-time 30" in lookup
    assert 'elif $rc == 0 and $c == "424"' in lookup                    # a 424 is classified, never BASELINE_OK
    snapshot = TEXT.split("snapshot() {")[1].split("compare_production() {")[0]
    assert "metadata.generation" not in snapshot.split("\n", 3)[-1] and "image: .spec.template.spec.containers[0].image" in snapshot
    assert 'or .name == "rehearsal-serving"' in TEXT.split("collisions() {")[1].split("none_found()")[0]
    seed = TEXT.split("seed_registry() {")[1].split("\n}\n")[0]
    # V2: the seed follows the ESO-managed Secret (after the real Redis rotation the original credential is retired);
    # the registry must contain neither the original nor the current synthetic password.
    assert "the registry contains the Redis password" in seed and "secretKeyRef: {name: $SEED_SECRET" in seed
    assert 'synthetic P1' in seed and 'synthetic PCUR' in seed and "SEED_SECRET=feast-online-redis-vault" in TEXT


def test_the_gke_procedure_is_fail_fast_and_gated():
    """§9: every block stops at its first failure, each cloud step is gated, both sleeps refuse unapproved targets."""
    runbook = (ROOT / "docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md").read_text().split("## 9. Campaign A+")[1]
    run = runbook.split("### GKE run")[1].split("### Failure")[0]
    assert "|| break" not in run
    blocks = [b for b in run.split("\n# ")[1:] if b[:2] in {f"{n}." for n in range(1, 7)}]
    assert len(blocks) == 6 and all("set -euo pipefail" in b and f'BLOCK {b[0]} PASS' in b for b in blocks)
    for n, gate in [(3, "retain"), (4, "retain"), (6, "clean")]:
        assert blocks[n - 1].split("set -euo pipefail")[1].lstrip("; \n").startswith(f"$R gate {gate}")
    assert "$R gate retain" in blocks[1] and "$R gate after-wake" in blocks[3] and "$R gate clean" in blocks[4]
    assert 'guarded_sleep "$REHEARSAL_DIR/recovery-manifest-mid"' in blocks[2] and 'guarded_sleep "$REHEARSAL_DIR/recovery-manifest"' in blocks[5]
    helper = run.split("guarded_sleep() (")[1].split("\n)\n")[0]
    assert '((.guarded_preparation.not_covered // []) - $ok) == []' in helper and '"UNSAFE_TO_SLEEP"' in helper
    assert "--also-approve" in helper and "verify --expect sleep" in helper
    failure = runbook.split("### Failure")[1]
    assert "$R inspect" in failure and "**F1**" in failure and "**F2**" in failure and "Never re-run a failed mutating phase" in failure


def test_cleanup_refuses_a_cluster_secret_store_it_did_not_create(tmp_path):
    """Vault V2: a ClusterSecretStore is cluster-scoped; cleanup never deletes one without this rehearsal's label."""
    foreign = [{"metadata": {"name": "ewallet-vault-rehearsal", "labels": {"ewallet.io/vault-rehearsal": ID}}},
               {"metadata": {"name": "ewallet-vault", "labels": {"app.kubernetes.io/part-of": "ewallet-platform"}}}]
    done, calls, results = stubbed(tmp_path, "cleanup", cluster_stores=foreign)
    assert done.returncode != 0 and results[-1]["step"] == "eso-scope" and "ewallet-vault" in results[-1]["detail"]
    assert " delete " not in f" {calls} " and " patch " not in f" {calls} "
