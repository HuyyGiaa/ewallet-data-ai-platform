"""scripts/vault/gke_rehearsal.sh and its Vault fixture: isolation, approvals and refusals, without a cluster."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/vault/gke_rehearsal.sh"
TEXT = SCRIPT.read_text()
REHEARSAL = json.loads((ROOT / "scripts/vault/rehearsal/bootstrap.json").read_text())
OFFICIAL = json.loads((ROOT / "infra/kubernetes/vault/bootstrap.json").read_text())
GKE = "gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev"
ID = "vault-rehearsal-20261125T010203Z"


def grants(policy_path: str) -> dict[str, str]:
    text = (ROOT / policy_path).read_text()
    return dict(re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text))


# ---- the rehearsal's Vault role and policy

def test_rehearsal_role_is_bound_only_to_the_temporary_namespace():
    (name, role), = REHEARSAL["roles"].items()
    assert name == "eso-vault-rehearsal" and name not in OFFICIAL["roles"]
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == ["vault-rehearsal"]
    assert role["audience"] == "vault" and role["token_no_default_policy"] is True
    assert role["token_policies"] == ["eso-vault-rehearsal"]


def test_rehearsal_policy_reads_one_synthetic_path_never_the_real_credential():
    paths = grants(REHEARSAL["policies"]["eso-vault-rehearsal"])
    reads = [p for p, caps in paths.items() if not p.startswith("auth/token/")]
    assert reads == ["ewallet/data/feature-api/rehearsal-redis"] and paths[reads[0]].strip() == '"read"'
    assert "ewallet/data/feature-api/feast-redis" not in paths


def test_the_existing_operator_token_can_seed_the_rehearsal_path():
    """The rehearsal uses the official put --token-file workflow; no operator policy change is needed."""
    operator = grants(OFFICIAL["policies"]["secret-operator"])
    assert "create" in operator["ewallet/data/feature-api/*"] and "update" in operator["ewallet/data/feature-api/*"]


def test_rehearsal_bootstrap_reuses_the_official_mounts():
    for key in ("kv_mount", "audit_file", "kubernetes_auth_mount", "token_audience"):
        assert REHEARSAL[key] == OFFICIAL[key]


# ---- the script refuses before touching any cluster

def run(phase: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "REHEARSAL_CONTEXT": "kind-test",
            "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(tmp_path / "ev")}
    base.update(env)
    return subprocess.run(["bash", str(SCRIPT), phase], env=base, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("change,reason", [
    ({"REHEARSAL_CONTEXT": "gke_other-project_zone_cluster"}, "neither kind-* nor"),
    ({"REHEARSAL_ID": "my-test"}, "REHEARSAL_ID must look like"),
    ({"REHEARSAL_CONTEXT": GKE, "KUBECONFIG": "/home/x/.kube/config"}, "KUBECONFIG must be"),
    ({"REHEARSAL_DIR": str(ROOT / "tmp-rehearsal")}, "outside the repository"),
], ids=["other-context", "bad-id", "gke-default-kubeconfig", "dir-in-repo"])
def test_refusals(tmp_path, change, reason):
    done = run("preflight", tmp_path, **change)
    assert done.returncode == 2 and reason in done.stderr
    assert not (ROOT / "tmp-rehearsal").exists()


@pytest.mark.parametrize("phase", ["vault", "init", "configure", "eso", "consumer", "negative", "lifecycle"])
def test_every_gke_mutation_needs_the_approval_for_this_id(tmp_path, phase):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    common = {"REHEARSAL_CONTEXT": GKE, "KUBECONFIG": str(kubeconfig)}
    assert "needs VAULT_REHEARSAL_APPROVED" in run(phase, tmp_path, **common).stderr
    wrong = run(phase, tmp_path, **common, VAULT_REHEARSAL_APPROVED="vault-rehearsal-20260101T000000Z")
    assert wrong.returncode == 2 and "needs VAULT_REHEARSAL_APPROVED" in wrong.stderr


def test_gke_cleanup_needs_its_own_approval(tmp_path):
    kubeconfig = tmp_path / ".cache/ewallet-cloud-lab/kubeconfig"
    done = run("cleanup", tmp_path, REHEARSAL_CONTEXT=GKE, KUBECONFIG=str(kubeconfig), VAULT_REHEARSAL_APPROVED=ID)
    assert done.returncode == 2 and "VAULT_REHEARSAL_CLEANUP_APPROVED" in done.stderr


def test_xtrace_is_refused(tmp_path):
    base = {"PATH": os.environ["PATH"], "REHEARSAL_CONTEXT": "kind-test", "REHEARSAL_ID": ID, "REHEARSAL_DIR": str(tmp_path)}
    done = subprocess.run(["bash", "-x", str(SCRIPT), "preflight"], env=base, capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "xtrace" in done.stderr


# ---- static properties of the procedure

def test_objects_are_created_never_applied_and_every_call_is_pinned():
    assert "kubectl apply" not in TEXT and " k apply" not in TEXT and "set -x" not in TEXT
    code = [line for line in TEXT.splitlines() if not line.lstrip().startswith("#")]
    # kubectl or helm in command position (line start, after ( ; | & or $( ): always pinned to the context
    calls = [line for line in code if re.search(r"(^|[(;|&]|\$\()\s*(kubectl|helm)\s", line)]
    assert calls and all("--context" in line or "--kube-context" in line for line in calls), calls


def test_nothing_is_created_in_an_existing_production_namespace():
    for namespace in ("ml-platform", "ewallet-agents", "feature-store", "kagent", "ewallet-ingress"):
        for line in TEXT.splitlines():
            if re.search(rf"(-n\s+|namespace:\s*){namespace}\b", line) and not line.lstrip().startswith("#"):
                assert re.search(r"\bget\b", line), f"{namespace} used outside a read: {line.strip()}"


def test_cleanup_deletes_only_namespaces_proven_to_be_this_rehearsals():
    cleanup = TEXT.split("phase_cleanup()")[1].split("phase_verify_clean()")[0]
    assert "without $LABEL=$ID" in cleanup
    for deletion in re.findall(r"k delete ns \"\$(\w+)\"", cleanup):
        assert f'if mine "${deletion}"' in cleanup


def test_consumer_side_uses_the_platform_redis_image_and_restricted_pods():
    platform = (ROOT / "infra/kubernetes/feast-online/redis.yaml").read_text()
    digest = re.search(r"REDIS_IMAGE=\S+@(sha256:[0-9a-f]{64})", TEXT).group(1)
    assert digest in platform
    assert "pod-security.kubernetes.io/enforce: restricted" in TEXT and "runAsNonRoot: true" in TEXT
