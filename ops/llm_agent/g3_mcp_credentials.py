"""Prepare immutable, release-scoped MCP credentials for a G3 rollout.

Credential values are generated in memory, sent directly to Kubernetes and
never printed or written to disk. A release is prepared exactly once:

* client Secrets are named ``mcp-token-<subject>-<server>-<release>``;
* server tables are named ``mcp-auth-<server>-<server-set>``;
* every Secret is immutable and carries a non-secret plan fingerprint.

Transition server sets contain both the previous active credentials and the
new release credentials. This lets old clients remain valid while MCP server
pods roll. Client references move only after every server pod uses the
transition table. A later final server set contains only the new credentials;
deleting the old set is a separate, approval-gated operation.

Repeated preparation is metadata-only and idempotent when the complete set
already exists with the expected fingerprint. A partial or conflicting set
fails closed and is never overwritten.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
from typing import Callable, Iterable, Mapping, Protocol, Sequence

import yaml  # type: ignore[import-untyped]

NAMESPACE = "ewallet-agents"
TOKEN_BYTES = 48
RELEASE_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,22}[a-z0-9])?")
NAMESPACE_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
FINGERPRINT_ANNOTATION = "ewallet.io/credential-plan-sha256"
RELEASE_LABEL = "ewallet.io/credential-release"
SET_LABEL = "ewallet.io/credential-set"


@dataclass(frozen=True)
class Credential:
    subject: str
    server: str
    token: str
    release: str
    status: str = "active"

    @property
    def credential_id(self) -> str:
        return f"{self.subject}.{self.server}.{self.release}.{self.status}"


@dataclass(frozen=True)
class SecretIdentity:
    namespace: str
    name: str


@dataclass(frozen=True)
class ReleasePlan:
    """Non-secret identity and expected Secret names for one preparation."""

    release: str
    server_set: str
    mode: str
    previous_server_set: str | None
    client_namespace: str
    server_namespace: str
    previous_server_namespace: str
    grants: Mapping[tuple[str, str], tuple[str, ...]]

    def __post_init__(self) -> None:
        validate_identifier(self.release, "release")
        validate_identifier(self.server_set, "server set")
        if self.previous_server_set is not None:
            validate_identifier(self.previous_server_set, "previous server set", allow_legacy=True)
        if self.mode not in {"transition", "final"}:
            raise ValueError("mode must be transition or final")
        if not self.previous_server_set:
            raise ValueError(f"{self.mode} mode requires its source server set")
        for label, namespace in (
            ("client namespace", self.client_namespace),
            ("server namespace", self.server_namespace),
            ("previous server namespace", self.previous_server_namespace),
        ):
            validate_namespace(namespace, label)
        if not self.grants:
            raise ValueError("credential plan has no granted pairs")

    @property
    def fingerprint(self) -> str:
        public_plan = {
            "release": self.release,
            "server_set": self.server_set,
            "mode": self.mode,
            "previous_server_set": self.previous_server_set,
            "client_namespace": self.client_namespace,
            "server_namespace": self.server_namespace,
            "previous_server_namespace": self.previous_server_namespace,
            "grants": [
                {"subject": subject, "server": server, "tools": list(tools)}
                for (subject, server), tools in sorted(self.grants.items())
            ],
        }
        encoded = json.dumps(public_plan, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(sorted({server for _, server in self.grants}))

    @property
    def expected_secret_names(self) -> tuple[str, ...]:
        names = [auth_secret_name(server, self.server_set) for server in self.servers]
        if self.mode == "transition":
            names.extend(token_secret_name(subject, server, self.release) for subject, server in sorted(self.grants))
            names.extend(revoked_secret_name(server, self.release) for server in self.servers)
        return tuple(sorted(names))

    @property
    def expected_secret_identities(self) -> tuple[SecretIdentity, ...]:
        identities = [
            SecretIdentity(self.server_namespace, auth_secret_name(server, self.server_set))
            for server in self.servers
        ]
        if self.mode == "transition":
            identities.extend(
                SecretIdentity(self.client_namespace, token_secret_name(subject, server, self.release))
                for subject, server in sorted(self.grants)
            )
            identities.extend(
                SecretIdentity(self.client_namespace, revoked_secret_name(server, self.release))
                for server in self.servers
            )
        return tuple(sorted(identities, key=lambda item: (item.namespace, item.name)))


class SecretStore(Protocol):
    """Boundary around Kubernetes; tests use an in-memory implementation."""

    def metadata(self, namespace: str, names: Sequence[str]) -> Mapping[str, Mapping[str, str]]: ...

    def auth_table(self, namespace: str, name: str) -> Mapping[str, object]: ...

    def create(self, manifests: Sequence[Mapping[str, object]]) -> None: ...


def validate_identifier(value: str, label: str, *, allow_legacy: bool = False) -> str:
    if allow_legacy and value == "legacy":
        return value
    if not RELEASE_RE.fullmatch(value):
        raise ValueError(f"{label} must be a 1..24 character lowercase DNS label")
    return value


def validate_namespace(value: str, label: str) -> str:
    if not NAMESPACE_RE.fullmatch(value):
        raise ValueError(f"{label} must be a Kubernetes DNS label")
    return value


def auth_secret_name(server: str, server_set: str) -> str:
    return f"mcp-auth-{server}" if server_set == "legacy" else f"mcp-auth-{server}-{server_set}"


def token_secret_name(subject: str, server: str, release: str) -> str:
    base = f"mcp-token-{subject}-{server}"
    return base if release == "legacy" else f"{base}-{release}"


def revoked_secret_name(server: str, release: str) -> str:
    base = f"mcp-token-revoked-{server}"
    return base if release == "legacy" else f"{base}-{release}"


def grants_from_values(values: Mapping) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return ``(subject, server) -> tools`` from specialist and discovery values."""

    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for subject, spec in (values.get("agents", {}).get("specialists") or {}).items():
        for server, tools in (spec.get("tools") or {}).items():
            if tools:
                out[(subject, server)] = tuple(tools)
    for server, service in ((values.get("mcpServices") or {}).get("services") or {}).items():
        for subject in (service or {}).get("discoverySubjects") or []:
            if (subject, server) in out:
                raise ValueError("a subject cannot be both discovery-only and business-granted")
            out[(subject, server)] = ("tools/list",)
    return out


def new_credentials(
    pairs: Iterable[tuple[str, str]],
    release: str,
    *,
    revoked: frozenset[tuple[str, str]] = frozenset(),
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(TOKEN_BYTES),
) -> list[Credential]:
    """Generate private material for a release; the release is not token entropy."""

    validate_identifier(release, "release")
    credentials = [
        Credential(subject, server, token_factory(), release,
                   "revoked" if (subject, server) in revoked else "active")
        for subject, server in sorted(pairs)
    ]
    for server in sorted({server for _, server in pairs}):
        subject = sorted(subject for subject, candidate in pairs if candidate == server)[0]
        credentials.append(Credential(subject, server, token_factory(), release, "revoked"))
    return credentials


def _table_entries(credentials: Iterable[Credential]) -> dict[str, list[dict[str, str]]]:
    tables: dict[str, list[dict[str, str]]] = {}
    for credential in credentials:
        tables.setdefault(credential.server, []).append({
            "id": credential.credential_id,
            "subject": credential.subject,
            "token": credential.token,
            "status": credential.status,
        })
    return tables


def validate_previous_table(
    raw: Mapping[str, object], *, server: str, allowed_subjects: frozenset[str]
) -> list[dict[str, str]]:
    """Validate an old auth table before carrying it into a transition set."""

    items = raw.get("credentials")
    if raw.get("version") != 1 or not isinstance(items, list):
        raise ValueError(f"previous auth table for {server} is malformed")
    out: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"previous auth table for {server} is malformed")
        credential_id = item.get("id")
        subject = item.get("subject")
        token = item.get("token")
        status = item.get("status")
        if (
            not isinstance(credential_id, str)
            or not isinstance(subject, str)
            or subject not in allowed_subjects
            or not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or status not in {"active", "revoked"}
            or credential_id in seen_ids
            or token in seen_tokens
        ):
            raise ValueError(f"previous auth table for {server} failed validation")
        seen_ids.add(credential_id)
        seen_tokens.add(token)
        out.append({"id": credential_id, "subject": subject, "token": token, "status": status})
    if not any(item["status"] == "active" for item in out):
        raise ValueError(f"previous auth table for {server} has no active credential")
    return out


def secret_manifests(
    plan: ReleasePlan,
    credentials: Sequence[Credential],
    previous_tables: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Build immutable Secret manifests; callers must never render them to logs."""

    current = _table_entries(credentials)
    previous_tables = previous_tables or {}
    labels = {
        "app.kubernetes.io/part-of": "ewallet-llm-agent",
        "ewallet.io/credential": "mcp-service",
        RELEASE_LABEL: plan.release,
        SET_LABEL: plan.server_set,
    }
    annotations = {FINGERPRINT_ANNOTATION: plan.fingerprint}
    manifests: list[dict[str, object]] = []
    for server in plan.servers:
        entries: list[dict[str, str]] = []
        if plan.mode == "transition":
            allowed = frozenset(subject for subject, candidate in plan.grants if candidate == server)
            entries.extend(validate_previous_table(previous_tables.get(server, {}), server=server,
                                                   allowed_subjects=allowed))
        entries.extend(current[server])
        manifests.append({
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
            "metadata": {"name": auth_secret_name(server, plan.server_set),
                         "namespace": plan.server_namespace,
                         "labels": labels, "annotations": annotations},
            "stringData": {"auth-config.json": json.dumps({"version": 1, "credentials": entries},
                                                             sort_keys=True)},
        })
    if plan.mode == "final":
        return manifests
    revoked_written: set[str] = set()
    for credential in credentials:
        if credential.status == "active":
            name = token_secret_name(credential.subject, credential.server, plan.release)
        elif credential.server not in revoked_written:
            revoked_written.add(credential.server)
            name = revoked_secret_name(credential.server, plan.release)
        else:
            continue
        manifests.append({
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
            "metadata": {"name": name, "namespace": plan.client_namespace,
                         "labels": labels, "annotations": annotations},
            "stringData": {"token": credential.token},
        })
    return manifests


def preparation_state(
    plan: ReleasePlan, metadata: Mapping[SecretIdentity, Mapping[str, str]]
) -> str:
    """Return absent or complete; partial/conflicting state fails closed."""

    expected = set(plan.expected_secret_identities)
    present = expected & set(metadata)
    if not present:
        return "absent"
    if present != expected:
        raise RuntimeError("credential release is partial; no Secret was overwritten")
    if any(item.get(FINGERPRINT_ANNOTATION) != plan.fingerprint for item in metadata.values()):
        raise RuntimeError("credential release fingerprint conflicts with the requested plan")
    return "complete"


def prepare_release(
    plan: ReleasePlan,
    store: SecretStore,
    *,
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(TOKEN_BYTES),
) -> str:
    """Create one immutable release or return ``unchanged`` for an exact replay."""

    expected_by_namespace: dict[str, list[str]] = {}
    for identity in plan.expected_secret_identities:
        expected_by_namespace.setdefault(identity.namespace, []).append(identity.name)
    metadata: dict[SecretIdentity, Mapping[str, str]] = {}
    for namespace, names in expected_by_namespace.items():
        metadata.update(
            {SecretIdentity(namespace, name): annotations
             for name, annotations in store.metadata(namespace, names).items()}
        )
    state = preparation_state(plan, metadata)
    if state == "complete":
        return "unchanged"
    assert plan.previous_server_set is not None
    source_tables = {
        server: store.auth_table(
            plan.previous_server_namespace, auth_secret_name(server, plan.previous_server_set)
        )
        for server in plan.servers
    }
    credentials: list[Credential]
    if plan.mode == "transition":
        credentials = new_credentials(plan.grants, plan.release, token_factory=token_factory)
        store.create(secret_manifests(plan, credentials, source_tables))
    else:
        credentials = []
        for server in plan.servers:
            allowed = frozenset(subject for subject, candidate in plan.grants if candidate == server)
            entries = validate_previous_table(source_tables[server], server=server, allowed_subjects=allowed)
            for entry in entries:
                if entry["id"].endswith((f".{plan.release}.active", f".{plan.release}.revoked")):
                    credentials.append(Credential(entry["subject"], server, entry["token"], plan.release,
                                                  entry["status"]))
        if {credential.server for credential in credentials} != set(plan.servers):
            raise RuntimeError("source server set does not contain the requested credential release")
        store.create(secret_manifests(plan, credentials))
    return "created"


class KubectlSecretStore:
    """Secret-aware adapter. Values stay in memory and never enter stdout."""

    def __init__(self, context: str):
        self.context = context

    def _run(self, args: Sequence[str], *, payload: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["kubectl", "--context", self.context, *args], input=payload,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            message = result.stderr.strip().splitlines()[-1][:300] if result.stderr.strip() else "kubectl failed"
            raise RuntimeError(message)
        return result

    def metadata(self, namespace: str, names: Sequence[str]) -> Mapping[str, Mapping[str, str]]:
        expression = (
            '{range .items[*]}{.metadata.name}{"\\t"}'
            '{.metadata.annotations.ewallet\\.io/credential-plan-sha256}{"\\n"}{end}'
        )
        result = self._run(["-n", namespace, "get", "secret", "-o", f"jsonpath={expression}"])
        wanted = set(names)
        metadata: dict[str, Mapping[str, str]] = {}
        for line in result.stdout.splitlines():
            name, _, fingerprint = line.partition("\t")
            if name in wanted:
                metadata[name] = {FINGERPRINT_ANNOTATION: fingerprint}
        return metadata

    def auth_table(self, namespace: str, name: str) -> Mapping[str, object]:
        result = self._run(["-n", namespace, "get", "secret", name,
                            "-o", "jsonpath={.data.auth-config\\.json}"])
        return json.loads(base64.b64decode(result.stdout).decode())

    def create(self, manifests: Sequence[Mapping[str, object]]) -> None:
        payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(manifests)})
        result = self._run(["create", "-f", "-", "-o", "name"], payload=payload)
        print(result.stdout.strip())


def merged_values(base_path: Path, overlay_path: Path) -> Mapping[str, object]:
    base = yaml.safe_load(base_path.read_text())
    overlay = yaml.safe_load(overlay_path.read_text())
    specialists = dict(base["agents"]["specialists"])
    for subject, spec in (overlay.get("agents", {}).get("specialists") or {}).items():
        specialists[subject] = {**specialists.get(subject, {}), **spec}
    return {"agents": {"specialists": specialists}, "mcpServices": overlay.get("mcpServices", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--base-values", type=Path, default=Path("infra/helm/llm-agent-platform/values.yaml"))
    parser.add_argument("--client-namespace", default=NAMESPACE)
    parser.add_argument("--server-namespace", default=NAMESPACE)
    parser.add_argument(
        "--previous-server-namespace", default="",
        help="namespace containing --previous-server-set; defaults to --server-namespace",
    )
    parser.add_argument("--release", required=True)
    parser.add_argument("--server-set", required=True)
    parser.add_argument("--mode", choices=("transition", "final"), required=True)
    parser.add_argument("--previous-server-set", required=True,
                        help="legacy/transition set read in memory; never changed")
    parser.add_argument("--dry-run", action="store_true", help="print only non-secret plan metadata")
    args = parser.parse_args(argv)
    try:
        plan = ReleasePlan(
            release=args.release,
            server_set=args.server_set,
            mode=args.mode,
            previous_server_set=args.previous_server_set,
            client_namespace=args.client_namespace,
            server_namespace=args.server_namespace,
            previous_server_namespace=args.previous_server_namespace or args.server_namespace,
            grants=grants_from_values(merged_values(args.base_values, args.values)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps({
            "fingerprint": plan.fingerprint,
            "mode": plan.mode,
            "client_namespace": plan.client_namespace,
            "server_namespace": plan.server_namespace,
            "previous_server_namespace": plan.previous_server_namespace,
            "previous_server_set": plan.previous_server_set,
            "release": plan.release,
            "server_set": plan.server_set,
            "secret_refs": [
                {"namespace": identity.namespace, "name": identity.name}
                for identity in plan.expected_secret_identities
            ],
        }, indent=2, sort_keys=True))
        return 0
    result = prepare_release(plan, KubectlSecretStore(args.context))
    print(f"credential release {plan.release}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
