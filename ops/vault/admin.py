"""Operator-side Vault API: initialise, unseal, and apply declared configuration.

Reuses the workload transport. Every ``apply_*`` call is idempotent, so a repeated
bootstrap converges instead of failing. Nothing here logs a key or token.
"""

from __future__ import annotations

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

    def initialise(self, *, shares: int, threshold: int) -> dict[str, Any]:
        """Initialise Vault once; the result holds the unseal keys and root token."""
        return self._require(
            "POST", "sys/init", {"secret_shares": shares, "secret_threshold": threshold}
        )

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
