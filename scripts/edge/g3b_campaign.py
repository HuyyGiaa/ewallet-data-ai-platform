"""One entrypoint for an unattended G3-B campaign: pre-wake gates, phases, deadline, cleanup, watchdog.

It only sequences the repository's own tools (cloud_lab.py, platform_checks.py,
ownership_preflight.py, issue_certificate.sh, dynu_dns.py, dns_check.py, negative_tests.py,
public_e2e.py, release_diff.py, helm). State lives in <campaign dir>/state.json (no secrets),
written atomically, so any process can resume or clean up.

  init      record the owner's inputs and approvals, evaluate the pre-wake gates
  run       execute the phases; a failure, the deadline (3 h after wake) or SIGTERM/SIGINT
            stops new work and runs cleanup
  cleanup   idempotent, resumable, bounded retries; never bypasses Cloud Lab, PDB or KEDA guards
  watchdog  for the systemd user timer: cleans up when the deadline passed, or when the
            heartbeat is stale and the main process is gone
  status    print the state

Guarantees, honestly: cleanup in ``run`` covers failures and signals; the watchdog covers a
dead main process (Claude, terminal, crash) while the laptop is on and online; neither helps if
the laptop suspends, loses power or network. That needs a cloud-side fallback (see runbook).

DNS before the LB: the LB address is released only after dns_check.py proves the names no
longer point at it. If that cannot be proven, the LB is kept (its address stays ours, so no
record dangles to someone else's address), the nodes still go to guarded sleep, and the result
is CLEANUP_INCOMPLETE with the reason.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "scripts/edge"
CLUSTER, PROJECT = "ewallet-dev", "ewallet-data-ai-platform-hg26"
APPROVED_RECORD = ROOT / "docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z"
PRIVATE_VALUES = ROOT / "docs/evidence/edai2/gateway/g3a0_closure_20261121T172654Z/helm/g3a-private-values.yaml"
GATEWAY_CHART_SHA = "f8b350a2136ece7a103e479cb67ca9f71e2162ab7bc05100922fe65e681e125f"
DEADLINE_HOURS = 3
STALE_SECONDS = 600
GATES = ("CLOUD_LAB_RECOVERY_MANIFEST_VALID", "PDB_APPROVAL_COMPLETE", "KEDA_SLEEP_APPROVAL_READY",
         "GUARDED_SLEEP_PLAN_VALID", "UNATTENDED_CLEANUP_READY", "CLIENT_ADDRESS_APPROVED")


# --- state --------------------------------------------------------------------------------------

def now() -> float:
    return time.time()


def iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


def load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"invalid campaign state {path}: {error}")
    for key in ("CAMPAIGN_ID", "CLUSTER", "PROJECT", "config", "approvals", "gates"):
        if key not in state:
            raise SystemExit(f"invalid campaign state {path}: missing {key}")
    if state["CLUSTER"] != CLUSTER or state["PROJECT"] != PROJECT:
        raise SystemExit("campaign state is for another cluster")
    return state


def save(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    tmp.replace(path)


class Locked:
    """An exclusive, non-blocking lock beside the state file (one cleanup at a time)."""

    def __init__(self, path: Path):
        self.handle = open(path.with_suffix(".lock"), "a+")

    def __enter__(self):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            return None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


# --- command execution --------------------------------------------------------------------------

class Executor:
    """Runs a command; ``env`` adds variables for that command only. Output is not logged."""

    def run(self, cmd: list[str], env: dict | None = None, timeout: int = 3600) -> tuple[int, str]:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            return 124, ""
        return done.returncode, done.stdout


class Simulator(Executor):
    """Offline: records commands, answers like the real tools. EDGE_SIM_FAIL=<substring> makes
    matching commands fail; EDGE_SIM_SLEEP=<seconds> slows every command (kill tests)."""

    def __init__(self, log: Path):
        self.log = log

    def run(self, cmd, env=None, timeout=3600):
        text = " ".join(cmd)
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"cmd": cmd, "env": sorted((env or {}).keys())}) + "\n")
        time.sleep(float(os.environ.get("EDGE_SIM_SLEEP", "0")))
        fail = os.environ.get("EDGE_SIM_FAIL")
        if fail and fail in text:
            return 1, ""
        if "myip.opendns.com" in text:
            return 0, "203.0.113.7"
        if "loadBalancer.ingress" in text:
            return 0, "203.0.113.10"
        if "get nodes" in text:
            return 0, ""
        if "plan-sleep" in text:
            return 0, json.dumps({"pdb_blockers": ["ewallet-mcp/feature-rag-mcp"], "guarded_preparation": {
                "not_covered": ["ewallet-mcp/feature-rag-mcp"]}})
        return 0, ""


# --- phases -------------------------------------------------------------------------------------

def k(state: dict, *args: str) -> list[str]:
    c = state["config"]
    return ["kubectl", "--kubeconfig", c["kubeconfig"], "--context", c["context"], *args]


def helm(state: dict, *args: str) -> list[str]:
    c = state["config"]
    return ["helm", "--kubeconfig", c["kubeconfig"], "--kube-context", c["context"], *args]


def py(script: str, *args: str) -> list[str]:
    return [sys.executable, str(EDGE / script), *args]


def kargs(state: dict) -> list[str]:
    return ["--kubeconfig", state["config"]["kubeconfig"], "--context", state["config"]["context"]]


def edge_upgrade(state: dict, values: str) -> list[str]:
    return helm(state, "upgrade", "ewallet-edge", str(ROOT / "infra/helm/ewallet-edge"), "-n", "ewallet-ingress",
                "-f", str(PRIVATE_VALUES), "-f", values)


def gateway_upgrade(state: dict, public: bool) -> list[str]:
    files = ["values-gateway.yaml"] + (["values-gateway-public.yaml", "values-gateway-public-open.yaml"] if public else [])
    return helm(state, "upgrade", "ewallet-edge-gateway", state["config"]["gateway_chart"], "-n", "ewallet-ingress",
                *[x for f in files for x in ("-f", str(ROOT / "infra/helm/ewallet-edge" / f))])


def public_preflight(state: dict) -> list[str]:
    c = state["config"]
    return py("ownership_preflight.py", "live", "--context", c["context"], "--kubeconfig", c["kubeconfig"],
              "--allow-public-loadbalancer", "--domain", c["domain"], "--approved-cidrs-file", c["approved_cidrs"])


def cert(state: dict, environment: str, *extra: str) -> list[str]:
    c = state["config"]
    return ["bash", str(EDGE / "issue_certificate.sh"), "--environment", environment, "--domain", c["domain"],
            "--context", c["context"], "--kubeconfig", c["kubeconfig"], "--private-dir", str(Path(c["private"]) / "tls"), *extra]


def phases(state: dict) -> list[tuple[str, Callable]]:
    """(name, function(state, ex) -> error or None), in runbook order."""
    c, p = state["config"], Path(state["config"]["private"])
    selfsigned = str(p / "tls/self-signed/fullchain.pem")
    edge_args = ["--domain", c["domain"], "--credentials-file", c["edge_credentials"]]

    def step(*cmds, env=None, retries=1, interval=60):
        def run(state, ex):
            for cmd in cmds:
                cmd = cmd(state) if callable(cmd) else cmd
                for attempt in range(retries):
                    rc, _ = ex.run(cmd, env)
                    if rc == 0:
                        break
                    if attempt + 1 < retries:
                        time.sleep(interval if not isinstance(ex, Simulator) else 0)
                else:
                    return f"{Path(cmd[1] if cmd[0] in (sys.executable, 'bash') else cmd[0]).name} exit {rc}"
            return None
        return run

    def wake(state, ex):
        state["DEADLINE"] = iso(now() + DEADLINE_HOURS * 3600)
        state["deadline_epoch"] = now() + DEADLINE_HOURS * 3600
        state["wake_attempted"] = True  # persisted BEFORE the wake: a crash mid-wake must still lead to sleep
        save(Path(state["state_path"]), state)
        rc, _ = ex.run([sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "wake-full", "--manifest", c["manifest"],
                        "--nodes", "5", "--execute", "--confirm-cluster", CLUSTER], {"CLOUD_LAB_APPROVED": "yes"})
        state["WAKE_COMPLETED"] = rc == 0
        return None if rc == 0 else f"wake-full exit {rc}"

    def open_lb(state, ex):
        state["PUBLIC_LB_CREATED"] = True  # persisted BEFORE the upgrade: cleanup must always look
        save(Path(state["state_path"]), state)
        rc, _ = ex.run(gateway_upgrade(state, True))
        if rc != 0:
            return f"gateway upgrade exit {rc}"
        for _ in range(30):
            rc, out = ex.run(k(state, "-n", "ewallet-ingress", "get", "svc", "ewallet-edge-gateway", "-o",
                               "jsonpath={.status.loadBalancer.ingress[0].ip}"))
            if rc == 0 and out.strip():
                state["LB_IP"] = str(ipaddress.ip_address(out.strip()))
                save(Path(state["state_path"]), state)
                return step(public_preflight)(state, ex)
            time.sleep(0 if isinstance(ex, Simulator) else 20)
        return "no LB address within 10 minutes"

    def dns_online(state, ex):
        state["DNS_TARGET_STATE"] = "unknown"  # persisted BEFORE the update: cleanup takes DNS offline
        save(Path(state["state_path"]), state)
        rc, _ = ex.run(py("dynu_dns.py", "--credentials", c["dns_credentials"], "--execute", "online", "--ip", state["LB_IP"]))
        state["DNS_TARGET_STATE"] = "online" if rc == 0 else "unknown"
        return None if rc == 0 else f"DNS online exit {rc}"

    def grafana(state, ex):
        if not state["approvals"].get("grafana_root_url"):
            state["GRAFANA"] = "NOT_APPROVED"
            return None
        url, live, vals = f"https://observe.{c['domain']}/", p / "llm-live.yaml", p / "llm-values.yaml"
        chart = str(ROOT / "infra/helm/llm-agent-platform")
        cmds = [(helm(state, "get", "manifest", "llm-agent", "-n", "default"), live),
                (helm(state, "get", "values", "llm-agent", "-n", "default", "-a", "-o", "yaml"), vals)]
        for cmd, out in cmds:
            rc, text = ex.run(cmd)
            if rc != 0:
                state["GRAFANA"] = "SKIPPED (cannot read the release)"
                return None
            out.write_text(text, encoding="utf-8")
        for name, extra in (("llm-current.yaml", []), ("llm-proposed.yaml", ["--set", f"grafanaExternal.rootUrl={url}"])):
            rc, text = ex.run(helm(state, "template", "llm-agent", chart, "-n", "default", "-f", str(vals), *extra))
            (p / name).write_text(text, encoding="utf-8")
        drift, _ = ex.run(py("release_diff.py", "--before", str(live), "--after", str(p / "llm-current.yaml")))
        only, _ = ex.run(py("release_diff.py", "--before", str(p / "llm-current.yaml"), "--after", str(p / "llm-proposed.yaml"),
                            "--allow-grafana-root-url", url))
        if drift or only:
            state["GRAFANA"] = "SKIPPED (release_diff not clean: owner review)"
            return None
        rc, _ = ex.run(helm(state, "upgrade", "llm-agent", chart, "-n", "default", "-f", str(vals), "--set", f"grafanaExternal.rootUrl={url}"))
        state["GRAFANA"] = "APPLIED" if rc == 0 else f"UPGRADE_FAILED exit {rc}"
        return None if rc == 0 else "Grafana upgrade failed"

    ev = Path(c["campaign_dir"])
    return [
        ("wake", wake),
        ("baseline", step(py("platform_checks.py", *kargs(state), "backends", "--snapshot-out", str(ev / "baseline.json"),
                             "--out", str(ev / "02-backends.jsonl")), retries=25)),
        ("temporary-certificate", step(cert(state, "self-signed", "--import"))),
        ("edge-restricted", step(edge_upgrade(state, c["values_acme"]), py("platform_checks.py", *kargs(state), "gateway"),
                                 py("platform_checks.py", *kargs(state), "envoy", "--restricted"),
                                 py("platform_checks.py", *kargs(state), "watch"))),
        ("L0-live", step(public_preflight)),
        ("open-lb", open_lb),
        ("dns-online", dns_online),
        ("internet-gates", step(lambda s: py("negative_tests.py", "--address", s["LB_IP"], "--ca-file", selfsigned,
                                             "--rate-limit-mode", "http2", "--out", str(ev / "10-negative.jsonl"), *edge_args),
                                lambda s: py("public_e2e.py", "--address", s["LB_IP"], "--ca-file", selfsigned, "--agents", "allowed",
                                             "--evidence-class", "PUBLIC_INTERNET", "--out", str(ev / "10-e2e.jsonl"), *edge_args))),
        ("staging-certificate", step(cert(state, "staging"))),
        ("production-certificate", step(cert(state, "production", "--confirm-production", "--import"))
         if state["approvals"].get("production_certificate") else (lambda s, e: "production certificate not approved")),
        ("responder-off", step(edge_upgrade(state, c["values_final"]))),
        ("grafana", grafana),
        ("public-e2e", step(lambda s: py("public_e2e.py", "--address", s["LB_IP"], "--agents", "allowed", "--evidence-class",
                                         "PUBLIC_INTERNET", "--out", str(ev / "14-e2e.jsonl"), *edge_args, *(
                                             ["--grafana-credentials-file", c["grafana_credentials"], "--grafana-root-url",
                                              f"https://observe.{c['domain']}/"] if c.get("grafana_credentials") else [])),
                            lambda s: py("negative_tests.py", "--address", s["LB_IP"], "--rate-limit-mode", "http2",
                                         "--out", str(ev / "14-negative.jsonl"), *edge_args),
                            py("platform_checks.py", *kargs(state), "compare", "--baseline", str(ev / "baseline.json")),
                            public_preflight)),
    ]


# --- cleanup ------------------------------------------------------------------------------------

def cleanup(state_path: Path, ex: Executor, reason: str) -> dict:
    """Idempotent: each finished step is skipped on resume. Never bypasses a guard."""
    state = load(state_path)
    c, steps = state["config"], state.setdefault("cleanup_steps", {})
    state.update(CLEANUP_STARTED=state.get("CLEANUP_STARTED") or iso(now()), cleanup_reason=state.get("cleanup_reason") or reason)
    save(state_path, state)
    sim = isinstance(ex, Simulator)

    def done(name, ok, detail=""):
        steps[name] = {"ok": ok, "detail": detail, "at": iso(now())}
        save(state_path, state)
        return ok

    def retry(cmd, attempts=3, interval=60, env=None):
        rc = 1
        for attempt in range(attempts):
            rc, out = ex.run(cmd, env)
            if rc == 0:
                return 0, out
            if attempt + 1 < attempts:
                time.sleep(0 if sim else interval)
        return rc, ""

    dns_safe = True
    if state.get("DNS_TARGET_STATE") in ("online", "unknown") and not steps.get("dns-offline", {}).get("ok"):
        if not c.get("dns_credentials"):
            dns_safe = done("dns-offline", False, "no DNS automation configured")
        else:
            cmd = py("dynu_dns.py", "--credentials", c["dns_credentials"], "--execute", "offline", "--mode", c["dns_offline_mode"],
                     *(["--offline-ip", c["dns_offline_ip"]] if c.get("dns_offline_ip") else []),
                     *(["--released-ip", state["LB_IP"]] if state.get("LB_IP") else []))
            rc, _ = retry(cmd, attempts=2)
            dns_safe = done("dns-offline", rc == 0, "DNS_OFFLINE_OK verified" if rc == 0 else f"not proven (exit {rc})")
            if rc == 0:
                state["DNS_TARGET_STATE"] = "offline"
    if state.get("PUBLIC_LB_CREATED") and not steps.get("lb-release", {}).get("ok"):
        if dns_safe:
            rc, _ = retry(gateway_upgrade(state, False))
            rc2, _ = retry(py("ownership_preflight.py", "live", "--context", c["context"], "--kubeconfig", c["kubeconfig"]))
            done("lb-release", rc == 0 and rc2 == 0, "ClusterIP; no LoadBalancer" if rc == rc2 == 0 else f"exit {rc}/{rc2}")
        else:
            done("lb-release", False, "LB KEPT: DNS not proven offline, its address stays ours (no dangling record)")
    if state.get("wake_attempted") and not steps.get("sleep", {}).get("ok"):
        manifest = c["manifest"]
        if state.get("baseline_passed") and not steps.get("record-manifest", {}).get("ok"):
            fresh = str(Path(c["campaign_dir"]) / "recovery-manifest")
            rc, _ = ex.run([sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "record-manifest", "--out", fresh,
                            "--campaign", state["CAMPAIGN_ID"]])
            done("record-manifest", rc == 0, fresh if rc == 0 else f"exit {rc}: the starting manifest is used")
        if steps.get("record-manifest", {}).get("ok"):
            manifest = steps["record-manifest"]["detail"]
        state["sleep_manifest"] = manifest
        rc, out = ex.run([sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "plan-sleep", "--manifest", str(APPROVED_RECORD)])
        try:
            uncovered = set(json.loads(out).get("guarded_preparation", {}).get("not_covered", [])) if rc == 0 else None
        except ValueError:
            uncovered = None
        allowed = set(state["approvals"].get("also_approve", []))
        if uncovered is None or not uncovered <= allowed:
            done("sleep", False, f"SLEEP_BLOCKED: unapproved PDB blockers {sorted((uncovered or {'plan-sleep failed'}) - allowed)}")
        else:
            env = {"CLOUD_LAB_APPROVED": "yes", "CLOUD_LAB_GUARDED_SLEEP_APPROVAL": f"{CLUSTER}:{state['CAMPAIGN_ID']}",
                   "CLOUD_LAB_KEDA_PAUSE_APPROVAL": f"{CLUSTER}:{state['CAMPAIGN_ID']}"}
            cmd = [sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "sleep", "--scale-pdb-guarded", "--pause-keda",
                   "--manifest", str(APPROVED_RECORD), "--campaign", state["CAMPAIGN_ID"],
                   *[x for key in sorted(uncovered) for x in ("--also-approve", key)], "--execute", "--confirm-cluster", CLUSTER]
            rc, _ = retry(cmd, attempts=2, interval=120, env=env)
            done("sleep", rc == 0, "guarded sleep exit 0" if rc == 0 else f"guarded sleep exit {rc} (see cloud_lab output; no bypass)")
    if state.get("wake_attempted"):
        rc, _ = ex.run([sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "verify", "--expect", "sleep", "--manifest",
                        state.get("sleep_manifest", c["manifest"])])
        rc_n, nodes = ex.run(k(state, "get", "nodes", "--no-headers"))
        count = len([l for l in nodes.splitlines() if l.strip()]) if rc_n == 0 else None
        state["LAST_VERIFIED_NODE_COUNT"] = count
        done("verify-sleep", rc == 0 and count == 0, f"verify exit {rc}; nodes={count}")
    state["SLEEP_STATUS"] = "VERIFIED" if steps.get("verify-sleep", {}).get("ok") or not state.get("wake_attempted") else "NOT_VERIFIED"
    failed = [n for n, s in steps.items() if not s["ok"]]
    state["CLEANUP_STATUS"] = "COMPLETE" if not failed else "CLEANUP_INCOMPLETE"
    state["cleanup_failed_steps"] = failed
    state["campaign_active"] = bool(failed)
    save(state_path, state)
    if failed:
        alert = Path(c["campaign_dir"]) / "ALERT.txt"
        alert.write_text(f"{iso(now())} CLEANUP_INCOMPLETE {failed}: see state.json cleanup_steps; "
                         f"docs/runbooks/edai2/EXTERNAL_GATEWAY_RUNBOOK.md 'Recovery'.\n", encoding="utf-8")
        if shutil.which("notify-send") and not sim:
            subprocess.run(["notify-send", "-u", "critical", "G3-B cleanup incomplete", ", ".join(failed)], check=False)
    return state


# --- run / watchdog / init -----------------------------------------------------------------------

def run(state_path: Path, ex: Executor) -> dict:
    state = load(state_path)
    if not all(state["gates"].get(g) == "PASS" for g in GATES) and not isinstance(ex, Simulator):
        raise SystemExit(f"pre-wake gates not all PASS: {state['gates']}; not waking")
    stop = {"signal": None}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(signal="SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: stop.update(signal="SIGINT"))
    state.update(campaign_active=True, main_pid=os.getpid(), START_TIME=state.get("START_TIME") or iso(now()),
                 heartbeat=now(), state_path=str(state_path), G3B_STATUS="RUNNING")
    save(state_path, state)
    reason = "PASS"
    for name, function in phases(state):
        if stop["signal"]:
            reason = f"stopped by {stop['signal']}"
            break
        if state.get("deadline_epoch") and now() > state["deadline_epoch"]:
            reason = "DEADLINE_EXPIRED"
            break
        if state.get("phases", {}).get(name) == "PASS":
            continue
        state.update(CURRENT_PHASE=name, heartbeat=now())
        save(state_path, state)
        error = function(state, ex)
        state.setdefault("phases", {})[name] = "PASS" if error is None else f"FAIL: {error}"
        if name == "baseline" and error is None:
            state["baseline_passed"] = True
        state["heartbeat"] = now()
        save(state_path, state)
        if error:
            reason = f"FAIL at {name}: {error}"
            break
    state["G3B_STATUS"] = "PASS" if reason == "PASS" else reason
    state["CURRENT_PHASE"] = "cleanup"
    save(state_path, state)
    return cleanup(state_path, ex, reason)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watchdog(state_path: Path, ex: Executor, max_attempts: int = 3) -> str:
    state = load(state_path)
    if not state.get("campaign_active"):
        return "IDLE"
    expired = bool(state.get("deadline_epoch")) and now() > state["deadline_epoch"]
    stale = now() - state.get("heartbeat", 0) > STALE_SECONDS and not pid_alive(state.get("main_pid"))
    if not (expired or stale):
        return "OK"
    if state.get("watchdog_attempts", 0) >= max_attempts:
        return "GAVE_UP (see ALERT.txt)"
    with Locked(state_path) as lock:
        if lock is None:
            return "CLEANUP_ALREADY_RUNNING"
        state["watchdog_attempts"] = state.get("watchdog_attempts", 0) + 1
        save(state_path, state)
        result = cleanup(state_path, ex, "watchdog: " + ("deadline expired" if expired else "main process gone"))
        return result["CLEANUP_STATUS"]


def outside_repo(path: str | None, label: str, problems: list[str], required: bool = True) -> str | None:
    if not path:
        if required:
            problems.append(f"{label} missing")
        return None
    resolved = Path(path).expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        problems.append(f"{label} is inside the repository")
    elif not resolved.exists():
        problems.append(f"{label} does not exist")
    return str(resolved)


def init(args: argparse.Namespace, ex: Executor) -> dict:
    campaign = Path(args.campaign_dir).expanduser().resolve()
    private = campaign / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(campaign, 0o700)
    problems: list[str] = []
    cfg = {"campaign_dir": str(campaign), "private": str(private), "domain": args.domain, "context": args.context,
           "kubeconfig": str(Path(args.kubeconfig).expanduser()), "gateway_chart": args.gateway_chart,
           "manifest": outside_repo(args.manifest, "recovery manifest", problems),
           "approved_cidrs": outside_repo(args.approved_cidrs, "approved CIDRs file", problems),
           "edge_credentials": outside_repo(args.edge_credentials, "edge credentials", problems),
           "grafana_credentials": outside_repo(args.grafana_credentials, "Grafana credentials", problems, required=False),
           "dns_credentials": outside_repo(args.dns_credentials, "DNS credentials", problems, required=False),
           "dns_offline_mode": args.dns_offline_mode, "dns_offline_ip": args.dns_offline_ip}
    cidrs = [l.split("#")[0].strip() for l in Path(cfg["approved_cidrs"]).read_text().splitlines()] if cfg["approved_cidrs"] and Path(cfg["approved_cidrs"]).exists() else []
    cidrs = [x for x in cidrs if x]
    for name, acme in (("values_acme", True), ("values_final", False)):
        path = private / f"g3b-{name}.yaml"
        path.write_text(json.dumps({"domain": args.domain, "sourceRestriction": {"enabled": True, "routes": ["kagent-ui"],
                                                                                 "allowedCidrs": cidrs},
                                    "acmeHttp01": {"enabled": acme}}, indent=2) + "\n", encoding="utf-8")
        cfg[name] = str(path)
    approvals = {"guarded_sleep": args.approve_guarded_sleep, "keda_pause": args.approve_keda_pause,
                 "also_approve": args.also_approve, "production_certificate": args.approve_production_certificate,
                 "grafana_root_url": args.approve_grafana_root_url, "dns_offline_verified": args.dns_offline_verified,
                 "local_only_risk_accepted": args.accept_local_only_risk, "cloud_fallback": args.cloud_fallback_ready}
    gates = {}
    m = Path(cfg["manifest"] or "/nonexistent")
    needed = ("MANIFEST.json", "workloads.json", "workload-requests.json", "disks-before.json", "buckets-before.txt")
    try:
        manifest_ok = all((m / f).exists() for f in needed) and json.loads((m / "MANIFEST.json").read_text()).get("cluster") == CLUSTER
    except ValueError:
        manifest_ok = False
    gates["CLOUD_LAB_RECOVERY_MANIFEST_VALID"] = "PASS" if manifest_ok else "FAIL"
    rc, _ = ex.run(py("platform_checks.py", "--kubeconfig", cfg["kubeconfig"], "--context", cfg["context"], "sleep-feasibility",
                      "--manifest", str(m), *[x for key in args.also_approve for x in ("--also-approve", key)]))
    gates["PDB_APPROVAL_COMPLETE"] = "PASS" if rc == 0 else "FAIL"
    gates["KEDA_SLEEP_APPROVAL_READY"] = "PASS" if args.approve_keda_pause else "FAIL"
    gates["GUARDED_SLEEP_PLAN_VALID"] = "PASS" if args.approve_guarded_sleep and (APPROVED_RECORD / "pdb-after-scale.txt").exists() else "FAIL"
    rc_t, _ = ex.run(["systemctl", "--user", "is-active", "edge-campaign-watchdog.timer"])
    rc_l, linger = ex.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"])
    unattended = {"dns_automation": bool(cfg["dns_credentials"]) and args.dns_offline_verified,
                  "watchdog_timer_active": rc_t == 0, "linger": linger.strip() == "yes" or isinstance(ex, Simulator),
                  "laptop_risk_covered": args.cloud_fallback_ready or args.accept_local_only_risk}
    gates["UNATTENDED_CLEANUP_READY"] = "PASS" if all(unattended.values()) else "FAIL"
    rc_ip, ip = ex.run(["dig", "+short", "myip.opendns.com", "@resolver1.opendns.com"])
    try:
        mine = ipaddress.ip_address(ip.strip().splitlines()[-1]) if rc_ip == 0 and ip.strip() else None
        inside = mine is not None and any(mine in ipaddress.ip_network(x) for x in cidrs)
    except ValueError:
        inside = False
    gates["CLIENT_ADDRESS_APPROVED"] = "PASS" if inside else "FAIL"  # the address itself is not recorded
    if problems:
        gates = {g: ("FAIL" if g in ("CLOUD_LAB_RECOVERY_MANIFEST_VALID", "UNATTENDED_CLEANUP_READY") else v) for g, v in gates.items()}
    state = {"CAMPAIGN_ID": args.campaign, "CLUSTER": CLUSTER, "PROJECT": PROJECT, "config": cfg, "approvals": approvals,
             "gates": gates, "gate_problems": problems, "unattended": unattended, "START_TIME": None, "DEADLINE": None,
             "CURRENT_PHASE": "initialised", "WAKE_COMPLETED": False, "PUBLIC_LB_CREATED": False, "LB_IP": None,
             "DNS_TARGET_STATE": "untouched", "ACME_STATUS": None, "G3B_STATUS": "NOT_STARTED", "CLEANUP_STARTED": None,
             "CLEANUP_STATUS": None, "SLEEP_STATUS": None, "LAST_VERIFIED_NODE_COUNT": None, "campaign_active": False}
    save(campaign / "state.json", state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", help="record commands instead of running them (offline tests)")
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init")
    i.add_argument("--campaign", required=True)
    i.add_argument("--campaign-dir", required=True)
    i.add_argument("--domain", default="ewallet-hg.gleeze.com")
    i.add_argument("--context", default="gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev")
    i.add_argument("--kubeconfig", default="~/.cache/ewallet-cloud-lab/kubeconfig")
    i.add_argument("--gateway-chart", default=str(Path("~/.cache/helm/repository/gateway-1.30.7.tgz").expanduser()))
    i.add_argument("--manifest", required=True)
    i.add_argument("--approved-cidrs", required=True)
    i.add_argument("--edge-credentials", required=True)
    i.add_argument("--grafana-credentials")
    i.add_argument("--dns-credentials")
    i.add_argument("--dns-offline-mode", choices=["no-ipv4", "signoff"], default="no-ipv4")
    i.add_argument("--dns-offline-ip")
    for flag in ("approve-guarded-sleep", "approve-keda-pause", "approve-production-certificate", "approve-grafana-root-url",
                 "dns-offline-verified", "accept-local-only-risk", "cloud-fallback-ready"):
        i.add_argument(f"--{flag}", action="store_true")
    i.add_argument("--also-approve", action="append", default=[], metavar="NS/NAME")
    for name in ("run", "cleanup", "watchdog", "status"):
        sub.add_parser(name).add_argument("--state", required=True)
    args = parser.parse_args(argv)
    state_path = Path(getattr(args, "state", "") or (Path(getattr(args, "campaign_dir", ".")).expanduser() / "state.json"))
    ex: Executor = Simulator(state_path.parent / "simulated-commands.jsonl") if args.simulate else Executor()
    if args.command == "init":
        state = init(args, ex)
        print(json.dumps({"gates": state["gates"], "problems": state["gate_problems"], "unattended": state["unattended"]}, indent=2))
        return 0 if all(v == "PASS" for v in state["gates"].values()) else 1
    if args.command == "status":
        print(json.dumps(load(state_path), indent=2, sort_keys=True))
        return 0
    if args.command == "watchdog":
        print(watchdog(state_path, ex) if state_path.exists() else "IDLE (no active campaign)")
        return 0
    if args.command == "cleanup":
        with Locked(state_path) as lock:
            if lock is None:
                print("CLEANUP_ALREADY_RUNNING")
                return 1
            state = cleanup(state_path, ex, "manual cleanup")
        print(state["CLEANUP_STATUS"])
        return 0 if state["CLEANUP_STATUS"] == "COMPLETE" else 1
    with Locked(state_path) as lock:
        if lock is None:
            print("ANOTHER_PROCESS_HOLDS_THE_CAMPAIGN")
            return 1
        state = run(state_path, ex)
    print(json.dumps({k: state.get(k) for k in ("G3B_STATUS", "CLEANUP_STATUS", "SLEEP_STATUS", "LAST_VERIFIED_NODE_COUNT")}))
    return 0 if state["G3B_STATUS"] == "PASS" and state["CLEANUP_STATUS"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
