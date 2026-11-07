"""Bootstrap and operate Vault without placing key material in the repository.

``init`` writes the unseal keys and root token to one mode-0600 file that must lie
outside the Git work tree. Other commands read that file or ``VAULT_TOKEN``. Reach
Vault through a port-forward and pass ``--ca-file``. Run with no cluster state
assumed: every command reports what it did, and none prints a secret.
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


def write_keys_file(path: Path, material: dict[str, object]) -> None:
    """Create the key file exclusively, refusing any path inside the repository."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise VaultError("vault_keys_location", "key file must be outside the repository")
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(material, handle)


def read_keys_file(path: Path) -> dict[str, object]:
    """Read the key file, refusing one that other users can read."""
    if path.stat().st_mode & 0o077:
        raise VaultError("vault_keys_permissions", "key file must be mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def admin_for(args: argparse.Namespace, *, token: str | None = None) -> VaultAdmin:
    """Build an admin client from the common arguments."""
    return VaultAdmin(
        args.address,
        UrllibVaultTransport(args.ca_file),
        token=token or os.environ.get("VAULT_TOKEN"),
    )


def _root_token(args: argparse.Namespace) -> str | None:
    keys_file = getattr(args, "keys_file", None)
    return str(read_keys_file(keys_file)["root_token"]) if keys_file else None


def cmd_status(args: argparse.Namespace) -> int:
    """Print seal state without needing a token."""
    state = admin_for(args).seal_status()
    print(json.dumps({key: state.get(key) for key in ("initialized", "sealed", "t", "n", "version")}))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialise Vault and store the material in the key file."""
    admin = admin_for(args)
    if admin.seal_status().get("initialized"):
        raise VaultError("vault_already_initialized", "Vault is already initialised")
    write_keys_file(args.keys_out, {})
    args.keys_out.unlink()
    material = admin.initialise(shares=args.shares, threshold=args.threshold)
    write_keys_file(args.keys_out, material)
    print(json.dumps({"initialized": True, "shares": args.shares, "threshold": args.threshold}))
    return 0


def cmd_unseal(args: argparse.Namespace) -> int:
    """Submit threshold key shares until Vault reports unsealed."""
    material = read_keys_file(args.keys_file)
    admin = admin_for(args)
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
    configure.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    configure.add_argument("--kubernetes-host", default=IN_CLUSTER_API)
    configure.set_defaults(run=cmd_configure)
    for name, handler in (("put", cmd_put), ("versions", cmd_versions)):
        command = sub.add_parser(name)
        command.add_argument("--keys-file", type=Path)
        command.add_argument("--mount", default="ewallet")
        command.add_argument("--path", required=True)
        if name == "put":
            command.add_argument("--key", required=True)
            command.add_argument("--stdin", action="store_true")
        command.set_defaults(run=handler)
    revoke = sub.add_parser("revoke-role")
    revoke.add_argument("--keys-file", type=Path)
    revoke.add_argument("--auth-mount", default="kubernetes")
    revoke.add_argument("--role", required=True)
    revoke.set_defaults(run=cmd_revoke_role)
    retire = sub.add_parser("retire-root")
    retire.add_argument("--keys-file", type=Path)
    retire.set_defaults(run=cmd_retire_root)
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
