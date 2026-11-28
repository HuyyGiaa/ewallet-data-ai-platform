"""P5 offline: the vault-a2 partial wake (2 nodes) for Vault V2 Campaign A2, without cloud access.

The recorded inventory is the 2026-11-24 recovery manifest (copied into the P5 evidence package).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ops.gcp import cloud_lab as lab

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs/evidence/edai2/vault/p5_offline_optimization_20261128/inventory"
PROFILE = json.loads(lab.PROFILES_PATH.read_text())["experiment"]["vault-a2"]
REHEARSAL = ["ewallet-vault", "external-secrets", "vault-rehearsal", "vault-rehearsal-agents"]


def test_vault_a2_allows_only_the_rehearsal_and_the_keda_metrics_apiserver():
    assert sorted(PROFILE) == sorted([f"{ns}/*" for ns in REHEARSAL] + ["keda/keda-operator-metrics-apiserver"])
    recorded, _ = lab.read_manifest(INVENTORY)
    production = [w.key for w in recorded if lab.allowed(w, PROFILE)]
    assert production == ["keda/keda-operator-metrics-apiserver"], "no other production workload is woken"


def test_required_nodes_counts_pods_a_partial_wake_cannot_hold_down():
    rows = [{"namespace": "kube-system", "name": "dns", "cpu_m": 1000, "mem_mi": 100, "max_pod_cpu_m": 500},
            {"namespace": "ewallet-sandbox", "name": "pool-deployment", "cpu_m": 2000, "mem_mi": 100, "max_pod_cpu_m": 500},
            {"namespace": "app", "name": "held", "cpu_m": 9000, "mem_mi": 100, "max_pod_cpu_m": 100}]
    assert lab.required_nodes(rows, [])["nodes"] == 1, "without the manifest keys the owned pods are missed"
    need = lab.required_nodes(rows, [], recorded={"kube-system/dns", "app/held"})
    assert need["nodes"] == 2 and need["unholdable"] == ["ewallet-sandbox/pool-deployment"]
    assert "unholdable" not in lab.required_nodes(rows, None, recorded={"app/held"}), "a full wake counts everything anyway"


def test_vault_a2_needs_two_nodes_and_a_full_wake_five_on_the_recorded_inventory(capsys):
    for mode, profile, nodes in (("experiment", "vault-a2", 2), ("full", None, 5)):
        assert lab.cmd_plan_wake(None, argparse.Namespace(mode=mode, profile=profile, manifest=INVENTORY)) == 0
        need = json.loads(capsys.readouterr().out)
        assert need["nodes"] == nodes
    recorded, requests = lab.read_manifest(INVENTORY)
    need = lab.required_nodes(requests, PROFILE, recorded={w.key for w in recorded})
    assert need["unholdable"] == ["ewallet-sandbox/business-agents-deployment", "kagent-pilot/kagent-pilot-deployment"]
    # One node cannot hold the system pods plus the owned pods; the rehearsal itself adds about 0.35 CPU.
    free_cpu = (lab.NodeShape().cpu_m - lab.NodeShape().daemon_cpu_m) * lab.PACKING_LIMIT
    assert free_cpu < need["cpu_m"] and need["cpu_m"] + 350 < 2 * free_cpu


def workload(kind, ns, name, replicas, owner=None):
    meta = {"namespace": ns, "name": name}
    if owner:
        meta["ownerReferences"] = [{"kind": owner, "name": "pool"}]
    return {"kind": kind, "metadata": meta, "spec": {"replicas": replicas, "template": {"spec": {"containers": []}}},
            "status": {"readyReplicas": replicas}}


def so(ns, name, target, kind="Deployment", paused=True):
    ann = {lab.KEDA_PAUSE: "0", lab.PAUSED_BY: "vault-rehearsal-20261124T173156Z"} if paused else {}
    return {"metadata": {"namespace": ns, "name": name, "annotations": ann},
            "spec": {"scaleTargetRef": {"kind": kind, "name": target}, "minReplicaCount": 1}}


class Cluster:
    """Fake reads for a partially woken cluster; every mutation is only recorded."""

    def __init__(self, workloads, sos=(), apiservices=(), webhooks=(), slices=()):
        self.workloads, self.sos, self.apiservices = list(workloads), list(sos), list(apiservices)
        self.webhooks, self.slices, self.calls = list(webhooks), list(slices), []

    def __call__(self, command):
        self.calls.append(command)
        text = " ".join(command)
        if "compute instances list" in text:
            return json.dumps([{"name": lab.VM_PREFIX + "a", "status": "RUNNING"}])
        if "scaledobjects.keda.sh" in text:
            return json.dumps({"items": self.sos})
        if "get deploy,sts" in text:
            return json.dumps({"items": self.workloads})
        if "get deploy -A" in text:
            return json.dumps({"items": [w for w in self.workloads if w["kind"] == "Deployment"]})
        if "get apiservices" in text:
            return json.dumps({"items": self.apiservices})
        if "mutatingwebhookconfigurations" in text:
            return json.dumps({"items": self.webhooks})
        if "validatingwebhookconfigurations" in text or "get pods" in text:
            return json.dumps({"items": []})
        if "endpointslices" in text:
            return json.dumps({"items": self.slices})
        if "compute disks list" in text:
            return json.dumps([{"name": "pvc-1"}])
        if "storage buckets list" in text:
            return "bucket-a\n"
        return ""

    def mutations(self):
        return [" ".join(c) for c in self.calls if {"scale", "annotate", "resize", "label"} & set(c)]


def manifest(tmp_path, workloads):
    out = tmp_path / "m"
    out.mkdir()
    (out / "workloads.json").write_text(json.dumps([{"kind": k, "namespace": ns, "name": n, "replicas": r} for k, ns, n, r in workloads]))
    (out / "workload-requests.json").write_text("[]")
    (out / "disks-before.json").write_text(json.dumps([{"name": "pvc-1"}]))
    (out / "buckets-before.txt").write_text("bucket-a\n")
    return out


RECORDED = [("Deployment", "keda", "keda-operator", 1), ("Deployment", "keda", "keda-operator-metrics-apiserver", 1),
            ("Deployment", "ewallet-agents", "fraud-coordinator", 2), ("StatefulSet", "feature-store", "redis", 1),
            ("Deployment", "kube-system", "kube-dns", 2)]


def asleep():
    """The cluster as the 2026-11-24 guarded sleep left it: fraud-coordinator at 0, its ScaledObject paused."""
    return [workload("Deployment", "keda", "keda-operator", 1), workload("Deployment", "keda", "keda-operator-metrics-apiserver", 1),
            workload("Deployment", "ewallet-agents", "fraud-coordinator", 0), workload("StatefulSet", "feature-store", "redis", 1),
            workload("Deployment", "kube-system", "kube-dns", 2),
            workload("Deployment", "ewallet-sandbox", "business-agents-deployment", 2, owner="WorkerPool")]


def planned(capsys):
    """The commands a plan-mode wake printed, and the rest of its output."""
    out = capsys.readouterr().out
    return [line[len("PLAN: "):] for line in out.splitlines() if line.startswith("PLAN: ")], out


def held(cluster, plan):
    """Apply the planned scale commands to the fake cluster, as the API server would."""
    for call in plan:
        parts = call.split()
        if "scale" in parts:
            ns, name = parts[parts.index("-n") + 1], parts[parts.index("scale") + 2]
            for w in cluster.workloads:
                if (w["metadata"]["namespace"], w["metadata"]["name"]) == (ns, name):
                    w["spec"]["replicas"] = int(parts[-1].split("=")[1])


def wake_args(m, **kw):
    return argparse.Namespace(profile="vault-a2", manifest=m, nodes=2, timeout=5, execute=False,
                              confirm_cluster=lab.CLUSTER, **kw)


def test_the_partial_wake_holds_production_keeps_the_metrics_apiserver_and_keeps_keda_paused(tmp_path, capsys):
    m = manifest(tmp_path, RECORDED)
    cluster = Cluster(asleep(), sos=[so("ewallet-agents", "fraud-coordinator", "fraud-coordinator"),
                                     so("ewallet-sandbox", "business-agents-workerpool", "business-agents", kind="WorkerPool")])
    assert lab.wake(lab.Lab(False, "kc", cluster), wake_args(m), "experiment") == 0
    muts, out = planned(capsys)
    assert cluster.mutations() == [], "plan mode runs no mutation"
    scaled = [m.split(" -n ")[1] for m in muts if " scale " in m]
    assert scaled == ["feature-store scale statefulset redis --replicas=0", "keda scale deployment keda-operator --replicas=0"]
    assert any("--num-nodes=2" in m for m in muts)
    assert not any("annotate scaledobject" in m for m in muts), "no ScaledObject is unpaused by vault-a2"
    assert "stays paused" in out
    assert not any("kube-system" in m or "business-agents" in m for m in muts)


def test_a_full_wake_still_unpauses_every_cloud_lab_pause():
    cluster = Cluster(asleep(), sos=[so("ewallet-agents", "fraud-coordinator", "fraud-coordinator"),
                                     so("ewallet-sandbox", "business-agents-workerpool", "business-agents", kind="WorkerPool")])
    assert lab.unpause_keda(lab.Lab(False, "kc", cluster)) == ["ewallet-agents/fraud-coordinator",
                                                               "ewallet-sandbox/business-agents-workerpool"]


def test_record_manifest_refuses_the_partially_awake_cluster(tmp_path):
    """It would record the held-down zeros; the ScaledObjects the partial wake left paused make it refuse."""
    cluster = Cluster(asleep(), sos=[so("ewallet-agents", "fraud-coordinator", "fraud-coordinator")])
    with pytest.raises(lab.GateError, match="paused by a cloud-lab sleep"):
        lab.record_manifest(lab.Lab(False, "kc", cluster), tmp_path / "new", "vault-rehearsal-20261201T000000Z")
    assert not (tmp_path / "new").exists()


AVAILABLE = {"metadata": {"name": "v1beta1.external.metrics.k8s.io"}, "status": {"conditions": [{"type": "Available", "status": "True"}]}}


def verify(tmp_path, cluster, capsys):
    m = manifest(tmp_path, RECORDED)
    code = lab.cmd_verify(lab.Lab(False, "kc", cluster), argparse.Namespace(expect="awake", manifest=m, mode="experiment",
                                                                         profile="vault-a2"))
    return code, json.loads(capsys.readouterr().out)


def test_partial_verify_passes_on_the_held_cluster(tmp_path, capsys):
    cluster = Cluster(asleep(), sos=[so("ewallet-agents", "fraud-coordinator", "fraud-coordinator")], apiservices=[AVAILABLE])
    (tmp_path / "w").mkdir()
    lab.wake(lab.Lab(False, "kc", cluster), wake_args(manifest(tmp_path / "w", RECORDED)), "experiment")
    held(cluster, planned(capsys)[0])
    code, out = verify(tmp_path, cluster, capsys)
    assert (code, out["failures"], out["profile"]) == (0, [], "vault-a2")


def test_partial_verify_reports_a_revived_workload_a_down_apiservice_and_a_fail_closed_webhook(tmp_path, capsys):
    down = {"metadata": {"name": "v1beta1.external.metrics.k8s.io"},
            "status": {"conditions": [{"type": "Available", "status": "False", "reason": "MissingEndpoints"}]}}
    hook = {"metadata": {"name": "kserve"}, "webhooks": [
        {"name": "pods.kserve.io", "failurePolicy": "Fail", "clientConfig": {"service": {"namespace": "kserve", "name": "kserve-webhook"}},
         "rules": [{"apiGroups": [""], "resources": ["pods"]}]},
        {"name": "ignored.example", "failurePolicy": "Ignore", "clientConfig": {"service": {"namespace": "kserve", "name": "x"}},
         "rules": [{"apiGroups": [""]}]},
        {"name": "keda.sh-only", "clientConfig": {"service": {"namespace": "keda", "name": "keda-admission"}},
         "rules": [{"apiGroups": ["keda.sh"]}]}]}
    cluster = Cluster(asleep(), apiservices=[down], webhooks=[hook])   # fraud-coordinator 0; keda-operator and redis still 1
    code, out = verify(tmp_path, cluster, capsys)
    assert code == 1
    assert out["failures"] == [
        "keda/keda-operator not held down: replicas 1", "feature-store/redis not held down: replicas 1",
        "APIService v1beta1.external.metrics.k8s.io not Available",
        "webhook pods.kserve.io (MutatingWebhookConfiguration) fails closed and kserve/kserve-webhook has no ready endpoint"]


def test_a_webhook_with_a_ready_endpoint_does_not_block():
    hook = {"kind": "ValidatingWebhookConfiguration", "webhooks": [
        {"name": "w", "clientConfig": {"service": {"namespace": "cert-manager", "name": "cert-manager-webhook"}},
         "rules": [{"apiGroups": ["cert-manager.io"]}]}]}
    ready = {"metadata": {"namespace": "cert-manager", "labels": {"kubernetes.io/service-name": "cert-manager-webhook"}},
             "endpoints": [{"conditions": {"ready": True}}]}
    assert lab.blocking_webhooks([hook], [ready]) == []
    assert lab.blocking_webhooks([hook], []) != []


def test_full_verify_is_unchanged_by_the_partial_option(tmp_path, capsys):
    m = manifest(tmp_path, RECORDED)
    cluster = Cluster(asleep())
    code = lab.cmd_verify(lab.Lab(False, "kc", cluster), argparse.Namespace(expect="awake", manifest=m))
    assert code == 1 and json.loads(capsys.readouterr().out)["failures"] == ["ewallet-agents/fraud-coordinator replicas != 2"]
    assert not any("apiservices" in " ".join(c) for c in cluster.calls)


def test_diff_manifest_shows_what_the_last_sleep_changed_and_mutates_nothing(tmp_path, capsys):
    m = manifest(tmp_path, RECORDED)
    cluster = Cluster(asleep() + [workload("Deployment", "ewallet-vault", "stray", 1)])
    assert lab.cmd_diff_manifest(lab.Lab(False, "kc", cluster), argparse.Namespace(manifest=m)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["differs"] == [{"workload": "ewallet-agents/fraud-coordinator", "recorded": 2, "live": 0}]
    assert out["missing"] == [] and out["unrecorded"] == ["ewallet-vault/stray"]
    assert cluster.mutations() == []


class Live(Cluster):
    """A stateful fake: execute-mode scale and resize change what later reads return."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.nodes = 0

    def __call__(self, command):
        text = " ".join(command)
        if " scale " in f" {text} ":
            self.calls.append(command)
            ns, name = command[command.index("-n") + 1], command[command.index("scale") + 2]
            for w in self.workloads:
                if (w["metadata"]["namespace"], w["metadata"]["name"]) == (ns, name):
                    w["spec"]["replicas"] = int(command[-1].split("=")[1])
            return ""
        if "annotate scaledobject" in text and command[-1].endswith("-"):
            self.calls.append(command)
            name = command[command.index("scaledobject") + 1]
            for item in self.sos:
                if item["metadata"]["name"] == name:
                    item["metadata"]["annotations"] = {}
            return ""
        if "clusters resize" in text:
            self.calls.append(command)
            self.nodes = int(next(c for c in command if c.startswith("--num-nodes=")).split("=")[1])
            return ""
        if "get nodes" in text:
            ready = [{"metadata": {"name": f"n{i}"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                     for i in range(self.nodes)]
            return json.dumps({"items": ready}) if "-o json" in text else "\n".join(f"node/n{i}" for i in range(self.nodes))
        if "compute instances list" in text:
            return json.dumps([{"name": f"{lab.VM_PREFIX}{i}", "status": "RUNNING"} for i in range(self.nodes)])
        if "get pdb" in text or "get jobs" in text or "workflows.argoproj.io" in text:
            return json.dumps({"items": []})
        if "node-pools list" in text:
            return lab.NODE_POOL
        if "node-pools describe" in text or "clusters describe" in text:
            return "{}"
        if "dataproc batches" in text:
            return ""
        return super().__call__(command)


def test_the_two_node_campaign_transitions_end_asleep_with_only_held_down_zeros(tmp_path, capsys, monkeypatch):
    """T1 wake, verify, T3 plain sleep, T4 wake, verify, T6 plain sleep, diff: the offline dry run of the cloud-lab steps."""
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    monkeypatch.setattr(lab.time, "sleep", lambda s: None)
    m = manifest(tmp_path, RECORDED)
    paused = [so("ewallet-agents", "fraud-coordinator", "fraud-coordinator"),
              so("ewallet-sandbox", "business-agents-workerpool", "business-agents", kind="WorkerPool")]
    cluster = Live(asleep(), sos=paused, apiservices=[AVAILABLE])
    run = lab.Lab(True, "kc", cluster)
    awake = argparse.Namespace(expect="awake", manifest=m, mode="experiment", profile="vault-a2")
    plain = argparse.Namespace(scale_pdb_guarded=False, manifest=None, campaign=None, timeout=5, pause_keda=False,
                               also_approve=None, execute=True, confirm_cluster=lab.CLUSTER)
    for _ in range(2):                                    # T1/T3, then T4/T6
        assert lab.wake(run, argparse.Namespace(**{**vars(wake_args(m)), "execute": True}), "experiment") == 0
        assert cluster.nodes == 2
        assert lab.cmd_verify(run, awake) == 0, capsys.readouterr().out
        assert lab.cmd_sleep(run, plain) == 0, "SAFE_TO_SLEEP: no guarded or KEDA flag"
        assert cluster.nodes == 0
        assert lab.cmd_verify(run, argparse.Namespace(expect="sleep", manifest=m)) == 0
    capsys.readouterr()
    assert lab.cmd_diff_manifest(run, argparse.Namespace(manifest=m)) == 0
    diff = json.loads(capsys.readouterr().out)
    assert all(d["live"] == 0 for d in diff["differs"]) and diff["missing"] == []
    assert not any(" annotate " in f" {' '.join(c)} " for c in cluster.calls), "the ScaledObjects stayed paused"
    # The next full wake restores every recorded replica and unpauses them.
    full = argparse.Namespace(profile=None, manifest=m, nodes=5, timeout=5, execute=True, confirm_cluster=lab.CLUSTER)
    assert lab.wake(run, full, "full") == 0
    assert lab.cmd_verify(run, argparse.Namespace(expect="awake", manifest=m)) == 0, capsys.readouterr().out
    assert all(not item["metadata"]["annotations"] for item in cluster.sos)
    live = {f"{w['metadata']['namespace']}/{w['metadata']['name']}": w["spec"]["replicas"] for w in cluster.workloads}
    assert {k: live[k] for k in ("keda/keda-operator", "feature-store/redis", "ewallet-agents/fraud-coordinator")} == {
        "keda/keda-operator": 1, "feature-store/redis": 1, "ewallet-agents/fraud-coordinator": 2}
