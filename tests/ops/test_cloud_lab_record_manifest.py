"""P2-B: the recovery manifest is recorded from the awake cluster right before each sleep.

Phase 1 (2026-11-16) woke with the 2026-11-06 manifest: it restored both MCP servers to 1
replica after PKG-P1 had made them 2, and ``verify --expect awake`` then failed on the
correct state. A manifest recorded before the sleep restores and verifies today's replicas,
and ``verify`` keeps its exact comparison. No cloud access: a fake runner serves the reads.
"""

from __future__ import annotations

import argparse
import json

import pytest

from ops.gcp import cloud_lab as lab

VM = lab.VM_PREFIX + "abcd"


def workload(kind, ns, name, replicas, ready=None):
    return {"kind": kind, "metadata": {"namespace": ns, "name": name},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [
                {"name": "main", "image": f"registry/{name}@sha256:" + "a" * 64,
                 "env": [{"name": "TOKEN", "value": "never-recorded"}]}]}}},
            "status": {"readyReplicas": replicas if ready is None else ready}}


def pod(ns, name, owner, cpu="100m", mem="128Mi"):
    return {"metadata": {"namespace": ns, "name": name, "ownerReferences": [{"kind": "ReplicaSet", "name": owner}]},
            "spec": {"containers": [{"resources": {"requests": {"cpu": cpu, "memory": mem}}}]},
            "status": {"phase": "Running"}}


class Cluster:
    def __init__(self, workloads, vms=(VM,), scaled_objects=(), pods=()):
        self.workloads, self.vms, self.scaled_objects, self.pods = list(workloads), list(vms), list(scaled_objects), list(pods)
        self.calls = []

    def __call__(self, command):
        self.calls.append(command)
        text = " ".join(command)
        if "compute instances list" in text:
            return json.dumps([{"name": vm, "status": "RUNNING"} for vm in self.vms])
        if "scaledobjects.keda.sh" in text:
            return json.dumps({"items": self.scaled_objects})
        if "get deploy,sts" in text:
            return json.dumps({"items": self.workloads})
        if "get pods -A" in text:
            return json.dumps({"items": self.pods})
        if "compute disks list" in text:
            return json.dumps([{"name": "pvc-1"}, {"name": "boot"}])
        if "storage buckets list" in text:
            return "bucket-b\nbucket-a\n"
        return ""

    def mutations(self):
        return [c for c in self.calls if {"scale", "annotate", "resize", "label"} & set(c)]


def live(*, mcp=2):
    return [workload("Deployment", "ewallet-mcp", "drift-mcp", mcp), workload("Deployment", "ewallet-mcp", "feature-rag-mcp", mcp),
            workload("Deployment", "ewallet-agents", "fraud-coordinator", 2), workload("StatefulSet", "ate-system", "valkey-cluster", 6)]


def record(cluster, out, campaign="p2-foundation-20261116T084657Z"):
    return lab.record_manifest(lab.Lab(False, "kc", cluster), out, campaign, "49290a0d", clock=lambda: 0)


def test_records_the_live_replicas_requests_disks_and_buckets_without_env(tmp_path):
    cluster = Cluster(live(), pods=[pod("ewallet-mcp", "drift-mcp-x-1", "drift-mcp-x")])
    manifest = record(cluster, tmp_path / "m")
    recorded, requests = lab.read_manifest(tmp_path / "m")
    assert {w.key: w.replicas for w in recorded} == {
        "ewallet-mcp/drift-mcp": 2, "ewallet-mcp/feature-rag-mcp": 2, "ewallet-agents/fraud-coordinator": 2,
        "ate-system/valkey-cluster": 6}
    assert requests == [{"namespace": "ewallet-mcp", "name": "drift-mcp", "pods": 1, "cpu_m": 100.0, "mem_mi": 128.0,
                         "max_pod_cpu_m": 100.0}]
    assert json.loads((tmp_path / "m/disks-before.json").read_text()) == [{"name": "pvc-1"}, {"name": "boot"}]
    assert (tmp_path / "m/buckets-before.txt").read_text().split() == ["bucket-a", "bucket-b"]
    assert "never-recorded" not in "".join(p.read_text() for p in (tmp_path / "m").iterdir())
    assert manifest["campaign"] == "p2-foundation-20261116T084657Z" and manifest["source_commit"] == "49290a0d"
    assert cluster.mutations() == [], "recording reads the cluster only"


def test_the_fresh_manifest_wakes_and_verifies_todays_mcp_replicas(tmp_path):
    record(Cluster(live(mcp=2)), tmp_path / "m")
    recorded, _ = lab.read_manifest(tmp_path / "m")
    # After the sleep the guarded Deployments are at 0; wake restores exactly the recorded 2.
    asleep = {w.key: 0 for w in recorded}
    assert sorted((w.key, r) for w, r in lab.restore_plan(recorded, asleep)) == [
        ("ate-system/valkey-cluster", 6), ("ewallet-agents/fraud-coordinator", 2),
        ("ewallet-mcp/drift-mcp", 2), ("ewallet-mcp/feature-rag-mcp", 2)]
    assert lab.restore_plan(recorded, {w.key: w.replicas for w in recorded}) == []


def test_verify_awake_keeps_the_exact_comparison(tmp_path, capsys):
    record(Cluster(live(mcp=2)), tmp_path / "m")

    def runner(command):
        text = " ".join(command)
        if "get deploy,sts" in text:
            return json.dumps({"items": live(mcp=1)})   # one MCP server short of the record
        return Cluster(live())(command)

    args = argparse.Namespace(expect="awake", manifest=tmp_path / "m")
    assert lab.cmd_verify(lab.Lab(False, "kc", runner), args) == 1
    failures = json.loads(capsys.readouterr().out)["failures"]
    assert failures == ["ewallet-mcp/drift-mcp replicas != 2", "ewallet-mcp/feature-rag-mcp replicas != 2"]


def test_a_sleeping_cluster_is_refused(tmp_path):
    with pytest.raises(lab.GateError, match="awake cluster"):
        record(Cluster(live(), vms=()), tmp_path / "m")
    assert not (tmp_path / "m").exists()


def test_a_sleep_preparation_in_progress_is_refused(tmp_path):
    paused = {"metadata": {"namespace": "ewallet-agents", "name": "fraud-coordinator",
                           "annotations": {lab.KEDA_PAUSE: "0", lab.PAUSED_BY: "p1-20261116T075841Z"}}}
    with pytest.raises(lab.GateError, match="paused by a cloud-lab sleep"):
        record(Cluster(live(), scaled_objects=[paused]), tmp_path / "m")


def test_an_existing_manifest_is_never_overwritten(tmp_path):
    (tmp_path / "m").mkdir()
    with pytest.raises(lab.GateError, match="never overwritten"):
        record(Cluster(live()), tmp_path / "m")


@pytest.mark.parametrize("campaign", ["", "x", "bad campaign", "../escape"])
def test_the_campaign_id_is_validated(tmp_path, campaign):
    with pytest.raises(lab.GateError):
        record(Cluster(live()), tmp_path / "m", campaign)


def test_not_ready_workloads_are_recorded_for_the_operator(tmp_path):
    workloads = live() + [workload("Deployment", "ewallet-agents", "drift-specialist", 2, ready=1)]
    manifest = record(Cluster(workloads), tmp_path / "m")
    assert manifest["not_ready_at_record"] == ["ewallet-agents/drift-specialist"]


def test_the_cli_exposes_record_manifest_as_a_read_only_command():
    parser = lab.build_parser()
    args = parser.parse_args(["record-manifest", "--out", "/tmp/x", "--campaign", "p2-foundation-20261116T084657Z"])
    assert args.command == "record-manifest"
    assert not hasattr(args, "execute"), "record-manifest has no --execute: it never mutates the cluster"


def scheduled(node, cpu, phase="Running"):
    return {"metadata": {"namespace": "a", "name": f"p-{node}-{cpu}"},
            "spec": {"nodeName": node, "containers": [{"resources": {"requests": {"cpu": cpu}}}]}, "status": {"phase": phase}}


def test_g3_label_goes_to_the_nodes_with_the_most_free_cpu():
    pods = [scheduled("n-a", "3900m"), scheduled("n-b", "3900m"), scheduled("n-c", "3200m"),
            scheduled("n-d", "900m"), scheduled("n-e", "1100m"), scheduled("n-a", "500m", phase="Succeeded")]

    def runner(command):
        return json.dumps({"items": pods}) if "get pods -A" in " ".join(command) else ""

    names = [f"node/n-{x}" for x in "abcde"]
    assert lab.g3_label_targets(lab.Lab(True, "kc", runner), names, 3) == ["node/n-d", "node/n-e", "node/n-c"]


def test_g3_label_plan_mode_and_ties_are_deterministic():
    names = ["node/n-b", "node/n-a"]
    assert lab.g3_label_targets(lab.Lab(False, "kc", lambda c: ""), names, 1) == ["node/n-b"], "plan mode reads nothing"
    runner = lambda command: json.dumps({"items": []})
    assert lab.g3_label_targets(lab.Lab(True, "kc", runner), names, 2) == ["node/n-a", "node/n-b"]
