"""Vault loader behaviour with a scripted Vault API."""

from __future__ import annotations

import json

import pytest

from ai_platform.inference.vault import (
    VaultClient,
    VaultError,
    VaultSettings,
    load_from_environment,
    load_secrets,
)

SECRET_VALUE = "redis://:s3cret@redis:6379"


class FakeVault:
    """Answer the three workload calls and remember what was asked."""

    def __init__(self, *, login=200, read=200, data=None):
        self.login, self.read = login, read
        self.data = {"connection_string": SECRET_VALUE} if data is None else data
        self.calls = []

    def __call__(self, method, url, *, token, payload, timeout_seconds):
        self.calls.append((method, url, token))
        if url.endswith("/auth/kubernetes/login"):
            body = {"auth": {"client_token": "tok"}} if self.login == 200 else {}
            return self.login, body
        if url.endswith("/auth/token/revoke-self"):
            return 204, {}
        return self.read, {"data": {"data": self.data, "metadata": {"version": 7}}}


@pytest.fixture
def settings(tmp_path):
    token = tmp_path / "token"
    token.write_text("jwt\n")
    return VaultSettings(
        address="https://vault:8200",
        role="fraud-api",
        mappings=VaultSettings.from_env(_env()).mappings,
        token_path=str(token),
    )


def _env(**extra):
    return {
        "EWALLET_VAULT_ADDR": "https://vault:8200",
        "EWALLET_VAULT_ROLE": "fraud-api",
        "EWALLET_VAULT_SECRETS": json.dumps({"REDIS": "fraud-api/feast-redis#connection_string"}),
        **extra,
    }


def test_unset_address_leaves_the_environment_alone():
    environment = {"REDIS": "from-kubernetes-secret"}
    assert load_from_environment(environment) == ()
    assert environment == {"REDIS": "from-kubernetes-secret"}


@pytest.mark.parametrize(
    "values",
    [
        _env(EWALLET_VAULT_ADDR="http://vault:8200"),
        _env(EWALLET_VAULT_ROLE=""),
        _env(EWALLET_VAULT_SECRETS="{}"),
        _env(EWALLET_VAULT_SECRETS='{"REDIS": "no-key"}'),
        _env(EWALLET_VAULT_SECRETS="not json"),
    ],
)
def test_invalid_configuration_is_refused(values):
    with pytest.raises(VaultError):
        VaultSettings.from_env(values)


def test_plain_http_is_allowed_only_on_loopback():
    assert VaultSettings.from_env(_env(EWALLET_VAULT_ADDR="http://127.0.0.1:8200"))


def test_loads_value_reports_version_and_revokes_token(settings):
    vault = FakeVault()
    environment = {}
    loaded = load_secrets(settings, VaultClient(settings.address, vault), environment)
    assert environment == {"REDIS": SECRET_VALUE}
    assert (loaded[0].path, loaded[0].version) == ("fraud-api/feast-redis", 7)
    assert SECRET_VALUE not in repr(loaded)
    assert vault.calls[-1][1].endswith("/auth/token/revoke-self")


@pytest.mark.parametrize(
    ("vault", "code"),
    [
        (FakeVault(login=403), "vault_login_denied"),
        (FakeVault(read=403), "vault_read_denied"),
        (FakeVault(read=404), "vault_secret_missing"),
        (FakeVault(data={"other": "x"}), "vault_key_missing"),
    ],
)
def test_every_failure_keeps_the_environment_empty(settings, vault, code):
    environment = {}
    with pytest.raises(VaultError) as caught:
        load_secrets(settings, VaultClient(settings.address, vault), environment)
    assert caught.value.code == code
    assert environment == {}
    assert SECRET_VALUE not in str(caught.value)


def test_token_is_revoked_even_when_a_read_fails(settings):
    vault = FakeVault(read=403)
    with pytest.raises(VaultError):
        load_secrets(settings, VaultClient(settings.address, vault), {})
    assert vault.calls[-1][1].endswith("/auth/token/revoke-self")


def test_partial_failure_publishes_nothing(settings, tmp_path):
    two = VaultSettings.from_env(
        _env(EWALLET_VAULT_SECRETS=json.dumps({"A": "p#connection_string", "B": "p#absent"}))
    )
    two = VaultSettings(two.address, two.role, two.mappings, token_path=settings.token_path)
    environment = {}
    with pytest.raises(VaultError):
        load_secrets(two, VaultClient(two.address, FakeVault()), environment)
    assert environment == {}


def test_missing_identity_token_fails_closed(settings, tmp_path):
    broken = VaultSettings(settings.address, settings.role, settings.mappings, token_path=str(tmp_path / "none"))
    with pytest.raises(VaultError) as caught:
        load_secrets(broken, VaultClient(broken.address, FakeVault()), {})
    assert caught.value.code == "vault_identity_missing"


def test_failed_revocation_never_hides_the_read_failure(settings):
    class NoRevoke(FakeVault):
        def __call__(self, method, url, *, token, payload, timeout_seconds):
            if url.endswith("/auth/token/revoke-self"):
                return 403, {}
            return super().__call__(method, url, token=token, payload=payload, timeout_seconds=timeout_seconds)

    with pytest.raises(VaultError) as caught:
        load_secrets(settings, VaultClient(settings.address, NoRevoke(read=403)), {})
    assert caught.value.code == "vault_read_denied"
