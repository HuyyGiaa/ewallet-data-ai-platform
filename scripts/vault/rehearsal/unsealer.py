"""kind only: bootstrap the throwaway transit "unsealer" that stands in for Cloud KMS.

The application Vault (infra/kubernetes/vault/values-local.yaml) auto-unseals through a transit
seal on this second Vault. Every result that depends on it is SIMULATED_AUTO_UNSEAL: it exercises
Vault's auto-unseal code path (recovery-key init, unseal at every start, sealed while the seal is
unreachable), never Google Cloud KMS, IAM or Workload Identity.

bootstrap (one call, against a port-forward to the unsealer, TLS verified with vault-ca):
  1. init with 1 Shamir share (in-memory storage: the unsealer lives only as long as its pod);
  2. unseal; enable transit at transit/; create key ``autounseal``;
  3. policy ``autounseal``: update on transit/encrypt/autounseal and transit/decrypt/autounseal only;
  4. an orphan periodic token with that policy (the transit seal renews it);
  5. revoke the unsealer's root token.
The share and the token go to new mode-0600 files outside the repository; nothing is printed but a status.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ai_platform.inference.vault import UrllibVaultTransport, VaultError
from ops.vault.admin import VaultAdmin
from ops.vault.cli import write_keys_file

KEY = "autounseal"
POLICY = f"""# Transit seal of the kind rehearsal: encrypt and decrypt with one key, nothing else.
path "transit/encrypt/{KEY}" {{
  capabilities = ["update"]
}}
path "transit/decrypt/{KEY}" {{
  capabilities = ["update"]
}}
"""


class Unsealer(VaultAdmin):
    """The few transit operations the stand-in needs, on top of the operator API."""

    def enable_transit(self) -> None:
        if "transit/" not in self._require("GET", "sys/mounts").get("data", {}):
            self._require("POST", "sys/mounts/transit", {"type": "transit"})

    def create_key(self, name: str) -> None:
        self._require("POST", f"transit/keys/{name}", {"type": "aes256-gcm96"})

    def create_seal_token(self, policy: str) -> str:
        body = self._require("POST", "auth/token/create-orphan", {
            "policies": [policy], "period": "24h", "renewable": True, "display_name": "transit-seal"})
        return str(body["auth"]["client_token"])


def bootstrap(address: str, transport, keys_out: Path, token_out: Path) -> dict[str, object]:
    for path in (keys_out, token_out):
        if path.exists():
            raise VaultError("unsealer_file_exists", f"{path.name} already exists")
        write_keys_file(path, {})        # refuses a repository path before Vault is touched
        path.unlink()
    anonymous = Unsealer(address, transport)
    if anonymous.seal_status().get("initialized"):
        raise VaultError("unsealer_initialized", "the unsealer is already initialised: never re-run bootstrap")
    material = anonymous.initialise(shares=1, threshold=1)
    write_keys_file(keys_out, {"keys_base64": material["keys_base64"]})
    if anonymous.unseal(str(material["keys_base64"][0])).get("sealed") is not False:
        raise VaultError("unsealer_sealed", "the unsealer did not unseal")
    root = Unsealer(address, transport, token=str(material["root_token"]))
    root.enable_transit()
    root.create_key(KEY)
    root.apply_policy(KEY, POLICY)
    write_keys_file(token_out, {"token": root.create_seal_token(KEY), "policies": [KEY]})
    root.revoke_self()
    return {"unsealer": "ready", "transit_key": KEY, "seal_token_policy": KEY, "root_token": "revoked",
            "label": "SIMULATED_AUTO_UNSEAL"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap"])
    parser.add_argument("--address", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--keys-out", type=Path, required=True)
    parser.add_argument("--token-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(bootstrap(args.address, UrllibVaultTransport(args.ca_file), args.keys_out, args.token_out)))
    except VaultError as error:
        print(f"REFUSED {error.code}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
