"""Operator-side Vault API: initialise, unseal, and apply declared configuration.

Reuses the workload transport. Every ``apply_*`` call is idempotent, so a repeated
bootstrap converges instead of failing. Nothing here logs a key or token.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_platform.inference.vault import VaultError, VaultTransport


class VaultAdmin:
    """Thin typed wrapper over the Vault system API."""

    def __init__(
        self,
        address: str,
        transport: VaultTransport,
        *,
        token: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._address = address.rstrip("/")
        self._transport = transport
        self._token = token
        self._timeout = timeout_seconds

    def _call(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> tuple[int, dict[str, Any]]:
        return self._transport(
            method,
            f"{self._address}/v1/{path}",
            token=self._token,
            payload=payload,
            timeout_seconds=self._timeout,
        )

    def _require(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        status, body = self._call(method, path, payload)
        if status not in (200, 204):
            raise VaultError("vault_admin_failed", f"{method} {path} returned {status}", status=status)
        return body

    def seal_status(self) -> dict[str, Any]:
        """Return initialised and sealed state; needs no token."""
        return self._require("GET", "sys/seal-status")

    def health(self) -> tuple[int, dict[str, Any]]:
        """Return the raw health status code and body."""
        return self._call("GET", "sys/health")

    def initialise(self, *, shares: int, threshold: int, recovery: bool = False) -> dict[str, Any]:
        """Initialise Vault once; the result holds the root token and either the Shamir unseal
        keys (``keys_base64``) or, with an auto-unseal seal, the recovery keys
        (``recovery_keys_base64``). Recovery keys cannot unseal Vault; they authorise root
        generation and rekeying only."""
        payload = ({"recovery_shares": shares, "recovery_threshold": threshold} if recovery
                   else {"secret_shares": shares, "secret_threshold": threshold})
        return self._require("POST", "sys/init", payload)

    def unseal(self, key: str) -> dict[str, Any]:
        """Submit one unseal key share."""
        return self._require("POST", "sys/unseal", {"key": key})

    def apply_kv(self, mount: str) -> None:
        """Enable a KV v2 engine unless the mount already exists."""
        if f"{mount}/" not in self._require("GET", "sys/mounts").get("data", {}):
            self._require("POST", f"sys/mounts/{mount}", {"type": "kv", "options": {"version": "2"}})

    def apply_audit(self, file_path: str) -> None:
        """Enable the file audit device unless it already exists."""
        if "file/" not in self._require("GET", "sys/audit").get("data", {}):
            self._require(
                "POST", "sys/audit/file", {"type": "file", "options": {"file_path": file_path}}
            )

    def apply_kubernetes_auth(self, mount: str, kubernetes_host: str) -> None:
        """Enable Kubernetes auth and point it at the in-cluster API server."""
        if f"{mount}/" not in self._require("GET", "sys/auth").get("data", {}):
            self._require("POST", f"sys/auth/{mount}", {"type": "kubernetes"})
        self._require("POST", f"auth/{mount}/config", {"kubernetes_host": kubernetes_host})

    def apply_policy(self, name: str, text: str) -> None:
        """Create or replace one ACL policy."""
        self._require("POST", f"sys/policies/acl/{name}", {"policy": text})

    def apply_role(self, mount: str, name: str, role: Mapping[str, object]) -> None:
        """Create or replace one Kubernetes auth role."""
        self._require("POST", f"auth/{mount}/role/{name}", role)

    def delete_role(self, mount: str, name: str) -> None:
        """Revoke future logins for a workload by removing its role."""
        self._require("DELETE", f"auth/{mount}/role/{name}")

    def put_kv(self, mount: str, path: str, data: Mapping[str, str]) -> int:
        """Write a new secret version and return its number."""
        body = self._require("POST", f"{mount}/data/{path}", {"data": dict(data)})
        return int(body["data"]["version"])

    def kv_versions(self, mount: str, path: str) -> dict[str, Any]:
        """Read secret metadata only: versions and timestamps, never values."""
        return self._require("GET", f"{mount}/metadata/{path}").get("data", {})

    def revoke_self(self) -> None:
        """Revoke the current token; used to retire the root token."""
        self._require("POST", "auth/token/revoke-self")

    def create_operator_token(self, policies: list[str], ttl: str) -> str:
        """Create an orphan, non-renewable token with only ``policies``.

        Orphan, so revoking the root token that created it does not revoke it too.
        """
        body = self._require("POST", "auth/token/create-orphan", {
            "policies": policies, "no_default_policy": True, "renewable": False,
            "ttl": ttl, "display_name": "secret-operator",
        })
        return str(body["auth"]["client_token"])

    def login_kubernetes(self, mount: str, role: str, jwt: str) -> str:
        """Exchange a ServiceAccount token for a Vault token of ``role``."""
        body = self._require("POST", f"auth/{mount}/login", {"role": role, "jwt": jwt})
        return str(body["auth"]["client_token"])

    def generate_root(self, keys: list[str]) -> str:
        """Generate a new root token from key shares (one-time-password flow): the unseal
        shares under Shamir, the recovery shares under auto-unseal (Vault chooses).

        Vault 2.x requires an authenticated caller (policy root-generation). Cancels any
        unfinished attempt first, submits shares until Vault reports completion and returns
        the decoded token. If the shares run out, the attempt is cancelled and the call fails.
        Every API error raises; none is ignored.
        """
        self._require("DELETE", "sys/generate-root/attempt")
        attempt = self._require("POST", "sys/generate-root/attempt", {})
        otp, nonce = str(attempt["otp"]), str(attempt["nonce"])
        for key in keys:
            progress = self._require("POST", "sys/generate-root/update", {"key": key, "nonce": nonce})
            if progress.get("complete"):
                return decode_root_token(str(progress["encoded_token"]), otp)
        self._require("DELETE", "sys/generate-root/attempt")
        raise VaultError("vault_generate_root_incomplete", "not enough key shares to generate a root token")

    def describe(self, auth_mount: str) -> dict[str, Any]:
        """Read the live configuration for evidence: engine types, auth methods, audit devices,
        Kubernetes roles (bindings, audience, TTLs, policies) and ACL policy text.

        Read-only, and no secret data: mounts, roles and policies hold configuration only, and
        no secret path is requested. Needs a token that may read sys/ and the role endpoints
        (the initial or a generated root token, before it is retired).
        """
        mounts = self._require("GET", "sys/mounts").get("data", {})
        auths = self._require("GET", "sys/auth").get("data", {})
        audits = self._require("GET", "sys/audit").get("data", {})
        role_names = self._require("GET", f"auth/{auth_mount}/role?list=true").get("data", {}).get("keys", [])
        policy_names = self._require("GET", "sys/policies/acl?list=true").get("data", {}).get("keys", [])
        fields = ("bound_service_account_names", "bound_service_account_namespaces", "audience",
                  "token_policies", "token_ttl", "token_max_ttl", "token_no_default_policy")
        roles = {}
        for name in sorted(role_names):
            role = self._require("GET", f"auth/{auth_mount}/role/{name}").get("data", {})
            roles[name] = {field: role.get(field) for field in fields}
        policies = {name: self._require("GET", f"sys/policies/acl/{name}").get("data", {}).get("policy")
                    for name in sorted(policy_names) if name not in ("root", "default")}
        return {
            "secret_engines": {path: {"type": m.get("type"), "version": (m.get("options") or {}).get("version")}
                               for path, m in sorted(mounts.items())
                               if m.get("type") not in ("system", "identity", "cubbyhole")},
            "auth_methods": {path: a.get("type") for path, a in sorted(auths.items())},
            "audit_devices": {path: a.get("type") for path, a in sorted(audits.items())},
            "kubernetes_roles": roles,
            "acl_policies": policies,
        }

    def snapshot_path(self) -> str:
        """Return the Raft snapshot endpoint used for backups."""
        return f"{self._address}/v1/sys/storage/raft/snapshot"


def apply_bootstrap(
    admin: VaultAdmin, config: Mapping[str, Any], *, repo_root: Path, kubernetes_host: str
) -> list[str]:
    """Apply the declared mounts, audit, policies and roles; return step names."""
    mount = config["kubernetes_auth_mount"]
    admin.apply_audit(config["audit_file"])
    admin.apply_kv(config["kv_mount"])
    admin.apply_kubernetes_auth(mount, kubernetes_host)
    for name, path in sorted(config["policies"].items()):
        admin.apply_policy(name, (repo_root / path).read_text(encoding="utf-8"))
    for name, role in sorted(config["roles"].items()):
        admin.apply_role(mount, name, role)
    return ["audit", "kv", "kubernetes-auth", *sorted(config["policies"]), *sorted(config["roles"])]


def decode_root_token(encoded: str, otp: str) -> str:
    """Decode a generated root token: base64 (unpadded) XOR the one-time password."""
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(raw) != len(otp):
        raise VaultError("vault_generate_root_decode", "encoded token and OTP lengths differ")
    return bytes(a ^ b for a, b in zip(raw, otp.encode())).decode()
