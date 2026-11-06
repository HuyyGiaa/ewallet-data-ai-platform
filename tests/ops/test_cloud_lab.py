"""Tests for the on-demand cloud lab decisions, using no cloud access."""

from __future__ import annotations

import argparse
import json

import pytest

from ops.gcp import cloud_lab as lab


def pdb(ns, name, min_available=1, expected=1):
    """Build a minimal PodDisruptionBudget record."""
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"minAvailable": min_available},
            "status": {"expectedPods": expected}}


def test_min_available_pdb_blocks_even_when_disruptions_are_allowed():
    assert lab.evaluate_pdbs([pdb("a", "two", expected=2)]) == ["a/two"]


def test_scaled_down_pdb_and_max_unavailable_pdb_do_not_block():
    max_unavailable = {"metadata": {"namespace": "k", "name": "f"}, "spec": {"maxUnavailable": 1},
                       "status": {"expectedPods": 1}}
    assert lab.evaluate_pdbs([pdb("a", "zero", expected=0), max_unavailable]) == []


@pytest.mark.parametrize(
    ("blockers", "active", "identity", "verdict"),
    [
        ([], [], [], "SAFE_TO_SLEEP"),
        (["a/b"], [], [], "SAFE_AFTER_REVERSIBLE_PREPARATION"),
        (["a/b"], ["job x"], [], "UNSAFE_TO_SLEEP"),
        ([], [], ["autoscaling"], "UNSAFE_TO_SLEEP"),
    ],
)
def test_sleep_verdict(blockers, active, identity, verdict):
    assert lab.classify_sleep(blockers, active, identity) == verdict


RECORDED = [
    lab.Workload("Deployment", "ewallet-agents", "fraud-coordinator", 2),
    lab.Workload("Deployment", "ewallet-agents", "vllm-fallback", 0),
    lab.Workload("Deployment", "kagent", "kagent-controller", 1),
    lab.Workload("Deployment", "istio-system", "istiod", 1),
    lab.Workload("StatefulSet", "feature-store", "redis", 1),
]


def test_restore_returns_recorded_replicas_and_keeps_intentional_zero():
    current = {"ewallet-agents/fraud-coordinator": 0, "ewallet-agents/vllm-fallback": 0}
    assert [(w.name, n) for w, n in lab.restore_plan(RECORDED, current)] == [("fraud-coordinator", 2)]


def test_partial_restore_stays_inside_the_profile():
    current = {"ewallet-agents/fraud-coordinator": 0, "kagent/kagent-controller": 0}
    plan = lab.restore_plan(RECORDED, current, ["kagent/*"])
    assert [w.name for w, _ in plan] == ["kagent-controller"]


def test_partial_wake_holds_others_down_controllers_first_and_spares_system():
    current = {w.key: w.replicas for w in RECORDED}
    plan = lab.scale_down_plan(RECORDED, current, ["feature-store/redis"])
    assert [w.name for w in plan] == ["kagent-controller", "fraud-coordinator"]


def test_requests_ignore_daemonsets_and_group_replicas():
    def pod(name, owner_kind, owner, cpu="100m", memory="1Gi"):
        return {"metadata": {"namespace": "a", "name": name, "ownerReferences": [{"kind": owner_kind, "name": owner}]},
                "status": {"phase": "Running"},
                "spec": {"containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]}}

    rows = lab.summarize_requests([pod("x-1", "ReplicaSet", "x-abc"), pod("x-2", "ReplicaSet", "x-abc"),
                                   pod("d-1", "DaemonSet", "d")])
    assert rows == [{"namespace": "a", "name": "x", "pods": 2, "cpu_m": 200.0, "mem_mi": 2048.0, "max_pod_cpu_m": 100.0}]


def test_required_nodes_counts_system_pods_and_headroom():
    rows = [{"namespace": "kube-system", "name": "dns", "cpu_m": 500, "mem_mi": 500, "max_pod_cpu_m": 500},
            {"namespace": "app", "name": "big", "cpu_m": 2500, "mem_mi": 500, "max_pod_cpu_m": 2500},
            {"namespace": "app", "name": "other", "cpu_m": 9000, "mem_mi": 500, "max_pod_cpu_m": 100}]
    assert lab.required_nodes(rows, ["app/big"])["nodes"] == 2
    assert lab.required_nodes(rows, ["nothing"])["nodes"] == 1
    assert lab.required_nodes(rows, None)["nodes"] == 5


def test_hourly_estimate_matches_catalog_math():
    parts = lab.estimate_hourly(0, 73, 3442.08, 14.14)
    assert parts["nodes"] == 0
    assert parts["total_excluding_cluster_fee"] == pytest.approx(0.1072, abs=0.0002)
    assert lab.node_hourly() == pytest.approx(0.1653, abs=0.0001)


def test_sleep_verification_reports_lost_pvc_disk_and_bucket_but_not_boot_disk():
    failures = lab.sleep_failures(["vm"], {"other"}, {"pvc-1", "gke-boot"}, set(), {"b"})
    assert failures == ["pool VM remains: vm", "disk missing: pvc-1", "bucket missing: b"]


class Recorder:
    """Fake command runner that serves canned reads and records every call."""

    def __init__(self, pdbs=(), jobs=()):
        self.calls = []
        self.pdbs, self.jobs = list(pdbs), list(jobs)

    def __call__(self, command):
        self.calls.append(command)
        text = " ".join(command)
        if "get pdb" in text:
            return json.dumps({"items": self.pdbs})
        if "get jobs" in text:
            return json.dumps({"items": self.jobs})
        if "workflows.argoproj.io" in text:
            return json.dumps({"items": []})
        if "node-pools list" in text:
            return lab.NODE_POOL
        if "node-pools describe" in text:
            return "{}"
        return ""

    def resized(self):
        return [c for c in self.calls if "resize" in c]


def args(**kw):
    defaults = {"execute": False, "confirm_cluster": None, "scale_pdb_guarded": False}
    return argparse.Namespace(**{**defaults, **kw})


def test_sleep_refuses_pdb_bypass_without_the_owner_flag():
    runner = Recorder(pdbs=[pdb("a", "b")])
    with pytest.raises(lab.GateError, match="bypassed"):
        lab.cmd_sleep(lab.Lab(False, "k", runner), args())
    assert not runner.resized()


def test_sleep_refuses_active_work():
    job = {"metadata": {"namespace": "a", "name": "train"}, "status": {"active": 1}}
    runner = Recorder(jobs=[job])
    with pytest.raises(lab.GateError, match="UNSAFE"):
        lab.cmd_sleep(lab.Lab(False, "k", runner), args(scale_pdb_guarded=True))


def test_sleep_dry_run_mutates_nothing():
    runner = Recorder(pdbs=[pdb("a", "b")])
    lab.cmd_sleep(lab.Lab(False, "k", runner), args(scale_pdb_guarded=True))
    assert not runner.resized()
    assert not [c for c in runner.calls if "scale" in c]


def test_execute_requires_both_approvals(monkeypatch):
    monkeypatch.delenv("CLOUD_LAB_APPROVED", raising=False)
    with pytest.raises(lab.GateError):
        lab.require_approval(args(execute=True, confirm_cluster=lab.CLUSTER))
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    with pytest.raises(lab.GateError):
        lab.require_approval(args(execute=True, confirm_cluster="other"))
    lab.require_approval(args(execute=True, confirm_cluster=lab.CLUSTER))


def test_every_profile_name_resolves():
    profiles = json.loads(lab.PROFILES_PATH.read_text())
    for mode in ("minimal", "experiment"):
        for name in profiles[mode]:
            assert lab.wake_targets(argparse.Namespace(profile=name), mode)
    assert lab.wake_targets(argparse.Namespace(profile=None), "full") is None
    with pytest.raises(lab.GateError):
        lab.wake_targets(argparse.Namespace(profile="missing"), "minimal")
