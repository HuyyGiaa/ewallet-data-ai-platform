"""scripts/vault/gke_preflight.py: two modes, read-only, fail-closed, never prints Secret values."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/vault"))
import gke_preflight as pf  # noqa: E402

DEPLOY = """kind: Deployment
metadata: {name: feature-api, namespace: ml-platform}
spec:
  template:
    spec:
      containers:
        - env:
            - name: EWALLET_REDIS_CONNECTION_STRING
              valueFrom: {secretKeyRef: {key: connection_string, name: %s}}
"""
LIVE = DEPLOY % "feast-online-redis"
VALUE = "redis.feature-store.svc.cluster.local:6379,password=synthetic-preflight"
B64 = base64.b64encode(VALUE.encode()).decode()


def secret(name, owners=None, value=B64):
    return {"metadata": {"name": name, "ownerReferences": owners or []}, "data": {"connection_string": value}}


class Cluster:
    """Answers kubectl/helm argv. ``fail``/``invalid`` hold substrings whose commands fail or return junk."""

    def __init__(self, *, nodes=0, namespaces=("ml-platform",), crds=("certificates.cert-manager.io", "issuers.cert-manager.io"),
                 releases=None, live=LIVE, head=LIVE, overlay=DEPLOY % "feast-online-redis-vault", objects=None,
                 shadow=(0, "SHADOW_MATCH\n"), fail=(), invalid=(), pvs=(), cluster_objects=()):
        self.calls, self.nodes, self.namespaces, self.crds = [], nodes, namespaces, crds
        self.releases = releases if releases is not None else [
            {"name": "ewallet-serving", "namespace": "ml-platform", "revision": "3", "status": "deployed"}]
        self.live, self.head, self.overlay, self.shadow, self.fail, self.invalid = live, head, overlay, shadow, fail, invalid
        self.pvs, self.cluster_objects = pvs, cluster_objects
        self.objects = objects if objects is not None else {"secret feast-online-redis": secret("feast-online-redis")}

    def __call__(self, argv):
        self.calls.append(argv)
        text = " ".join(argv)
        if any(f in text for f in self.fail):
            return 1, ""
        if any(f in text for f in self.invalid):
            return 0, "{not json"
        items = lambda xs: (0, json.dumps({"items": xs}))
        if " version " in f" {text} ":
            return 0, json.dumps({"serverVersion": {"gitVersion": "v1.36.4-gke.2377000"}})
        if "get nodes" in text:
            return items([{"metadata": {"name": f"n{i}"}} for i in range(self.nodes)])
        if "get namespaces" in text:
            return items([{"metadata": {"name": n}} for n in self.namespaces])
        if "get crd" in text:
            return items([{"metadata": {"name": n}} for n in self.crds])
        if argv[0] == "helm" and " list " in f" {text} ":
            return 0, json.dumps(self.releases)
        if "get pvc" in text:
            return items([])
        if "get pv " in f"{text} ":
            return items(list(self.pvs))
        if "get clusterrole," in text:
            return items(list(self.cluster_objects))
        if "get deploy -n cert-manager" in text or "get deploy -n external-secrets" in text:
            return items([{"metadata": {"name": f"d{i}"}, "status": {"availableReplicas": 1}} for i in range(3)])
        if "get storageclass" in text:
            return items([{"metadata": {"name": "standard-rwo"}, "provisioner": "pd.csi.storage.gke.io",
                           "volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Delete"}])
        for key, obj in self.objects.items():
            if f"get {key} " in text + " ":
                return 0, json.dumps(obj)
        if "--ignore-not-found" in argv:
            return 0, ""                                   # confirmed absent
        if "shadow_compare.sh" in text:
            return self.shadow
        if "get manifest" in text:
            return 0, self.live
        if "get values" in text:
            return 0, "featureApi: {enabled: true}\n"
        if argv[:2] == ["helm", "template"]:
            return 0, self.overlay if "values-vault-eso.yaml" in text else self.head
        raise AssertionError(f"unexpected command {argv}")


def run(tmp_path, cluster, mode="preinstall"):
    gates = pf.Preflight("/kubeconfig", "gke_ctx", cluster, scratch=str(tmp_path)).check(mode)
    output = json.dumps(gates)
    assert VALUE not in output and B64 not in output
    return {g["gate"]: g for g in gates}, pf.verdict(gates)


# A Retained Vault disk left after its PVC and namespace were deleted.
RELEASED_PV = {"metadata": {"name": "pvc-0123"}, "status": {"phase": "Released"},
               "spec": {"claimRef": {"namespace": "ewallet-vault", "name": "data-vault-0"}}}


def switch_ready(**changes):
    """An awake cluster after G7: Vault and ESO installed, synced, SHADOW_MATCH, Feature API on the old Secret."""
    objects = {
        "secret feast-online-redis": secret("feast-online-redis"),
        "secret feast-online-redis-vault": secret("feast-online-redis-vault", [{"kind": "ExternalSecret", "name": "feast-online-redis"}]),
        "externalsecret feast-online-redis": {"metadata": {"name": "feast-online-redis"},
                                             "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "SecretSynced"}]}},
        "deploy feature-api": {"metadata": {"name": "feature-api"}, "status": {"availableReplicas": 1},
                               "spec": {"template": {"spec": {"containers": [{"env": [{"name": "EWALLET_REDIS_CONNECTION_STRING",
                                        "valueFrom": {"secretKeyRef": {"name": "feast-online-redis", "key": "connection_string"}}}]}]}}}},
    }
    objects.update(changes.pop("objects", {}))
    releases = [{"name": "ewallet-serving", "namespace": "ml-platform", "revision": "3", "status": "deployed"},
                {"name": "vault", "namespace": "ewallet-vault", "revision": "1", "status": "deployed"},
                {"name": "external-secrets", "namespace": "external-secrets", "revision": "1", "status": "deployed"}]
    changes.setdefault("nodes", 5)
    return Cluster(namespaces=("ml-platform", "ewallet-vault", "external-secrets"),
                   crds=("secretstores.external-secrets.io", "certificates.cert-manager.io"), releases=releases,
                   objects=objects, **changes)


# ---- preinstall
def test_preinstall_on_the_asleep_clean_cluster_is_ready_with_pod_checks_not_checked(tmp_path):
    gates, result = run(tmp_path, Cluster())
    assert result["verdict"] == "VAULT_GKE_PREFLIGHT_READY" and result["not_checked"] == ["cert-manager-ready"]
    assert gates["no-prior-vault-eso-namespaces"]["observed"] == "confirmed absent"


@pytest.mark.parametrize("cluster,gate", [
    (Cluster(namespaces=("ml-platform", "ewallet-vault")), "no-prior-vault-eso-namespaces"),
    (Cluster(crds=("certificates.cert-manager.io", "issuers.cert-manager.io", "secretstores.external-secrets.io")), "no-prior-eso-crds"),
    (Cluster(head=DEPLOY % "something-else"), "serving-no-drift"),
    (Cluster(overlay=(DEPLOY % "feast-online-redis-vault").replace("connection_string", "other_key")), "serving-overlay-scope"),
    (Cluster(releases=[]), "serving-release"),
    (Cluster(namespaces=("ml-platform", "vault-rehearsal")), "no-prior-vault-eso-namespaces"),
    (Cluster(pvs=[RELEASED_PV]), "no-prior-vault-volumes"),
    (Cluster(cluster_objects=[{"kind": "ClusterRoleBinding", "metadata": {"name": "vault-server-binding"}}]),
     "no-prior-chart-cluster-objects"),
], ids=["prior-namespace", "prior-crds", "chart-drift", "overlay-too-wide", "no-release",
        "rehearsal-namespace-left", "retained-vault-pv", "chart-cluster-object-left"])
def test_preinstall_blocking_conditions(tmp_path, cluster, gate):
    gates, result = run(tmp_path, cluster)
    assert gates[gate]["status"] == "BLOCKED" and result["verdict"] == "VAULT_GKE_PREFLIGHT_BLOCKED"


# ---- fail closed: a failed or invalid query is never PASS or "absent"
@pytest.mark.parametrize("broken,gates_hit", [
    ("get namespaces", ["no-prior-vault-eso-namespaces"]),
    ("get crd", ["no-prior-eso-crds", "cert-manager-crds"]),
    ("get pvc", ["no-prior-vault-data"]),
    ("get pv -o", ["no-prior-vault-volumes"]),
    ("get clusterrole,", ["no-prior-chart-cluster-objects"]),
    (" list -A", ["no-prior-vault-eso-releases", "serving-release"]),
    ("get manifest", ["serving-no-drift", "serving-overlay-scope"]),
    ("get values", ["serving-no-drift", "serving-overlay-scope"]),
    ("helm template", ["serving-no-drift", "serving-overlay-scope"]),
    ("get secret feast-online-redis", ["redis-secret"]),
    ("get storageclass", ["storage-class"]),
])
@pytest.mark.parametrize("how", ["fail", "invalid"])
def test_query_failure_or_junk_is_fail_never_pass(tmp_path, broken, gates_hit, how):
    if how == "invalid" and broken == "get values":
        pytest.skip("junk values are rejected by the real helm template, which this fake does not run")
    gates, result = run(tmp_path, Cluster(**{how: (broken,)}))
    for gate in gates_hit:
        assert gates[gate]["status"] == "FAIL", gates[gate]
        assert gates[gate]["observed"].startswith((pf.QUERY_FAILED, pf.INVALID)), gates[gate]
    assert result["verdict"] == "VAULT_GKE_PREFLIGHT_BLOCKED"


def test_empty_helm_output_is_invalid_not_identical(tmp_path):
    gates, _ = run(tmp_path, Cluster(live="", head=""))
    assert gates["serving-no-drift"]["status"] == "FAIL" and pf.INVALID in gates["serving-no-drift"]["observed"]


def test_unreachable_cluster_stops_with_fail(tmp_path):
    gates, result = run(tmp_path, Cluster(fail=("version",)))
    assert list(gates) == ["cluster-reachable"] and gates["cluster-reachable"]["status"] == "FAIL"


# ---- consumer-switch
def test_consumer_switch_accepts_installed_vault_and_eso(tmp_path):
    gates, result = run(tmp_path, switch_ready(), "consumer-switch")
    assert result["verdict"] == "VAULT_GKE_PREFLIGHT_READY", result
    assert "no-prior-vault-eso-namespaces" not in gates and gates["shadow-match"]["observed"] == "SHADOW_MATCH"


@pytest.mark.parametrize("change,gate", [
    ({"nodes": 0}, "node-pool"),
    ({"shadow": (1, "SHADOW_MISMATCH\n")}, "shadow-match"),
    ({"shadow": (1, "SHADOW_SHADOW_READ_FAILED\n")}, "shadow-match"),
    ({"objects": {"externalsecret feast-online-redis": {"metadata": {"name": "x"}, "status": {"conditions": [
        {"type": "Ready", "status": "False", "reason": "SecretSyncedError"}]}}}}, "externalsecret-synced"),
    ({"objects": {"secret feast-online-redis-vault": secret("feast-online-redis-vault")}}, "target-secret"),   # not ESO-owned
    ({"objects": {"deploy feature-api": {"metadata": {"name": "feature-api"}, "status": {"availableReplicas": 0},
        "spec": {"template": {"spec": {"containers": [{"env": []}]}}}}}}, "feature-api-before-switch"),
], ids=["asleep", "mismatch", "read-failed", "not-synced", "target-not-owned", "feature-api-unavailable"])
def test_consumer_switch_blocks_unsafe_states(tmp_path, change, gate):
    gates, result = run(tmp_path, switch_ready(**change), "consumer-switch")
    assert gates[gate]["status"] in ("BLOCKED", "FAIL") and result["verdict"] == "VAULT_GKE_PREFLIGHT_BLOCKED"


def test_consumer_switch_query_failure_is_fail(tmp_path):
    gates, _ = run(tmp_path, switch_ready(fail=("get externalsecret",)), "consumer-switch")
    assert gates["externalsecret-synced"]["status"] == "FAIL" and gates["externalsecret-synced"]["observed"].startswith(pf.QUERY_FAILED)


def test_consumer_switch_passes_the_pinned_kubeconfig_to_shadow_compare(tmp_path):
    cluster = switch_ready()
    run(tmp_path, cluster, "consumer-switch")
    call = next(c for c in cluster.calls if "shadow_compare.sh" in " ".join(c))
    assert call[:2] == ["env", "KUBECONFIG=/kubeconfig"] and "gke_ctx" in call


# ---- read-only
@pytest.mark.parametrize("mode,cluster", [("preinstall", Cluster()), ("consumer-switch", switch_ready())])
def test_every_command_is_read_only_and_pinned(tmp_path, mode, cluster):
    run(tmp_path, cluster, mode)
    takes_value = {"--kubeconfig", "--context", "--kube-context", "-n", "-o"}
    for argv in cluster.calls:
        if "shadow_compare.sh" in " ".join(argv):
            continue                                       # read-only by its own tests
        rest, verb = argv[1:], ""
        while rest:
            token = rest.pop(0)
            if token in takes_value:
                rest.pop(0)
            elif not token.startswith("-"):
                verb = token
                break
        assert verb in ("version", "get", "list", "template"), argv
        if verb != "template":
            assert "gke_ctx" in argv and "/kubeconfig" in argv, argv


def test_unrelated_volumes_and_cluster_objects_do_not_block(tmp_path):
    other = {"metadata": {"name": "pvc-redis"}, "status": {"phase": "Bound"},
             "spec": {"claimRef": {"namespace": "feature-store", "name": "data-redis-0"}}}
    unclaimed = {"metadata": {"name": "pv-static"}, "status": {"phase": "Available"}, "spec": {}}
    gates, result = run(tmp_path, Cluster(pvs=[other, unclaimed]))
    assert gates["no-prior-vault-volumes"]["status"] == "PASS" and gates["no-prior-chart-cluster-objects"]["status"] == "PASS"
    assert result["verdict"] == "VAULT_GKE_PREFLIGHT_READY"
