"""Vault -> External Secrets Operator -> Kubernetes Secret: the declared contract, without a cluster.

Pins what the local end-to-end run (scripts/vault/eso_local_validate.sh) exercises live:
each namespace has its own SecretStore, reader ServiceAccount, Vault role and policy; a
policy reads exactly the paths its namespace's ExternalSecrets map; stores use HTTPS with
a pinned CA; ESO ignores cluster-wide stores; and the Feature API switches to the synced
Secret only through the opt-in overlay, never through the direct Vault loader.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "infra/kubernetes/vault"
ESO = ROOT / "infra/kubernetes/external-secrets"
BOOTSTRAP = json.loads((VAULT / "bootstrap.json").read_text(encoding="utf-8"))
NAMESPACES = {"ml-platform": ESO / "ml-platform.yaml", "ewallet-agents": ESO / "ewallet-agents.yaml"}
SELF_TOKEN_PATHS = {"auth/token/lookup-self", "auth/token/revoke-self"}
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def objects(path: Path) -> dict[str, dict]:
    return {doc["kind"]: doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc}


def grants(policy: str) -> dict[str, list[str]]:
    text = (ROOT / BOOTSTRAP["policies"][policy]).read_text(encoding="utf-8")
    found = re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text)
    return {path: re.findall(r'"(\w+)"', caps) for path, caps in found}


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_store_authenticates_with_its_own_identity_over_verified_https(namespace):
    docs = objects(NAMESPACES[namespace])
    account, store = docs["ServiceAccount"], docs["SecretStore"]
    assert account["metadata"] == {"name": "eso-vault-reader", "namespace": namespace, "labels": account["metadata"]["labels"]}
    assert account["automountServiceAccountToken"] is False
    vault = store["spec"]["provider"]["vault"]
    assert vault["server"] == "https://vault.ewallet-vault.svc.cluster.local:8200"
    assert vault["caProvider"] == {"type": "ConfigMap", "name": "vault-ca", "key": "ca.crt"}
    assert (vault["path"], vault["version"]) == (BOOTSTRAP["kv_mount"], "v2")
    assert "tls" not in vault and "caBundle" not in vault   # no inline CA, no client cert bypass
    auth = vault["auth"]["kubernetes"]
    assert auth["mountPath"] == BOOTSTRAP["kubernetes_auth_mount"]
    assert auth["serviceAccountRef"] == {"name": "eso-vault-reader", "audiences": [BOOTSTRAP["token_audience"]]}
    role = BOOTSTRAP["roles"][auth["role"]]
    assert auth["role"] == f"eso-{namespace}"
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == [namespace]
    assert role["audience"] == BOOTSTRAP["token_audience"]
    assert role["token_policies"] == [auth["role"]] and role["token_no_default_policy"] is True


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_policy_reads_exactly_the_mapped_paths(namespace):
    docs = objects(NAMESPACES[namespace])
    external = docs["ExternalSecret"]
    mapped = {f"{BOOTSTRAP['kv_mount']}/data/{item['remoteRef']['key']}" for item in external["spec"]["data"]}
    policy = grants(f"eso-{namespace}")
    readable = {path for path in policy if path not in SELF_TOKEN_PATHS}
    assert readable == mapped
    assert all(policy[path] == ["read"] for path in readable)
    assert policy["auth/token/lookup-self"] == ["read"] and policy["auth/token/revoke-self"] == ["update"]
    assert not any("*" in path or "+" in path or "/metadata/" in path for path in policy)
    assert "dataFrom" not in external["spec"]   # named keys only, never a whole record


def test_namespaces_cannot_read_each_others_paths():
    readable = {ns: set(grants(f"eso-{ns}")) - SELF_TOKEN_PATHS for ns in NAMESPACES}
    assert not readable["ml-platform"] & readable["ewallet-agents"]
    assert all(path.startswith("ewallet/data/feature-api/") for path in readable["ml-platform"])
    assert all(path.startswith("ewallet/data/agent-platform/") for path in readable["ewallet-agents"])


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_targets_are_new_secrets_never_existing_ones(namespace):
    spec = objects(NAMESPACES[namespace])["ExternalSecret"]["spec"]
    target = spec["target"]
    assert target["name"].endswith("-vault")
    assert target["creationPolicy"] == "Owner" and target["deletionPolicy"] == "Retain"
    existing = {"feast-online-redis", "agentgateway-client-key", "vllm-api-key", "redis"}
    assert target["name"] not in existing
    assert spec["secretStoreRef"] == {"kind": "SecretStore", "name": "vault"}


def test_eso_installation_processes_only_namespaced_stores_and_pins_images():
    values = yaml.safe_load((ESO / "values.yaml").read_text(encoding="utf-8"))
    for flag in ("processClusterStore", "processClusterExternalSecret", "processPushSecret", "processClusterPushSecret"):
        assert values[flag] is False
    for image in (values["image"], values["webhook"]["image"], values["certController"]["image"]):
        assert re.fullmatch(r"v2\.14\.0@sha256:[0-9a-f]{64}", image["tag"])
    assert yaml.safe_load((ESO / "namespace.yaml").read_text())["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_no_rbac_or_workload_uses_the_reader_identity():
    for path in list((ROOT / "infra").rglob("*.yaml")) + list((ROOT / "infra").rglob("*.tmpl")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "eso-vault-reader" in text:
            assert path.parent == ESO, path
            assert "serviceAccountName" not in text and "RoleBinding" not in text


def test_vault_stays_internal():
    for path in (ROOT / "infra").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "kind: HTTPRoute" in text or "kind: VirtualService" in text:
            assert "ewallet-vault" not in text and "vault." not in text, path
    values = yaml.safe_load((VAULT / "values-gke.yaml").read_text(encoding="utf-8"))
    assert values["ui"]["enabled"] is False and "service" not in values["server"]


@needs_helm
def test_overlay_switches_the_feature_api_to_the_synced_secret_without_the_direct_loader():
    image = "asia-southeast1-docker.pkg.dev/p/r/fraud-api@sha256:" + "a" * 64
    argv = ["helm", "template", "s", str(ROOT / "infra/helm/ewallet-serving"),
            "-f", str(ROOT / "infra/helm/ewallet-serving/values-vault-eso.yaml"),
            "--set", "featureApi.enabled=true", "--set", f"featureApi.image={image}",
            "--set", "featureApi.registryUri=gs://b/r.db",
            "--set", "featureApi.serviceAccount.gcpServiceAccount=ewallet-feature-api@p-1234.iam.gserviceaccount.com"]
    rendered = list(yaml.safe_load_all(subprocess.run(argv, check=True, capture_output=True, text=True).stdout))
    pod = next(o for o in rendered if o and o["kind"] == "Deployment")["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"] == {
        "name": "feature-api-redis-vault", "key": "connection_string"}
    assert not any(name.startswith("EWALLET_VAULT_") for name in env)
    assert not any(v["name"].startswith("vault") for v in pod.get("volumes", []))
    synced = objects(ESO / "ml-platform.yaml")["ExternalSecret"]["spec"]
    assert synced["target"]["name"] == "feature-api-redis-vault"
    assert [item["secretKey"] for item in synced["data"]] == ["connection_string"]
