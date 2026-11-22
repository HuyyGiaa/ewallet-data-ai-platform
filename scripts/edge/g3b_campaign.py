"""One entrypoint for an unattended G3-B campaign: pre-wake gates, phases, deadline, cleanup, watchdog.

It only sequences the repository's own tools (cloud_lab.py, platform_checks.py,
ownership_preflight.py, issue_certificate.sh, dynu_dns.py, dns_check.py, negative_tests.py,
public_e2e.py, release_diff.py, helm). State lives in <campaign dir>/state.json (no secrets),
written atomically, so any process can resume or clean up.

  init      record the owner's inputs and approvals, evaluate the pre-wake gates
            (--mode supervised: the owner watches; no watchdog/linger/laptop-risk gate,
            everything that protects security and data still required)
  run       execute the phases; a failure, the deadline (3 h after wake) or SIGTERM/SIGINT
            stops new work and runs cleanup
  cleanup   idempotent, resumable, bounded retries; never bypasses Cloud Lab, PDB or KEDA guards
  watchdog  for the systemd user timer: cleans up when the deadline passed, or when the
            heartbeat is stale and the main process is gone
  status    print the state

Guarantees, honestly: commands run in their own process group with a timeout bounded by the
deadline, so no step outlives it by more than its own kill; SIGTERM/SIGINT stops the running
command and runs cleanup (cleanup itself is not interrupted by a second signal). Only one
process changes the cluster at a time (the campaign lock): the watchdog never cleans up while
the main process holds it; past the deadline plus a grace period it sends the main process
SIGTERM, so the main process cleans up. The watchdog cannot stop a main process that ignores
SIGTERM (it does not) or a hung kernel call; the owner then uses ``kill -9`` and
``g3b_campaign.py cleanup`` (resumable). Nothing here helps if the laptop suspends, loses power
or network.

Scopes (init --scope):
  g3b             the full campaign (certificates, Internet gates, G4 rows). SAFE_CLEANUP_READY needs
                  --dns-offline-verified AND --dns-transition-evidence: the state.json of a
                  dns-transition campaign that ended DNS_TRANSITION=VERIFIED with the same domain,
                  offline mode and offline addresses. A bare flag proves nothing.
  dns-transition  supervised only, its own approval (--approve-dns-transition-test, not the G3-B
                  live approval): the same phases up to and including open-lb (private security
                  checks first, so the LB opens only on a checked edge), then the first real
                  Dynu cycle on the owner's own GKE LB address: DNS proven offline, online to the
                  LB (A = LB, no AAAA), signoff back to the approved offline IPv4 + IPv6, every
                  step decided by dns_check at the authoritative servers and public resolvers.
                  Then normal cleanup: LB released only when DNS is proven offline, guarded sleep.

Edge Basic Auth (phase edge-auth, before private-e2e): create_basic_auth.sh --check compares the
Secret with the owner's edge-cred file (nothing secret is printed). A mismatch stops the campaign
unless --approve-edge-secret-sync was given; then the Secret is replaced by the same script, the
gateway is restarted (Envoy reads the users file at start-up) and re-checked. private-e2e then
proves the behaviour (no / wrong credentials refused, valid accepted, agents restricted, rate
limit) before any public LB.

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

import yaml

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "scripts/edge"
CLUSTER, PROJECT = "ewallet-dev", "ewallet-data-ai-platform-hg26"
APPROVED_RECORD = ROOT / "docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z"
PRIVATE_VALUES = ROOT / "docs/evidence/edai2/gateway/g3a0_closure_20261121T172654Z/helm/g3a-private-values.yaml"
GATEWAY_CHART_SHA = "f8b350a2136ece7a103e479cb67ca9f71e2162ab7bc05100922fe65e681e125f"
DEADLINE_HOURS = 3
STALE_SECONDS = 600
DEADLINE_GRACE_SECONDS = 900        # watchdog: SIGTERM to a main process still running this long after the deadline
COMMAND_TIMEOUT = 1800              # no single command runs longer
MIN_COMMAND_TIMEOUT = 30            # a command started just before the deadline still gets this long
ACME_WAIT_SECONDS = 600             # cleanup lets an in-flight certbot finish this long before ending it
L4_WAIT_SECONDS = 1200              # how long the L4 phase waits for an external vantage point
PORT_FORWARD = ("18443", "18080")
GATEWAY_DEPLOYMENT = "ewallet-edge-gateway"
COMMON_GATES = ("CLOUD_LAB_RECOVERY_MANIFEST_VALID", "PDB_APPROVAL_COMPLETE", "KEDA_SLEEP_APPROVAL_READY",
                "GUARDED_SLEEP_PLAN_VALID", "CLIENT_ADDRESS_APPROVED", "SAFE_CLEANUP_READY", "L0_RENDER_READY",
                "EDGE_AUTH_READY", "OWNER_LIVE_APPROVAL")
GATES = COMMON_GATES + ("UNATTENDED_CLEANUP_READY",)   # unattended mode; supervised mode uses COMMON_GATES
# dns-transition scope: no G3-B live approval and no SAFE_CLEANUP_READY (that is what it proves);
# instead the DNS test preconditions and the owner's approval of exactly this test.
TRANSITION_GATES = tuple(g for g in COMMON_GATES if g not in ("SAFE_CLEANUP_READY", "OWNER_LIVE_APPROVAL")) + (
    "DNS_TRANSITION_TEST_READY", "OWNER_DNS_TRANSITION_APPROVAL")


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
        self.acquired = False

    def __enter__(self):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            return None
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:  # a lock someone else holds was never ours to release
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


# --- command execution --------------------------------------------------------------------------

class Executor:
    """Runs commands in their own process group. ``deadline`` (epoch) bounds every command's
    timeout during the campaign phases; on a timeout or ``terminate_current()`` the whole group
    (kubectl, certbot, hooks...) gets SIGTERM, then SIGKILL. Output is not logged."""

    def __init__(self):
        self.current: subprocess.Popen | None = None
        self.deadline: float | None = None
        self.on_start: Callable[[int], None] | None = None

    def run(self, cmd: list[str], env: dict | None = None, timeout: int = COMMAND_TIMEOUT) -> tuple[int, str]:
        if self.deadline:
            timeout = max(MIN_COMMAND_TIMEOUT, min(timeout, int(self.deadline - now())))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                env={**os.environ, **(env or {})}, start_new_session=True)
        self.current = proc
        if self.on_start:
            self.on_start(proc.pid)
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, out or ""
        except subprocess.TimeoutExpired:
            self.kill(proc)
            return 124, ""
        finally:
            self.current = None

    @staticmethod
    def kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    def terminate_current(self) -> None:
        if self.current is not None:
            self.kill(self.current)

    def background(self, cmd: list[str]):
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class Simulator(Executor):
    """Offline: records commands, answers like the real tools. EDGE_SIM_FAIL=<substring> makes
    matching commands fail; EDGE_SIM_SLEEP=<seconds> slows every command (kill tests)."""

    def __init__(self, log: Path):
        super().__init__()
        self.log = log
        self.auth_applied = False

    def background(self, cmd):
        self.run(cmd)
        return None

    def run(self, cmd, env=None, timeout=COMMAND_TIMEOUT):
        text = " ".join(cmd)
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"cmd": cmd, "env": sorted((env or {}).keys())}) + "\n")
        time.sleep(float(os.environ.get("EDGE_SIM_SLEEP", "0")))
        fail = os.environ.get("EDGE_SIM_FAIL")
        if fail and fail in text:
            return 1, ""
        if "create_basic_auth.sh" in text:
            # EDGE_SIM_EDGE_AUTH: unset = Secret matches; mismatch = matches after --apply;
            # stuck = never matches; error = cannot be read
            auth = os.environ.get("EDGE_SIM_EDGE_AUTH", "")
            if "--apply" in cmd:
                self.auth_applied = True
                return 0, "EDGE_AUTH_APPLIED"
            if auth == "error":
                return 2, "EDGE_AUTH_CHECK_FAILED"
            if auth == "stuck" or (auth == "mismatch" and not self.auth_applied):
                return 1, "EDGE_AUTH_MISMATCH"
            return 0, "EDGE_AUTH_MATCH"
        if "myip.opendns.com" in text:
            return 0, os.environ.get("EDGE_SIM_MYIP", "203.0.113.7")
        if "-o name" in text and "app.kubernetes.io/part-of=ewallet-edge" in text:
            names = ["httproute.gateway.networking.k8s.io/" + n for n in ("feature-api", "kagent-ui", "agent-registry", "grafana", "https-redirect")]
            if os.environ.get("EDGE_SIM_ACME_STAYS"):
                names += ["httproute.gateway.networking.k8s.io/acme-http01", "deployment.apps/edge-acme-responder"]
            return 0, "\n".join(names)
        if "get secret edge-tls" in text:
            return 0, "secret/edge-tls"
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


def edge_auth_cmd(cfg: dict, mode: str) -> list[str]:
    """create_basic_auth.sh against the owner's edge-cred file; only file paths on the command line."""
    return ["bash", str(EDGE / "create_basic_auth.sh"), "--credentials-file", cfg["edge_credentials"], mode,
            "--kubeconfig", cfg["kubeconfig"], "--context", cfg["context"]]


def offline_args(cfg: dict) -> list[str]:
    return [*(["--offline-ip", cfg["dns_offline_ip"]] if cfg.get("dns_offline_ip") else []),
            *(["--offline-ipv6", cfg["dns_offline_ipv6"]] if cfg.get("dns_offline_ipv6") else [])]


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


def client_in_allowlist(state: dict, ex: Executor) -> bool | None:
    """Is this machine's public address inside the approved CIDRs? (None: unknown.) The
    address itself is never written to the state."""
    rc, out = ex.run(["dig", "+short", "myip.opendns.com", "@resolver1.opendns.com"], timeout=30)
    try:
        mine = ipaddress.ip_address(out.strip().splitlines()[-1]) if rc == 0 and out.strip() else None
    except ValueError:
        return None
    if mine is None:
        return None
    cidrs = [l.split("#")[0].strip() for l in Path(state["config"]["approved_cidrs"]).read_text().splitlines()]
    return any(mine in ipaddress.ip_network(x) for x in cidrs if x)


def jsonl_rows(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


ACME_OBJECTS = ("httproute.gateway.networking.k8s.io/acme-http01", "deployment.apps/edge-acme-responder",
                "service/edge-acme-responder", "configmap/edge-acme-responder")


def expected_routes(state: dict) -> set[str]:
    """The application HTTPRoutes the approved campaign values enable (each route renders an
    HTTPRoute of the same name), plus https-redirect, which the chart always renders."""
    routes: dict = {}
    for values in (PRIVATE_VALUES, Path(state["config"]["values_final"])):
        for name, route in (yaml.safe_load(Path(values).read_text(encoding="utf-8")) or {}).get("routes", {}).items():
            routes[name] = {**routes.get(name, {}), **(route or {})}
    return {name for name, route in routes.items() if route.get("enabled")} | {"https-redirect"}


def acme_objects_gone(state: dict, ex: Executor) -> tuple[bool, str]:
    """By exact object identity: the four temporary ACME objects are gone, every application
    route the approved values enable (and https-redirect) is still there, and so is edge-tls."""
    rc, names = ex.run(k(state, "-n", "ewallet-ingress", "get", "httproute,deployment,service,configmap", "-l",
                         "app.kubernetes.io/part-of=ewallet-edge", "-o", "name"))
    rc_s, secret = ex.run(k(state, "-n", "ewallet-ingress", "get", "secret", "edge-tls", "-o", "name"))
    if rc != 0:
        return False, f"cannot list ewallet-ingress objects (exit {rc})"
    present = set(names.split())
    left = sorted(set(ACME_OBJECTS) & present)
    if left:
        return False, f"ACME objects still present: {left}"
    routes = {n.split("/", 1)[1] for n in present if n.startswith("httproute.gateway.networking.k8s.io/")}
    missing = sorted(expected_routes(state) - routes)
    if missing:
        return False, f"application routes missing: {missing}"
    if rc_s != 0 or secret.strip() != "secret/edge-tls":
        return False, "TLS Secret ewallet-ingress/edge-tls missing"
    return True, f"ACME objects gone; routes {sorted(expected_routes(state))} and edge-tls kept"


def phases(state: dict) -> list[tuple[str, Callable]]:
    """(name, function(state, ex) -> error or None), in runbook order. A phase that returns an
    error stops new work; gates that are only blocked (L4 without a verified external source, Grafana
    not verifiable) record their status and let the campaign continue to a non-COMPLETE verdict."""
    c, p = state["config"], Path(state["config"]["private"])
    ev = Path(c["campaign_dir"])
    selfsigned = str(p / "tls/self-signed/fullchain.pem")
    edge_args = ["--domain", c["domain"], "--credentials-file", c["edge_credentials"]]
    grafana_args = ["--grafana-credentials-file", c["grafana_credentials"]] if c.get("grafana_credentials") else []
    path = lambda: Path(state["state_path"])

    def remember(**fields):
        state.update(fields)
        save(path(), state)

    def step(*cmds, env=None, retries=1, interval=60):
        def run(state, ex):
            for cmd in cmds:
                if ex.deadline and now() > ex.deadline:
                    return "DEADLINE_EXPIRED"
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
        remember(DEADLINE=iso(now() + DEADLINE_HOURS * 3600), deadline_epoch=now() + DEADLINE_HOURS * 3600,
                 wake_attempted=True)  # persisted BEFORE the wake: a crash mid-wake must still lead to sleep
        ex.deadline = state["deadline_epoch"]
        rc, _ = ex.run([sys.executable, str(ROOT / "ops/gcp/cloud_lab.py"), "wake-full", "--manifest", c["manifest"],
                        "--nodes", "5", "--execute", "--confirm-cluster", CLUSTER], {"CLOUD_LAB_APPROVED": "yes"})
        state["WAKE_COMPLETED"] = rc == 0
        return None if rc == 0 else f"wake-full exit {rc}"

    def edge_restricted(state, ex):
        remember(ACME_STATUS="responder-enabled")  # before the upgrade that creates it
        return step(edge_upgrade(state, c["values_acme"]), py("platform_checks.py", *kargs(state), "gateway"),
                    py("platform_checks.py", *kargs(state), "envoy", "--restricted"),
                    py("platform_checks.py", *kargs(state), "watch"))(state, ex)

    def l0_render(state, ex):
        """Render exactly what the public rollout applies and prove it with ownership_preflight."""
        gw, edge = ev / "l0-gateway.yaml", ev / "l0-edge.yaml"
        files = [x for f in ("values-gateway.yaml", "values-gateway-public.yaml", "values-gateway-public-open.yaml")
                 for x in ("-f", str(ROOT / "infra/helm/ewallet-edge" / f))]
        for out, cmd in ((gw, ["helm", "template", "ewallet-edge-gateway", c["gateway_chart"], "-n", "ewallet-ingress", *files]),
                         (edge, ["helm", "template", "ewallet-edge", str(ROOT / "infra/helm/ewallet-edge"), "-n", "ewallet-ingress",
                                 "-f", str(PRIVATE_VALUES), "-f", c["values_acme"]])):
            rc, text = ex.run(cmd)
            if rc != 0:
                return f"helm template exit {rc}"
            out.write_text(text, encoding="utf-8")
        return step(py("ownership_preflight.py", "render", "--allow-public-loadbalancer", "--domain", c["domain"],
                       "--approved-cidrs-file", c["approved_cidrs"], str(gw), str(edge)))(state, ex)

    def edge_auth(state, ex):
        """The Secret must hold exactly the owner's edge-cred user; a sync is an approved mutation,
        followed by a gateway restart (Envoy reads the file at start-up) and a re-check. The
        behaviour itself is proven by private-e2e right after."""
        rc, out = ex.run(edge_auth_cmd(c, "--check"))
        if rc == 0:
            remember(EDGE_AUTH="EDGE_AUTH_MATCH")
            return None
        if rc != 1:
            remember(EDGE_AUTH="EDGE_AUTH_CHECK_FAILED")
            return "EDGE_AUTH_CHECK_FAILED"
        if not state["approvals"].get("edge_secret_sync"):
            remember(EDGE_AUTH="EDGE_AUTH_MISMATCH: Secret update not approved")
            return state["EDGE_AUTH"]
        remember(EDGE_AUTH="sync-requested")  # before the mutation
        error = step(edge_auth_cmd(c, "--apply"),
                     k(state, "-n", "ewallet-ingress", "rollout", "restart", f"deployment/{GATEWAY_DEPLOYMENT}"),
                     k(state, "-n", "ewallet-ingress", "rollout", "status", f"deployment/{GATEWAY_DEPLOYMENT}", "--timeout=300s"),
                     py("platform_checks.py", *kargs(state), "gateway"),
                     edge_auth_cmd(c, "--check"))(state, ex)
        remember(EDGE_AUTH="EDGE_AUTH_SYNCED" if error is None else f"EDGE_AUTH_SYNC_FAILED: {error}")
        return error

    def dns_transition(state, ex):
        """First real Dynu cycle on our own LB address. Each step is decided by dns_check, never by
        Dynu's answer word. A failure leaves DNS_TARGET_STATE at online/unknown, so cleanup takes
        DNS offline (signoff) and keeps the LB unless that is proven."""
        dns = ["--credentials", c["dns_credentials"], "--execute", "--attempts", "10", "--interval", "30"]

        def record(name, out):
            (ev / f"dns-transition-{name}.jsonl").write_text(out, encoding="utf-8")

        rc, out = ex.run(py("dns_check.py", "--domain", c["domain"], "--expect-offline", *offline_args(c)))
        record("0-precheck", out)
        if rc != 0:
            remember(DNS_TRANSITION="DNS_TRANSITION_NOT_STARTED: DNS not proven offline before the test")
            return state["DNS_TRANSITION"]
        remember(DNS_TARGET_STATE="unknown", DNS_TRANSITION="online-requested")  # before the update
        rc, out = ex.run(py("dynu_dns.py", *dns, "online", "--ip", state["LB_IP"]))
        record("1-online", out)
        if rc != 0:
            remember(DNS_TRANSITION="DNS_TRANSITION_ONLINE_NOT_PROVEN")
            return state["DNS_TRANSITION"]
        remember(DNS_TARGET_STATE="online", DNS_TRANSITION="offline-requested")
        rc, out = ex.run(py("dynu_dns.py", *dns, "offline", "--mode", "signoff", *offline_args(c),
                            "--released-ip", state["LB_IP"]))
        record("2-offline", out)
        if rc != 0:
            remember(DNS_TRANSITION="DNS_TRANSITION_OFFLINE_NOT_PROVEN")
            return state["DNS_TRANSITION"]
        remember(DNS_TARGET_STATE="offline", DNS_TRANSITION="VERIFIED")
        return None

    def private_e2e(state, ex):
        """GKE_PRIVATE: the real backends through the private gateway (port-forward). The
        port-forward peer is never an approved address, so agents must answer 403 here."""
        forward = ex.background(k(state, "-n", "ewallet-ingress", "port-forward", "--address", "127.0.0.1",
                                  "svc/ewallet-edge-gateway", f"{PORT_FORWARD[0]}:443", f"{PORT_FORWARD[1]}:80"))
        try:
            time.sleep(0 if isinstance(ex, Simulator) else 5)
            local = ["--address", "127.0.0.1", "--https-port", PORT_FORWARD[0], "--http-port", PORT_FORWARD[1], "--ca-file", selfsigned]
            return step(py("public_e2e.py", *local, *edge_args, *grafana_args, "--agents", "denied",
                           "--evidence-class", "GKE_PRIVATE", "--out", str(ev / "07-private-e2e.jsonl")),
                        py("negative_tests.py", *local, *edge_args, "--rate-limit-mode", "http2",
                           "--out", str(ev / "07-private-negative.jsonl")))(state, ex)
        finally:
            if forward is not None:
                Executor.kill(forward)

    def open_lb(state, ex):
        remember(PUBLIC_LB_CREATED=True)  # persisted BEFORE the upgrade: cleanup must always look
        rc, _ = ex.run(gateway_upgrade(state, True))
        if rc != 0:
            return f"gateway upgrade exit {rc}"
        for _ in range(30):
            rc, out = ex.run(k(state, "-n", "ewallet-ingress", "get", "svc", "ewallet-edge-gateway", "-o",
                               "jsonpath={.status.loadBalancer.ingress[0].ip}"))
            if rc == 0 and out.strip():
                remember(LB_IP=str(ipaddress.ip_address(out.strip())))
                return step(public_preflight)(state, ex)
            time.sleep(0 if isinstance(ex, Simulator) else 20)
        return "no LB address within 10 minutes"

    def dns_online(state, ex):
        remember(DNS_TARGET_STATE="unknown")  # persisted BEFORE the update: cleanup takes DNS offline
        rc, _ = ex.run(py("dynu_dns.py", "--credentials", c["dns_credentials"], "--execute", "online", "--ip", state["LB_IP"]))
        state["DNS_TARGET_STATE"] = "online" if rc == 0 else "unknown"
        return None if rc == 0 else f"DNS online exit {rc}"

    def internet_gates(state, ex):
        """L2/L3/L6 (negative_tests) and L5 (agents allowed) from this client, which must be an
        approved address; otherwise L5 cannot be judged here."""
        inside = client_in_allowlist(state, ex)
        if not inside:
            remember(L5="FAIL: this client is not inside the approved CIDRs" if inside is False else "FAIL: client address unknown")
            return state["L5"]
        error = step(lambda s: py("negative_tests.py", "--address", s["LB_IP"], "--ca-file", selfsigned, "--rate-limit-mode",
                                  "http2", "--out", str(ev / "10-negative.jsonl"), *edge_args),
                     lambda s: py("public_e2e.py", "--address", s["LB_IP"], "--ca-file", selfsigned, "--agents", "allowed",
                                  "--evidence-class", "PUBLIC_INTERNET", "--out", str(ev / "10-e2e.jsonl"), *edge_args))(state, ex)
        state["L5"] = "PASS" if error is None else f"FAIL: {error}"
        return error

    def l4_denied(state, ex):
        """L4 needs a real request from OUTSIDE the approved CIDRs: this machine on another network
        (e.g. a phone hotspot). Its public address is checked before and after the request (never
        recorded), the request goes to the public LB with valid credentials, and the agents host
        must answer 403 in a result file this run wrote. Files from elsewhere (l4-external.jsonl),
        mocks, headers or port-forward never count: without a verified source the gate is
        L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE, never PASS."""
        result = ev / "11-l4-denied.jsonl"
        polls = 3 if isinstance(ex, Simulator) else max(1, L4_WAIT_SECONDS // 30)
        for _ in range(polls):
            if ex.deadline and now() > ex.deadline:
                break
            if client_in_allowlist(state, ex) is False:
                result.unlink(missing_ok=True)  # only rows written by this request count
                ex.run(py("public_e2e.py", "--address", state["LB_IP"], "--ca-file", selfsigned, "--agents", "denied",
                          "--evidence-class", "PUBLIC_INTERNET", "--out", str(result), *edge_args))
                still_outside = client_in_allowlist(state, ex) is False
                agents = [r for r in jsonl_rows(result) if r.get("row") == "40"]
                if still_outside and agents and all(r.get("evidence_class") == "PUBLIC_INTERNET" for r in agents):
                    ok = all(r.get("passed") for r in agents)
                    remember(L4="PASS (this client, outside the approved CIDRs before and after)" if ok else "FAIL")
                    return None if ok else "L4 FAIL: agents reachable from a non-approved address"
            time.sleep(0 if isinstance(ex, Simulator) else 30)
        remember(L4="L4_BLOCKED_NO_VERIFIED_EXTERNAL_SOURCE")
        return None

    def certificate(environment, *extra):
        def run(state, ex):
            remember(ACME_STATUS=f"issuing-{environment}")
            ex.on_start = lambda pid: remember(acme_pgid=pid)
            try:
                error = step(cert(state, environment, *extra))(state, ex)
            finally:
                ex.on_start = None
            remember(ACME_STATUS=f"{environment}-{'issued' if error is None else 'failed'}", acme_pgid=None)
            return error
        return run

    def responder_off(state, ex):
        error = step(edge_upgrade(state, c["values_final"]))(state, ex)
        if error:
            return error
        gone, detail = acme_objects_gone(state, ex)
        if gone:
            remember(ACME_STATUS="responder-disabled")
        return None if gone else f"ACME responder not removed: {detail}"

    def grafana(state, ex):
        if not state["approvals"].get("grafana_root_url"):
            remember(GRAFANA="GRAFANA_NOT_APPROVED")
            return None
        if not c.get("grafana_credentials"):
            remember(GRAFANA="GRAFANA_VERIFICATION_BLOCKED: no Grafana credentials")
            return None
        url, live, vals = f"https://observe.{c['domain']}/", p / "llm-live.yaml", p / "llm-values.yaml"
        chart = str(ROOT / "infra/helm/llm-agent-platform")
        for cmd, out in ((helm(state, "get", "manifest", "llm-agent", "-n", "default"), live),
                         (helm(state, "get", "values", "llm-agent", "-n", "default", "-a", "-o", "yaml"), vals)):
            rc, text = ex.run(cmd)
            if rc != 0:
                remember(GRAFANA="GRAFANA_VERIFICATION_BLOCKED: cannot read the llm-agent release")
                return None
            out.write_text(text, encoding="utf-8")
        for name, extra in (("llm-current.yaml", []), ("llm-proposed.yaml", ["--set", f"grafanaExternal.rootUrl={url}"])):
            rc, text = ex.run(helm(state, "template", "llm-agent", chart, "-n", "default", "-f", str(vals), *extra))
            if rc != 0:
                remember(GRAFANA=f"GRAFANA_VERIFICATION_BLOCKED: helm template exit {rc}")
                return None
            (p / name).write_text(text, encoding="utf-8")
        drift, _ = ex.run(py("release_diff.py", "--before", str(live), "--after", str(p / "llm-current.yaml")))
        only, _ = ex.run(py("release_diff.py", "--before", str(p / "llm-current.yaml"), "--after", str(p / "llm-proposed.yaml"),
                            "--allow-grafana-root-url", url))
        if drift or only:
            remember(GRAFANA="GRAFANA_VERIFICATION_BLOCKED: release_diff not clean (no upgrade; owner review)")
            return None
        rc, _ = ex.run(helm(state, "upgrade", "llm-agent", chart, "-n", "default", "-f", str(vals), "--set", f"grafanaExternal.rootUrl={url}"))
        remember(GRAFANA="ROOT_URL_APPLIED" if rc == 0 else f"GRAFANA_VERIFICATION_FAILED: upgrade exit {rc}")
        return None

    def public_e2e(state, ex):
        root_url = ["--grafana-root-url", f"https://observe.{c['domain']}/"] if state.get("GRAFANA") == "ROOT_URL_APPLIED" else []
        error = step(lambda s: py("public_e2e.py", "--address", s["LB_IP"], "--agents", "allowed", "--evidence-class",
                                  "PUBLIC_INTERNET", "--out", str(ev / "14-e2e.jsonl"), *edge_args, *grafana_args, *root_url),
                     lambda s: py("negative_tests.py", "--address", s["LB_IP"], "--rate-limit-mode", "http2",
                                  "--out", str(ev / "14-negative.jsonl"), *edge_args),
                     py("platform_checks.py", *kargs(state), "compare", "--baseline", str(ev / "baseline.json")),
                     public_preflight)(state, ex)
        if state.get("GRAFANA") == "ROOT_URL_APPLIED":
            rows = [r for r in jsonl_rows(ev / "14-e2e.jsonl") if r.get("row") in ("37", "38", "39")]
            ok = bool(rows) and all(r["passed"] for r in rows) and any("appUrl" in r["check"] for r in rows)
            remember(GRAFANA="PASS" if ok else "GRAFANA_VERIFICATION_FAILED: rows 37-39")
        return error

    full = [
        ("wake", wake),
        ("baseline", step(py("platform_checks.py", *kargs(state), "backends", "--snapshot-out", str(ev / "baseline.json"),
                             "--out", str(ev / "02-backends.jsonl")), retries=25)),
        ("temporary-certificate", step(cert(state, "self-signed", "--import"))),
        ("edge-restricted", edge_restricted),
        ("L0-render", l0_render),
        ("L0-live", step(public_preflight)),
        ("edge-auth", edge_auth),
        ("private-e2e", private_e2e),
        ("open-lb", open_lb),
        ("dns-online", dns_online),
        ("internet-gates", internet_gates),
        ("L4-denied", l4_denied),
        ("staging-certificate", certificate("staging")),
        ("production-certificate", certificate("production", "--confirm-production", "--import")
         if state["approvals"].get("production_certificate") else (lambda s, e: "production certificate not approved")),
        ("responder-off", responder_off),
        ("grafana", grafana),
        ("public-e2e", public_e2e),
    ]
    if state.get("scope") == "dns-transition":
        names = [n for n, _ in full]
        return full[:names.index("open-lb") + 1] + [("dns-transition", dns_transition)]
    return full


SECURITY_PHASES = ("L0-render", "L0-live", "edge-auth", "private-e2e", "internet-gates", "production-certificate", "public-e2e")
DEPLOYMENT_PHASES = ("wake", "baseline", "temporary-certificate", "edge-restricted", "open-lb", "dns-online",
                     "staging-certificate", "production-certificate", "responder-off")


def verdicts(state: dict) -> dict:
    """Summary statuses from what was actually observed. G3B_SECURITY_COMPLETE needs every
    security gate, including L4 from a real non-approved address."""
    ph = state.get("phases", {})
    if state.get("scope") == "dns-transition":
        not_run = "NOT_RUN (dns-transition scope)"
        return {"SCOPE": "dns-transition", "DNS_TRANSITION": state.get("DNS_TRANSITION", "NOT_RUN"),
                "EDGE_AUTH": state.get("EDGE_AUTH", "NOT_RUN"), "PRIVATE_E2E": ph.get("private-e2e", "NOT_RUN"),
                "G3B_DEPLOYMENT_STATUS": not_run, "G3B_SECURITY_STATUS": not_run, "G3B_PUBLIC_E2E_STATUS": not_run,
                "G4_AUTOMATED_CHECKS_STATUS": not_run, "CLEANUP_STATUS": state.get("CLEANUP_STATUS"),
                "SLEEP_STATUS": state.get("SLEEP_STATUS")}
    status = lambda names: ("NOT_STARTED" if not any(n in ph for n in names) else
                            "FAIL" if any(ph.get(n, "").startswith("FAIL") for n in names) else
                            "PASS" if all(ph.get(n) == "PASS" for n in names) else "INCOMPLETE")
    l4 = state.get("L4", "NOT_RUN")
    security = status(SECURITY_PHASES)
    if security == "PASS":
        security = "G3B_SECURITY_COMPLETE" if l4.startswith("PASS") else "INCOMPLETE (L4: " + l4 + ")"
    elif l4.startswith("FAIL"):
        security = "FAIL"
    e2e = "NOT_STARTED" if "public-e2e" not in ph else ("PASS" if ph["public-e2e"] == "PASS" else "FAIL")
    grafana = state.get("GRAFANA", "NOT_RUN")
    if e2e == "NOT_STARTED":
        g4 = "BLOCKED" if any(v.startswith("FAIL") for v in ph.values()) else "NOT_STARTED"
    elif e2e == "PASS" and grafana == "PASS" and l4.startswith("PASS"):
        g4 = "COMPLETE"
    else:
        g4 = "PARTIAL"
    return {"SCOPE": "g3b", "EDGE_AUTH": state.get("EDGE_AUTH", "NOT_RUN"),
            "G3B_DEPLOYMENT_STATUS": status(DEPLOYMENT_PHASES), "G3B_SECURITY_STATUS": security,
            "G3B_PUBLIC_E2E_STATUS": e2e, "G4_AUTOMATED_CHECKS_STATUS": g4, "L4": l4, "L5": state.get("L5", "NOT_RUN"),
            "GRAFANA": grafana, "CLEANUP_STATUS": state.get("CLEANUP_STATUS"), "SLEEP_STATUS": state.get("SLEEP_STATUS")}


# --- cleanup ------------------------------------------------------------------------------------

def cleanup(state_path: Path, ex: Executor, reason: str) -> dict:
    """Idempotent: each finished step is skipped on resume. Never bypasses a guard."""
    state = load(state_path)
    c, steps = state["config"], state.setdefault("cleanup_steps", {})
    state.update(CLEANUP_STARTED=state.get("CLEANUP_STARTED") or iso(now()), cleanup_reason=state.get("cleanup_reason") or reason)
    save(state_path, state)
    sim = isinstance(ex, Simulator)
    ex.deadline = None  # cleanup is never cut short by the campaign deadline; each command keeps its own timeout

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

    # 1. ACME: let an in-flight certbot finish (bounded), then remove the temporary responder and
    #    its port-80 route. edge-tls (possibly the new production certificate) and the
    #    application routes are kept and checked.
    if state.get("ACME_STATUS") not in (None, "responder-disabled") and not steps.get("acme-responder-off", {}).get("ok"):
        pgid = state.get("acme_pgid")
        if pgid and pid_alive(pgid):
            waited = 0
            while pid_alive(pgid) and waited < (0 if sim else ACME_WAIT_SECONDS):
                time.sleep(5)
                waited += 5
            if pid_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        state["acme_pgid"] = None
        rc, _ = retry(edge_upgrade(state, c["values_final"]))
        gone, detail = acme_objects_gone(state, ex) if rc == 0 else (False, f"helm upgrade exit {rc}")
        done("acme-responder-off", gone, detail)
        if gone:
            state["ACME_STATUS"] = "responder-disabled"
            save(state_path, state)
    dns_safe = True
    if state.get("DNS_TARGET_STATE") in ("online", "unknown") and not steps.get("dns-offline", {}).get("ok"):
        if not c.get("dns_credentials"):
            dns_safe = done("dns-offline", False, "no DNS automation configured")
        else:
            cmd = py("dynu_dns.py", "--credentials", c["dns_credentials"], "--execute", "--attempts", "10", "--interval", "30",
                     "offline", "--mode", c["dns_offline_mode"], *offline_args(c),
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
    gates_pass = bool(state["gates"]) and all(v == "PASS" for v in state["gates"].values())
    if not gates_pass and not isinstance(ex, Simulator):
        raise SystemExit(f"pre-wake gates not all PASS: {state['gates']}; not waking")
    stop = {"signal": None}

    def on_signal(signum, _frame):
        stop["signal"] = signal.Signals(signum).name
        ex.terminate_current()  # the running command (and its children) stops now; cleanup follows
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if state.get("deadline_epoch"):
        ex.deadline = state["deadline_epoch"]
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
        if stop["signal"] and error is None:
            error = f"stopped by {stop['signal']}"
        result = "PASS" if error is None else f"FAIL: {error}"
        # Gates that can end blocked record what they observed, never a bare PASS.
        if error is None and name == "L4-denied" and not state.get("L4", "").startswith("PASS"):
            result = state.get("L4", "NOT_RUN")
        if error is None and name == "grafana" and state.get("GRAFANA") != "ROOT_URL_APPLIED":
            result = state.get("GRAFANA", "NOT_RUN")
        state.setdefault("phases", {})[name] = result
        if name == "baseline" and error is None:
            state["baseline_passed"] = True
        state["heartbeat"] = now()
        save(state_path, state)
        if error:
            reason = f"stopped by {stop['signal']}" if stop["signal"] else (
                "DEADLINE_EXPIRED" if error == "DEADLINE_EXPIRED" else f"FAIL at {name}: {error}")
            break
    if stop["signal"] and not reason.startswith("stopped"):
        reason = f"stopped by {stop['signal']}"
    state["G3B_STATUS"] = "PASS" if reason == "PASS" else reason
    state["CURRENT_PHASE"] = "cleanup"
    save(state_path, state)
    # Cleanup runs to its end: a second SIGTERM/SIGINT is recorded, not obeyed (kill -9 and a
    # later `cleanup` or the watchdog resume it).
    ignored = lambda signum, _frame: None
    signal.signal(signal.SIGTERM, ignored)
    signal.signal(signal.SIGINT, ignored)
    final = cleanup(state_path, ex, reason)
    final["verdicts"] = verdicts(final)
    save(state_path, final)
    return final


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
            # The main process holds the campaign. If it overran the deadline, ask it to stop:
            # it then cleans up itself, so only one process ever changes the cluster.
            pid = state.get("main_pid")
            if expired and pid_alive(pid) and now() > state["deadline_epoch"] + DEADLINE_GRACE_SECONDS \
                    and not state.get("watchdog_sigterm_sent"):
                os.kill(pid, signal.SIGTERM)
                state["watchdog_sigterm_sent"] = iso(now())
                save(state_path, state)
                return "SIGTERM_SENT_TO_MAIN"
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


def transition_evidence(path: str | None, cfg: dict, problems: list[str]) -> bool:
    """--dns-offline-verified counts only with the state.json of a dns-transition campaign that
    proved Offline -> Online (LB) -> signoff -> Offline for this domain, mode and offline addresses."""
    if not path:
        problems.append("--dns-offline-verified needs --dns-transition-evidence (state.json of a VERIFIED dns-transition campaign)")
        return False
    try:
        evidence = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        problems.append("DNS transition evidence is unreadable")
        return False
    ec = evidence.get("config", {})
    checks = {"scope": evidence.get("scope") == "dns-transition", "DNS_TRANSITION": evidence.get("DNS_TRANSITION") == "VERIFIED",
              "domain": ec.get("domain") == cfg["domain"], "dns_offline_mode": ec.get("dns_offline_mode") == cfg["dns_offline_mode"],
              "dns_offline_ip": ec.get("dns_offline_ip") == cfg["dns_offline_ip"],
              "dns_offline_ipv6": ec.get("dns_offline_ipv6") == cfg["dns_offline_ipv6"]}
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        problems.append(f"DNS transition evidence does not prove this configuration: {bad}")
    return not bad


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
           "dns_offline_mode": args.dns_offline_mode, "dns_offline_ip": args.dns_offline_ip,
           "dns_offline_ipv6": getattr(args, "dns_offline_ipv6", None)}
    for key, version in (("dns_offline_ip", 4), ("dns_offline_ipv6", 6)):
        if cfg[key]:
            try:
                parsed = ipaddress.ip_address(cfg[key])
                if parsed.version != version:
                    raise ValueError
                cfg[key] = str(parsed)
            except ValueError:
                problems.append(f"{key} is not an IPv{version} address")
    scope = getattr(args, "scope", "g3b")
    mode = getattr(args, "mode", "unattended")
    if scope == "dns-transition" and mode != "supervised":
        problems.append("the dns-transition scope runs supervised only (--mode supervised)")
    if cfg["dns_offline_mode"] == "signoff" and not (cfg["dns_offline_ip"] and cfg["dns_offline_ipv6"]):
        problems.append("signoff needs --dns-offline-ip and --dns-offline-ipv6 (Dynu answers its offline server on both families)")
    cidrs = [l.split("#")[0].strip() for l in Path(cfg["approved_cidrs"]).read_text().splitlines()] if cfg["approved_cidrs"] and Path(cfg["approved_cidrs"]).exists() else []
    cidrs = [x for x in cidrs if x]
    for name, acme in (("values_acme", True), ("values_final", False)):
        path = private / f"g3b-{name}.yaml"
        path.write_text(json.dumps({"domain": args.domain, "sourceRestriction": {"enabled": True, "routes": ["kagent-ui"],
                                                                                 "allowedCidrs": cidrs},
                                    "acmeHttp01": {"enabled": acme}}, indent=2) + "\n", encoding="utf-8")
        cfg[name] = str(path)
    approvals = {"live_campaign": getattr(args, "approve_live_campaign", False),
                 "edge_secret_sync": getattr(args, "approve_edge_secret_sync", False),
                 "dns_transition_test": getattr(args, "approve_dns_transition_test", False),
                 "guarded_sleep": args.approve_guarded_sleep, "keda_pause": args.approve_keda_pause,
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
    proven = scope == "g3b" and args.dns_offline_verified and transition_evidence(
        getattr(args, "dns_transition_evidence", None), cfg, problems)
    unattended = {"dns_automation": bool(cfg["dns_credentials"]) and bool(proven),
                  "watchdog_timer_active": rc_t == 0, "linger": linger.strip() == "yes" or isinstance(ex, Simulator),
                  "laptop_risk_covered": args.cloud_fallback_ready or args.accept_local_only_risk}
    if scope == "g3b":
        gates["SAFE_CLEANUP_READY"] = "PASS" if unattended["dns_automation"] and not problems else "FAIL"
        if mode == "unattended":
            gates["UNATTENDED_CLEANUP_READY"] = "PASS" if all(unattended.values()) else "FAIL"
        gates["OWNER_LIVE_APPROVAL"] = "PASS" if approvals["live_campaign"] else "FAIL"
    else:
        # read-only: the hostname must be proven offline on the approved addresses right now
        rc_d, _ = ex.run(py("dns_check.py", "--domain", args.domain, "--expect-offline", *offline_args(cfg)), timeout=300)
        ready = bool(cfg["dns_credentials"]) and cfg["dns_offline_mode"] == "signoff" and rc_d == 0
        gates["DNS_TRANSITION_TEST_READY"] = "PASS" if ready and not problems else "FAIL"
        gates["OWNER_DNS_TRANSITION_APPROVAL"] = "PASS" if approvals["dns_transition_test"] else "FAIL"
    # read-only: the Secret against edge-cred; a mismatch is ready only with the sync approval
    rc_a, out_a = ex.run(edge_auth_cmd(cfg, "--check"), timeout=120) if cfg["edge_credentials"] else (2, "")
    word = (out_a.split() or ["EDGE_AUTH_CHECK_FAILED"])[0].rstrip(":")
    edge_auth_init = word if word in ("EDGE_AUTH_MATCH", "EDGE_AUTH_MISMATCH", "EDGE_AUTH_MISSING") else "EDGE_AUTH_CHECK_FAILED"
    gates["EDGE_AUTH_READY"] = "PASS" if rc_a == 0 or (rc_a == 1 and approvals["edge_secret_sync"]) else "FAIL"
    # L0 render, offline: the exact public render must already pass the strict preflight.
    l0 = []
    for out, cmd in ((campaign / "l0-gateway.yaml", ["helm", "template", "ewallet-edge-gateway", cfg["gateway_chart"], "-n",
                                                     "ewallet-ingress", *[x for f in ("values-gateway.yaml", "values-gateway-public.yaml",
                                                                                      "values-gateway-public-open.yaml")
                                                                          for x in ("-f", str(ROOT / "infra/helm/ewallet-edge" / f))]]),
                     (campaign / "l0-edge.yaml", ["helm", "template", "ewallet-edge", str(ROOT / "infra/helm/ewallet-edge"), "-n",
                                                  "ewallet-ingress", "-f", str(PRIVATE_VALUES), "-f", cfg["values_acme"]])):
        rc_r, text = ex.run(cmd, timeout=120)
        out.write_text(text, encoding="utf-8")
        l0.append(rc_r)
    rc_p, _ = ex.run(py("ownership_preflight.py", "render", "--allow-public-loadbalancer", "--domain", args.domain,
                        "--approved-cidrs-file", cfg["approved_cidrs"] or "/nonexistent",
                        str(campaign / "l0-gateway.yaml"), str(campaign / "l0-edge.yaml")), timeout=120)
    gates["L0_RENDER_READY"] = "PASS" if l0 == [0, 0] and rc_p == 0 else "FAIL"
    rc_ip, ip = ex.run(["dig", "+short", "myip.opendns.com", "@resolver1.opendns.com"])
    try:
        mine = ipaddress.ip_address(ip.strip().splitlines()[-1]) if rc_ip == 0 and ip.strip() else None
        inside = mine is not None and any(mine in ipaddress.ip_network(x) for x in cidrs)
    except ValueError:
        inside = False
    gates["CLIENT_ADDRESS_APPROVED"] = "PASS" if inside else "FAIL"  # the address itself is not recorded
    if problems:
        gates = {g: ("FAIL" if g in ("CLOUD_LAB_RECOVERY_MANIFEST_VALID", "UNATTENDED_CLEANUP_READY", "SAFE_CLEANUP_READY",
                                     "DNS_TRANSITION_TEST_READY") else v)
                 for g, v in gates.items()}
    state = {"CAMPAIGN_ID": args.campaign, "CLUSTER": CLUSTER, "PROJECT": PROJECT, "mode": mode, "scope": scope,
             "config": cfg, "approvals": approvals, "edge_auth_at_init": edge_auth_init,
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
    # signoff (offline=yes) is the default: myip=no answered nochg and left the old A record (2026-11-22).
    i.add_argument("--dns-offline-mode", choices=["no-ipv4", "signoff"], default="signoff")
    i.add_argument("--dns-offline-ip", help="approved offline IPv4 (Dynu's offline server), checked by dns_check")
    i.add_argument("--dns-offline-ipv6", help="approved offline IPv6 (Dynu's offline server), checked by dns_check")
    i.add_argument("--scope", choices=["g3b", "dns-transition"], default="g3b",
                   help="dns-transition: the first supervised Dynu Offline->Online->Offline test on the LB, then cleanup")
    i.add_argument("--dns-transition-evidence", help="state.json of a dns-transition campaign with DNS_TRANSITION=VERIFIED")
    i.add_argument("--mode", choices=["supervised", "unattended"], default="unattended",
                   help="supervised: the owner watches the run (no watchdog/linger/laptop-risk gate)")
    for flag in ("approve-live-campaign", "approve-edge-secret-sync", "approve-dns-transition-test", "approve-guarded-sleep", "approve-keda-pause", "approve-production-certificate", "approve-grafana-root-url",
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
        state = load(state_path)
        print(json.dumps({**state, "verdicts": verdicts(state)}, indent=2, sort_keys=True))
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
    print(json.dumps({**state["verdicts"], "G3B_STATUS": state.get("G3B_STATUS"),
                      "LAST_VERIFIED_NODE_COUNT": state.get("LAST_VERIFIED_NODE_COUNT")}))
    goal = state.get("DNS_TRANSITION") == "VERIFIED" if state.get("scope") == "dns-transition" else \
        state["verdicts"]["G3B_SECURITY_STATUS"] == "G3B_SECURITY_COMPLETE"
    ok = goal and state["CLEANUP_STATUS"] == "COMPLETE" and state.get("SLEEP_STATUS") == "VERIFIED"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
