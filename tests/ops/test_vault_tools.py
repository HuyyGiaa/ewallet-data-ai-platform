"""Key handling and declared Vault configuration, without a server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_platform.inference.vault import VaultError
from ops.vault.admin import VaultAdmin, apply_bootstrap
from ops.vault import cli

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "infra/kubernetes/vault/bootstrap.json").read_text())


def test_key_file_inside_the_repository_is_refused():
    with pytest.raises(VaultError) as caught:
        cli.write_keys_file(ROOT / "keys.json", {"root_token": "x"})
    assert caught.value.code == "vault_keys_location"


def test_key_file_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "keys.json"
    cli.write_keys_file(path, {"root_token": "x"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.write_keys_file(path, {})


def test_readable_key_file_is_refused(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(VaultError):
        cli.read_keys_file(path)


class Recording:
    """Accept every call and pretend nothing is mounted yet."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, *, token, payload, timeout_seconds):
        self.calls.append((method, url.split("/v1/")[1], payload))
        return 200, {"data": {}}


def test_bootstrap_applies_audit_before_any_secret_engine():
    transport = Recording()
    apply_bootstrap(VaultAdmin("https://v", transport, token="t"), CONFIG, repo_root=ROOT, kubernetes_host="https://k")
    writes = [path for method, path, _ in transport.calls if method == "POST"]
    assert writes[0] == "sys/audit/file"
    assert "sys/mounts/ewallet" in writes
    assert "auth/kubernetes/role/fraud-api" in writes


def test_workload_role_is_scoped_and_short_lived():
    role = CONFIG["roles"]["fraud-api"]
    assert role["bound_service_account_namespaces"] == ["ml-platform"]
    assert "*" not in role["bound_service_account_names"]
    assert role["token_no_default_policy"] is True
    assert role["audience"] == "vault"
    assert role["token_ttl"] == "5m"


def test_workload_policy_grants_one_read_only_path():
    text = (ROOT / "infra/kubernetes/vault/policies/fraud-api.hcl").read_text()
    assert 'path "ewallet/data/fraud-api/feast-redis"' in text
    assert 'path "auth/token/revoke-self"' in text
    assert text.count("path ") == 2
    assert text.count('["read"]') == 1
    assert "*" not in text


def test_gke_values_never_use_dev_mode_or_plain_http():
    text = (ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text()
    assert "dev:" not in text
    assert "tlsDisable: false" in text
    assert "tls_disable" not in text
    assert "@sha256:" in text
