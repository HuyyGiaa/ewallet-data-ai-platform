"""The PDB-guarded sleep path is a separate owner approval and cannot be reached by accident.

No cloud access: a fake runner serves the cluster reads and records every command.
"""

from __future__ import annotations

import argparse
import json

import pytest

from ops.gcp import cloud_lab as lab

APPROVED = ("ewallet-agents/fraud-coordinator", "ewallet-mcp/drift-mcp")


def pdb(ns, name, expected=1, labels=None):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"minAvailable": 1, "selector": {"matchLabels": labels or {"app": name}}},
            "status": {"expectedPods": expected}}


def deployment(ns, name, labels=None):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"template": {"metadata": {"labels": labels or {"app": name, "tier": "x"}}}}}


class Cluster:
    """Fake runner. ``pdb_states`` is served in order; the last state repeats."""

    def __init__(self, pdb_states, deployments=(), jobs=(), pools=None):
        self.calls = []
        self.pdb_states = [list(state) for state in pdb_states]
        self.deployments, self.jobs = list(deployments), list(jobs)
        self.pools = pools or lab.NODE_POOL

    def __call__(self, command):
        self.calls.append(command)
        text = " ".join(command)
        if "get pdb" in text:
            state = self.pdb_states.pop(0) if len(self.pdb_states) > 1 else self.pdb_states[0]
            return json.dumps({"items": state})
        if "get deploy -A" in text:
            return json.dumps({"items": self.deployments})
        if "get jobs" in text:
            return json.dumps({"items": self.jobs})
        if "workflows.argoproj.io" in text:
            return json.dumps({"items": []})
        if "node-pools list" in text:
            return self.pools
        if "node-pools describe" in text:
            return "{}"
        return ""

    def mutations(self):
        return [c for c in self.calls if "resize" in c or "scale" in c]

    def scaled(self):
        return [(c[c.index("-n") + 1], c[c.index("deploy") + 1]) for c in self.calls if "scale" in c and "deploy" in c]

    def resized(self):
        return [c for c in self.calls if "resize" in c]


@pytest.fixture
def manifest(tmp_path):
    rows = ["NS N EXP CUR ALLOWED"] + [f"{key.split('/')[0]} {key.split('/')[1]} 0 0 0" for key in APPROVED]
    rows.append("kube-system filestore-lock-release-controller-pdb 1 1 1")
    (tmp_path / "pdb-after-scale.txt").write_text("\n".join(rows) + "\n")
    return tmp_path


@pytest.fixture
def approved_env(monkeypatch):
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    monkeypatch.setenv(lab.GUARDED_APPROVAL_ENV, f"{lab.CLUSTER}:agi-c1")


def args(**kw):
    defaults = {"execute": True, "confirm_cluster": lab.CLUSTER, "scale_pdb_guarded": False,
                "manifest": None, "campaign": None, "timeout": 0, "also_approve": None}
    return argparse.Namespace(**{**defaults, **kw})


def blocked():
    return [pdb(*key.split("/")) for key in APPROVED]


def released():
    return [pdb(*key.split("/"), expected=0) for key in APPROVED]


def deployments():
    return [deployment(*key.split("/")) for key in APPROVED]


def test_normal_safe_sleep_only_resizes(approved_env):
    cluster = Cluster([released()])
    assert lab.cmd_sleep(lab.Lab(True, "k", cluster), args()) == 0
    assert cluster.resized() and not cluster.scaled()


def test_pdb_blocker_without_the_flag_is_sleep_blocked(approved_env, manifest):
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match="SLEEP_BLOCKED"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


@pytest.mark.parametrize("approval", [None, "yes", f"{lab.CLUSTER}:other-campaign", f"other-cluster:agi-c1"])
def test_flag_without_the_exact_guarded_approval_mutates_nothing(monkeypatch, manifest, approval):
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    if approval is None:
        monkeypatch.delenv(lab.GUARDED_APPROVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(lab.GUARDED_APPROVAL_ENV, approval)
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match=lab.GUARDED_APPROVAL_ENV):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


@pytest.mark.parametrize("missing", ["manifest", "campaign"])
def test_flag_needs_manifest_and_campaign(approved_env, manifest, missing):
    kw = {"scale_pdb_guarded": True, "manifest": manifest, "campaign": "agi-c1", missing: None}
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match=f"--{missing}"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(**kw))
    assert not cluster.mutations()


def test_an_agi_campaign_id_is_accepted(monkeypatch, manifest):
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    monkeypatch.setenv(lab.GUARDED_APPROVAL_ENV, f"{lab.CLUSTER}:agi-20261115T000000Z")
    cluster = Cluster([blocked(), released()], deployments())
    assert lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest,
                                                            campaign="agi-20261115T000000Z", timeout=5)) == 0


def test_valid_approval_scales_the_selected_deployments_then_resizes(approved_env, manifest):
    cluster = Cluster([blocked(), released()], deployments())
    assert lab.cmd_sleep(lab.Lab(True, "k", cluster),
                         args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1", timeout=5)) == 0
    assert sorted(cluster.scaled()) == sorted(tuple(key.split("/")) for key in APPROVED)
    assert cluster.resized()
    last_scale = max(i for i, c in enumerate(cluster.calls) if "scale" in c and "deploy" in c)
    assert cluster.calls.index(cluster.resized()[0]) > last_scale


def test_unexpected_pdb_blocker_is_refused_before_any_mutation(approved_env, manifest):
    cluster = Cluster([blocked() + [pdb("ml-platform", "fraud-api")]], deployments() + [deployment("ml-platform", "fraud-api")])
    with pytest.raises(lab.GateError, match="not in the approved manifest.*fraud-api"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


def test_a_pdb_added_since_the_manifest_needs_an_explicit_also_approve(approved_env, manifest):
    new = (pdb("ewallet-mcp", "feature-rag-mcp"), deployment("ewallet-mcp", "feature-rag-mcp"))
    cluster = Cluster([blocked() + [new[0]], released()], deployments() + [new[1]])
    with pytest.raises(lab.GateError, match="not in the approved manifest"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()
    cluster = Cluster([blocked() + [new[0]], released()], deployments() + [new[1]])
    assert lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1",
                                                            also_approve=["ewallet-mcp/feature-rag-mcp"], timeout=5)) == 0
    assert ("ewallet-mcp", "feature-rag-mcp") in cluster.scaled() and cluster.resized()


@pytest.mark.parametrize("key", ["kube-system/coredns", "no-slash", "Upper/Case"])
def test_also_approve_rejects_system_and_malformed_names(approved_env, manifest, key):
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match="--also-approve"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1",
                                                         also_approve=[key]))
    assert not cluster.mutations()


@pytest.mark.parametrize("extra", [[], [deployment("ewallet-mcp", "drift-mcp-copy", {"app": "drift-mcp"})]])
def test_selector_must_select_exactly_one_deployment(approved_env, manifest, extra):
    items = [d for d in deployments() if d["metadata"]["name"] != "drift-mcp"] if not extra else deployments() + extra
    cluster = Cluster([blocked()], items)
    with pytest.raises(lab.GateError, match="expected exactly one"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


def test_active_work_blocks_even_with_the_guarded_approval(approved_env, manifest):
    job = {"metadata": {"namespace": "a", "name": "train"}, "status": {"active": 1}}
    cluster = Cluster([blocked()], deployments(), jobs=[job])
    with pytest.raises(lab.GateError, match="UNSAFE_TO_SLEEP"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


def test_wrong_cluster_identity_blocks(approved_env, manifest):
    cluster = Cluster([blocked()], deployments(), pools="cpu-pool\ngpu-pool")
    with pytest.raises(lab.GateError, match="unexpected node pools"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


def test_wrong_confirm_cluster_blocks(approved_env, manifest):
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(confirm_cluster="other", scale_pdb_guarded=True,
                                                         manifest=manifest, campaign="agi-c1"))
    assert not cluster.mutations()


def test_budgets_that_never_release_stop_before_the_resize(approved_env, manifest):
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match="still protect pods.*not resized"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1"))
    assert cluster.scaled() and not cluster.resized()


def test_repeated_sleep_after_preparation_is_a_plain_resize(approved_env, manifest):
    cluster = Cluster([released()], deployments())
    assert lab.cmd_sleep(lab.Lab(True, "k", cluster), args(manifest=manifest, campaign="agi-c1")) == 0
    assert cluster.resized() and not cluster.scaled()


def test_missing_manifest_record_refuses(approved_env, tmp_path):
    cluster = Cluster([blocked()], deployments())
    with pytest.raises(lab.GateError, match="pdb-after-scale.txt is missing"):
        lab.cmd_sleep(lab.Lab(True, "k", cluster), args(scale_pdb_guarded=True, manifest=tmp_path, campaign="agi-c1"))
    assert not cluster.mutations()


def test_plan_sleep_is_read_only_and_shows_guarded_coverage(manifest, capsys):
    cluster = Cluster([blocked() + [pdb("ml-platform", "fraud-api")]], deployments())
    lab.cmd_plan_sleep(lab.Lab(False, "k", cluster), argparse.Namespace(manifest=manifest))
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "SAFE_AFTER_REVERSIBLE_PREPARATION"
    assert report["guarded_preparation"]["covered_by_manifest"] == sorted(APPROVED)
    assert report["guarded_preparation"]["not_covered"] == ["ml-platform/fraud-api"]
    assert not cluster.mutations()


def test_failed_sleep_verification_is_reported(monkeypatch, manifest, capsys):
    (manifest / "disks-before.json").write_text(json.dumps([{"name": "pvc-1"}]))
    (manifest / "buckets-before.txt").write_text("bucket-a\n")

    def runner(command):
        text = " ".join(command)
        if "disks list" in text:
            return json.dumps([{"name": "pvc-1"}])
        if "buckets list" in text:
            return "bucket-a"
        if "instances list" in text:
            return f"{lab.VM_PREFIX}abc"
        return "{}"

    monkeypatch.setattr(lab.Lab, "pool_vms", lambda self: [f"{lab.VM_PREFIX}abc"])
    assert lab.cmd_verify(lab.Lab(False, "k", runner), argparse.Namespace(expect="sleep", manifest=manifest)) == 1
    assert json.loads(capsys.readouterr().out)["failures"]


def test_approved_sleep_manifest_in_repo_is_parseable():
    approved = lab.approved_guarded_pdbs(lab.Path("docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z"))
    assert approved == {"ewallet-agents/drift-mcp", "ewallet-agents/drift-specialist", "ewallet-agents/feature-rag-specialist",
                        "ewallet-agents/fraud-coordinator", "ewallet-mcp/drift-mcp"}


# --- KEDA-aware guarded sleep (2026-11-16: KEDA minReplicaCount restored the agents, SLEEP_BLOCKED) ---------

def scaled_object(ns, name, target, annotations=None):
    return {"metadata": {"namespace": ns, "name": name, "annotations": annotations or {}},
            "spec": {"scaleTargetRef": {"name": target}}}


class KedaCluster(Cluster):
    def __init__(self, *args, scaled_objects=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.scaled_objects = list(scaled_objects)

    def __call__(self, command):
        if "scaledobjects.keda.sh" in " ".join(command) and "get" in command:
            self.calls.append(command)
            return json.dumps({"items": self.scaled_objects})
        return super().__call__(command)

    def annotations(self):
        return [c[c.index("scaledobject") + 1:] for c in self.calls if "annotate" in c]


KEDA_DEPLOY = deployment("ewallet-agents", "fraud-coordinator")
KEDA_DEPLOY["spec"]["replicas"] = 2


def keda_args(manifest, **extra):
    return argparse.Namespace(scale_pdb_guarded=True, manifest=manifest, campaign="agi-c1", also_approve=None,
                              execute=True, confirm_cluster=lab.CLUSTER, timeout=1, **extra)


def keda_env(monkeypatch, keda=True):
    monkeypatch.setenv("CLOUD_LAB_APPROVED", "yes")
    monkeypatch.setenv(lab.GUARDED_APPROVAL_ENV, f"{lab.CLUSTER}:agi-c1")
    if keda:
        monkeypatch.setenv(lab.KEDA_APPROVAL_ENV, f"{lab.CLUSTER}:agi-c1")


def test_a_keda_controlled_guarded_deployment_is_refused_before_any_mutation(manifest, monkeypatch):
    keda_env(monkeypatch, keda=False)
    cluster = KedaCluster([[pdb("ewallet-agents", "fraud-coordinator")]], deployments=[KEDA_DEPLOY],
                          scaled_objects=[scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator")])
    with pytest.raises(lab.GateError, match="KEDA ScaledObjects"):
        lab.cmd_sleep(lab.Lab(True, "kc", cluster), keda_args(manifest))
    assert cluster.mutations() == [] and cluster.annotations() == []


def test_pause_needs_its_own_approval_token(manifest, monkeypatch):
    keda_env(monkeypatch, keda=False)
    cluster = KedaCluster([[pdb("ewallet-agents", "fraud-coordinator")]], deployments=[KEDA_DEPLOY],
                          scaled_objects=[scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator")])
    with pytest.raises(lab.GateError, match=lab.KEDA_APPROVAL_ENV):
        lab.cmd_sleep(lab.Lab(True, "kc", cluster), keda_args(manifest, pause_keda=True))
    assert cluster.mutations() == [] and cluster.annotations() == []


def test_approved_pause_happens_before_scaling_and_then_resizes(manifest, monkeypatch):
    keda_env(monkeypatch)
    cluster = KedaCluster([[pdb("ewallet-agents", "fraud-coordinator")], [pdb("ewallet-agents", "fraud-coordinator", 0)]],
                          deployments=[KEDA_DEPLOY],
                          scaled_objects=[scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator")])
    assert lab.cmd_sleep(lab.Lab(True, "kc", cluster), keda_args(manifest, pause_keda=True)) == 0
    text = [" ".join(c) for c in cluster.calls]
    pause = next(i for i, c in enumerate(text) if "annotate scaledobject fraud-coordinator" in c)
    scale = next(i for i, c in enumerate(text) if "scale deploy fraud-coordinator --replicas=0" in c)
    assert pause < scale and any("--num-nodes=0" in c for c in text)
    assert cluster.annotations()[0][:3] == ["fraud-coordinator", f"{lab.KEDA_PAUSE}=0", f"{lab.PAUSED_BY}=agi-c1"]


def test_a_failed_preparation_unpauses_and_restores_the_replicas(manifest, monkeypatch):
    keda_env(monkeypatch)
    monkeypatch.setattr(lab.time, "sleep", lambda seconds: None)
    cluster = KedaCluster([[pdb("ewallet-agents", "fraud-coordinator")]], deployments=[KEDA_DEPLOY],
                          scaled_objects=[scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator")])
    with pytest.raises(lab.GateError, match="still protect"):
        lab.cmd_sleep(lab.Lab(True, "kc", cluster), keda_args(manifest, pause_keda=True))
    text = [" ".join(c) for c in cluster.calls]
    assert any(f"{lab.KEDA_PAUSE}- {lab.PAUSED_BY}-" in c for c in text)
    assert text[-1].endswith("scale deploy fraud-coordinator --replicas=2")
    assert not any("--num-nodes=0" in c for c in text)


def test_wake_unpauses_only_what_cloud_lab_paused():
    cluster = KedaCluster([[]], scaled_objects=[
        scaled_object("ewallet-agents", "fraud-coordinator", "fraud-coordinator",
                      {lab.KEDA_PAUSE: "0", lab.PAUSED_BY: "agi-c1"}),
        scaled_object("ewallet-agents", "other", "other", {lab.KEDA_PAUSE: "1"})])
    resumed = lab.unpause_keda(lab.Lab(True, "kc", cluster))
    assert resumed == ["ewallet-agents/fraud-coordinator"]
    assert cluster.annotations() == [["fraud-coordinator", f"{lab.KEDA_PAUSE}-", f"{lab.PAUSED_BY}-"]]


def test_no_keda_installed_keeps_the_previous_behaviour():
    class NoKeda(Cluster):
        def __call__(self, command):
            if "scaledobjects.keda.sh" in " ".join(command):
                raise lab.subprocess.CalledProcessError(1, command)
            return super().__call__(command)
    assert lab.keda_controllers(lab.scaled_objects(lab.Lab(True, "kc", NoKeda([[]]))), {"a/b"}) == []
