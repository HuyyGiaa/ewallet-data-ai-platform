"""Bootstrap and operate Vault without placing key material in the repository.

``init`` writes the key shares and root token to one mode-0600 file that must lie
outside the Git work tree. The seal decides which shares: under Shamir they are unseal keys
(``keys_base64``); under an auto-unseal seal (Vault V2: ``gcpckms`` on GKE, ``transit`` on kind)
they are recovery keys (``recovery_keys_base64``), which never unseal Vault and only authorise
root generation. ``unseal`` refuses on an auto-unseal Vault. Token lifecycle:

- the initial root token (``--keys-file``) applies the bootstrap and creates an
  orphan ``secret-operator`` token (``operator-token``), then is revoked (``retire-root``);
- day-to-day writes and metadata reads use that token (``--token-file``);
- policy or role changes use a short-lived root token from ``generate-root``: the owner
  logs in as Kubernetes role ``vault-admin`` (``login``, policy root-generation only), then
  feeds the unseal or recovery shares; the root token is revoked right after (``retire-root --token-file``).

Token files are mode 0600, outside the repository, created exclusively. ``VAULT_TOKEN``
is the fallback. Reach Vault through a port-forward and pass ``--ca-file``. Every
command reports what it did, and none prints a secret.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from ai_platform.inference.vault import UrllibVaultTransport, VaultError
from ops.vault.admin import VaultAdmin, apply_bootstrap

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "infra/kubernetes/vault/bootstrap.json"
IN_CLUSTER_API = "https://kubernetes.default.svc"


def open_keys_file(path: Path):
    """Create the key file exclusively (mode 0600) and return it open for writing.

    Refuses any path inside the repository and any existing file.
    """
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise VaultError("vault_keys_location", "key file must be outside the repository")
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _store(handle, material: dict[str, object]) -> None:
    """Write JSON to an open key file and force it to disk before returning."""
    with handle:
        json.dump(material, handle)
        handle.flush()
        os.fsync(handle.fileno())


def write_keys_file(path: Path, material: dict[str, object]) -> None:
    """Create the key file exclusively, refusing any path inside the repository."""
    _store(open_keys_file(path), material)


def read_keys_file(path: Path) -> dict[str, object]:
    """Read the key file, refusing one that other users can read."""
    if path.stat().st_mode & 0o077:
        raise VaultError("vault_keys_permissions", "key file must be mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def read_token_file(path: Path) -> str:
    """Read a token file written by ``operator-token`` or ``generate-root``."""
    return str(read_keys_file(path)["token"])


def admin_for(args: argparse.Namespace, *, token: str | None = None) -> VaultAdmin:
    """Build an admin client from the common arguments."""
    return VaultAdmin(
        args.address,
        UrllibVaultTransport(args.ca_file),
        token=token or os.environ.get("VAULT_TOKEN"),
    )


def _root_token(args: argparse.Namespace) -> str | None:
    """The token for this command: --token-file, else the initial root token, else VAULT_TOKEN."""
    token_file = getattr(args, "token_file", None)
    if token_file:
        return read_token_file(token_file)
    keys_file = getattr(args, "keys_file", None)
    return str(read_keys_file(keys_file)["root_token"]) if keys_file else None


SHAMIR = "shamir"


def seal_type(state: dict[str, object]) -> str:
    """The live seal type from sys/seal-status: shamir, gcpckms, transit, ..."""
    return str(state.get("type") or SHAMIR)


def cmd_status(args: argparse.Namespace) -> int:
    """Print seal state without needing a token, including the seal type."""
    state = admin_for(args).seal_status()
    shown = {key: state.get(key) for key in ("initialized", "sealed", "t", "n", "version")}
    shown.update({"seal_type": seal_type(state), "recovery_seal": bool(state.get("recovery_seal"))})
    print(json.dumps(shown))
    return 0


STOP_NEVER_REINIT = "STOP: never run init again over this Raft data (runbook step 3, recovery)"


def cmd_init(args: argparse.Namespace) -> int:
    """Initialise Vault and store the material in the key file.

    The key file is created (exclusive, 0600, outside the repository) and held open before
    Vault is asked to initialise, so the shares go into a file that already exists and are
    forced to disk before success is reported. Two cases cannot be repaired by any client and
    are reported as such: the write fails after Vault answered, or the answer is lost after
    Vault initialised. Both leave an initialised Vault without usable shares.
    """
    admin = admin_for(args)
    state = admin.seal_status()
    if state.get("initialized"):
        raise VaultError("vault_already_initialized", "Vault is already initialised")
    kind = seal_type(state)
    recovery = kind != SHAMIR
    handle = open_keys_file(args.keys_out)
    try:
        material = admin.initialise(shares=args.shares, threshold=args.threshold, recovery=recovery)
    except Exception as error:
        handle.close()
        args.keys_out.unlink()          # empty placeholder: no material was received
        try:
            initialised = admin.seal_status().get("initialized")
        except Exception:
            raise VaultError("vault_init_outcome_unknown",
                             f"init failed ({error}) and the seal status is unreadable: "
                             "check `status` before any retry") from error
        if initialised:
            raise VaultError("vault_initialised_keys_not_stored",
                             f"Vault IS initialised but no key material was received ({error}). "
                             f"{STOP_NEVER_REINIT}") from error
        raise
    shares_field = "recovery_keys_base64" if recovery else "keys_base64"
    try:
        _store(handle, {"seal_type": kind, **material})
    except OSError as error:
        raise VaultError("vault_initialised_keys_not_stored",
                         f"Vault IS initialised but the key file could not be written "
                         f"({error.strerror}). {STOP_NEVER_REINIT}") from error
    if len(material.get(shares_field) or []) != args.shares:
        raise VaultError("vault_init_shares_unexpected",
                         f"Vault IS initialised and the answer was stored, but it does not hold {args.shares} "
                         f"{shares_field}: check the key file before anything else. {STOP_NEVER_REINIT}")
    print(json.dumps({"initialized": True, "seal_type": kind,
                      "key_type": "recovery" if recovery else "unseal",
                      "shares": args.shares, "threshold": args.threshold}))
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    """Print the live engines, auth methods, audit devices, roles and policies (no secret data)."""
    print(json.dumps(admin_for(args, token=_root_token(args)).describe(args.auth_mount), indent=1))
    return 0


def cmd_unseal(args: argparse.Namespace) -> int:
    """Submit threshold key shares until Vault reports unsealed (Shamir only).

    An auto-unseal Vault unseals itself through its seal (KMS on GKE, transit on kind) and
    refuses manual unseal; recovery keys can never unseal it, so they are never submitted.
    """
    admin = admin_for(args)
    kind = seal_type(admin.seal_status())
    if kind != SHAMIR:
        raise VaultError("vault_auto_unseal", f"Vault uses the {kind} seal: it unseals itself, and recovery "
                         "keys cannot unseal it. A sealed auto-unseal Vault means its seal is unreachable "
                         "(runbook: auto-unseal failure); nothing was submitted")
    material = read_keys_file(args.keys_file)
    if not material.get("keys_base64"):
        raise VaultError("vault_no_unseal_keys", "the key file holds no unseal keys (recovery keys never unseal)")
    state: dict[str, object] = {}
    for key in material["keys_base64"]:
        state = admin.unseal(str(key))
        if not state.get("sealed"):
            break
    print(json.dumps({"sealed": state.get("sealed")}))
    return 0 if state.get("sealed") is False else 1


def cmd_configure(args: argparse.Namespace) -> int:
    """Apply the declared bootstrap configuration."""
    config = json.loads(args.config.read_text(encoding="utf-8"))
    steps = apply_bootstrap(
        admin_for(args, token=_root_token(args)),
        config,
        repo_root=REPO_ROOT,
        kubernetes_host=args.kubernetes_host,
    )
    print(json.dumps({"applied": steps}))
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    """Write one key from a hidden prompt or stdin; the value is never an argument."""
    value = sys.stdin.read().rstrip("\n") if args.stdin else getpass.getpass("value: ")
    if not value:
        raise VaultError("vault_empty_value", "refusing to store an empty value")
    version = admin_for(args, token=_root_token(args)).put_kv(args.mount, args.path, {args.key: value})
    print(json.dumps({"path": args.path, "version": version}))
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    """Print secret metadata, for rotation evidence."""
    data = admin_for(args, token=_root_token(args)).kv_versions(args.mount, args.path)
    print(json.dumps({"current_version": data.get("current_version"), "updated_time": data.get("updated_time")}))
    return 0


def cmd_revoke_role(args: argparse.Namespace) -> int:
    """Stop future logins for a workload role."""
    admin_for(args, token=_root_token(args)).delete_role(args.auth_mount, args.role)
    print(json.dumps({"revoked_role": args.role}))
    return 0


def cmd_retire_root(args: argparse.Namespace) -> int:
    """Revoke the root token after bootstrap; unseal keys can regenerate one."""
    admin_for(args, token=_root_token(args)).revoke_self()
    print(json.dumps({"root_token_revoked": True}))
    return 0


def cmd_operator_token(args: argparse.Namespace) -> int:
    """Create the orphan secret-operator token and store it in a new mode-0600 file."""
    if args.token_out.exists():
        raise VaultError("vault_token_file_exists", "token file already exists")
    write_keys_file(args.token_out, {})   # refuse a repository path before creating anything
    args.token_out.unlink()
    token = admin_for(args, token=_root_token(args)).create_operator_token([args.policy], args.ttl)
    write_keys_file(args.token_out, {"token": token, "policies": [args.policy], "ttl": args.ttl})
    print(json.dumps({"operator_token_created": True, "policies": [args.policy], "ttl": args.ttl, "orphan": True}))
    return 0


def _new_token_file(path: Path) -> None:
    """Refuse an existing file or a repository path before any Vault call."""
    if path.exists():
        raise VaultError("vault_token_file_exists", "token file already exists")
    write_keys_file(path, {})
    path.unlink()


def cmd_login(args: argparse.Namespace) -> int:
    """Log in with a ServiceAccount token read from stdin; store the Vault token in a new file."""
    _new_token_file(args.token_out)
    jwt = sys.stdin.read().strip()
    if not jwt:
        raise VaultError("vault_empty_jwt", "no ServiceAccount token on stdin")
    token = admin_for(args, token=None).login_kubernetes(args.auth_mount, args.role, jwt)
    write_keys_file(args.token_out, {"token": token, "role": args.role})
    print(json.dumps({"logged_in": True, "role": args.role}))
    return 0


def cmd_generate_root(args: argparse.Namespace) -> int:
    """Generate a temporary root token from the unseal or recovery shares, for policy or role changes."""
    _new_token_file(args.token_out)
    if not args.token_file:
        raise VaultError("vault_admin_login_required", "pass --token-file from 'login --role vault-admin'")
    material = read_keys_file(args.keys_file)
    # The shares this Vault expects: unseal keys under Shamir, recovery keys under auto-unseal.
    # A file holds exactly one kind (init writes it); Vault rejects the other kind anyway.
    keys = [str(key) for key in (material.get("recovery_keys_base64") or material.get("keys_base64") or [])]
    if not keys:
        raise VaultError("vault_no_key_shares", "the key file holds neither unseal nor recovery keys")
    token = admin_for(args, token=read_token_file(args.token_file)).generate_root(keys)
    write_keys_file(args.token_out, {"token": token})
    print(json.dumps({"root_token_generated": True, "revoke_with": "retire-root --token-file"}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="https://127.0.0.1:8200")
    parser.add_argument("--ca-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(run=cmd_status)
    init = sub.add_parser("init")
    init.add_argument("--keys-out", type=Path, required=True)
    init.add_argument("--shares", type=int, default=3)
    init.add_argument("--threshold", type=int, default=2)
    init.set_defaults(run=cmd_init)
    unseal = sub.add_parser("unseal")
    unseal.add_argument("--keys-file", type=Path, required=True)
    unseal.set_defaults(run=cmd_unseal)
    configure = sub.add_parser("configure")
    configure.add_argument("--keys-file", type=Path)
    configure.add_argument("--token-file", type=Path)
    configure.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    configure.add_argument("--kubernetes-host", default=IN_CLUSTER_API)
    configure.set_defaults(run=cmd_configure)
    for name, handler in (("put", cmd_put), ("versions", cmd_versions)):
        command = sub.add_parser(name)
        command.add_argument("--keys-file", type=Path)
        command.add_argument("--token-file", type=Path)
        command.add_argument("--mount", default="ewallet")
        command.add_argument("--path", required=True)
        if name == "put":
            command.add_argument("--key", required=True)
            command.add_argument("--stdin", action="store_true")
        command.set_defaults(run=handler)
    describe = sub.add_parser("describe")
    describe.add_argument("--keys-file", type=Path)
    describe.add_argument("--token-file", type=Path)
    describe.add_argument("--auth-mount", default="kubernetes")
    describe.set_defaults(run=cmd_describe)
    revoke = sub.add_parser("revoke-role")
    revoke.add_argument("--keys-file", type=Path)
    revoke.add_argument("--token-file", type=Path)
    revoke.add_argument("--auth-mount", default="kubernetes")
    revoke.add_argument("--role", required=True)
    revoke.set_defaults(run=cmd_revoke_role)
    retire = sub.add_parser("retire-root")
    retire.add_argument("--keys-file", type=Path)
    retire.add_argument("--token-file", type=Path)
    retire.set_defaults(run=cmd_retire_root)
    operator = sub.add_parser("operator-token")
    operator.add_argument("--keys-file", type=Path)
    operator.add_argument("--token-file", type=Path)
    operator.add_argument("--token-out", type=Path, required=True)
    operator.add_argument("--policy", default="secret-operator")
    operator.add_argument("--ttl", default="720h")
    operator.set_defaults(run=cmd_operator_token)
    generate = sub.add_parser("generate-root")
    generate.add_argument("--keys-file", type=Path, required=True)
    generate.add_argument("--token-file", type=Path, help="vault-admin login token")
    generate.add_argument("--token-out", type=Path, required=True)
    generate.set_defaults(run=cmd_generate_root)
    login = sub.add_parser("login")
    login.add_argument("--auth-mount", default="kubernetes")
    login.add_argument("--role", default="vault-admin")
    login.add_argument("--token-out", type=Path, required=True)
    login.set_defaults(run=cmd_login)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one command; a Vault failure becomes exit code 2 with a safe message."""
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except VaultError as error:
        print(f"REFUSED {error.code}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
