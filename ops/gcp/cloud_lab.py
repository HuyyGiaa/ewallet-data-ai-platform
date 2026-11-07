"""Operate the ``ewallet-dev`` GKE cluster as an on-demand cloud lab.

The tool covers sleep (zero CPU nodes) and profile-based wake. Read-only
commands are ``status``, ``plan-sleep``, ``plan-wake``, ``verify`` and
``cost-status``. Every mutating command prints its plan by default and acts
only with ``--execute``, ``--confirm-cluster ewallet-dev`` and
``CLOUD_LAB_APPROVED=yes``. Nothing here deletes a resource, starts a GPU, or
wakes the cluster implicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT = "ewallet-data-ai-platform-hg26"
CLUSTER = "ewallet-dev"
LOCATION = "asia-southeast1-b"
NODE_POOL = "cpu-pool"
CONTEXT = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"
VM_PREFIX = f"gke-{CLUSTER}-{NODE_POOL}-"
G3_LABEL = "ewallet.io/g3-llm"
G3_NODE_TARGET = 3
PACKING_LIMIT = 0.85
PROFILES_PATH = Path(__file__).with_name("cloud_lab_profiles.json")

SYSTEM_NAMESPACES = ("kube-system", "gmp-system", "gke-managed-cim", "istio-system", "cert-manager")
# Controllers stop first so they cannot recreate the workloads they own.
CONTROLLER_NAMESPACES = ("kagent", "ate-system", "kserve", "kubeflow")

# Cloud Billing Catalog list prices read on 2026-11-06, USD, asia-southeast1.
PRICES = {
    "e2_core_hour": 0.02690931,
    "e2_ram_gib_hour": 0.00360594,
    "balanced_pd_gib_month": 0.11,
    "gcs_standard_gib_month": 0.02,
    "artifact_registry_gib_month": 0.10,
    "external_ip_hour": 0.005,
    "gke_cluster_hour": 0.10,
}
HOURS_PER_MONTH = 730
NODE_CORES = 4
NODE_RAM_GIB = 16
NODE_BOOT_DISK_GIB = 50


class GateError(RuntimeError):
    """A safety gate refused the requested operation."""


@dataclass(frozen=True)
class Workload:
    """One Deployment or StatefulSet and its recorded replica count."""

    kind: str
    namespace: str
    name: str
    replicas: int

    @property
    def key(self) -> str:
        """Return the ``namespace/name`` form used by profiles."""
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class NodeShape:
    """Measured schedulable capacity of one pool node."""

    cpu_m: float = 3920
    mem_mi: float = 13273
    daemon_cpu_m: float = 918
    daemon_mem_mi: float = 1585


def evaluate_pdbs(pdbs: Iterable[dict[str, Any]]) -> list[str]:
    """Return PDBs that cannot release every pod during a drain to zero.

    A ``minAvailable`` budget with any selected pod can never be met at zero
    nodes, even when it currently allows disruptions, because the last pod is
    always protected. GKE honours budgets for one hour per node and then
    drains anyway, so such a budget would be bypassed rather than satisfied.
    """
    blocked = []
    for pdb in pdbs:
        expected = pdb.get("status", {}).get("expectedPods", 0)
        min_available = pdb.get("spec", {}).get("minAvailable")
        if expected and min_available not in (None, 0, "0", "0%"):
            meta = pdb["metadata"]
            blocked.append(f"{meta['namespace']}/{meta['name']}")
    return sorted(blocked)


def classify_sleep(pdb_blockers: list[str], active_work: list[str], identity: list[str]) -> str:
    """Map gate results onto the owner's three sleep verdicts."""
    if active_work or identity:
        return "UNSAFE_TO_SLEEP"
    return "SAFE_AFTER_REVERSIBLE_PREPARATION" if pdb_blockers else "SAFE_TO_SLEEP"


def load_workloads(records: Iterable[dict[str, Any]]) -> list[Workload]:
    """Keep the Deployments and StatefulSets from a ``workloads.json`` record."""
    return [
        Workload(r["kind"], r["namespace"], r["name"], int(r.get("replicas") or 0))
        for r in records
        if r["kind"] in {"Deployment", "StatefulSet"}
    ]


def allowed(workload: Workload, patterns: Iterable[str]) -> bool:
    """Return whether a workload matches a profile allow-list."""
    return any(fnmatch(workload.key, pattern) for pattern in patterns)


def restore_plan(
    recorded: Iterable[Workload],
    current: dict[str, int],
    patterns: Iterable[str] | None = None,
) -> list[tuple[Workload, int]]:
    """Return workloads whose live replicas differ from the recorded value.

    Every workload is considered when ``patterns`` is None. Otherwise only the
    allow-listed ones are, which keeps a partial wake partial.
    """
    plan = []
    for workload in recorded:
        if patterns is not None and not allowed(workload, patterns):
            continue
        if workload.key in current and current[workload.key] != workload.replicas:
            plan.append((workload, workload.replicas))
    return plan


def scale_down_plan(
    recorded: Iterable[Workload],
    current: dict[str, int],
    patterns: Iterable[str],
) -> list[Workload]:
    """List application workloads to hold at zero during a partial wake.

    Without this a node would admit whatever the scheduler picks from all
    pending pods. System namespaces are never touched and controller
    namespaces come first.
    """
    selected = [
        w
        for w in recorded
        if w.namespace not in SYSTEM_NAMESPACES
        and not allowed(w, patterns)
        and current.get(w.key, 0) > 0
    ]
    return sorted(
        selected,
        key=lambda w: (w.namespace not in CONTROLLER_NAMESPACES, w.namespace, w.name),
    )


def _cpu_m(value: str) -> float:
    return float(value[:-1]) if value.endswith("m") else float(value) * 1000


def _mem_mi(value: str) -> float:
    factors = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "M": 1e6 / 2**20, "G": 1e9 / 2**20}
    for suffix, factor in factors.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    return float(value) / 2**20


def pod_requests(pod: dict[str, Any]) -> tuple[float, float]:
    """Return effective CPU millicores and memory MiB requested by a pod."""
    cpu = mem = 0.0
    for container in pod["spec"]["containers"]:
        requests = container.get("resources", {}).get("requests", {})
        cpu += _cpu_m(requests.get("cpu", "0"))
        mem += _mem_mi(requests.get("memory", "0"))
    for container in pod["spec"].get("initContainers", []):
        requests = container.get("resources", {}).get("requests", {})
        cpu = max(cpu, _cpu_m(requests.get("cpu", "0")))
        mem = max(mem, _mem_mi(requests.get("memory", "0")))
    overhead = pod["spec"].get("overhead") or {}
    return cpu + _cpu_m(overhead.get("cpu", "0")), mem + _mem_mi(overhead.get("memory", "0"))


def summarize_requests(pods: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce running pods to per-workload request totals, without any env data."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for pod in pods:
        if pod["status"].get("phase") != "Running":
            continue
        owners = pod["metadata"].get("ownerReferences", [])
        kind = owners[0]["kind"] if owners else "Pod"
        if kind == "DaemonSet" or pod["metadata"]["name"].startswith("kube-proxy-gke"):
            continue
        name = owners[0]["name"] if owners else pod["metadata"]["name"]
        if kind == "ReplicaSet":
            name = name.rsplit("-", 1)[0]
        cpu, mem = pod_requests(pod)
        row = totals.setdefault(
            (pod["metadata"]["namespace"], name),
            {"namespace": pod["metadata"]["namespace"], "name": name, "pods": 0, "cpu_m": 0.0, "mem_mi": 0.0, "max_pod_cpu_m": 0.0},
        )
        row["pods"] += 1
        row["cpu_m"] += cpu
        row["mem_mi"] += mem
        row["max_pod_cpu_m"] = max(row["max_pod_cpu_m"], cpu)
    return sorted(totals.values(), key=lambda r: (r["namespace"], r["name"]))


def required_nodes(
    requests: Iterable[dict[str, Any]],
    patterns: Iterable[str] | None,
    shape: NodeShape = NodeShape(),
) -> dict[str, Any]:
    """Compute nodes needed for system pods plus the selected workloads.

    ``patterns=None`` means the whole recorded system. Node counts keep 15%
    headroom for bin-packing, since requests are not perfectly divisible.
    """
    pattern_list = None if patterns is None else list(patterns)
    cpu = mem = largest = 0.0
    for row in requests:
        key = f"{row['namespace']}/{row['name']}"
        system = row["namespace"] in SYSTEM_NAMESPACES
        if system or pattern_list is None or any(fnmatch(key, p) for p in pattern_list):
            cpu += row["cpu_m"]
            mem += row["mem_mi"]
            largest = max(largest, row["max_pod_cpu_m"])
    free_cpu = (shape.cpu_m - shape.daemon_cpu_m) * PACKING_LIMIT
    free_mem = (shape.mem_mi - shape.daemon_mem_mi) * PACKING_LIMIT
    nodes = max(1, math.ceil(cpu / free_cpu), math.ceil(mem / free_mem))
    return {"nodes": nodes, "cpu_m": round(cpu), "mem_mi": round(mem), "largest_pod_cpu_m": round(largest)}


def node_hourly() -> float:
    """Return the on-demand hourly price of one e2-standard-4 node."""
    return NODE_CORES * PRICES["e2_core_hour"] + NODE_RAM_GIB * PRICES["e2_ram_gib_hour"]


def estimate_hourly(nodes: int, pvc_gib: float, gcs_gib: float, ar_gib: float) -> dict[str, float]:
    """Estimate list-price hourly cost, excluding logging, metrics and egress.

    The cluster fee is shown apart because the GKE free tier normally offsets
    it for one zonal cluster; that offset has not been read from billing data.
    """
    parts = {
        "nodes": nodes * node_hourly(),
        "node_external_ips": nodes * PRICES["external_ip_hour"],
        "node_boot_disks": nodes * NODE_BOOT_DISK_GIB * PRICES["balanced_pd_gib_month"] / HOURS_PER_MONTH,
        "pvc_disks": pvc_gib * PRICES["balanced_pd_gib_month"] / HOURS_PER_MONTH,
        "gcs": gcs_gib * PRICES["gcs_standard_gib_month"] / HOURS_PER_MONTH,
        "artifact_registry": max(ar_gib - 0.5, 0) * PRICES["artifact_registry_gib_month"] / HOURS_PER_MONTH,
    }
    parts["total_excluding_cluster_fee"] = sum(parts.values())
    parts["cluster_fee_before_free_tier"] = PRICES["gke_cluster_hour"]
    return {key: round(value, 4) for key, value in parts.items()}


class Lab:
    """Thin command layer that keeps reads and mutations separate."""

    def __init__(self, execute: bool, kubeconfig: str, runner: Callable[..., str] | None = None):
        self.execute = execute
        self.kubeconfig = kubeconfig
        self.runner = runner or self._run

    @staticmethod
    def _run(command: list[str]) -> str:
        return subprocess.run(command, check=True, text=True, capture_output=True).stdout

    def gcloud(self, *args: str) -> str:
        """Run a read-only gcloud command pinned to the project."""
        return self.runner(["gcloud", *args, "--project", PROJECT])

    def kubectl(self, *args: str) -> str:
        """Run a read-only kubectl command pinned to the cluster context."""
        return self.runner(["kubectl", "--kubeconfig", self.kubeconfig, "--context", CONTEXT, *args])

    def mutate(self, command: list[str]) -> None:
        """Print a mutating command and run it only in execute mode."""
        print(("EXECUTE: " if self.execute else "PLAN: ") + shlex.join(command))
        if self.execute:
            self.runner(command)

    def k_mutate(self, *args: str) -> None:
        """Apply :meth:`mutate` to a context-pinned kubectl command."""
        self.mutate(["kubectl", "--kubeconfig", self.kubeconfig, "--context", CONTEXT, *args])

    def connect(self) -> None:
        """Write credentials for the IAM DNS endpoint to the tool's kubeconfig."""
        Path(self.kubeconfig).parent.mkdir(parents=True, exist_ok=True)
        env_prefix = ["env", f"KUBECONFIG={self.kubeconfig}"]
        self.runner([*env_prefix, "gcloud", "container", "clusters", "get-credentials", CLUSTER,
                     "--location", LOCATION, "--project", PROJECT, "--dns-endpoint"])

    def pool(self) -> dict[str, Any]:
        """Read the node pool definition."""
        return json.loads(self.gcloud("container", "node-pools", "describe", NODE_POOL,
                                      "--cluster", CLUSTER, "--location", LOCATION, "--format=json"))

    def pool_vms(self) -> list[str]:
        """List Compute Engine VMs that belong to the node pool."""
        rows = json.loads(self.gcloud("compute", "instances", "list", "--format=json(name,status)"))
        return [row["name"] for row in rows if row["name"].startswith(VM_PREFIX)]

    def identity_problems(self) -> list[str]:
        """Return reasons a resize could hit the wrong or an autoscaled target."""
        problems = []
        pools = self.gcloud("container", "node-pools", "list", "--cluster", CLUSTER,
                            "--location", LOCATION, "--format=value(name)").split()
        if pools != [NODE_POOL]:
            problems.append(f"unexpected node pools: {pools}")
        if self.pool().get("autoscaling", {}).get("enabled"):
            problems.append("node pool autoscaling is enabled")
        nap = self.gcloud("container", "clusters", "describe", CLUSTER, "--location", LOCATION,
                          "--format=value(autoscaling.enableNodeAutoprovisioning)").strip()
        if nap.lower() == "true":
            problems.append("node auto-provisioning is enabled")
        return problems

    def active_work(self) -> list[str]:
        """Return in-cluster and Dataproc work that a shutdown would interrupt."""
        active = []
        jobs = json.loads(self.kubectl("get", "jobs", "-A", "-o", "json"))["items"]
        active += [f"job {j['metadata']['namespace']}/{j['metadata']['name']}"
                   for j in jobs if j.get("status", {}).get("active")]
        flows = json.loads(self.kubectl("get", "workflows.argoproj.io", "-A", "-o", "json"))["items"]
        active += [f"workflow {w['metadata']['name']}" for w in flows
                   if w.get("status", {}).get("phase") in (None, "", "Pending", "Running")]
        batches = self.gcloud("dataproc", "batches", "list", "--region", "asia-southeast1",
                              "--filter=state=RUNNING OR state=PENDING", "--format=value(name)")
        active += [f"dataproc {name}" for name in batches.split()]
        return active

    def live_replicas(self) -> dict[str, int]:
        """Read current replica counts for Deployments and StatefulSets."""
        items = json.loads(self.kubectl("get", "deploy,sts", "-A", "-o", "json"))["items"]
        return {f"{i['metadata']['namespace']}/{i['metadata']['name']}": i["spec"].get("replicas", 0)
                for i in items}


def require_approval(args: argparse.Namespace) -> None:
    """Refuse execute mode without both explicit approval signals."""
    if args.execute and (args.confirm_cluster != CLUSTER or os.environ.get("CLOUD_LAB_APPROVED") != "yes"):
        raise GateError("execute needs --confirm-cluster ewallet-dev and CLOUD_LAB_APPROVED=yes")


def read_manifest(path: Path) -> tuple[list[Workload], list[dict[str, Any]]]:
    """Load recorded workloads and request totals from a recovery manifest."""
    return (load_workloads(json.loads((path / "workloads.json").read_text())),
            json.loads((path / "workload-requests.json").read_text()))


def resize_command(nodes: int) -> list[str]:
    """Build the supported node pool resize command."""
    return ["gcloud", "container", "clusters", "resize", CLUSTER, "--project", PROJECT,
            "--location", LOCATION, "--node-pool", NODE_POOL, f"--num-nodes={nodes}", "--quiet"]


def cmd_status(lab: Lab, _: argparse.Namespace) -> int:
    """Print pool size, VMs and scaled-down workloads."""
    pool = lab.pool()
    print(json.dumps({"cluster": CLUSTER, "pool": NODE_POOL, "pool_status": pool.get("status"),
                      "pool_autoscaling": pool.get("autoscaling", {}), "pool_vms": lab.pool_vms()}, indent=2))
    return 0


def cmd_plan_sleep(lab: Lab, _: argparse.Namespace) -> int:
    """Evaluate every sleep gate and print the verdict."""
    pdbs = json.loads(lab.kubectl("get", "pdb", "-A", "-o", "json"))["items"]
    blockers, active, identity = evaluate_pdbs(pdbs), lab.active_work(), lab.identity_problems()
    print(json.dumps({"verdict": classify_sleep(blockers, active, identity), "pdb_blockers": blockers,
                      "active_work": active, "identity_problems": identity}, indent=2))
    return 0


def cmd_sleep(lab: Lab, args: argparse.Namespace) -> int:
    """Resize to zero after the gates pass; PDB-guarded scaling needs a flag."""
    require_approval(args)
    pdbs = json.loads(lab.kubectl("get", "pdb", "-A", "-o", "json"))["items"]
    blockers, active, identity = evaluate_pdbs(pdbs), lab.active_work(), lab.identity_problems()
    if active or identity:
        raise GateError(f"UNSAFE_TO_SLEEP: {active + identity}")
    if blockers and not args.scale_pdb_guarded:
        raise GateError(f"PDBs would be bypassed: {blockers}; rerun with owner approval and --scale-pdb-guarded")
    for pdb in pdbs:
        key = f"{pdb['metadata']['namespace']}/{pdb['metadata']['name']}"
        if key in blockers:
            lab.k_mutate("-n", pdb["metadata"]["namespace"], "scale", "deploy", pdb["metadata"]["name"], "--replicas=0")
    lab.mutate(resize_command(0))
    return 0


def wake_targets(args: argparse.Namespace, mode: str) -> list[str] | None:
    """Return the allow-list for a mode, or None for the whole system."""
    if mode == "full":
        return None
    profiles = json.loads(PROFILES_PATH.read_text())[mode]
    if args.profile not in profiles:
        raise GateError(f"--profile must be one of {sorted(profiles)}")
    return profiles[args.profile]


def cmd_plan_wake(lab: Lab, args: argparse.Namespace) -> int:
    """Print the node count and cost for a wake without touching the cluster."""
    patterns = wake_targets(args, args.mode)
    _, requests = read_manifest(args.manifest)
    need = required_nodes(requests, patterns)
    if args.mode == "full":
        need["nodes"] = max(need["nodes"], 4)
    need["node_hourly_usd"] = round(need["nodes"] * node_hourly(), 4)
    print(json.dumps(need, indent=2))
    return 0


def wake(lab: Lab, args: argparse.Namespace, mode: str) -> int:
    """Bring up a mode: hold others down, add nodes, label, restore replicas."""
    require_approval(args)
    patterns = wake_targets(args, mode)
    recorded, requests = read_manifest(args.manifest)
    nodes = args.nodes or required_nodes(requests, patterns)["nodes"]
    if mode == "full":
        nodes = args.nodes or max(required_nodes(requests, None)["nodes"], 4)
    current = lab.live_replicas()
    if patterns is not None:
        for workload in scale_down_plan(recorded, current, patterns):
            lab.k_mutate("-n", workload.namespace, "scale", workload.kind.lower(), workload.name, "--replicas=0")
    lab.mutate(resize_command(nodes))
    if lab.execute:
        wait_for_nodes(lab, nodes, args.timeout)
        names = lab.kubectl("get", "nodes", "-o", "name").split()
    else:
        names = [f"node/<new-node-{i}>" for i in range(nodes)]
    for name in names[: min(nodes, G3_NODE_TARGET)]:
        lab.k_mutate("label", name, f"{G3_LABEL}=true", "--overwrite")
    for workload, replicas in restore_plan(recorded, current, patterns):
        lab.k_mutate("-n", workload.namespace, "scale", workload.kind.lower(), workload.name, f"--replicas={replicas}")
    return 0


def wait_for_nodes(lab: Lab, nodes: int, timeout: int) -> None:
    """Block until the requested number of nodes reports Ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = json.loads(lab.kubectl("get", "nodes", "-o", "json"))["items"]
        ready = [n for n in items for c in n["status"]["conditions"]
                 if c["type"] == "Ready" and c["status"] == "True"]
        if len(ready) >= nodes:
            return
        time.sleep(15)
    raise GateError(f"{nodes} nodes were not Ready within {timeout}s")


def sleep_failures(vms: list[str], disks: set[str], before: set[str], buckets: set[str], buckets_before: set[str]) -> list[str]:
    """Compare post-sleep state with the recorded manifest."""
    failures = [f"pool VM remains: {vm}" for vm in vms]
    failures += [f"disk missing: {d}" for d in sorted(before - disks) if d.startswith("pvc-")]
    failures += [f"bucket missing: {b}" for b in sorted(buckets_before - buckets)]
    return failures


def cmd_verify(lab: Lab, args: argparse.Namespace) -> int:
    """Check durable data and, for sleep, that no pool VM remains."""
    before = {d["name"] for d in json.loads((args.manifest / "disks-before.json").read_text())}
    buckets_before = set((args.manifest / "buckets-before.txt").read_text().split())
    disks = {d["name"] for d in json.loads(lab.gcloud("compute", "disks", "list", "--format=json(name)"))}
    buckets = set(lab.gcloud("storage", "buckets", "list", "--format=value(name)").split())
    vms = lab.pool_vms() if args.expect == "sleep" else []
    failures = sleep_failures(vms, disks, before, buckets, buckets_before)
    if args.expect == "awake":
        recorded, _ = read_manifest(args.manifest)
        failures += [f"{w.key} replicas != {r}" for w, r in restore_plan(recorded, lab.live_replicas())]
    print(json.dumps({"expect": args.expect, "failures": failures}, indent=2))
    return 1 if failures else 0


def cmd_cost_status(lab: Lab, args: argparse.Namespace) -> int:
    """Print list-price estimates for a node count; billing data is not read."""
    disks = json.loads(lab.gcloud("compute", "disks", "list", "--format=json(name,sizeGb)"))
    pvc_gib = sum(int(d["sizeGb"]) for d in disks if d["name"].startswith("pvc-"))
    nodes = len(lab.pool_vms())
    print(json.dumps({"nodes": nodes, "pvc_gib": pvc_gib, "gcs_gib": args.gcs_gib, "estimate_usd_per_hour":
                      estimate_hourly(nodes, pvc_gib, args.gcs_gib, args.ar_gib)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", default=str(Path.home() / ".cache/ewallet-cloud-lab/kubeconfig"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("plan-sleep")
    sleep = sub.add_parser("sleep")
    sleep.add_argument("--scale-pdb-guarded", action="store_true")
    plan = sub.add_parser("plan-wake")
    plan.add_argument("--mode", choices=["minimal", "experiment", "full"], required=True)
    plan.add_argument("--profile")
    plan.add_argument("--manifest", type=Path, required=True)
    for name in ("wake-minimal", "wake-experiment", "wake-full"):
        command = sub.add_parser(name)
        command.add_argument("--profile")
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--nodes", type=int)
        command.add_argument("--timeout", type=int, default=900)
    verify = sub.add_parser("verify")
    verify.add_argument("--expect", choices=["sleep", "awake"], required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    cost = sub.add_parser("cost-status")
    cost.add_argument("--gcs-gib", type=float, default=3442.3)
    cost.add_argument("--ar-gib", type=float, default=14.14)
    for name in ("sleep", "wake-minimal", "wake-experiment", "wake-full"):
        sub.choices[name].add_argument("--execute", action="store_true")
        sub.choices[name].add_argument("--confirm-cluster")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one command and turn gate refusals into exit code 2."""
    args = build_parser().parse_args(argv)
    lab = Lab(getattr(args, "execute", False), args.kubeconfig)
    handlers: dict[str, Callable[[Lab, argparse.Namespace], int]] = {
        "status": cmd_status, "plan-sleep": cmd_plan_sleep, "sleep": cmd_sleep, "plan-wake": cmd_plan_wake,
        "verify": cmd_verify, "cost-status": cmd_cost_status,
        "wake-minimal": lambda l, a: wake(l, a, "minimal"),
        "wake-experiment": lambda l, a: wake(l, a, "experiment"),
        "wake-full": lambda l, a: wake(l, a, "full"),
    }
    try:
        if args.command not in {"cost-status", "status", "plan-wake"}:
            lab.connect()
        return handlers[args.command](lab, args)
    except GateError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
