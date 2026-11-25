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
    assert "auth/kubernetes/role/eso-ewallet-reader" in writes and "auth/kubernetes/role/vault-admin" in writes
    assert not any(path.endswith(("/role/fraud-api", "/role/feature-api")) for path in writes)   # V1 direct loader retired


def test_reader_role_is_scoped_and_short_lived():
    """V2: the only reader role binds one dedicated identity (V1's per-workload direct-loader roles are retired)."""
    role = CONFIG["roles"]["eso-ewallet-reader"]
    assert role["bound_service_account_names"] == ["eso-vault-reader"]
    assert role["bound_service_account_namespaces"] == ["external-secrets"]
    assert role["token_no_default_policy"] is True
    assert role["audience"] == "vault"
    assert role["token_ttl"] == "5m"


def test_reader_policy_grants_read_only_on_two_prefixes():
    text = (ROOT / "infra/kubernetes/vault/policies/eso-ewallet-reader.hcl").read_text()
    assert 'path "ewallet/data/ml-platform/*"' in text and 'path "ewallet/data/agent-platform/*"' in text
    assert 'path "auth/token/revoke-self"' in text and 'path "auth/token/lookup-self"' in text
    import re
    grants = dict(re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text))
    assert grants == {"ewallet/data/ml-platform/*": '"read"', "ewallet/data/agent-platform/*": '"read"',
                      "auth/token/lookup-self": '"read"', "auth/token/revoke-self": '"update"'}


def test_gke_values_never_use_dev_mode_or_plain_http():
    text = (ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text()
    assert "dev:" not in text
    assert "tlsDisable: false" in text
    assert "tls_disable" not in text
    assert "@sha256:" in text


# ---- token lifecycle: root -> orphan secret-operator token -> retire root -> generate-root only for admin

class Fake:
    """Answer by path and record (method, path, token, payload)."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, method, url, *, token, payload, timeout_seconds):
        path = url.split("/v1/")[1]
        self.calls.append((method, path, token, payload))
        answer = self.answers.get((method, path), (200, {"data": {"version": 3, "current_version": 3}}))
        return answer(payload) if callable(answer) else answer


def run_cli(monkeypatch, capsys, fake, *argv):
    monkeypatch.setattr(cli, "UrllibVaultTransport", lambda ca_file: fake)
    code = cli.main(list(argv))
    return code, capsys.readouterr()


def keys_file(tmp_path, root="root-token-value"):
    path = tmp_path / "vault-keys.json"
    cli.write_keys_file(path, {"root_token": root, "keys_base64": ["share-1", "share-2", "share-3"]})
    return path


def test_operator_token_is_an_orphan_with_only_the_operator_policy(tmp_path, monkeypatch, capsys):
    fake = Fake({("POST", "auth/token/create-orphan"): (200, {"auth": {"client_token": "operator-token-value"}})})
    out = tmp_path / "operator-token.json"
    code, printed = run_cli(monkeypatch, capsys, fake, "operator-token", "--keys-file", str(keys_file(tmp_path)), "--token-out", str(out))
    assert code == 0
    (method, path, token, payload), = fake.calls
    assert (method, path, token) == ("POST", "auth/token/create-orphan", "root-token-value")
    assert payload["policies"] == ["secret-operator"] and payload["no_default_policy"] is True
    assert payload["renewable"] is False and payload["ttl"] == "720h"
    assert out.stat().st_mode & 0o777 == 0o600 and cli.read_token_file(out) == "operator-token-value"
    assert "operator-token-value" not in printed.out + printed.err and "root-token-value" not in printed.out + printed.err


def test_after_root_retirement_writes_and_metadata_use_the_operator_token(tmp_path, monkeypatch, capsys):
    out = tmp_path / "operator-token.json"
    cli.write_keys_file(out, {"token": "operator-token-value"})
    keys = keys_file(tmp_path)
    fake = Fake()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("new-value"))
    code, _ = run_cli(monkeypatch, capsys, fake, "put", "--keys-file", str(keys), "--token-file", str(out),
                      "--path", "ml-platform/feast-online-redis", "--key", "connection_string", "--stdin")
    assert code == 0
    code, printed = run_cli(monkeypatch, capsys, fake, "versions", "--token-file", str(out), "--path", "ml-platform/feast-online-redis")
    assert code == 0 and json.loads(printed.out)["current_version"] == 3
    assert [(m, p, t) for m, p, t, _ in fake.calls] == [
        ("POST", "ewallet/data/ml-platform/feast-online-redis", "operator-token-value"),   # --token-file wins over --keys-file
        ("GET", "ewallet/metadata/ml-platform/feast-online-redis", "operator-token-value"),
    ]


def test_a_retired_root_token_fails_closed_with_a_safe_message(tmp_path, monkeypatch, capsys):
    fake = Fake({("GET", "ewallet/metadata/ml-platform/feast-online-redis"): (403, {"errors": ["permission denied"]})})
    code, printed = run_cli(monkeypatch, capsys, fake, "versions", "--keys-file", str(keys_file(tmp_path)), "--path", "ml-platform/feast-online-redis")
    assert code == 2 and "REFUSED vault_admin_failed" in printed.err and "root-token-value" not in printed.err


def ceremony(otp="O" * 29, root="hvs.generated-root-token-0123", complete_after=2):
    """A fake root-generation ceremony that completes after ``complete_after`` shares."""
    import base64
    encoded = base64.b64encode(bytes(a ^ b for a, b in zip(root.encode(), otp.encode()))).decode().rstrip("=")
    progress = iter([{"complete": False}] * (complete_after - 1) + [{"complete": True, "encoded_token": encoded}])
    return {
        ("DELETE", "sys/generate-root/attempt"): (204, {}),
        ("POST", "sys/generate-root/attempt"): (200, {"otp": otp, "nonce": "n-1", "started": True}),
        ("POST", "sys/generate-root/update"): lambda payload: (200, next(progress, {"complete": False})),
    }


def admin_login_file(tmp_path, token="admin-login-token"):
    path = tmp_path / "admin-login.json"
    cli.write_keys_file(path, {"token": token, "role": "vault-admin"})
    return path


def test_an_authorized_ceremony_uses_the_admin_login_and_stops_at_the_threshold(tmp_path, monkeypatch, capsys):
    fake = Fake(ceremony())
    out = tmp_path / "admin-token.json"
    code, printed = run_cli(monkeypatch, capsys, fake, "generate-root", "--keys-file", str(keys_file(tmp_path)),
                            "--token-file", str(admin_login_file(tmp_path)), "--token-out", str(out))
    assert code == 0 and cli.read_token_file(out) == "hvs.generated-root-token-0123"
    assert all(token == "admin-login-token" for _, _, token, _ in fake.calls)   # authenticated, never anonymous
    updates = [payload for method, path, _, payload in fake.calls if path == "sys/generate-root/update"]
    assert updates == [{"key": "share-1", "nonce": "n-1"}, {"key": "share-2", "nonce": "n-1"}]
    assert "hvs.generated-root-token-0123" not in printed.out + printed.err and "O" * 29 not in printed.out


def test_generate_root_refuses_to_run_without_an_admin_login(tmp_path, monkeypatch, capsys):
    fake = Fake(ceremony())
    code, printed = run_cli(monkeypatch, capsys, fake, "generate-root", "--keys-file", str(keys_file(tmp_path)),
                            "--token-out", str(tmp_path / "admin-token.json"))
    assert code == 2 and "vault_admin_login_required" in printed.err and fake.calls == []


def test_an_unauthorized_token_is_rejected_and_nothing_is_written(tmp_path, monkeypatch, capsys):
    answers = {("DELETE", "sys/generate-root/attempt"): (403, {"errors": ["permission denied"]})}
    fake = Fake(answers)
    out = tmp_path / "admin-token.json"
    code, printed = run_cli(monkeypatch, capsys, fake, "generate-root", "--keys-file", str(keys_file(tmp_path)),
                            "--token-file", str(admin_login_file(tmp_path, "operator-token-value")), "--token-out", str(out))
    assert code == 2 and "returned 403" in printed.err and not out.exists()
    assert [(m, p) for m, p, _, _ in fake.calls] == [("DELETE", "sys/generate-root/attempt")]   # stops at the first error


def test_fewer_shares_than_the_threshold_cannot_complete_and_the_attempt_is_cancelled(tmp_path, monkeypatch, capsys):
    fake = Fake(ceremony(complete_after=2))
    one_share = tmp_path / "one-share.json"
    cli.write_keys_file(one_share, {"keys_base64": ["share-1"]})
    out = tmp_path / "admin-token.json"
    code, printed = run_cli(monkeypatch, capsys, fake, "generate-root", "--keys-file", str(one_share),
                            "--token-file", str(admin_login_file(tmp_path)), "--token-out", str(out))
    assert code == 2 and "vault_generate_root_incomplete" in printed.err and not out.exists()
    assert [(m, p) for m, p, _, _ in fake.calls][-1] == ("DELETE", "sys/generate-root/attempt")


def test_login_exchanges_a_service_account_token_from_stdin(tmp_path, monkeypatch, capsys):
    import io
    fake = Fake({("POST", "auth/kubernetes/login"): (200, {"auth": {"client_token": "admin-login-token"}})})
    out = tmp_path / "admin-login.json"
    monkeypatch.setattr("sys.stdin", io.StringIO("eyJ.service-account.jwt\n"))
    code, printed = run_cli(monkeypatch, capsys, fake, "login", "--role", "vault-admin", "--token-out", str(out))
    assert code == 0 and cli.read_token_file(out) == "admin-login-token"
    (method, path, token, payload), = fake.calls
    assert (method, path, token, payload) == ("POST", "auth/kubernetes/login", None, {"role": "vault-admin", "jwt": "eyJ.service-account.jwt"})
    assert "admin-login-token" not in printed.out and "eyJ" not in printed.out
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, printed = run_cli(monkeypatch, capsys, fake, "login", "--token-out", str(tmp_path / "again.json"))
    assert code == 2 and "vault_empty_jwt" in printed.err


def test_token_files_are_never_written_inside_the_repository_or_over_an_existing_file(tmp_path, monkeypatch, capsys):
    fake = Fake({("POST", "auth/token/create-orphan"): (200, {"auth": {"client_token": "x"}})})
    keys = keys_file(tmp_path)
    code, printed = run_cli(monkeypatch, capsys, fake, "operator-token", "--keys-file", str(keys),
                            "--token-out", str(ROOT / "operator-token.json"))
    assert code == 2 and "vault_keys_location" in printed.err and fake.calls == []
    existing = tmp_path / "existing.json"
    existing.write_text("{}")
    code, printed = run_cli(monkeypatch, capsys, fake, "operator-token", "--keys-file", str(keys), "--token-out", str(existing))
    assert code == 2 and "vault_token_file_exists" in printed.err and fake.calls == []


def test_operator_policy_writes_and_reads_metadata_but_never_reads_values_or_administers():
    text = (ROOT / "infra/kubernetes/vault/policies/secret-operator.hcl").read_text()
    import re
    grants = dict(re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text))
    for path, caps in grants.items():
        assert path.startswith(("ewallet/data/", "ewallet/metadata/")), path
        if "/data/" in path:
            assert "read" not in caps and "delete" not in caps
    assert not any(p.startswith(("sys/", "auth/")) for p in grants)


def test_root_generation_is_authenticated_and_only_the_admin_login_may_run_it():
    import re
    values = (ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text()
    assert "enable_unauthenticated_access" not in values      # keep the Vault 2.0 authenticated default
    grants = dict(re.findall(r'path "([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]',
                             (ROOT / CONFIG["policies"]["root-generation"]).read_text()))
    assert set(grants) == {"sys/generate-root/attempt", "sys/generate-root/update"}
    assert "sudo" not in "".join(grants.values())
    holders = [name for name, role in CONFIG["roles"].items() if "root-generation" in role["token_policies"]]
    assert holders == ["vault-admin"]
    admin = CONFIG["roles"]["vault-admin"]
    assert admin["bound_service_account_names"] == ["vault-admin"]
    assert admin["bound_service_account_namespaces"] == ["ewallet-vault"]
    assert admin["token_policies"] == ["root-generation"] and admin["token_no_default_policy"] is True
    assert admin["token_ttl"] == "15m"
    for name, path in CONFIG["policies"].items():
        if name != "root-generation":
            assert "sys/" not in (ROOT / path).read_text(), name


def test_single_replica_vault_has_no_disruption_budget():
    """A maxUnavailable-0 budget would stall the cloud-lab resize to zero (evaluate_pdbs checks minAvailable only)."""
    import yaml
    values = yaml.safe_load((ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text())
    assert values["server"]["ha"]["replicas"] == 1
    assert values["server"]["ha"]["disruptionBudget"] == {"enabled": False}


# ---- init: the key file exists before Vault initialises; unrecoverable windows are named, never hidden

INIT_MATERIAL = {"keys_base64": ["share-1", "share-2", "share-3"], "root_token": "root-token-value"}


def init_fake(tmp_path, *, initialise, after=False):
    """sys/init answers with ``initialise``; seal-status reports ``after`` once init was attempted."""
    state = {"attempted": False}
    out = tmp_path / "vault-keys.json"

    def seal(_):
        return 200, {"initialized": after if state["attempted"] else False, "sealed": True}

    def init(_):
        state["attempted"] = True
        assert out.exists() and out.stat().st_mode & 0o777 == 0o600   # created before Vault is asked
        return initialise()
    return Fake({("GET", "sys/seal-status"): seal, ("POST", "sys/init"): init}), out


def test_init_stores_the_material_in_a_file_created_before_vault_initialises(tmp_path, monkeypatch, capsys):
    fake, out = init_fake(tmp_path, initialise=lambda: (200, INIT_MATERIAL))
    code, printed = run_cli(monkeypatch, capsys, fake, "init", "--keys-out", str(out))
    assert code == 0 and json.loads(out.read_text()) == INIT_MATERIAL
    assert "share-1" not in printed.out + printed.err and "root-token-value" not in printed.out + printed.err


def test_init_refuses_an_existing_key_file_before_calling_vault(tmp_path, monkeypatch, capsys):
    fake, out = init_fake(tmp_path, initialise=lambda: (200, INIT_MATERIAL))
    out.write_text("{}")
    with pytest.raises(FileExistsError):
        run_cli(monkeypatch, capsys, fake, "init", "--keys-out", str(out))
    assert ("POST", "sys/init") not in [(m, p) for m, p, _, _ in fake.calls]


def test_init_rejected_by_an_uninitialised_vault_can_be_retried(tmp_path, monkeypatch, capsys):
    fake, out = init_fake(tmp_path, initialise=lambda: (500, {"errors": ["boom"]}), after=False)
    code, printed = run_cli(monkeypatch, capsys, fake, "init", "--keys-out", str(out))
    assert code == 2 and "REFUSED vault_admin_failed" in printed.err and not out.exists()


def test_a_lost_answer_after_vault_initialised_says_stop(tmp_path, monkeypatch, capsys):
    def lost():
        raise VaultError("vault_unreachable", "connection reset")
    fake, out = init_fake(tmp_path, initialise=lost, after=True)
    code, printed = run_cli(monkeypatch, capsys, fake, "init", "--keys-out", str(out))
    assert code == 2 and "REFUSED vault_initialised_keys_not_stored" in printed.err
    assert "never run init again" in printed.err and not out.exists()


def test_a_failed_key_write_after_vault_answered_says_stop(tmp_path, monkeypatch, capsys):
    fake, out = init_fake(tmp_path, initialise=lambda: (200, INIT_MATERIAL), after=True)

    def disk_full(handle, material):
        handle.close()
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cli, "_store", disk_full)
    code, printed = run_cli(monkeypatch, capsys, fake, "init", "--keys-out", str(out))
    assert code == 2 and "REFUSED vault_initialised_keys_not_stored" in printed.err
    assert "share-1" not in printed.err and "root-token-value" not in printed.err


# ---- describe: live configuration for evidence, read-only, no secret data

def test_describe_reads_configuration_only(tmp_path, monkeypatch, capsys):
    role = {"bound_service_account_names": ["eso-vault-reader"], "bound_service_account_namespaces": ["ml-platform"],
            "audience": "vault", "token_policies": ["eso-ewallet-reader"], "token_ttl": 300, "token_max_ttl": 900,
            "token_no_default_policy": True, "alias_name_source": "serviceaccount_uid"}
    fake = Fake({
        ("GET", "sys/mounts"): (200, {"data": {"ewallet/": {"type": "kv", "options": {"version": "2"}},
                                               "sys/": {"type": "system"}, "cubbyhole/": {"type": "cubbyhole"}}}),
        ("GET", "sys/auth"): (200, {"data": {"kubernetes/": {"type": "kubernetes"}, "token/": {"type": "token"}}}),
        ("GET", "sys/audit"): (200, {"data": {"file/": {"type": "file"}}}),
        ("GET", "auth/kubernetes/role?list=true"): (200, {"data": {"keys": ["eso-ewallet-reader"]}}),
        ("GET", "auth/kubernetes/role/eso-ewallet-reader"): (200, {"data": role}),
        ("GET", "sys/policies/acl?list=true"): (200, {"data": {"keys": ["default", "eso-ewallet-reader", "root"]}}),
        ("GET", "sys/policies/acl/eso-ewallet-reader"): (200, {"data": {"policy": 'path "ewallet/data/ml-platform/feast-online-redis" {}'}}),
    })
    code, printed = run_cli(monkeypatch, capsys, fake, "describe", "--keys-file", str(keys_file(tmp_path)))
    shown = json.loads(printed.out)
    assert code == 0
    assert shown["secret_engines"] == {"ewallet/": {"type": "kv", "version": "2"}}
    assert shown["kubernetes_roles"]["eso-ewallet-reader"]["audience"] == "vault"
    assert "alias_name_source" not in shown["kubernetes_roles"]["eso-ewallet-reader"]
    assert list(shown["acl_policies"]) == ["eso-ewallet-reader"]
    assert {m for m, _, _, _ in fake.calls} == {"GET"}
    assert not any(p.startswith("ewallet/") for _, p, _, _ in fake.calls)       # no secret path is read
    assert "root-token-value" not in printed.out + printed.err
