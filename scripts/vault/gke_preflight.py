"""Read-only GKE preflight for the Vault + ESO acceptance campaign.

Runs only get/list/version/template commands, pinned to --kubeconfig and --context. For the
Redis Secret it checks key names and non-emptiness only; the value is never printed or stored. Prints one JSON line per gate with a status
of PASS, FAIL, BLOCKED or NOT_CHECKED, and exits 0 only when no gate is FAIL or BLOCKED.
Checks that need running pods report NOT_CHECKED (RUNTIME_CHECK_PENDING_APPROVED_WAKE)
while the node pool is at zero; source configuration alone never yields PASS for them.

  python3 scripts/vault/gke_preflight.py --kubeconfig ~/.cache/ewallet-cloud-lab/kubeconfig \\
      --context gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev [--out DIR]
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import difflib
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/ewallet-serving"
OVERLAY = CHART / "values-vault-eso.yaml"
PENDING = "RUNTIME_CHECK_PENDING_APPROVED_WAKE"
Runner = Callable[[list[str]], tuple[int, str]]


def run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout


def objects(text: str) -> dict[tuple[str, str, str], dict]:
    return {(d["kind"], d["metadata"].get("namespace") or "", d["metadata"]["name"]): d
            for d in yaml.safe_load_all(text) if d}


def manifest_diff(first: str, second: str) -> list[str]:
    """Per-object YAML differences between two rendered manifests (empty when identical)."""
    a, b = objects(first), objects(second)
    out = []
    for key in sorted(set(a) | set(b)):
        if key not in a or key not in b:
            out.append(f"{'+' if key not in a else '-'}object {key}")
            continue
        lines = difflib.unified_diff(yaml.safe_dump(a[key], sort_keys=True).splitlines(),
                                     yaml.safe_dump(b[key], sort_keys=True).splitlines(), lineterm="", n=0)
        out += [f"{key} {line[0]}{line[1:].strip()}" for line in lines if line[:1] in "+-" and line[:3] not in ("---", "+++")]
    return out


def overlay_is_secret_name_only(diff: list[str]) -> bool:
    """True when the overlay changes exactly the Feature API Redis Secret name, and nothing else."""
    key = "('Deployment', 'ml-platform', 'feature-api')"
    return sorted(diff) == sorted([f"{key} -name: feast-online-redis", f"{key} +name: feature-api-redis-vault"])


class Preflight:
    def __init__(self, kubeconfig: str, context: str, runner: Runner = run) -> None:
        self.base = ["--kubeconfig", kubeconfig]
        self.context, self.runner, self.gates = context, runner, []

    def k(self, *args: str) -> tuple[int, str]:
        return self.runner(["kubectl", *self.base, "--context", self.context, *args])

    def h(self, *args: str) -> tuple[int, str]:
        return self.runner(["helm", *self.base, "--kube-context", self.context, *args])

    def gate(self, name: str, expected: str, observed: str, status: str) -> None:
        self.gates.append({"gate": name, "expected": expected, "observed": observed, "status": status})

    def items(self, *args: str) -> list[dict] | None:
        code, out = self.k(*args, "-o", "json")
        return json.loads(out).get("items", []) if code == 0 else None

    def check(self) -> list[dict]:
        code, out = self.k("version", "-o", "json")
        if code:
            self.gate("cluster-reachable", "API server answers on the pinned context", "unreachable", "FAIL")
            return self.gates
        self.gate("cluster-reachable", "API server answers", json.loads(out)["serverVersion"]["gitVersion"], "PASS")
        nodes = self.items("get", "nodes") or []
        awake = bool(nodes)
        self.gate("node-pool", "known size (0 = asleep)", f"{len(nodes)} nodes", "PASS")

        names = {n["metadata"]["name"] for n in self.items("get", "namespaces") or []}
        clash = sorted(names & {"ewallet-vault", "external-secrets"})
        self.gate("no-prior-vault-eso-namespaces", "ewallet-vault and external-secrets absent",
                  ", ".join(clash) or "absent", "BLOCKED" if clash else "PASS")
        crds = {c["metadata"]["name"] for c in self.items("get", "crd") or []}
        eso = sorted(c for c in crds if c.endswith(".external-secrets.io"))
        self.gate("no-prior-eso-crds", "no external-secrets.io CRDs", f"{len(eso)} found", "BLOCKED" if eso else "PASS")
        code, out = self.h("list", "-A", "-a", "-o", "json")
        releases = {(r["name"], r["namespace"]): r for r in json.loads(out or "[]")} if code == 0 else {}
        prior = sorted(f"{ns}/{n}" for n, ns in releases if n in ("vault", "external-secrets"))
        self.gate("no-prior-vault-eso-releases", "no vault or external-secrets release", ", ".join(prior) or "none",
                  "BLOCKED" if prior else ("PASS" if code == 0 else "FAIL"))
        pvcs = sorted(f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in self.items("get", "pvc", "-A") or []
                      if "vault" in p["metadata"]["name"] or p["metadata"]["namespace"] == "ewallet-vault")
        self.gate("no-prior-vault-data", "no Vault PVC (no Raft data to adopt)", ", ".join(pvcs) or "none",
                  "BLOCKED" if pvcs else "PASS")

        needed = {"certificates.cert-manager.io", "issuers.cert-manager.io"}
        self.gate("cert-manager-crds", "Certificate and Issuer CRDs", "present" if needed <= crds else "missing",
                  "PASS" if needed <= crds else "FAIL")
        deployments = self.items("get", "deploy", "-n", "cert-manager") or []
        ready = [d for d in deployments if d.get("status", {}).get("availableReplicas")]
        self.gate("cert-manager-ready", "3 deployments available",
                  f"{len(ready)}/{len(deployments)} available" if awake else PENDING,
                  ("PASS" if deployments and len(ready) == len(deployments) else "FAIL") if awake else "NOT_CHECKED")
        sc = next((s for s in self.items("get", "storageclass") or [] if s["metadata"]["name"] == "standard-rwo"), None)
        ok = bool(sc) and sc["provisioner"] == "pd.csi.storage.gke.io" and sc.get("volumeBindingMode") == "WaitForFirstConsumer"
        self.gate("storage-class", "standard-rwo, pd.csi, WaitForFirstConsumer",
                  f"{sc['provisioner']}, {sc.get('volumeBindingMode')}, reclaim {sc.get('reclaimPolicy')}" if sc else "missing",
                  "PASS" if ok else "FAIL")

        code, out = self.k("-n", "ml-platform", "get", "secret", "feast-online-redis", "-o", "json")
        if code:
            self.gate("redis-secret", "ml-platform/feast-online-redis with connection_string", "missing", "FAIL")
        else:
            data = json.loads(out).get("data") or {}
            present = bool(data.get("connection_string"))
            self.gate("redis-secret", "key connection_string present and non-empty (value never printed)",
                      f"keys {sorted(data)}", "PASS" if present else "FAIL")

        release = releases.get(("ewallet-serving", "ml-platform"))
        self.gate("serving-release", "ewallet-serving deployed in ml-platform",
                  f"revision {release['revision']}, {release['status']}" if release else "missing",
                  "PASS" if release and release["status"] == "deployed" else "BLOCKED")
        if release:
            _, live = self.h("get", "manifest", "ewallet-serving", "-n", "ml-platform")
            _, values = self.h("get", "values", "ewallet-serving", "-n", "ml-platform", "-o", "yaml")
            values_file = Path(self.scratch) / "serving-values.yaml"
            values_file.write_text(values)
            render = ["helm", "template", "ewallet-serving", str(CHART), "-n", "ml-platform", "-f", str(values_file)]
            _, head = self.runner(render)
            _, overlay = self.runner(render + ["-f", str(OVERLAY)])
            drift = manifest_diff(live, head)
            self.gate("serving-no-drift", "live release == repository chart with the release's values",
                      f"{len(drift)} differing lines", "PASS" if not drift else "BLOCKED")
            change = manifest_diff(head, overlay)
            self.gate("serving-overlay-scope", "overlay changes only the Feature API Secret name",
                      "; ".join(change) or "no change", "PASS" if overlay_is_secret_name_only(change) else "BLOCKED")
            self.drift, self.change = drift, change
        return self.gates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--out", type=Path, help="evidence directory for gates.jsonl (no values are recorded)")
    args = parser.parse_args(argv)
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        preflight = Preflight(args.kubeconfig, args.context)
        preflight.scratch = scratch
        gates = preflight.check()
    lines = [json.dumps(g) for g in gates]
    print("\n".join(lines))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "gates.jsonl").write_text("\n".join(lines) + "\n")
    bad = [g["gate"] for g in gates if g["status"] in ("FAIL", "BLOCKED")]
    print(json.dumps({"verdict": "VAULT_GKE_PREFLIGHT_BLOCKED" if bad else "VAULT_GKE_PREFLIGHT_READY", "blocking": bad,
                      "not_checked": [g["gate"] for g in gates if g["status"] == "NOT_CHECKED"]}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
