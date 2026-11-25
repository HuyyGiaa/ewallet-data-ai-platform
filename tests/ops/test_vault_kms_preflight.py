"""scripts/vault/kms_preflight.py: read-only Cloud KMS / Workload Identity checks, against a fake gcloud (no cloud)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kms_preflight", ROOT / "scripts/vault/kms_preflight.py")
kp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kp)

P = kp.PROJECT
GSA = f"ewallet-vault-unseal@{P}.iam.gserviceaccount.com"
KEY = f"projects/{P}/locations/asia-southeast1/keyRings/ewallet-vault/cryptoKeys/vault-unseal"
GOOD = {
    "services": [{"config": {"name": "cloudkms.googleapis.com"}}],
    "keyrings": {"name": f"projects/{P}/locations/asia-southeast1/keyRings/ewallet-vault"},
    "keys describe": {"name": KEY, "purpose": "ENCRYPT_DECRYPT", "versionTemplate": {"protectionLevel": "SOFTWARE",
                      "algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION"}, "primary": {"name": f"{KEY}/cryptoKeyVersions/1", "state": "ENABLED"}},
    "versions": [{"name": f"{KEY}/cryptoKeyVersions/1", "state": "ENABLED"}],
    "keys get-iam-policy": {"bindings": [{"role": "roles/cloudkms.cryptoKeyEncrypterDecrypter", "members": [f"serviceAccount:{GSA}"]},
                                         {"role": "roles/cloudkms.viewer", "members": [f"serviceAccount:{GSA}"]}]},
    "service-accounts describe": {"email": GSA, "disabled": False},
    "keys list": [],
    "service-accounts get-iam-policy": {"bindings": [{"role": "roles/iam.workloadIdentityUser",
                                                      "members": [f"serviceAccount:{P}.svc.id.goog[ewallet-vault/vault]"]}]},
}
ROUTES = [("services list", "services"), ("keyrings describe", "keyrings"), ("keys describe", "keys describe"),
          ("versions list", "versions"), ("keys get-iam-policy", "keys get-iam-policy"),
          ("service-accounts describe", "service-accounts describe"), ("service-accounts keys list", "keys list"),
          ("service-accounts get-iam-policy", "service-accounts get-iam-policy")]


class FakeGcloud:
    def __init__(self, answers=None, errors=None, raw=None):
        self.answers = copy.deepcopy(GOOD)
        self.answers.update(answers or {})
        self.errors, self.raw, self.calls = errors or {}, raw or {}, []

    def __call__(self, argv):
        self.calls.append(argv)
        text = " ".join(argv)
        for needle, key in ROUTES:
            if needle in text:
                if key in self.errors:
                    return 1, "", self.errors[key]
                if key in self.raw:
                    return 0, self.raw[key], ""
                return 0, json.dumps(self.answers[key]), ""
        raise AssertionError(f"unexpected command {argv}")


def run(runner, mode="cloud"):
    check = kp.KmsPreflight(P, runner)
    check.repository(None)
    check.cloud() if mode == "cloud" else check.not_checked()
    return {g["gate"]: g["status"] for g in check.gates}, kp.verdict(check.gates, mode), check


def test_a_correct_project_is_ready_except_the_unrendered_chart():
    gates, result, _ = run(FakeGcloud())
    assert {g: s for g, s in gates.items() if g != "vault-ksa-rendered"} == {g: "PASS" for g in gates if g != "vault-ksa-rendered"}
    assert gates["vault-ksa-rendered"] == "NOT_CHECKED" and result["verdict"] == "VAULT_KMS_PREFLIGHT_OFFLINE_ONLY"


def test_every_query_is_read_only_and_pinned_to_the_project():
    fake = FakeGcloud()
    run(fake)
    assert len(fake.calls) == 8
    for argv in fake.calls:
        assert argv[0] == "gcloud" and argv[argv.index("--project") + 1] == P and argv[argv.index("--format") + 1] == "json"
        assert set(argv) & {"describe", "list", "get-iam-policy"}
        assert not set(argv) & {"create", "delete", "update", "add-iam-policy-binding", "set-iam-policy", "enable",
                                "disable", "destroy", "restore", "set-primary-version"}


def test_offline_mode_never_calls_gcloud_and_is_never_ready():
    fake = FakeGcloud()
    gates, result, _ = run(fake, mode="offline")
    assert fake.calls == [] and result["verdict"] == "VAULT_KMS_PREFLIGHT_OFFLINE_ONLY" and result["blocking"] == []
    assert gates["kms-crypto-key"] == "NOT_CHECKED"


@pytest.mark.parametrize("change,gate", [
    ({"services": []}, "kms-api-enabled"),
    ({"keyrings": {"name": "projects/another/locations/asia-southeast1/keyRings/ewallet-vault"}}, "kms-key-ring"),
    ({"keys describe": {**GOOD["keys describe"], "purpose": "ASYMMETRIC_DECRYPT"}}, "kms-crypto-key"),
    ({"keys describe": {**GOOD["keys describe"], "versionTemplate": {"protectionLevel": "HSM"}}}, "kms-crypto-key"),
    ({"keys describe": {**GOOD["keys describe"], "rotationPeriod": "7776000s"}}, "kms-crypto-key"),
    ({"keys describe": {**GOOD["keys describe"], "primary": {"state": "DISABLED"}}}, "kms-crypto-key"),
    ({"keys describe": {**GOOD["keys describe"], "name": KEY.replace("vault-unseal", "other")}}, "kms-crypto-key"),
    ({"versions": GOOD["versions"] + [{"name": f"{KEY}/cryptoKeyVersions/2", "state": "DISABLED"}]}, "kms-key-versions"),
    ({"versions": [{"name": f"{KEY}/cryptoKeyVersions/1", "state": "DESTROY_SCHEDULED"}]}, "kms-key-versions"),
    ({"versions": []}, "kms-key-versions"),
    ({"keys get-iam-policy": {"bindings": GOOD["keys get-iam-policy"]["bindings"][:1]}}, "kms-key-iam"),
    ({"keys get-iam-policy": {"bindings": GOOD["keys get-iam-policy"]["bindings"] + [
        {"role": "roles/cloudkms.cryptoKeyDecrypter", "members": ["user:someone@example.com"]}]}}, "kms-key-iam"),
    ({"keys get-iam-policy": {"bindings": [{**b, "condition": {"expression": "true"}} for b in GOOD["keys get-iam-policy"]["bindings"]]}}, "kms-key-iam"),
    ({"service-accounts describe": {"email": GSA, "disabled": True}}, "vault-gsa"),
    ({"keys list": [{"name": "projects/x/serviceAccounts/y/keys/z"}]}, "vault-gsa-no-json-key"),
    ({"service-accounts get-iam-policy": {"bindings": [{"role": "roles/iam.workloadIdentityUser",
      "members": [f"serviceAccount:{P}.svc.id.goog[ewallet-vault/default]"]}]}}, "vault-workload-identity"),
    ({"service-accounts get-iam-policy": {"bindings": GOOD["service-accounts get-iam-policy"]["bindings"] + [
        {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:someone@example.com"]}]}}, "vault-workload-identity"),
], ids=["api-off", "ring-wrong-project", "purpose", "hsm", "rotation", "primary-disabled", "wrong-key", "old-version-disabled",
        "version-destroy-scheduled", "no-versions", "viewer-missing", "other-decrypter", "conditional", "gsa-disabled",
        "json-key", "wrong-ksa", "extra-gsa-role"])
def test_each_wrong_fact_fails_its_own_gate(change, gate):
    gates, result, _ = run(FakeGcloud(change))
    assert gates[gate] == "FAIL" and result["verdict"] == "VAULT_KMS_PREFLIGHT_BLOCKED"
    assert [g for g, s in gates.items() if s == "FAIL"] == [gate]


@pytest.mark.parametrize("stderr,status", [
    ("ERROR: (gcloud.kms.keys.describe) NOT_FOUND: CryptoKey projects/x was not found. Authenticated as owner@example.com", "FAIL"),
    ("ERROR: (gcloud.kms.keys.describe) PERMISSION_DENIED: Permission 'cloudkms.cryptoKeys.get' denied for owner@example.com", "BLOCKED"),
    ("ERROR: There was a problem refreshing your current auth tokens: owner@example.com", "BLOCKED"),
])
def test_a_missing_resource_fails_and_a_failed_query_blocks_never_passes(stderr, status):
    gates, result, check = run(FakeGcloud(errors={"keys describe": stderr}))
    assert gates["kms-crypto-key"] == status and result["verdict"] == "VAULT_KMS_PREFLIGHT_BLOCKED"
    assert "owner@example.com" not in json.dumps(check.gates)        # the account in gcloud's error is never recorded


def test_a_malformed_answer_fails():
    gates, _, _ = run(FakeGcloud(raw={"keyrings": "not json", "versions": '{"unexpected": "shape"}'}))
    assert gates["kms-key-ring"] == "FAIL" and gates["kms-key-versions"] == "FAIL"


def test_repository_checks_fail_on_a_wrong_seal_or_annotation(tmp_path, monkeypatch):
    text = (ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text()
    bad = tmp_path / "values.yaml"
    bad.write_text(text.replace('crypto_key = "vault-unseal"', 'crypto_key = "other"')
                   .replace(f"ewallet-vault-unseal@{P}", f"intruder@{P}"))
    monkeypatch.setattr(kp, "VALUES", bad)
    gates, _, _ = run(FakeGcloud(), mode="offline")
    assert gates["vault-seal-config"] == "FAIL" and gates["vault-ksa-annotation"] == "FAIL"


def test_cli_refuses_another_project_and_exits_nonzero_unless_ready(capsys):
    assert kp.main(["--mode", "cloud", "--project", "another"], runner=FakeGcloud()) == 2
    assert kp.main(["--mode", "offline"], runner=FakeGcloud()) == 1
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert shown[-1]["verdict"] == "VAULT_KMS_PREFLIGHT_OFFLINE_ONLY"


def test_cli_cloud_ready_when_every_gate_passes(tmp_path, monkeypatch):
    def fake_helm_and_gcloud(argv):
        if argv[0] == "helm":
            return 0, f"apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: vault\n  namespace: ewallet-vault\n" \
                      f"  annotations:\n    iam.gke.io/gcp-service-account: {GSA}\n", ""
        return FakeGcloud()(argv)
    chart = tmp_path / "vault.tgz"
    chart.write_text("x")
    assert kp.main(["--mode", "cloud", "--vault-chart", str(chart), "--out", str(tmp_path / "ev")], runner=fake_helm_and_gcloud) == 0
    lines = (tmp_path / "ev/kms-gates-cloud.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["verdict"] == "VAULT_KMS_PREFLIGHT_READY" and len(lines) == 12
