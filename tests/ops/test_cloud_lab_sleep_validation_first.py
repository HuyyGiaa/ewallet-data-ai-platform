"""A refused sleep changes nothing: every gate runs before the first KEDA pause or scale.

Before 2026-11-22 the WorkerPool ScaledObjects were paused before the guarded-approval,
manifest, unexpected-PDB and selector checks, so a refusal there left them paused. No cloud
access: the fake runner serves reads and records every command.
"""

from __future__ import annotations

import pytest

from ops.gcp import cloud_lab as lab
from tests.ops.test_cloud_lab_guarded_sleep import (  # noqa: F401  (manifest is a fixture)
    KEDA_DEPLOY, KedaCluster, deployment, keda_args, manifest, pdb, scaled_object)

COORDINATOR = scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator")
POOL = {"metadata": {"namespace": "ewallet-sandbox", "name": "business-agents-workerpool", "annotations": {}},
        "spec": {"scaleTargetRef": {"kind": "WorkerPool", "name": "business-agents"}, "minReplicaCount": 2}}


def mutations(cluster):
    """Every command that would change the cluster: KEDA annotations, scales and the resize."""
    return [c for c in cluster.calls if "annotate" in c or "scale" in c or "resize" in c]


def env(monkeypatch, guarded=True, keda=True):
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    for name, on in ((lab.GUARDED_APPROVAL_ENV, guarded), (lab.KEDA_APPROVAL_ENV, keda)):
        if on:
            monkeypatch.setenv(name, f"{lab.CLUSTER}:agi-c1")
        else:
            monkeypatch.delenv(name, raising=False)


def cluster(blockers=None, deployments=None, pdb_states=None):
    blockers = blockers if blockers is not None else [pdb("ewallet-agents", "fraud-coordinator")]
    return KedaCluster(pdb_states or [blockers], deployments=deployments or [KEDA_DEPLOY],
                       scaled_objects=[COORDINATOR, POOL])


def refused(c, arguments, match):
    with pytest.raises(lab.GateError, match=match):
        lab.cmd_sleep(lab.Lab(True, "kc", c), arguments)
    return mutations(c)


def test_missing_guarded_approval_mutates_nothing(manifest, monkeypatch):
    env(monkeypatch, guarded=False)
    c = cluster()
    assert refused(c, keda_args(manifest, pause_keda=True), lab.GUARDED_APPROVAL_ENV) == []


def test_missing_keda_pause_approval_mutates_nothing(manifest, monkeypatch):
    env(monkeypatch, keda=False)
    c = cluster()
    assert refused(c, keda_args(manifest, pause_keda=True), lab.KEDA_APPROVAL_ENV) == []


def test_unexpected_pdb_blocker_mutates_nothing(manifest, monkeypatch):
    env(monkeypatch)
    extra = pdb("ewallet-mcp", "feature-rag-mcp")
    c = cluster(blockers=[pdb("ewallet-agents", "fraud-coordinator"), extra],
                deployments=[KEDA_DEPLOY, deployment("ewallet-mcp", "feature-rag-mcp")])
    assert refused(c, keda_args(manifest, pause_keda=True), "not in the approved manifest") == []


def test_invalid_recovery_manifest_mutates_nothing(tmp_path, monkeypatch):
    env(monkeypatch)
    c = cluster()  # tmp_path has no pdb-after-scale.txt
    assert refused(c, keda_args(tmp_path, pause_keda=True), "pdb-after-scale.txt") == []


@pytest.mark.parametrize("campaign", ["", "x", "bad id!", "-starts-with-dash"])
def test_invalid_campaign_id_mutates_nothing(manifest, monkeypatch, campaign):
    env(monkeypatch)
    monkeypatch.setenv(lab.GUARDED_APPROVAL_ENV, f"{lab.CLUSTER}:{campaign}")
    monkeypatch.setenv(lab.KEDA_APPROVAL_ENV, f"{lab.CLUSTER}:{campaign}")
    args = keda_args(manifest, pause_keda=True)
    args.campaign = campaign
    assert refused(cluster(), args, "campaign") == []


def test_invalid_pdb_selector_mutates_nothing(manifest, monkeypatch):
    env(monkeypatch)
    twin = deployment("ewallet-agents", "fraud-coordinator-twin", labels=KEDA_DEPLOY["spec"]["template"]["metadata"]["labels"])
    c = cluster(deployments=[KEDA_DEPLOY, twin])
    assert refused(c, keda_args(manifest, pause_keda=True), "expected exactly one") == []


def test_unpaused_workerpool_without_pause_flag_mutates_nothing(manifest, monkeypatch):
    env(monkeypatch)
    assert refused(cluster(), keda_args(manifest), "scale a WorkerPool") == []


def test_successful_preparation_pauses_both_then_scales_then_resizes(manifest, monkeypatch):
    env(monkeypatch)
    c = cluster(pdb_states=[[pdb("ewallet-agents", "fraud-coordinator")], [pdb("ewallet-agents", "fraud-coordinator", 0)]])
    assert lab.cmd_sleep(lab.Lab(True, "kc", c), keda_args(manifest, pause_keda=True)) == 0
    text = [" ".join(m) for m in mutations(c)]
    assert "annotate scaledobject business-agents-workerpool" in text[0] and f"{lab.KEDA_PAUSE}=2" in text[0]
    assert "annotate scaledobject fraud-coordinator" in text[1] and f"{lab.KEDA_PAUSE}=0" in text[1]
    assert text[2].endswith("scale deploy fraud-coordinator --replicas=0")
    assert "--num-nodes=0" in text[3] and len(text) == 4


class FailingCluster(KedaCluster):
    """Fails the commands that contain ``fail_on`` (and, optionally, the rollback ones)."""

    def __init__(self, *args, fail_on, fail_rollback=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on, self.fail_rollback = fail_on, fail_rollback

    def __call__(self, command):
        text = " ".join(command)
        rollback = f"{lab.KEDA_PAUSE}-" in text or "--replicas=2" in text
        if self.fail_on in text or (self.fail_rollback and rollback):
            self.calls.append(command)
            raise lab.subprocess.CalledProcessError(1, command)
        return super().__call__(command)


def failing(fail_on, fail_rollback=False):
    return FailingCluster([[pdb("ewallet-agents", "fraud-coordinator")], [pdb("ewallet-agents", "fraud-coordinator", 0)]],
                          deployments=[KEDA_DEPLOY], scaled_objects=[COORDINATOR, POOL],
                          fail_on=fail_on, fail_rollback=fail_rollback)


@pytest.mark.parametrize("fail_on", ["annotate scaledobject fraud-coordinator", "scale deploy fraud-coordinator --replicas=0",
                                     "--num-nodes=0"])
def test_a_mid_operation_failure_restores_what_was_changed(manifest, monkeypatch, fail_on):
    env(monkeypatch)
    c = failing(fail_on)
    with pytest.raises(lab.subprocess.CalledProcessError):
        lab.cmd_sleep(lab.Lab(True, "kc", c), keda_args(manifest, pause_keda=True))
    text = [" ".join(m) for m in mutations(c)]
    # Every pause that was applied is lifted; a Deployment is scaled back only if it was scaled.
    assert any("business-agents-workerpool" in t and f"{lab.KEDA_PAUSE}-" in t for t in text)
    if fail_on == "annotate scaledobject fraud-coordinator":
        assert not any("scale deploy" in t for t in text)
    else:
        assert any("annotate scaledobject fraud-coordinator" in t and f"{lab.KEDA_PAUSE}-" in t for t in text)
        assert text[-1].endswith("scale deploy fraud-coordinator --replicas=2")


def test_an_incomplete_rollback_is_reported_and_the_original_error_kept(manifest, monkeypatch, capsys):
    env(monkeypatch)
    c = failing("--num-nodes=0", fail_rollback=True)
    with pytest.raises(lab.subprocess.CalledProcessError) as error:
        lab.cmd_sleep(lab.Lab(True, "kc", c), keda_args(manifest, pause_keda=True))
    assert "--num-nodes=0" in " ".join(error.value.cmd)
    err = capsys.readouterr().err
    assert "ROLLBACK INCOMPLETE" in err and "business-agents-workerpool" in err and "fraud-coordinator" in err
