"""Vault V2 -> External Secrets Operator -> Kubernetes Secret: the declared contract, without a cluster.

Pins what the kind campaign (scripts/vault/gke_rehearsal.sh, docs/edai2/platform/VAULT.md) exercises
live: one ClusterSecretStore, ewallet-vault, usable only from an exact namespace list; one dedicated
reader identity (external-secrets/eso-vault-reader) and one read-only Vault role whose policy covers two
explicit prefixes; HTTPS with one pinned CA; one Vault record per underlying credential; new target
Secrets only; and every consumer switches only through its opt-in overlay.

RETIRED INVARIANT (docs/edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md §7.4, decision O1): V1 asserted
that the ml-platform reader could not read agent-platform paths (one reader role per namespace). V2 has
one reader for the trusted namespaces, so that separation is intentionally gone. What replaces it is
tested below: the store's namespace list, the reader's two prefixes, no list/metadata/write/sys, and the
negative tests (unapproved namespace, out-of-prefix path, wrong identity, wrong audience, untrusted CA).
"""

from __future__ import annotations

import json
import os
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
CONSUMERS = {"ml-platform": ESO / "ml-platform.yaml", "ewallet-agents": ESO / "ewallet-agents.yaml"}
SELF_TOKEN_PATHS = {"auth/token/lookup-self", "auth/token/revoke-self"}
PREFIXES = ("ewallet/data/ml-platform/", "ewallet/data/agent-platform/")
ESO_CHART = Path(os.environ.get("ESO_CHART", Path.home() / "ewallet-evidence/vault/charts/external-secrets-2.14.0.tgz"))
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
needs_chart = pytest.mark.skipif(not ESO_CHART.is_file(), reason=f"pinned ESO chart not at {ESO_CHART} (set ESO_CHART)")


def docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def one(path: Path, kind: str) -> dict:
    (found,) = [doc for doc in docs(path) if doc["kind"] == kind]
    return found


def grants(policy: str) -> dict[str, list[str]]:
    text = (ROOT / BOOTSTRAP["policies"][policy]).read_text(encoding="utf-8")
    found = re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text)
    return {path: re.findall(r'"(\w+)"', caps) for path, caps in found}


def external_secrets() -> list[dict]:
    return [doc for path in CONSUMERS.values() for doc in docs(path) if doc["kind"] == "ExternalSecret"]


STORE = one(ESO / "cluster-secret-store.yaml", "ClusterSecretStore")


def test_cluster_store_authenticates_as_the_dedicated_reader_over_verified_https():
    account = one(ESO / "cluster-secret-store.yaml", "ServiceAccount")
    assert (account["metadata"]["name"], account["metadata"]["namespace"]) == ("eso-vault-reader", "external-secrets")
    assert account["automountServiceAccountToken"] is False
    assert STORE["metadata"]["name"] == "ewallet-vault"
    vault = STORE["spec"]["provider"]["vault"]
    assert vault["server"] == "https://vault.ewallet-vault.svc.cluster.local:8200"
    assert vault["caProvider"] == {"type": "ConfigMap", "name": "vault-ca", "key": "ca.crt", "namespace": "external-secrets"}
    assert (vault["path"], vault["version"]) == (BOOTSTRAP["kv_mount"], "v2")
    assert "tls" not in vault and "caBundle" not in vault   # no inline CA, no client-certificate bypass
    auth = vault["auth"]["kubernetes"]
    assert auth == {"mountPath": BOOTSTRAP["kubernetes_auth_mount"], "role": "eso-ewallet-reader",
                    "serviceAccountRef": {"name": "eso-vault-reader", "namespace": "external-secrets",
                                          "audiences": [BOOTSTRAP["token_audience"]]}}
    role = BOOTSTRAP["roles"]["eso-ewallet-reader"]
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == ["external-secrets"]
    assert role["audience"] == BOOTSTRAP["token_audience"] and role["token_no_default_policy"] is True
    assert role["token_policies"] == ["eso-ewallet-reader"] and (role["token_ttl"], role["token_max_ttl"]) == ("5m", "15m")


def test_store_is_limited_to_an_exact_namespace_list():
    assert STORE["spec"]["conditions"] == [{"namespaces": ["ml-platform", "ewallet-agents"]}]   # exact names, no selector or regex
    allowed = STORE["spec"]["conditions"][0]["namespaces"]
    for excluded in ("ewallet-sandbox", "ewallet-ci", "ewallet-ingress", "ewallet-mcp", "external-secrets", "ewallet-vault"):
        assert excluded not in allowed
    assert sorted(allowed) == sorted(CONSUMERS)           # every allowed namespace has a reviewed consumer file, and no more


def test_reader_policy_reads_only_the_two_prefixes():
    policy = grants("eso-ewallet-reader")
    readable = {path for path in policy if path not in SELF_TOKEN_PATHS}
    assert readable == {prefix + "*" for prefix in PREFIXES}
    assert all(policy[path] == ["read"] for path in readable)
    assert policy["auth/token/lookup-self"] == ["read"] and policy["auth/token/revoke-self"] == ["update"]
    assert "ewallet/data/*" not in policy and not any("/metadata/" in path or path.startswith("sys/") for path in policy)


def test_one_vault_record_per_credential_inside_the_reader_prefixes():
    keys = {}
    for es in external_secrets():
        assert "dataFrom" not in es["spec"]                    # named keys only, never a whole record
        for item in es["spec"]["data"]:
            keys.setdefault(item["remoteRef"]["key"], set()).add(es["metadata"]["namespace"])
            assert f"{BOOTSTRAP['kv_mount']}/data/{item['remoteRef']['key']}".startswith(PREFIXES)
    assert keys == {"ml-platform/feast-online-redis": {"ml-platform"}, "agent-platform/vllm-api-key": {"ewallet-agents"},
                    "agent-platform/agentgateway-client-key": {"ewallet-agents"}}


def test_v1_per_namespace_and_direct_loader_roles_are_retired():
    assert sorted(BOOTSTRAP["roles"]) == ["eso-ewallet-reader", "vault-admin"]
    assert sorted(BOOTSTRAP["policies"]) == ["eso-ewallet-reader", "root-generation", "secret-operator"]
    for retired in ("eso-ml-platform", "eso-ewallet-agents", "feature-api", "fraud-api"):
        assert not (VAULT / f"policies/{retired}.hcl").exists()
    for role in BOOTSTRAP["roles"].values():               # no role binds a consumer namespace
        assert not set(role["bound_service_account_namespaces"]) & set(CONSUMERS)


def test_targets_are_new_secrets_never_existing_ones():
    existing = {"feast-online-redis", "agentgateway-client-key", "vllm-api-key", "redis"}
    for es in external_secrets():
        spec = es["spec"]
        assert spec["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "ewallet-vault"}
        assert spec["target"]["name"] == es["metadata"]["name"] + "-vault" and spec["target"]["name"] not in existing
        assert spec["target"]["creationPolicy"] == "Owner" and spec["target"]["deletionPolicy"] == "Retain"
        assert "refreshPolicy" not in spec and spec["refreshInterval"] == "1h"


def test_eso_installation_processes_only_the_cluster_store_and_pins_images():
    values = yaml.safe_load((ESO / "values.yaml").read_text(encoding="utf-8"))
    assert values["processClusterStore"] is True
    for flag in ("processClusterExternalSecret", "processPushSecret", "processClusterPushSecret"):
        assert values[flag] is False
    for image in (values["image"], values["webhook"]["image"], values["certController"]["image"]):
        assert re.fullmatch(r"v2\.14\.0@sha256:[0-9a-f]{64}", image["tag"])
    assert yaml.safe_load((ESO / "namespace.yaml").read_text())["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


@needs_helm
@needs_chart
def test_the_pinned_chart_turns_the_values_into_controller_flags():
    argv = ["helm", "template", "external-secrets", str(ESO_CHART), "-n", "external-secrets", "-f", str(ESO / "values.yaml")]
    rendered = [d for d in yaml.safe_load_all(subprocess.run(argv, check=True, capture_output=True, text=True).stdout) if d]
    controller = next(d for d in rendered if d["kind"] == "Deployment" and d["metadata"]["name"] == "external-secrets")
    args = controller["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--enable-cluster-store-reconciler=false" not in args
    for disabled in ("cluster-external-secret", "cluster-push-secret", "push-secret"):
        assert f"--enable-{disabled}-reconciler=false" in args
    rules = [r for d in rendered if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "external-secrets-controller"
             for r in d["rules"]]
    assert any("clustersecretstores" in r.get("resources", []) for r in rules)


@needs_helm
@needs_chart
def test_store_external_secrets_and_negative_template_match_the_pinned_crd_schemas():
    jsonschema = pytest.importorskip("jsonschema")
    argv = ["helm", "template", "external-secrets", str(ESO_CHART), "-n", "external-secrets", "-f", str(ESO / "values.yaml")]
    crds = {}
    for crd in yaml.safe_load_all(subprocess.run(argv, check=True, capture_output=True, text=True).stdout):
        if crd and crd.get("kind") == "CustomResourceDefinition" and crd["spec"]["group"] == "external-secrets.io":
            v1 = next((v for v in crd["spec"]["versions"] if v["name"] == "v1"), None)
            if v1:
                crds[crd["spec"]["names"]["kind"]] = v1["schema"]["openAPIV3Schema"]
    negative = (ESO / "negative-tests.yaml").read_text().replace("__CID__", "vault-gke-20261201T000000Z")
    objects = docs(ESO / "cluster-secret-store.yaml") + external_secrets() + [d for d in yaml.safe_load_all(negative) if d]
    checked = 0
    for obj in objects:
        if obj["apiVersion"].startswith("external-secrets.io/"):
            assert obj["apiVersion"] == "external-secrets.io/v1"
            jsonschema.validate(obj, crds[obj["kind"]])
            checked += 1
    assert checked == 12 and {"ClusterSecretStore", "SecretStore", "ExternalSecret"} <= set(crds)


def test_no_rbac_or_workload_uses_the_reader_identity():
    for path in list((ROOT / "infra").rglob("*.yaml")) + list((ROOT / "infra").rglob("*.tmpl")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "eso-vault-reader" in text:
            assert path.parent == ESO, path
            assert "serviceAccountName" not in text and "RoleBinding" not in text


def test_vault_stays_internal_and_admits_only_the_eso_controller():
    for path in (ROOT / "infra").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "kind: HTTPRoute" in text or "kind: VirtualService" in text:
            assert "ewallet-vault" not in text and "vault." not in text, path
    values = yaml.safe_load((VAULT / "values-gke.yaml").read_text(encoding="utf-8"))
    assert values["ui"]["enabled"] is False and "service" not in values["server"]
    policy = yaml.safe_load((VAULT / "network-policy.yaml").read_text())
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "external-secrets"}},
                             "podSelector": {"matchLabels": {"app.kubernetes.io/name": "external-secrets"}}}]
    assert rule["ports"] == [{"port": 8200, "protocol": "TCP"}]


@needs_helm
def test_serving_overlay_switches_the_feature_api_to_the_shared_synced_secret_without_the_direct_loader():
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
        "name": "feast-online-redis-vault", "key": "connection_string"}
    assert not any(name.startswith("EWALLET_VAULT_") for name in env)
    assert not any(v["name"].startswith("vault") for v in pod.get("volumes", []))
    synced = one(ESO / "ml-platform.yaml", "ExternalSecret")["spec"]
    assert synced["target"]["name"] == "feast-online-redis-vault"
    assert [item["secretKey"] for item in synced["data"]] == ["connection_string"]


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_fraud_patch_moves_only_the_redis_secret_name(tmp_path):
    base = next(d for d in yaml.safe_load_all((ROOT / "infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl").read_text())
                if d and d["kind"] == "Deployment")
    (tmp_path / "deploy.yaml").write_text(yaml.safe_dump(base))
    out = subprocess.run(["kubectl", "patch", "--local", "-f", str(tmp_path / "deploy.yaml"), "--type", "strategic",
                          "--patch-file", str(ROOT / "infra/kubernetes/fraud-serving/fraud-api-eso.patch.yaml"), "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    patched = json.loads(out)
    before, after = base["spec"]["template"]["spec"]["containers"][0], patched["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in after["env"]}
    assert env["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"] == {"name": "feast-online-redis-vault", "key": "connection_string"}
    assert not any(name.startswith("EWALLET_VAULT_") for name in env)

    def others(container):
        return {e["name"]: e for e in container["env"] if e["name"] != "EWALLET_REDIS_CONNECTION_STRING"}
    assert others(before) == others(after) and before.get("volumeMounts") == after.get("volumeMounts")


AGENT_REFS = {  # (kind, name, env var) -> Secret, with the chart defaults
    ("Deployment", "agentgateway", "VLLM_API_KEY"): "vllm-api-key",
    ("Deployment", "agentgateway", "GATEWAY_CLIENT_KEY"): "agentgateway-client-key",
    ("Deployment", "vllm-primary", "VLLM_API_KEY"): "vllm-api-key",
    ("Deployment", "vllm-fallback", "VLLM_API_KEY"): "vllm-api-key",
}
AGENT_OVERLAY = ROOT / "infra/helm/llm-agent-platform/values-vault-eso.yaml"


def _agent_render(*overlays: Path) -> tuple[dict, str]:
    argv = ["helm", "template", "llm-agent", str(ROOT / "infra/helm/llm-agent-platform")]
    for overlay in overlays:
        argv += ["-f", str(overlay)]
    text = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    refs = {}
    for d in yaml.safe_load_all(text):
        if d and d["kind"] == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                for e in c.get("env", []):
                    ref = (e.get("valueFrom") or {}).get("secretKeyRef")
                    if ref:
                        refs[("Deployment", d["metadata"]["name"], e["name"])] = ref["name"]
        if d and d["kind"] == "ModelConfig":
            refs[("ModelConfig", d["metadata"]["name"], "apiKeySecret")] = d["spec"]["apiKeySecret"]
    return refs, text


@needs_helm
def test_agent_overlay_switches_the_validator_and_every_client_together():
    before, _ = _agent_render()
    after, text = _agent_render(AGENT_OVERLAY)
    model = ("ModelConfig", "ewallet-global-llm", "apiKeySecret")
    assert {k: before[k] for k in AGENT_REFS} == AGENT_REFS and before[model] == "agentgateway-client-key"
    assert {k: after[k] for k in AGENT_REFS} == {k: v + "-vault" for k, v in AGENT_REFS.items()}
    assert after[model] == after[("Deployment", "agentgateway", "GATEWAY_CLIENT_KEY")] == "agentgateway-client-key-vault"
    assert set(before) == set(after)                       # the overlay adds or removes no consumer
    overlay = yaml.safe_load(AGENT_OVERLAY.read_text())
    assert overlay == {"vllm": {"apiKeySecret": {"name": "vllm-api-key-vault", "key": "api-key"}},
                       "agentgateway": {"clientKeySecret": {"name": "agentgateway-client-key-vault", "key": "api-key"}},
                       "modelConfig": {"apiKeySecret": {"name": "agentgateway-client-key-vault", "key": "api-key"}}}
    assert "kind: Secret\n" not in text                    # Helm renders references, never credential material


@needs_helm
def test_agent_overlay_on_the_agi_profile_moves_byo_agents_and_keeps_mcp_and_idempotency_secrets():
    """The deployed agent profile (g3 + AGI BYO agents): every BYO agent's gateway key follows modelConfig,
    while the MCP credential releases and the Coordinator idempotency Secret are untouched (audit §11, MCP-A)."""
    chart = ROOT / "infra/helm/llm-agent-platform"
    digest, byo = "sha256:" + "a" * 64, "asia-southeast1-docker.pkg.dev/p/r/agent@sha256:" + "b" * 64
    base = ["helm", "template", "llm-agent", str(chart)]
    for overlay in ("values-gke-cpu.yaml", "values-gke-cpu-g1.yaml", "g3/g3.yaml", "n11/values-n11-agents.yaml", "agi/agents.yaml"):
        base += ["-f", str(chart / overlay)]
    base += ["--set", "idempotencyRedis.enabled=true", "--set", "otelCollector.enabled=true",
             "--set", f"vllm.image=r.example/p/vllm@{digest}", "--set", f"mcpServices.image=r.example/p/mcp@{digest}",
             "--set", f"mcpServices.identity.imageDigest={digest}", "--set", "mcpServices.identity.gitSha=" + "c" * 40,
             "--set", f"agents.coordinator.byo.image={byo}", "--set", f"agents.specialists.feature-rag-specialist.byo.image={byo}",
             "--set", f"agents.specialists.drift-specialist.byo.image={byo}", "--set", "modelConfig.byoResolver.apiServer.cidrs={10.0.0.1/32}"]
    plain = subprocess.run(base, check=True, capture_output=True, text=True).stdout
    switched = subprocess.run(base + ["-f", str(AGENT_OVERLAY)], check=True, capture_output=True, text=True).stdout

    def gateway_key_refs(text):
        refs = re.findall(r"name: (agentgateway-client-key(?:-vault)?)\b", text)
        return sorted(set(refs)), len(refs)

    def untouched(text):
        return sorted(set(re.findall(r"mcp-(?:token|auth)-[a-z0-9-]+|coordinator-idempotency-redis-auth", text)))
    before_names, before_count = gateway_key_refs(plain)
    after_names, after_count = gateway_key_refs(switched)
    assert before_names == ["agentgateway-client-key"] and after_names == ["agentgateway-client-key-vault"]
    assert before_count == after_count >= 2                 # the validator and the BYO agents, all moved
    assert untouched(plain) and untouched(plain) == untouched(switched)


# ---- Campaign B gate G10: the committed negative-test template and the documents that use it

CID = "vault-gke-20261201T000000Z"


def _negative_objects(cid=CID):
    text = (ESO / "negative-tests.yaml").read_text().replace("__CID__", cid)
    return [d for d in yaml.safe_load_all(text) if d]


def test_negative_tests_are_labelled_throwaway_objects():
    objects = _negative_objects()
    assert sorted((o["kind"], o["metadata"].get("namespace", ""), o["metadata"]["name"]) for o in objects) == [
        ("ClusterSecretStore", "", "vault-neg-wrong-aud"),
        ("ExternalSecret", "ewallet-sandbox", "vault-neg-sandbox"),
        ("ExternalSecret", "ml-platform", "vault-neg-out-of-prefix"), ("ExternalSecret", "ml-platform", "vault-neg-wrong-aud"),
        ("ExternalSecret", "ml-platform", "vault-neg-wrong-ns"), ("ExternalSecret", "ml-platform", "vault-neg-wrong-sa"),
        ("SecretStore", "ml-platform", "vault-neg-wrong-ns"), ("SecretStore", "ml-platform", "vault-neg-wrong-sa"),
        ("ServiceAccount", "ml-platform", "eso-vault-reader"), ("ServiceAccount", "ml-platform", "vault-neg-intruder")]
    for o in objects:
        assert o["metadata"]["labels"] == {"ewallet.io/vault-campaign": CID}
    assert "__CID__" not in str(objects) and "kind: Secret\n" not in str(objects)


def test_negative_stores_differ_from_the_real_store_only_in_the_tested_property():
    real = STORE["spec"]["provider"]["vault"]
    stores = {o["metadata"]["name"]: o for o in _negative_objects() if o["kind"] in ("SecretStore", "ClusterSecretStore")}
    for name, store in stores.items():
        vault = store["spec"]["provider"]["vault"]
        for key in ("server", "path", "version", "caProvider"):
            assert vault[key] == real[key], (name, key)
        assert vault["auth"]["kubernetes"]["role"] == "eso-ewallet-reader"

    def ref(name):
        return stores[name]["spec"]["provider"]["vault"]["auth"]["kubernetes"]["serviceAccountRef"]
    assert ref("vault-neg-wrong-sa") == {"name": "vault-neg-intruder", "audiences": ["vault"]}
    assert ref("vault-neg-wrong-ns") == {"name": "eso-vault-reader", "audiences": ["vault"]}      # same name, consumer namespace
    assert ref("vault-neg-wrong-aud") == {"name": "eso-vault-reader", "namespace": "external-secrets", "audiences": ["kubernetes"]}
    assert stores["vault-neg-wrong-aud"]["spec"]["conditions"] == [{"namespaces": ["ml-platform"]}]


def test_negative_external_secrets_target_their_own_name_and_a_denied_path_or_login():
    secrets = {o["metadata"]["name"]: o["spec"] for o in _negative_objects() if o["kind"] == "ExternalSecret"}
    for name, spec in secrets.items():
        assert spec["target"] == {"name": name, "creationPolicy": "Owner"}
    out = secrets["vault-neg-out-of-prefix"]
    assert out["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "ewallet-vault"}
    assert not f"ewallet/data/{out['data'][0]['remoteRef']['key']}".startswith(PREFIXES)
    assert secrets["vault-neg-sandbox"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "ewallet-vault"}
    for name in ("vault-neg-wrong-sa", "vault-neg-wrong-ns", "vault-neg-wrong-aud"):
        assert secrets[name]["secretStoreRef"]["name"] == name
        assert secrets[name]["data"][0]["remoteRef"]["key"] == "ml-platform/feast-online-redis"


def test_campaign_documents_use_only_objects_the_template_creates():
    names = {o["metadata"]["name"] for o in _negative_objects()}
    plan = (ROOT / "docs/runbooks/edai2/VAULT_GKE_ACCEPTANCE_CAMPAIGN.md").read_text()
    runbook = (ROOT / "docs/runbooks/edai2/VAULT_ESO_RUNBOOK.md").read_text()
    for text in (plan, runbook):
        assert set(re.findall(r"vault-neg-[a-z-]*[a-z]", text)) <= names
    section = plan.split("## 5.")[1].split("## 6.")[0]
    assert "-l ewallet.io/vault-campaign=$CID" in section and "kubectl apply" not in section
    for cause in ("permission denied", "service account name not authorized", "namespace not authorized", "invalid audience"):
        assert cause in section
    assert "neg-fraud" not in runbook


def test_business_lookup_never_passes_on_a_timeout_5xx_or_424():
    plan = (ROOT / "docs/runbooks/edai2/VAULT_GKE_ACCEPTANCE_CAMPAIGN.md").read_text()
    lookup = plan.split("## 4.")[1].split("## 5.")[0]
    assert "--max-time 30" in lookup and "curl_exit" in lookup
    for verdict in ("BASELINE_OK", "BASELINE_DATA_UNAVAILABLE", "BASELINE_UNHEALTHY", "BUSINESS_LOOKUP_PASS",
                    "SECRET_INTEGRATION_PASS_BUSINESS_LOOKUP_UNAVAILABLE", "REGRESSION_AFTER_MIGRATION", "CAMPAIGN_B_FAILED_AT_G11"):
        assert verdict in lookup
    assert "HTTP 424 is never a business success" in lookup
    assert "`ewallet-edge` 21" not in plan and "`ewallet-edge-gateway` 16" not in plan
