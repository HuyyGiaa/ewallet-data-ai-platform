"""Read-only Cloud KMS / Workload Identity preflight for Vault V2 (P4-B verification, Campaign A2 gate).

Two modes:

  offline  repository checks only: the Vault seal stanza and KSA annotation against the expected key and
           GSA (and, with --vault-chart, the rendered Vault ServiceAccount). Every cloud gate is
           NOT_CHECKED, so the verdict is VAULT_KMS_PREFLIGHT_OFFLINE_ONLY, never READY.
  cloud    the same, plus read-only gcloud queries pinned to --project: Cloud KMS API enabled, key ring,
           key (purpose, protection, no rotation, primary ENABLED), every key version ENABLED, the key's
           IAM (the GSA holds encrypterDecrypter + viewer, nobody else can use the key), the GSA (exists,
           enabled, no user-managed key), and its Workload Identity binding (exactly ewallet-vault/vault).
           Run only in an owner-approved phase: it reads the real project.
  api-ready  fresh installs only (two-stage P4, stage 1 -> stage 2): after the Cloud KMS API was enabled,
           probe it with a read-only `gcloud kms keyrings list` until --required consecutive probes succeed,
           --interval seconds apart. Any failure resets the count. Not ready within --timeout is
           VAULT_KMS_API_NOT_READY: stop, do not plan stage 2. Stable success lowers the propagation risk,
           it does not guarantee every Cloud KMS method is ready.

Status per gate: PASS, FAIL (missing, wrong, or malformed answer), BLOCKED (the query itself failed or was
denied: nothing can be concluded) or NOT_CHECKED. A failed query is never read as "absent". Exit 0 only for
VAULT_KMS_PREFLIGHT_READY. No credential, token or command error text is printed or recorded.

  python3 scripts/vault/kms_preflight.py --mode offline [--vault-chart ~/ewallet-evidence/vault/charts/vault-0.34.1.tgz]
  python3 scripts/vault/kms_preflight.py --mode cloud --project ewallet-data-ai-platform-hg26 [--out DIR]
  python3 scripts/vault/kms_preflight.py --mode api-ready [--required 5 --interval 30 --timeout 900]
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[2]
VALUES = ROOT / "infra/kubernetes/vault/values-gke.yaml"
PROJECT = "ewallet-data-ai-platform-hg26"
LOCATION, KEY_RING, CRYPTO_KEY = "asia-southeast1", "ewallet-vault", "vault-unseal"
NAMESPACE, KSA = "ewallet-vault", "vault"
USE_ROLES = {"roles/cloudkms.cryptoKeyEncrypterDecrypter", "roles/cloudkms.cryptoKeyEncrypter",
             "roles/cloudkms.cryptoKeyDecrypter", "roles/cloudkms.admin", "roles/owner", "roles/editor"}
Runner = Callable[[list[str]], tuple[int, str, str]]


def run(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 127, "", ""
    return done.returncode, done.stdout, done.stderr


class Query(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status


class KmsPreflight:
    def __init__(self, project: str = PROJECT, runner: Runner = run) -> None:
        self.project, self.runner, self.gates = project, runner, []
        self.gsa = f"ewallet-vault-unseal@{project}.iam.gserviceaccount.com"
        self.key_args = ["--keyring", KEY_RING, "--location", LOCATION, "--project", project, "--format", "json"]

    def gate(self, name: str, expected: str, observed: str, status: str) -> None:
        self.gates.append({"gate": name, "expected": expected, "observed": observed, "status": status})

    def guarded(self, name: str, expected: str, check: Callable[[], tuple[str, str]]) -> None:
        try:
            observed, status = check()
        except Query as error:
            observed, status = str(error), error.status
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            observed, status = "DATA_INVALID: unexpected answer shape", "FAIL"
        self.gate(name, expected, observed, status)

    def gcloud(self, *args: str, what: str) -> object:
        """A read-only gcloud query: NOT_FOUND -> FAIL (missing), any other failure -> BLOCKED, bad JSON -> FAIL."""
        code, out, err = self.runner(["gcloud", *args])
        if code:
            if "NOT_FOUND" in err or "was not found" in err:
                raise Query("FAIL", f"{what}: not found")
            if "PERMISSION_DENIED" in err or "403" in err:
                raise Query("BLOCKED", f"{what}: permission denied, nothing concluded")
            raise Query("BLOCKED", f"{what}: query failed (exit {code}), nothing concluded")
        try:
            return json.loads(out)
        except ValueError as error:
            raise Query("FAIL", f"{what}: answer is not JSON") from error

    # ---- repository (always)
    def repository(self, vault_chart: Path | None) -> None:
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        config = values["server"]["ha"]["raft"]["config"]

        def seal():
            stanzas = re.findall(r'seal "(\w+)" \{([^}]*)\}', config)
            if [s[0] for s in stanzas] != ["gcpckms"]:
                return f"seal stanzas {[s[0] for s in stanzas]}", "FAIL"
            fields = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', stanzas[0][1]))
            want = {"project": self.project, "region": LOCATION, "key_ring": KEY_RING, "crypto_key": CRYPTO_KEY}
            ok = fields == want and "credentials" not in stanzas[0][1]
            return f"{fields}", "PASS" if ok else "FAIL"
        self.guarded("vault-seal-config", f"seal gcpckms {self.project}/{LOCATION}/{KEY_RING}/{CRYPTO_KEY}, no credentials", seal)

        def annotation():
            got = values["server"]["serviceAccount"]["annotations"]["iam.gke.io/gcp-service-account"]
            no_key = not re.search(r"GOOGLE_APPLICATION_CREDENTIALS|credentials\s*=", yaml.safe_dump(values))
            return got, "PASS" if got == self.gsa and no_key else "FAIL"
        self.guarded("vault-ksa-annotation", f"iam.gke.io/gcp-service-account: {self.gsa}; no credentials file", annotation)

        if vault_chart is None:
            self.gate("vault-ksa-rendered", f"chart renders ServiceAccount {NAMESPACE}/{KSA} with the annotation",
                      "pass --vault-chart to render", "NOT_CHECKED")
            return

        def rendered():
            code, out, _ = self.runner(["helm", "template", "vault", str(vault_chart), "-n", NAMESPACE, "-f", str(VALUES)])
            if code:
                raise Query("FAIL", "helm template failed")
            accounts = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "ServiceAccount"
                        and d["metadata"]["name"] == KSA]
            ok = len(accounts) == 1 and accounts[0]["metadata"].get("namespace") == NAMESPACE and \
                accounts[0]["metadata"].get("annotations", {}).get("iam.gke.io/gcp-service-account") == self.gsa
            return f"{len(accounts)} ServiceAccount {KSA}", "PASS" if ok else "FAIL"
        self.guarded("vault-ksa-rendered", f"chart renders ServiceAccount {NAMESPACE}/{KSA} with the annotation", rendered)

    # ---- cloud (read-only)
    def cloud(self) -> None:
        def api():
            doc = self.gcloud("services", "list", "--enabled", "--filter=config.name=cloudkms.googleapis.com",
                              "--project", self.project, "--format", "json", what="enabled services")
            names = [s["config"]["name"] for s in doc]
            return f"{names}", "PASS" if names == ["cloudkms.googleapis.com"] else "FAIL"
        self.guarded("kms-api-enabled", "cloudkms.googleapis.com enabled", api)

        def key_ring():
            doc = self.gcloud("kms", "keyrings", "describe", KEY_RING, "--location", LOCATION, "--project", self.project,
                              "--format", "json", what="key ring")
            want = f"projects/{self.project}/locations/{LOCATION}/keyRings/{KEY_RING}"
            return doc["name"], "PASS" if doc["name"] == want else "FAIL"
        self.guarded("kms-key-ring", f"{KEY_RING} in {LOCATION}, project {self.project}", key_ring)

        def key():
            doc = self.gcloud("kms", "keys", "describe", CRYPTO_KEY, *self.key_args, what="crypto key")
            want = f"projects/{self.project}/locations/{LOCATION}/keyRings/{KEY_RING}/cryptoKeys/{CRYPTO_KEY}"
            facts = {"name_ok": doc["name"] == want, "purpose": doc.get("purpose"),
                     "protection": doc.get("versionTemplate", {}).get("protectionLevel"),
                     "rotation": doc.get("rotationPeriod") or doc.get("nextRotationTime"),
                     "primary": (doc.get("primary") or {}).get("state")}
            ok = facts == {"name_ok": True, "purpose": "ENCRYPT_DECRYPT", "protection": "SOFTWARE", "rotation": None,
                           "primary": "ENABLED"}
            return json.dumps(facts, sort_keys=True), "PASS" if ok else "FAIL"
        self.guarded("kms-crypto-key", "ENCRYPT_DECRYPT, SOFTWARE, no scheduled rotation, primary ENABLED", key)

        def versions():
            doc = self.gcloud("kms", "keys", "versions", "list", "--key", CRYPTO_KEY, *self.key_args, what="key versions")
            states = sorted({v["state"] for v in doc})
            return f"{len(doc)} versions, states {states}", "PASS" if doc and states == ["ENABLED"] else "FAIL"
        self.guarded("kms-key-versions", "every version ENABLED (Vault may need any of them)", versions)

        def key_iam():
            doc = self.gcloud("kms", "keys", "get-iam-policy", CRYPTO_KEY, *self.key_args, what="key IAM policy")
            member = f"serviceAccount:{self.gsa}"
            roles = {b["role"]: set(b.get("members", [])) for b in doc.get("bindings", []) if not b.get("condition")}
            has = {r for r in ("roles/cloudkms.cryptoKeyEncrypterDecrypter", "roles/cloudkms.viewer") if member in roles.get(r, set())}
            others = sorted({m for r, ms in roles.items() if r in USE_ROLES for m in ms if m != member})
            conditional = [b["role"] for b in doc.get("bindings", []) if b.get("condition")]
            ok = len(has) == 2 and not others and not conditional
            return f"GSA roles {sorted(has)}; other principals with use rights: {len(others)}; conditional: {conditional}", "PASS" if ok else "FAIL"
        self.guarded("kms-key-iam", "the GSA has encrypterDecrypter + viewer on the key; nobody else can use it", key_iam)

        def gsa():
            doc = self.gcloud("iam", "service-accounts", "describe", self.gsa, "--project", self.project, "--format", "json",
                              what="Vault GSA")
            return f"{doc['email']} disabled={doc.get('disabled', False)}", "PASS" if doc["email"] == self.gsa and not doc.get("disabled") else "FAIL"
        self.guarded("vault-gsa", f"{self.gsa} exists and is enabled", gsa)

        def gsa_keys():
            doc = self.gcloud("iam", "service-accounts", "keys", "list", "--iam-account", self.gsa, "--managed-by", "user",
                              "--project", self.project, "--format", "json", what="GSA user-managed keys")
            return f"{len(doc)} user-managed keys", "PASS" if doc == [] else "FAIL"
        self.guarded("vault-gsa-no-json-key", "no user-managed (JSON) key: Vault uses Workload Identity", gsa_keys)

        def workload_identity():
            doc = self.gcloud("iam", "service-accounts", "get-iam-policy", self.gsa, "--project", self.project, "--format", "json",
                              what="GSA IAM policy")
            want = f"serviceAccount:{self.project}.svc.id.goog[{NAMESPACE}/{KSA}]"
            members = sorted({m for b in doc.get("bindings", []) if b["role"] == "roles/iam.workloadIdentityUser"
                              for m in b.get("members", [])})
            extra = sorted({b["role"] for b in doc.get("bindings", []) if b["role"] != "roles/iam.workloadIdentityUser"})
            return f"workloadIdentityUser members {members}; other roles {extra}", "PASS" if members == [want] and not extra else "FAIL"
        self.guarded("vault-workload-identity", f"exactly {NAMESPACE}/{KSA} may act as the GSA", workload_identity)

    def not_checked(self) -> None:
        for name in ("kms-api-enabled", "kms-key-ring", "kms-crypto-key", "kms-key-versions", "kms-key-iam", "vault-gsa",
                     "vault-gsa-no-json-key", "vault-workload-identity"):
            self.gate(name, "cloud check", "offline mode: not queried", "NOT_CHECKED")


def verdict(gates: list[dict], mode: str) -> dict:
    bad = [g["gate"] for g in gates if g["status"] in ("FAIL", "BLOCKED")]
    unchecked = [g["gate"] for g in gates if g["status"] == "NOT_CHECKED"]
    if bad:
        name = "VAULT_KMS_PREFLIGHT_BLOCKED"
    elif mode == "offline" or unchecked:
        name = "VAULT_KMS_PREFLIGHT_OFFLINE_ONLY"
    else:
        name = "VAULT_KMS_PREFLIGHT_READY"
    return {"mode": mode, "verdict": name, "blocking": bad, "not_checked": unchecked}


def api_ready(project: str, runner: Runner, required: int, interval: float, timeout: float,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Bounded read-only probe: `required` consecutive successful Cloud KMS reads, or NOT_READY at `timeout`."""
    start, streak, probes, failures = clock(), 0, 0, 0
    while True:
        probes += 1
        code, out, _ = runner(["gcloud", "kms", "keyrings", "list", "--location", LOCATION, "--project", project,
                               "--format", "json"])
        try:
            ok = code == 0 and isinstance(json.loads(out), list)
        except ValueError:
            ok = False
        streak, failures = (streak + 1, failures) if ok else (0, failures + 1)
        if streak >= required:
            return {"mode": "api-ready", "verdict": "VAULT_KMS_API_READY", "probes": probes, "failed_probes": failures,
                    "consecutive_successes": streak}
        if clock() - start + interval > timeout:
            return {"mode": "api-ready", "verdict": "VAULT_KMS_API_NOT_READY", "probes": probes,
                    "failed_probes": failures, "consecutive_successes": streak}
        sleep(interval)


def main(argv: list[str] | None = None, runner: Runner = run,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("offline", "cloud", "api-ready"), required=True)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--vault-chart", type=Path)
    parser.add_argument("--out", type=Path, help="evidence directory for kms-gates-<mode>.jsonl")
    parser.add_argument("--required", type=int, default=5, help="api-ready: consecutive successful probes")
    parser.add_argument("--interval", type=float, default=30.0, help="api-ready: seconds between probes")
    parser.add_argument("--timeout", type=float, default=900.0, help="api-ready: give up after this many seconds")
    args = parser.parse_args(argv)
    if args.project != PROJECT:
        print(f"REFUSED: only {PROJECT} is supported", file=sys.stderr)
        return 2
    if args.mode == "api-ready":
        if args.required < 1 or args.interval <= 0 or args.timeout <= 0:
            print("REFUSED: --required, --interval and --timeout must be positive", file=sys.stderr)
            return 2
        result = api_ready(args.project, runner, args.required, args.interval, args.timeout, sleep, clock)
        print(json.dumps(result))
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "kms-api-ready.json").write_text(json.dumps(result) + "\n")
        return 0 if result["verdict"] == "VAULT_KMS_API_READY" else 1
    check = KmsPreflight(args.project, runner)
    check.repository(args.vault_chart)
    check.cloud() if args.mode == "cloud" else check.not_checked()
    result = verdict(check.gates, args.mode)
    lines = [json.dumps(g) for g in check.gates] + [json.dumps(result)]
    print("\n".join(lines))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"kms-gates-{args.mode}.jsonl").write_text("\n".join(lines) + "\n")
    return 0 if result["verdict"] == "VAULT_KMS_PREFLIGHT_READY" else 1


if __name__ == "__main__":
    sys.exit(main())
