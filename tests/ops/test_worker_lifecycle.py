"""P2-A R1/R2 harness: assertions come from recorded observations; mutations are gated and R2 always
restores ateapi. No cloud access: a fake cluster serves kubectl and the kagent status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.experiments import worker_lifecycle as wl

CAMPAIGN = "p2a-closure-20261116T151631Z"


def pod(name, uid, ready=True, deleting=False):
    return {"name": name, "uid": uid, "node": "n1", "phase": "Running", "ready": ready, "deleting": deleting, "created": "t"}


def snap(pods, workers, actors=(), ateapi_pods=1, label="s", t="t"):
    return {"label": label, "time": t, "pods": pods,
            "ateapi": {"spec_replicas": ateapi_pods, "ready": ateapi_pods, "pods": [{"name": "a"}] * ateapi_pods},
            "registry": {"workers": [{"pod": w, "pool": wl.POOL, "ip": "x", "assigned": a, "actor": "x" if a else None}
                                     for w, a in workers],
                         "actors": list(actors)}}


A, B, C = "business-agents-deployment-1-a", "business-agents-deployment-1-b", "business-agents-deployment-1-c"


def test_consistency_and_idle_selection():
    s = snap([pod(A, "ua"), pod(B, "ub")], [(A, False), (B, True), ("business-agents-gone", False)],
             actors=[{"id": "x", "status": "Running", "pod": B}, {"id": "y", "status": "Suspended", "pod": None}])
    c = wl.consistency(s)
    assert c["stale_records"] == ["business-agents-gone"] and c["unregistered_ready_pods"] == [] and c["busy_workers"] == [B]
    assert wl.idle_candidates(s) == [A]
    s2 = snap([pod(A, "ua")], [(A, False)], actors=[{"id": "z", "status": "Paused", "pod": A}])
    assert wl.idle_candidates(s2) == [], "a paused actor keeps node-local state: not idle"


def test_assessment_r1_pass_and_stale_window_reported():
    t = [snap([pod(A, "ua"), pod(B, "ub")], [(A, False), (B, False)], t="t0"),
         snap([pod(B, "ub"), pod(C, "uc", ready=False)], [(A, False), (B, False)], t="t1"),
         snap([pod(B, "ub"), pod(C, "uc")], [(B, False), (C, False)], t="t2")]
    r = wl.assess("r1", t, A, "ua")
    assert r["verdict_inputs_ok"] and r["replacement_distinct_uid"] and r["registry_converged"]
    assert r["stale_record_window"] == {"first": "t1", "last": "t1", "samples": 1}
    assert r["replacement_registered_at"] == "t2"


def test_assessment_fails_when_registry_does_not_converge_or_r2_not_exercised():
    t = [snap([pod(A, "ua"), pod(B, "ub")], [(A, False), (B, False)]),
         snap([pod(B, "ub"), pod(C, "uc")], [(A, False), (B, False), (C, False)])]
    assert not wl.assess("r1", t, A, "ua")["verdict_inputs_ok"], "stale record at the end"
    ok = [t[0], snap([pod(B, "ub"), pod(C, "uc")], [(B, False), (C, False)])]
    r2 = wl.assess("r2", ok, A, "ua")
    assert not r2["ateapi_had_no_pod"] and not r2["verdict_inputs_ok"], "R2 must prove the deletion happened while ateapi was down"


class FakeCluster(wl.Cluster):
    """Scripted states; records mutations."""

    def __init__(self, states, fail_on=None):
        super().__init__("ctx", "http://k")
        self.states, self.i, self.mutations, self.fail_on = states, 0, [], fail_on

    def snapshot(self, label):
        s = dict(self.states[min(self.i, len(self.states) - 1)], label=label)
        self.i += 1
        return s

    def kubectl(self, *args):
        self.mutations.append(" ".join(args))
        if self.fail_on and self.fail_on in " ".join(args):
            raise RuntimeError("injected kubectl failure")
        return ""


def idle_state():
    return snap([pod(A, "ua"), pod(B, "ub")], [(A, False), (B, False)])


def test_mutations_need_their_own_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("P2A_CAMPAIGN", CAMPAIGN)
    monkeypatch.setenv("P2A_APPROVAL", f"R1:{CAMPAIGN}")
    with pytest.raises(wl.Refused):
        wl.run_r2(FakeCluster([idle_state()]), tmp_path, A, "ua", 60, 1)
    monkeypatch.delenv("P2A_APPROVAL")
    cluster = FakeCluster([idle_state()])
    with pytest.raises(wl.Refused):
        wl.run_r1(cluster, tmp_path, A, "ua", 1)
    assert cluster.mutations == []


def test_busy_or_wrong_target_is_refused_before_any_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("P2A_CAMPAIGN", CAMPAIGN)
    monkeypatch.setenv("P2A_APPROVAL", f"R1:{CAMPAIGN}")
    busy = FakeCluster([snap([pod(A, "ua"), pod(B, "ub")], [(A, True), (B, False)])])
    with pytest.raises(wl.Refused, match="not idle"):
        wl.run_r1(busy, tmp_path, A, "ua", 1)
    wrong_uid = FakeCluster([idle_state()])
    with pytest.raises(wl.Refused, match="not a live worker"):
        wl.run_r1(wrong_uid, tmp_path, A, "other", 1)
    alone = FakeCluster([snap([pod(A, "ua")], [(A, False)])])
    with pytest.raises(wl.Refused, match="2 Ready workers"):
        wl.run_r1(alone, tmp_path, A, "ua", 1)
    assert busy.mutations == wrong_uid.mutations == alone.mutations == []


def test_r1_deletes_gracefully_and_records_the_transition(monkeypatch, tmp_path):
    monkeypatch.setenv("P2A_CAMPAIGN", CAMPAIGN)
    monkeypatch.setenv("P2A_APPROVAL", f"R1:{CAMPAIGN}")
    end = snap([pod(B, "ub"), pod(C, "uc")], [(B, False), (C, False)])
    cluster = FakeCluster([idle_state(), end])
    assert wl.run_r1(cluster, tmp_path, A, "ua", 5) == 0
    assert cluster.mutations == [f"-n {wl.SANDBOX_NS} delete pod {A} --wait=false"], "graceful, no --force"
    assert json.loads((tmp_path / "assessment.json").read_text())["verdict_inputs_ok"]


def test_r2_order_and_ateapi_always_restored(monkeypatch, tmp_path):
    monkeypatch.setenv("P2A_CAMPAIGN", CAMPAIGN)
    monkeypatch.setenv("P2A_APPROVAL", f"R2:{CAMPAIGN}")
    down = snap([pod(A, "ua"), pod(B, "ub")], [(A, False), (B, False)], ateapi_pods=0)
    gone = snap([pod(B, "ub")], [(A, False), (B, False)], ateapi_pods=0)
    end = snap([pod(B, "ub"), pod(C, "uc")], [(B, False), (C, False)])
    cluster = FakeCluster([idle_state(), down, gone, end])
    assert wl.run_r2(cluster, tmp_path, A, "ua", 60, 5) == 0
    assert cluster.mutations == [f"-n {wl.ATE_NS} scale deployment {wl.ATEAPI} --replicas=0",
                                 f"-n {wl.SANDBOX_NS} delete pod {A} --wait=false",
                                 f"-n {wl.ATE_NS} scale deployment {wl.ATEAPI} --replicas=1"]
    r = json.loads((tmp_path / "assessment.json").read_text())
    assert r["ateapi_had_no_pod"] and r["target_deleted_while_ateapi_down"] and r["verdict_inputs_ok"]

    failing = FakeCluster([idle_state(), down], fail_on="delete pod")
    with pytest.raises(RuntimeError):
        (tmp_path / "f").mkdir(); wl.run_r2(failing, tmp_path / "f", A, "ua", 60, 1)
    assert failing.mutations[-1] == f"-n {wl.ATE_NS} scale deployment {wl.ATEAPI} --replicas=1", "ateapi restored on failure"
