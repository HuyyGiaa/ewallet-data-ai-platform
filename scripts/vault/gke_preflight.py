"""Read-only GKE preflight for the Vault + ESO acceptance campaign.

Two modes, both running only get/list/version/template commands pinned to --kubeconfig
and --context:

  preinstall       before G2: no earlier Vault/ESO resources, cert-manager, storage,
                   the Redis Secret, and the ewallet-serving release (drift, overlay scope)
  consumer-switch  before G8: Vault and ESO are expected to exist. Checks the awake cluster,
                   the synced ExternalSecret and its target Secret, SHADOW_MATCH, the current
                   Feature API, and the serving release (drift, overlay scope)

Prints one JSON line per gate with status PASS, FAIL, BLOCKED or NOT_CHECKED, then a
verdict line, and exits 0 only when no gate is FAIL or BLOCKED. Every query distinguishes
a confirmed result from a failed or invalid one; a failed or invalid query is FAIL, never
PASS or "absent". Secret values are checked for presence only and never printed or stored.

  python3 scripts/vault/gke_preflight.py --mode preinstall --kubeconfig ~/.cache/ewallet-cloud-lab/kubeconfig \\
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
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/ewallet-serving"
OVERLAY = CHART / "values-vault-eso.yaml"
SHADOW = ROOT / "scripts/vault/shadow_compare.sh"
PENDING = "RUNTIME_CHECK_PENDING_APPROVED_WAKE"
QUERY_FAILED, INVALID = "QUERY_FAILED", "DATA_INVALID"
# ewallet-vault holds Vault itself; vault-rehearsal is the technical rehearsal's consumer namespace
# (docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md). Anything left in either blocks a fresh install.
VAULT_NAMESPACES = ("ewallet-vault", "vault-rehearsal")
CLUSTER_KINDS = "clusterrole,clusterrolebinding,validatingwebhookconfiguration,mutatingwebhookconfiguration"
Runner = Callable[[list[str]], tuple[int, str]]


def run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return done.returncode, done.stdout


class QueryError(Exception):
    """A query failed or returned data that cannot be trusted."""

    def __init__(self, kind: str, what: str) -> None:
        super().__init__(f"{kind}: {what}")
        self.kind = kind


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
    return sorted(diff) == sorted([f"{key} -name: feast-online-redis", f"{key} +name: feast-online-redis-vault"])


class Preflight:
    def __init__(self, kubeconfig: str, context: str, runner: Runner = run, scratch: str | None = None) -> None:
        self.kubeconfig, self.context, self.runner = kubeconfig, context, runner
        self.scratch, self.gates = scratch or tempfile.mkdtemp(), []

    # ---- queries: each returns trusted data or raises QueryError
    def kubectl(self, *args: str) -> list[str]:
        return ["kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context, *args]

    def helm(self, *args: str) -> list[str]:
        return ["helm", "--kubeconfig", self.kubeconfig, "--kube-context", self.context, *args]

    def text(self, argv: list[str], what: str) -> str:
        code, out = self.runner(argv)
        if code:
            raise QueryError(QUERY_FAILED, what)
        if not out.strip():
            raise QueryError(INVALID, f"{what} returned nothing")
        return out

    def document(self, argv: list[str], what: str) -> dict | list:
        try:
            return json.loads(self.text(argv, what))
        except ValueError as error:
            raise QueryError(INVALID, f"{what} is not JSON") from error

    def items(self, what: str, *args: str) -> list[dict]:
        doc = self.document(self.kubectl(*args, "-o", "json"), what)
        if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
            raise QueryError(INVALID, f"{what} has no item list")
        return doc["items"]

    def optional(self, what: str, *args: str) -> dict | None:
        """One named object: None only when the API confirms it does not exist."""
        code, out = self.runner(self.kubectl(*args, "-o", "json", "--ignore-not-found"))
        if code:
            raise QueryError(QUERY_FAILED, what)
        if not out.strip():
            return None
        try:
            doc = json.loads(out)
        except ValueError as error:
            raise QueryError(INVALID, f"{what} is not JSON") from error
        if not isinstance(doc, dict) or "metadata" not in doc:
            raise QueryError(INVALID, f"{what} is not an object")
        return doc

    def releases(self) -> dict[tuple[str, str], dict]:
        doc = self.document(self.helm("list", "-A", "-a", "-o", "json"), "helm releases")
        if not isinstance(doc, list):
            raise QueryError(INVALID, "helm releases is not a list")
        try:
            return {(r["name"], r["namespace"]): r for r in doc}
        except (KeyError, TypeError) as error:
            raise QueryError(INVALID, "helm releases entries lack name/namespace") from error

    # ---- gates
    def gate(self, name: str, expected: str, observed: str, status: str) -> None:
        self.gates.append({"gate": name, "expected": expected, "observed": observed, "status": status})

    def guarded(self, name: str, expected: str, check: Callable[[], tuple[str, str]]) -> None:
        """Run one gate; a failed or invalid query is FAIL with its reason, never PASS."""
        try:
            observed, status = check()
        except QueryError as error:
            observed, status = str(error), "FAIL"
        except (KeyError, TypeError, IndexError):
            observed, status = f"{INVALID}: unexpected object shape", "FAIL"
        self.gate(name, expected, observed, status)

    def check(self, mode: str) -> list[dict]:
        try:
            version = self.document(self.kubectl("version", "-o", "json"), "API version")["serverVersion"]["gitVersion"]
        except (QueryError, KeyError, TypeError) as error:
            self.gate("cluster-reachable", "API server answers on the pinned context", str(error), "FAIL")
            return self.gates
        self.gate("cluster-reachable", "API server answers on the pinned context", version, "PASS")
        try:
            nodes = len(self.items("nodes", "get", "nodes"))
        except QueryError as error:
            self.gate("node-pool", "node count readable", str(error), "FAIL")
            return self.gates
        self.awake = nodes > 0
        if mode == "consumer-switch":
            self.gate("node-pool", "awake (the switch needs running pods)", f"{nodes} nodes", "PASS" if self.awake else "FAIL")
        else:
            self.gate("node-pool", "known size (0 = asleep)", f"{nodes} nodes", "PASS")
        (self.preinstall if mode == "preinstall" else self.consumer_switch)()
        self.serving()
        return self.gates

    def preinstall(self) -> None:
        def namespaces():
            clash = sorted({n["metadata"]["name"] for n in self.items("namespaces", "get", "namespaces")}
                           & {*VAULT_NAMESPACES, "external-secrets"})
            return (", ".join(clash) + " present") if clash else "confirmed absent", "BLOCKED" if clash else "PASS"
        self.guarded("no-prior-vault-eso-namespaces", "ewallet-vault, external-secrets and vault-rehearsal absent", namespaces)

        def crds():
            names = {c["metadata"]["name"] for c in self.items("CRDs", "get", "crd")}
            self.crd_names = names
            eso = sorted(c for c in names if c.endswith(".external-secrets.io"))
            return (f"{len(eso)} present" if eso else "confirmed absent"), "BLOCKED" if eso else "PASS"
        self.guarded("no-prior-eso-crds", "no external-secrets.io CRDs", crds)

        def releases():
            prior = sorted(f"{ns}/{n}" for n, ns in self.releases() if n in ("vault", "external-secrets"))
            return (", ".join(prior) + " present") if prior else "confirmed absent", "BLOCKED" if prior else "PASS"
        self.guarded("no-prior-vault-eso-releases", "no vault or external-secrets release", releases)

        def pvcs():
            found = sorted(f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in self.items("PVCs", "get", "pvc", "-A")
                           if "vault" in p["metadata"]["name"] or p["metadata"]["namespace"] == "ewallet-vault")
            return (", ".join(found) + " present") if found else "confirmed absent", "BLOCKED" if found else "PASS"
        self.guarded("no-prior-vault-data", "no Vault PVC (no Raft data to adopt)", pvcs)

        def volumes():
            # A Retained PV outlives its PVC and namespace (phase Released): still a disk with data.
            found = sorted(f"{p['metadata']['name']} ({p['spec']['claimRef']['namespace']}, {p.get('status', {}).get('phase')})"
                           for p in self.items("PVs", "get", "pv")
                           if (p["spec"].get("claimRef") or {}).get("namespace") in VAULT_NAMESPACES)
            return (", ".join(found) + " present") if found else "confirmed absent", "BLOCKED" if found else "PASS"
        self.guarded("no-prior-vault-volumes", "no PersistentVolume claimed from ewallet-vault or vault-rehearsal, any phase", volumes)

        def chart_cluster_objects():
            found = sorted(f"{o['kind']}/{o['metadata']['name']}" for o in self.items(
                "chart cluster objects", "get", CLUSTER_KINDS, "-l", "app.kubernetes.io/instance in (vault,external-secrets)"))
            return (f"{len(found)} present: " + ", ".join(found[:4])) if found else "confirmed absent", "BLOCKED" if found else "PASS"
        self.guarded("no-prior-chart-cluster-objects", "no ClusterRole, binding or webhook from a vault or external-secrets release",
                     chart_cluster_objects)

        def cert_manager_crds():
            names = getattr(self, "crd_names", None)
            if names is None:
                raise QueryError(QUERY_FAILED, "CRDs (see no-prior-eso-crds)")
            ok = {"certificates.cert-manager.io", "issuers.cert-manager.io"} <= names
            return ("present" if ok else "missing"), "PASS" if ok else "FAIL"
        self.guarded("cert-manager-crds", "Certificate and Issuer CRDs", cert_manager_crds)

        def cert_manager_ready():
            if not self.awake:
                return PENDING, "NOT_CHECKED"
            deployments = self.items("cert-manager deployments", "get", "deploy", "-n", "cert-manager")
            ready = [d for d in deployments if d.get("status", {}).get("availableReplicas")]
            return f"{len(ready)}/{len(deployments)} available", "PASS" if deployments and len(ready) == len(deployments) else "FAIL"
        self.guarded("cert-manager-ready", "3 deployments available", cert_manager_ready)

        def storage():
            sc = next((s for s in self.items("StorageClasses", "get", "storageclass") if s["metadata"]["name"] == "standard-rwo"), None)
            if not sc:
                return "missing", "FAIL"
            ok = sc.get("provisioner") == "pd.csi.storage.gke.io" and sc.get("volumeBindingMode") == "WaitForFirstConsumer"
            return f"{sc.get('provisioner')}, {sc.get('volumeBindingMode')}, reclaim {sc.get('reclaimPolicy')}", "PASS" if ok else "FAIL"
        self.guarded("storage-class", "standard-rwo, pd.csi, WaitForFirstConsumer", storage)
        self.secret_present("redis-secret", "feast-online-redis")

    def consumer_switch(self) -> None:
        def vault_installed():
            release = self.releases().get(("vault", "ewallet-vault"))
            return (f"revision {release['revision']}, {release['status']}" if release else "missing",
                    "PASS" if release and release["status"] == "deployed" else "BLOCKED")
        self.guarded("vault-release", "vault deployed in ewallet-vault", vault_installed)

        def eso_installed():
            deployments = self.items("ESO deployments", "get", "deploy", "-n", "external-secrets")
            ready = [d["metadata"]["name"] for d in deployments if d.get("status", {}).get("availableReplicas")]
            return f"{len(ready)}/{len(deployments)} available", "PASS" if len(deployments) == 3 and len(ready) == 3 else "BLOCKED"
        self.guarded("eso-ready", "controller, webhook and cert-controller available", eso_installed)

        def synced():
            es = self.optional("ExternalSecret feast-online-redis", "-n", "ml-platform", "get", "externalsecret", "feast-online-redis")
            if es is None:
                return "missing", "BLOCKED"
            ready = next((c for c in es.get("status", {}).get("conditions", []) if c.get("type") == "Ready"), {})
            ok = ready.get("status") == "True" and ready.get("reason") == "SecretSynced"
            return f"Ready={ready.get('status')} {ready.get('reason')}", "PASS" if ok else "BLOCKED"
        self.guarded("externalsecret-synced", "feast-online-redis Ready=True, SecretSynced", synced)

        def target_owned():
            secret = self.optional("Secret feast-online-redis-vault", "-n", "ml-platform", "get", "secret", "feast-online-redis-vault")
            if secret is None:
                return "missing", "BLOCKED"
            owners = [f"{o.get('kind')}/{o.get('name')}" for o in secret["metadata"].get("ownerReferences", [])]
            present = bool((secret.get("data") or {}).get("connection_string"))
            ok = owners == ["ExternalSecret/feast-online-redis"] and present
            return f"owner {owners}, connection_string {'present' if present else 'missing/empty'}", "PASS" if ok else "BLOCKED"
        self.guarded("target-secret", "owned by ExternalSecret/feast-online-redis, non-empty key", target_owned)
        self.secret_present("redis-secret", "feast-online-redis")

        def shadow():
            code, out = self.runner(["env", f"KUBECONFIG={self.kubeconfig}", "bash", str(SHADOW), self.context,
                                     "ml-platform", "feast-online-redis", "feast-online-redis-vault"])
            status = out.strip().splitlines()[-1] if out.strip() else "no status"
            return status, "PASS" if code == 0 and status == "SHADOW_MATCH" else "BLOCKED"
        self.guarded("shadow-match", "SHADOW_MATCH, exit 0", shadow)

        def feature_api():
            dep = self.optional("Deployment feature-api", "-n", "ml-platform", "get", "deploy", "feature-api")
            if dep is None:
                return "missing", "BLOCKED"
            env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
            ref = ((env.get("EWALLET_REDIS_CONNECTION_STRING") or {}).get("valueFrom") or {}).get("secretKeyRef", {}).get("name")
            available = dep.get("status", {}).get("availableReplicas") or 0
            ok = ref == "feast-online-redis" and available >= 1
            return f"secretKeyRef {ref}, {available} available", "PASS" if ok else "BLOCKED"
        self.guarded("feature-api-before-switch", "available, still on feast-online-redis", feature_api)

    def secret_present(self, gate: str, name: str) -> None:
        def check():
            secret = self.optional(f"Secret {name}", "-n", "ml-platform", "get", "secret", name)
            if secret is None:
                return "missing", "FAIL"
            data = secret.get("data") or {}
            return f"keys {sorted(data)}", "PASS" if data.get("connection_string") else "FAIL"
        self.guarded(gate, f"ml-platform/{name} with a non-empty connection_string (value never printed)", check)

    def serving(self) -> None:
        def release():
            r = self.releases().get(("ewallet-serving", "ml-platform"))
            self.serving_release = r
            return (f"revision {r['revision']}, {r['status']}" if r else "missing",
                    "PASS" if r and r["status"] == "deployed" else "BLOCKED")
        self.guarded("serving-release", "ewallet-serving deployed in ml-platform", release)
        if not getattr(self, "serving_release", None):
            return
        try:
            live = self.text(self.helm("get", "manifest", "ewallet-serving", "-n", "ml-platform"), "helm get manifest")
            values = self.text(self.helm("get", "values", "ewallet-serving", "-n", "ml-platform", "-o", "yaml"), "helm get values")
            values_file = Path(self.scratch) / "serving-values.yaml"
            values_file.write_text(values)
            render = ["helm", "template", "ewallet-serving", str(CHART), "-n", "ml-platform", "-f", str(values_file)]
            head = self.text(render, "helm template (repository chart)")
            overlay = self.text(render + ["-f", str(OVERLAY)], "helm template (with overlay)")
            drift, change = manifest_diff(live, head), manifest_diff(head, overlay)
        except (QueryError, yaml.YAMLError, KeyError, TypeError) as error:
            reason = str(error) if isinstance(error, QueryError) else f"{INVALID}: unreadable manifest"
            self.gate("serving-no-drift", "live release == repository chart with the release's values", reason, "FAIL")
            self.gate("serving-overlay-scope", "overlay changes only the Feature API Secret name", reason, "FAIL")
            return
        self.gate("serving-no-drift", "live release == repository chart with the release's values",
                  f"{len(drift)} differing lines", "PASS" if not drift else "BLOCKED")
        self.gate("serving-overlay-scope", "overlay changes only the Feature API Secret name",
                  "; ".join(change) or "no change", "PASS" if overlay_is_secret_name_only(change) else "BLOCKED")


def verdict(gates: list[dict]) -> dict:
    bad = [g["gate"] for g in gates if g["status"] in ("FAIL", "BLOCKED")]
    return {"verdict": "VAULT_GKE_PREFLIGHT_BLOCKED" if bad else "VAULT_GKE_PREFLIGHT_READY", "blocking": bad,
            "not_checked": [g["gate"] for g in gates if g["status"] == "NOT_CHECKED"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("preinstall", "consumer-switch"), required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--out", type=Path, help="evidence directory for gates-<mode>.jsonl (no values are recorded)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as scratch:
        gates = Preflight(args.kubeconfig, args.context, scratch=scratch).check(args.mode)
    result = {"mode": args.mode, **verdict(gates)}
    lines = [json.dumps(g) for g in gates] + [json.dumps(result)]
    print("\n".join(lines))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"gates-{args.mode}.jsonl").write_text("\n".join(lines) + "\n")
    return 1 if result["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main())
