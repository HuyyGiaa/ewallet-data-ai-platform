"""Load workload secrets from HashiCorp Vault with Kubernetes identity.

The caller's projected ServiceAccount token is exchanged for a short-lived Vault
token, the mapped KV v2 values are copied into the process environment, and the
Vault token is revoked. Unset ``EWALLET_VAULT_ADDR`` leaves the process exactly as
before, so an existing ``secretKeyRef`` keeps working. Once enabled the loader
fails closed: a workload that cannot read its secret must not start with a stale
one. Secret values never appear in logs, errors or return values.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import ssl
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_TOKEN_PATH = "/var/run/secrets/vault/token"
DEFAULT_AUTH_MOUNT = "kubernetes"
DEFAULT_KV_MOUNT = "ewallet"
MAX_RESPONSE_BYTES = 1_048_576
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
logger = logging.getLogger("ewallet.vault")


class VaultError(RuntimeError):
    """A Vault step failed; the message never contains a secret value."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class VaultTransport(Protocol):
    """Send one Vault API request and return its status and decoded body."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        token: str | None,
        payload: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, object]]: ...


class UrllibVaultTransport:
    """Dependency-free HTTPS transport that trusts one explicit CA bundle."""

    def __init__(self, ca_path: str | None = None) -> None:
        self._context = ssl.create_default_context(cafile=ca_path) if ca_path else None

    def __call__(
        self,
        method: str,
        url: str,
        *,
        token: str | None,
        payload: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, object]]:
        """Perform the request; HTTP error statuses are returned, not raised."""
        headers = {"accept": "application/json"}
        if token:
            headers["x-vault-token"] = token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds, context=self._context) as response:
                return response.status, _decode(response.read(MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            return exc.code, _decode(exc.read(MAX_RESPONSE_BYTES + 1))
        except (URLError, TimeoutError, OSError) as exc:
            raise VaultError("vault_unreachable", "Vault request failed") from exc


def _decode(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise VaultError("vault_response_too_large", "Vault response exceeds its byte budget")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaultError("vault_invalid_response", "Vault returned invalid JSON") from exc
    return decoded if isinstance(decoded, dict) else {}


@dataclass(frozen=True)
class SecretMapping:
    """Copy one key of one KV v2 secret into one environment variable."""

    env_name: str
    path: str
    key: str


@dataclass(frozen=True)
class LoadedSecret:
    """Audit record of a loaded secret: its location and version, never its value."""

    env_name: str
    path: str
    version: int


@dataclass(frozen=True)
class VaultSettings:
    """Validated workload configuration for :func:`load_secrets`."""

    address: str
    role: str
    mappings: tuple[SecretMapping, ...]
    token_path: str = DEFAULT_TOKEN_PATH
    ca_path: str | None = None
    auth_mount: str = DEFAULT_AUTH_MOUNT
    kv_mount: str = DEFAULT_KV_MOUNT
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> "VaultSettings | None":
        """Read ``EWALLET_VAULT_*``; return None when Vault is not enabled."""
        address = values.get("EWALLET_VAULT_ADDR", "").strip().rstrip("/")
        if not address:
            return None
        parsed = urlsplit(address)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in _LOOPBACK
        ):
            raise VaultError("vault_configuration", "Vault address must use https")
        role = values.get("EWALLET_VAULT_ROLE", "").strip()
        if not role:
            raise VaultError("vault_configuration", "EWALLET_VAULT_ROLE is required")
        return cls(
            address=address,
            role=role,
            mappings=_parse_mappings(values.get("EWALLET_VAULT_SECRETS", "")),
            token_path=values.get("EWALLET_VAULT_TOKEN_PATH", DEFAULT_TOKEN_PATH),
            ca_path=values.get("EWALLET_VAULT_CA_PATH") or None,
            auth_mount=values.get("EWALLET_VAULT_AUTH_MOUNT", DEFAULT_AUTH_MOUNT),
            kv_mount=values.get("EWALLET_VAULT_KV_MOUNT", DEFAULT_KV_MOUNT),
        )


def _parse_mappings(raw: str) -> tuple[SecretMapping, ...]:
    """Parse ``{"ENV": "path#key"}``; the mapping must not be empty."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VaultError("vault_configuration", "EWALLET_VAULT_SECRETS must be JSON") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise VaultError("vault_configuration", "EWALLET_VAULT_SECRETS must be a non-empty object")
    mappings = []
    for env_name, target in decoded.items():
        path, separator, key = str(target).partition("#")
        if not separator or not path or not key:
            raise VaultError("vault_configuration", "secret target must look like path#key")
        mappings.append(SecretMapping(str(env_name), path, key))
    return tuple(mappings)


class VaultClient:
    """The three Vault operations a workload needs, over an injected transport."""

    def __init__(
        self, address: str, transport: VaultTransport, *, timeout_seconds: float = 5.0
    ) -> None:
        self._address = address.rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds

    def _call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        return self._transport(
            method,
            f"{self._address}/v1/{path}",
            token=token,
            payload=payload,
            timeout_seconds=self._timeout,
        )

    def login_kubernetes(self, *, role: str, jwt: str, mount: str = DEFAULT_AUTH_MOUNT) -> str:
        """Exchange a ServiceAccount JWT for a Vault client token."""
        status, body = self._call(
            "POST", f"auth/{quote(mount)}/login", payload={"role": role, "jwt": jwt}
        )
        auth = body.get("auth")
        if status != 200 or not isinstance(auth, dict) or not auth.get("client_token"):
            raise VaultError("vault_login_denied", "Vault Kubernetes login failed", status=status)
        return str(auth["client_token"])

    def read_kv(self, *, token: str, mount: str, path: str) -> tuple[dict[str, object], int]:
        """Read the current version of a KV v2 secret and return data and version."""
        status, body = self._call("GET", f"{quote(mount)}/data/{quote(path)}", token=token)
        if status == 403:
            raise VaultError("vault_read_denied", "Vault policy denied the read", status=status)
        if status == 404:
            raise VaultError("vault_secret_missing", "Vault secret does not exist", status=status)
        data = body.get("data")
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise VaultError("vault_read_failed", "Vault read failed", status=status)
        metadata = data.get("metadata")
        version = metadata.get("version", 0) if isinstance(metadata, dict) else 0
        return data["data"], int(version)

    def revoke_self(self, token: str) -> None:
        """Revoke a client token; a failure is reported to the caller."""
        status, _ = self._call("POST", "auth/token/revoke-self", token=token)
        if status not in (200, 204):
            raise VaultError("vault_revoke_failed", "Vault token revocation failed", status=status)


def _revoke_quietly(client: VaultClient, token: str) -> None:
    """Revoke as hygiene: the short TTL still bounds the token if this fails.

    Raising here would hide the read failure that brought us to this point.
    """
    try:
        client.revoke_self(token)
    except VaultError as error:
        logger.warning('{"event":"vault_token_revoke_failed","code":"%s"}', error.code)


def load_secrets(
    settings: VaultSettings,
    client: VaultClient,
    environment: MutableMapping[str, str],
) -> tuple[LoadedSecret, ...]:
    """Fetch every mapped secret, then publish them to ``environment`` together.

    Nothing is written unless every read succeeds, so a partial failure cannot
    leave a mix of Vault and platform values. The token is always revoked.
    """
    try:
        jwt = Path(settings.token_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultError("vault_identity_missing", "ServiceAccount token is unavailable") from exc
    token = client.login_kubernetes(role=settings.role, jwt=jwt, mount=settings.auth_mount)
    staged: dict[str, str] = {}
    loaded: list[LoadedSecret] = []
    try:
        for mapping in settings.mappings:
            data, version = client.read_kv(
                token=token, mount=settings.kv_mount, path=mapping.path
            )
            value = data.get(mapping.key)
            if not isinstance(value, str) or not value:
                raise VaultError("vault_key_missing", f"key is missing from {mapping.path}")
            staged[mapping.env_name] = value
            loaded.append(LoadedSecret(mapping.env_name, mapping.path, version))
    finally:
        _revoke_quietly(client, token)
    environment.update(staged)
    return tuple(loaded)


def load_from_environment(
    environment: MutableMapping[str, str],
    transport: VaultTransport | None = None,
) -> tuple[LoadedSecret, ...]:
    """Run the loader when Vault is configured; return nothing when it is not."""
    settings = VaultSettings.from_env(environment)
    if settings is None:
        return ()
    client = VaultClient(
        settings.address,
        transport or UrllibVaultTransport(settings.ca_path),
        timeout_seconds=settings.timeout_seconds,
    )
    return load_secrets(settings, client, environment)
