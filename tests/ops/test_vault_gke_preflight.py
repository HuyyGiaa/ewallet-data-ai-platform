"""scripts/vault/gke_preflight.py: read-only gates, no PASS without runtime evidence, no values printed."""

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
SECRET_VALUE = "redis.feature-store.svc.cluster.local:6379,password=synthetic"


class Cluster:
    """Answers kubectl/helm argv like the asleep cloud lab; every command is recorded."""

    def __init__(self, *, nodes=0, namespaces=("ml-platform",), crds=("certificates.cert-manager.io", "issuers.cert-manager.io"),
                 releases=None, live=LIVE, head=LIVE, overlay=DEPLOY % "feature-api-redis-vault", secret=True):
        self.calls, self.nodes, self.namespaces, self.crds = [], nodes, namespaces, crds
        self.releases = releases if releases is not None else [{"name": "ewallet-serving", "namespace": "ml-platform", "revision": "3", "status": "deployed"}]
        self.live, self.head, self.overlay, self.secret = live, head, overlay, secret

    def __call__(self, argv):
        self.calls.append(argv)
        text = " ".join(argv)
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
        if "get deploy -n cert-manager" in text:
            return items([{"metadata": {"name": "cert-manager"}, "status": {}}])
        if "get storageclass" in text:
            return items([{"metadata": {"name": "standard-rwo"}, "provisioner": "pd.csi.storage.gke.io",
                           "volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Delete"}])
        if "get secret feast-online-redis" in text:
            if not self.secret:
                return 1, ""
            return 0, json.dumps({"data": {"connection_string": base64.b64encode(SECRET_VALUE.encode()).decode()}})
        if "get manifest" in text:
            return 0, self.live
        if "get values" in text:
            return 0, "featureApi: {enabled: true}\n"
        if argv[:2] == ["helm", "template"]:
            return 0, self.overlay if "values-vault-eso.yaml" in text else self.head
        raise AssertionError(f"unexpected command {argv}")


def run(tmp_path, cluster):
    preflight = pf.Preflight("/kubeconfig", "gke_ctx", cluster)
    preflight.scratch = str(tmp_path)
    return {g["gate"]: g for g in preflight.check()}, cluster


def test_asleep_clean_cluster_is_ready_and_pod_checks_stay_not_checked(tmp_path):
    gates, cluster = run(tmp_path, Cluster())
    assert gates["cert-manager-ready"]["status"] == "NOT_CHECKED" and gates["cert-manager-ready"]["observed"] == pf.PENDING
    assert all(g["status"] in ("PASS", "NOT_CHECKED") for g in gates.values())
    assert gates["serving-no-drift"]["status"] == "PASS" and gates["serving-overlay-scope"]["status"] == "PASS"
    assert SECRET_VALUE not in json.dumps(gates)


def test_every_command_is_read_only_and_pinned(tmp_path):
    _, cluster = run(tmp_path, Cluster())
    takes_value = {"--kubeconfig", "--context", "--kube-context", "-n", "-o"}
    for argv in cluster.calls:
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


@pytest.mark.parametrize("cluster,gate", [
    (Cluster(namespaces=("ml-platform", "ewallet-vault")), "no-prior-vault-eso-namespaces"),
    (Cluster(crds=("certificates.cert-manager.io", "issuers.cert-manager.io", "secretstores.external-secrets.io")), "no-prior-eso-crds"),
    (Cluster(head=DEPLOY % "something-else"), "serving-no-drift"),
    (Cluster(overlay=(DEPLOY % "feature-api-redis-vault").replace("connection_string", "other_key")), "serving-overlay-scope"),
    (Cluster(releases=[]), "serving-release"),
], ids=["prior-namespace", "prior-crds", "chart-drift", "overlay-too-wide", "no-release"])
def test_blocking_conditions(tmp_path, cluster, gate):
    gates, _ = run(tmp_path, cluster)
    assert gates[gate]["status"] == "BLOCKED"


def test_missing_redis_secret_and_cert_manager_fail(tmp_path):
    gates, _ = run(tmp_path, Cluster(secret=False, crds=(), nodes=2))
    assert gates["redis-secret"]["status"] == "FAIL" and gates["cert-manager-crds"]["status"] == "FAIL"
    assert gates["cert-manager-ready"]["status"] == "FAIL"     # awake: the runtime check now counts
