"""R2: the Fraud API, the Feature API and MLflow each run as their own identity.

Pins the source contract: separate Kubernetes ServiceAccounts with no mounted API
token and no RBAC, and GCP Workload Identity mappings to GSAs that read only the
online Feast registry prefix.

RETIRED IN VAULT V2 (docs/edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md §3, decision O4): the
direct-loader Vault roles fraud-api and feature-api, their two policies, and their NetworkPolicy
entries. No deployed manifest used them. Their invariant ("each serving pod reads only its own Vault
path") is replaced by a stronger one tested below: no serving account can obtain a Vault token at all,
Vault admits only the ESO controller, and both APIs read one ESO-synced Secret.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from ops.serving.fraud_api_manifest import render

ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "infra/kubernetes/vault"
CHART = ROOT / "infra/helm/ewallet-serving"
BOOTSTRAP = json.loads((VAULT / "bootstrap.json").read_text(encoding="utf-8"))
IMAGE = "asia-southeast1-docker.pkg.dev/p/r/fraud-api@sha256:" + "a" * 64
GSA = "ewallet-feature-api@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com"
VALUES = {"FRAUD_API_IMAGE": IMAGE, "ONLINE_REGISTRY_URI": "gs://b/r.db", "MODEL_VERSION": "7", "GIT_SHA": "a" * 40}
FEATURE_SET = [
    "featureApi.enabled=true", f"featureApi.image={IMAGE}", "featureApi.registryUri=gs://b/r.db",
    f"featureApi.serviceAccount.gcpServiceAccount={GSA}",
]


def helm(*sets: str) -> list[dict]:
    """Render the serving chart with ``sets`` and return its objects."""
    argv = ["helm", "template", "s", str(CHART)]
    for value in sets:
        argv += ["--set", value]
    out = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def helm_error(*sets: str) -> str:
    """Return the error a refused render prints."""
    argv = ["helm", "template", "s", str(CHART)]
    for value in sets:
        argv += ["--set", value]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode != 0
    return result.stderr


needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def test_fraud_api_runs_as_its_own_account_without_an_api_token():
    deployment = next(d for d in yaml.safe_load_all(render("keda", VALUES)) if d and d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "fraud-api"
    assert pod["automountServiceAccountToken"] is False


def test_fraud_api_service_account_maps_to_its_own_gsa():
    text = (ROOT / "infra/kubernetes/fraud-serving/service-account.yaml.tmpl").read_text(encoding="utf-8")
    account = yaml.safe_load(text.replace("${GCP_PROJECT_ID}", "proj"))
    assert account["metadata"] == {
        "name": "fraud-api", "namespace": "ml-platform", "labels": {"app.kubernetes.io/name": "fraud-api"},
        "annotations": {"iam.gke.io/gcp-service-account": "ewallet-fraud-api@proj.iam.gserviceaccount.com"},
    }
    assert account["automountServiceAccountToken"] is False


@needs_helm
def test_feature_api_runs_as_its_own_account():
    objects = helm(*FEATURE_SET)
    account = next(o for o in objects if o["kind"] == "ServiceAccount")
    deployment = next(o for o in objects if o["kind"] == "Deployment")
    assert account["metadata"]["name"] == "feature-api"
    assert account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == GSA
    assert account["automountServiceAccountToken"] is False
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "feature-api"
    assert pod["automountServiceAccountToken"] is False


@needs_helm
@pytest.mark.parametrize("override", ["training", "fraud-api", "default"])
def test_feature_api_refuses_another_workloads_account(override):
    assert "own account" in helm_error(*FEATURE_SET, f"featureApi.serviceAccount.name={override}")


@needs_helm
def test_feature_api_refuses_the_fraud_api_vault_role():
    assert "never borrow" in helm_error(*FEATURE_SET, "featureApi.vault.enabled=true", "featureApi.vault.role=fraud-api")


@needs_helm
def test_feature_api_needs_a_workload_identity_gsa():
    assert "gcpServiceAccount" in helm_error(*FEATURE_SET, "featureApi.serviceAccount.gcpServiceAccount=")


@needs_helm
def test_feature_api_vault_mode_reads_only_its_own_path():
    deployment = next(o for o in helm(*FEATURE_SET, "featureApi.vault.enabled=true") if o["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["EWALLET_VAULT_ROLE"]["value"] == "feature-api"
    assert json.loads(env["EWALLET_VAULT_SECRETS"]["value"]) == {
        "EWALLET_REDIS_CONNECTION_STRING": "feature-api/feast-redis#connection_string"
    }
    assert "valueFrom" not in env.get("EWALLET_REDIS_CONNECTION_STRING", {})
    token = next(v for v in pod["volumes"] if v["name"] == "vault-token")
    assert token["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "vault"


@needs_helm
def test_feature_api_secret_mode_is_unchanged_without_vault():
    deployment = next(o for o in helm(*FEATURE_SET) if o["kind"] == "Deployment")
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"]["name"] == "feast-online-redis"
    assert not any(name.startswith("EWALLET_VAULT_") for name in env)


def test_no_vault_role_binds_a_serving_account():
    for name, role in BOOTSTRAP["roles"].items():
        assert not {"fraud-api", "feature-api", "training"} & set(role["bound_service_account_names"]), name
        assert "ml-platform" not in role["bound_service_account_namespaces"], name
    assert not {"fraud-api", "feature-api"} & set(BOOTSTRAP["policies"])


def test_both_serving_apis_read_one_eso_synced_secret_not_vault():
    serving = yaml.safe_load((CHART / "values-vault-eso.yaml").read_text(encoding="utf-8"))["featureApi"]
    assert serving["vault"]["enabled"] is False
    fraud = yaml.safe_load((ROOT / "infra/kubernetes/fraud-serving/fraud-api-eso.patch.yaml").read_text(encoding="utf-8"))
    (env,) = fraud["spec"]["template"]["spec"]["containers"][0]["env"]
    assert serving["redisSecret"]["name"] == env["valueFrom"]["secretKeyRef"]["name"] == "feast-online-redis-vault"


def test_vault_network_policy_admits_only_the_eso_controller():
    policy = yaml.safe_load((VAULT / "network-policy.yaml").read_text(encoding="utf-8"))
    sources = [peer for rule in policy["spec"]["ingress"] for peer in rule["from"]]
    peers = sorted((peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
                    peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]) for peer in sources)
    assert peers == [("external-secrets", "external-secrets")]
    assert all(set(peer) == {"namespaceSelector", "podSelector"} for peer in sources)


def test_no_rbac_binding_grants_the_serving_accounts_api_access():
    sources = list((ROOT / "infra/kubernetes").rglob("*.yaml")) + list((ROOT / "infra/kubernetes").rglob("*.tmpl"))
    sources += list((ROOT / "infra/helm").rglob("*.yaml"))
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "RoleBinding" not in text:
            continue
        for account in ("fraud-api", "feature-api"):
            assert not re.search(rf"kind:\s*ServiceAccount\s*\n\s*name:\s*{account}\b", text), path


def test_workload_identity_maps_each_account_to_its_own_gsa():
    module = (ROOT / "infra/terraform/gcp/modules/workload_identity/main.tf").read_text(encoding="utf-8")
    assert re.search(r'fraud_api\s*=\s*"ml-platform/fraud-api"', module)
    assert re.search(r'feature_api\s*=\s*"ml-platform/feature-api"', module)
    assert re.search(r'training\s*=\s*"ml-platform/training"', module)


def test_serving_gsas_read_only_the_registry_prefix():
    iam = (ROOT / "infra/terraform/gcp/modules/iam/main.tf").read_text(encoding="utf-8")
    assert re.search(r'fraud_api\s*=\s*"', iam) and re.search(r'feature_api\s*=\s*"', iam)
    data_roles = re.search(r"data_bucket_roles\s*=\s*\{([^}]*)\}", iam).group(1)
    assert "fraud_api" not in data_roles and "feature_api" not in data_roles
    block = re.search(r'resource "google_storage_bucket_iam_member" "registry_reader" \{(.*?)\n\}', iam, re.S).group(1)
    assert 'role   = "roles/storage.objectViewer"' in block
    assert "condition {" in block and "resource.name.startsWith" in block
    assert re.search(r'registry_prefix\s*=\s*"features/registries/"', iam)


def test_mlflow_keeps_the_training_account():
    accounts = list(yaml.safe_load_all((ROOT / "infra/kubernetes/mlflow/service-account.yaml").read_text()))
    assert any(a["metadata"]["name"] == "training" for a in accounts)
