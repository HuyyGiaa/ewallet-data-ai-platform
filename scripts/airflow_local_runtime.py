"""Manage the local Airflow 3 runtime: API server, scheduler and DAG processor.

Entry point: ``scripts/start_airflow_local.sh``. Standard library only, so it
runs with any Python 3.10+ interpreter.

Design rules:

* ``status`` is read-only: it never creates files or directories.
* ``start`` reuses a healthy runtime, refuses conflicts, and starts only the
  components that are missing, one at a time, waiting for each to be healthy.
* Components are started as plain background processes in their own session,
  never with Airflow's ``--daemon`` flag. ``--daemon`` uses python-daemon's
  ``DaemonContext``, which closes every inherited file descriptor. A SQLite
  connection opened by the CLI before the fork then loses its descriptors (and
  its POSIX locks), and the next WAL operation fails with ``locking protocol``
  (``SQLITE_PROTOCOL``). That is what stopped the DAG processor.
* ``stop`` sends SIGTERM only to processes proven to belong to this runtime.
* It never initializes, migrates or resets the metadata database, and never
  triggers or unpauses a DAG.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

HOST = "127.0.0.1"
PORT = 8084
HEALTH_URL = f"http://{HOST}:{PORT}/api/v2/monitor/health"
COMPONENTS = ("api-server", "scheduler", "dag-processor")
HEALTH_KEYS = {"api-server": "metadatabase", "scheduler": "scheduler",
               "dag-processor": "dag_processor"}
LOCKING_PROTOCOL = "locking protocol"
DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class Proc:
    pid: int
    argv: tuple[str, ...]
    airflow_home: str | None
    ppid: int | None = None

    @property
    def cmdline(self) -> str:
        return " ".join(self.argv)


def _read(path: Path) -> bytes | None:
    """Read a /proc file; a process that exits mid-scan is simply skipped."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def list_processes(proc_root: Path = Path("/proc")) -> list[Proc]:
    procs: list[Proc] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return procs
    for entry in entries:
        if not entry.name.isdigit():
            continue
        raw_cmd = _read(entry / "cmdline")
        if not raw_cmd:
            continue
        argv = tuple(raw_cmd.decode(errors="replace").rstrip("\0").split("\0"))
        if not any("airflow" in arg for arg in argv[:3]):
            continue
        home = None
        raw_env = _read(entry / "environ")
        if raw_env:
            for item in raw_env.split(b"\0"):
                if item.startswith(b"AIRFLOW_HOME="):
                    home = item.split(b"=", 1)[1].decode(errors="replace")
                    break
        procs.append(Proc(int(entry.name), argv, home, _ppid(entry / "stat")))
    return procs


def _subcommand(argv: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """The Airflow sub-command and its arguments, matched on argv tokens only.

    Accepted forms: ``[python] .../airflow <sub> ...``, and the process title some
    components set, ``airflow <sub_with_underscore> -- key:value ...`` in argv[0].
    A shell or grep command that merely mentions these words does not match.
    """
    if argv and argv[0].startswith("airflow ") and " -- " in argv[0]:
        title = argv[0].split()
        return title[1].replace("_", "-"), tuple(title[2:])
    for index, arg in enumerate(argv[:2]):
        if os.path.basename(arg) == "airflow" and index + 1 < len(argv):
            return argv[index + 1].replace("_", "-"), tuple(argv[index + 2:])
    return None, ()


def _is_component(proc: Proc, component: str, airflow_home: str) -> bool:
    sub, rest = _subcommand(proc.argv)
    if sub != component:
        return False
    if component == "api-server":
        # The API server may drop AIRFLOW_HOME from its environment, so bind it by
        # its dedicated listen address (title "-- host:127.0.0.1 port:8084", or CLI flags).
        joined = " ".join(rest)
        return (f"host:{HOST}" in joined and f"port:{PORT}" in joined) or (
            HOST in rest and str(PORT) in rest)
    return proc.airflow_home == airflow_home


def component_pids(component: str, airflow_home: str,
                   procs: Iterable[Proc]) -> list[int]:
    """Main process ids of a component; forked children of the same component
    (same title, parent is a member) are folded into their parent."""
    members = [p for p in procs if _is_component(p, component, airflow_home)]
    pids = {p.pid for p in members}
    return sorted(p.pid for p in members if p.ppid not in pids)


def _ppid(stat_path: Path) -> int | None:
    raw = _read(stat_path)
    if not raw:
        return None
    try:
        return int(raw.rsplit(b")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def parse_health(body: str | None) -> dict[str, str | None]:
    """Map the health JSON to component statuses; unreadable bodies give None."""
    result: dict[str, str | None] = {k: None for k in HEALTH_KEYS.values()}
    if not body:
        return result
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return result
    if not isinstance(data, dict):
        return result
    for key in result:
        entry = data.get(key)
        if isinstance(entry, dict):
            status = entry.get("status")
            result[key] = status if isinstance(status, str) else None
    return result


def fetch_health(url: str = HEALTH_URL, timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local URL
            return response.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


@dataclass
class ComponentState:
    name: str
    pids: list[int]
    health: str | None

    @property
    def process(self) -> str:
        if not self.pids:
            return "STOPPED"
        return "RUNNING" if len(self.pids) == 1 else "CONFLICT"

    @property
    def healthy(self) -> bool:
        return self.process == "RUNNING" and self.health == "healthy"


@dataclass
class Runtime:
    """Dependencies are injected so tests run without real processes."""

    airflow_home: str
    airflow_bin: str
    runtime_dir: Path
    list_procs: Callable[[], list[Proc]] = list_processes
    health_body: Callable[[], str | None] = fetch_health
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    popen: Callable[..., object] = subprocess.Popen
    port_in_use: Callable[[], bool] = field(default=None)  # type: ignore[assignment]
    out: Callable[[str], None] = print
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr)

    def __post_init__(self) -> None:
        if self.port_in_use is None:
            self.port_in_use = _port_in_use

    # -- read-only inspection -------------------------------------------------
    def states(self) -> dict[str, ComponentState]:
        procs = self.list_procs()
        health = parse_health(self.health_body())
        return {c: ComponentState(c, component_pids(c, self.airflow_home, procs),
                                  health[HEALTH_KEYS[c]]) for c in COMPONENTS}

    def report(self, states: dict[str, ComponentState]) -> bool:
        ok = True
        for name in COMPONENTS:
            state = states[name]
            pids = ",".join(str(p) for p in state.pids) or "-"
            self.out(f"{name:<14} {state.process:<9} health={state.health or 'unknown':<10} pids={pids}")
            ok = ok and state.healthy
        self.out(f"{'overall':<14} {'HEALTHY' if ok else 'NOT_READY'} {HEALTH_URL}")
        return ok

    def status(self) -> int:
        return 0 if self.report(self.states()) else 1

    # -- diagnostics ------------------------------------------------------------
    def diagnose(self, component: str, lines: int = 25) -> None:
        for suffix in ("err", "out"):
            path = self.runtime_dir / f"{component}.{suffix}"
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            tail = [line for line in text.splitlines() if line.strip()][-lines:]
            if not tail:
                continue
            self.err(f"--- last lines of {path}")
            for line in tail:
                self.err(f"    {line[:300]}")
            if LOCKING_PROTOCOL in text:
                self.err(
                    f"DIAGNOSIS: {component} hit SQLite 'locking protocol' (SQLITE_PROTOCOL). "
                    "The usual cause is a component started with Airflow's --daemon flag, "
                    "which closes the SQLite file descriptors it inherited. This helper never "
                    "uses --daemon; stop any component started that way (--stop), then retry. "
                    "See docs/runbooks/edai1/airflow/AIRFLOW_LOCAL_RUNTIME.md.")

    # -- start ----------------------------------------------------------------------
    def _launch(self, component: str) -> int:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        args = [self.airflow_bin, component]
        if component == "api-server":
            args += ["--host", HOST, "--port", str(PORT)]
        for suffix in ("out", "err"):
            # Keep one previous log; a new start must not be diagnosed from an old traceback.
            log = self.runtime_dir / f"{component}.{suffix}"
            if log.exists():
                log.replace(log.with_name(log.name + ".prev"))
        stdout = open(self.runtime_dir / f"{component}.out", "wb")  # noqa: SIM115 - handed to child
        stderr = open(self.runtime_dir / f"{component}.err", "wb")  # noqa: SIM115
        try:
            process = self.popen(args, stdin=subprocess.DEVNULL, stdout=stdout,
                                 stderr=stderr, start_new_session=True, close_fds=True)
        finally:
            stdout.close()
            stderr.close()
        pid = int(getattr(process, "pid"))
        (self.runtime_dir / f"{component}.pid").write_text(f"{pid}\n")
        self.out(f"started {component} pid={pid}")
        return pid

    def _wait_healthy(self, component: str, pid: int, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            state = self.states()[component]
            if pid not in state.pids and not _alive(pid, self.list_procs()):
                self.err(f"ERROR: {component} (pid {pid}) exited during startup.")
                return False
            if state.healthy:
                return True
            self.sleep(2.0)
        self.err(f"ERROR: {component} did not become healthy within {timeout:.0f} s.")
        return False

    def start(self, timeout: float = DEFAULT_TIMEOUT) -> int:
        states = self.states()
        if all(s.healthy for s in states.values()):
            self.report(states)
            self.out("Airflow local runtime is already healthy; reusing it.")
            return 0
        conflicts = [s.name for s in states.values() if s.process == "CONFLICT"]
        if conflicts:
            self.report(states)
            self.err(f"ERROR: duplicate processes for {', '.join(conflicts)}; refusing to start more. "
                     "Inspect them with --status and stop the extra ones. No process was killed.")
            return 1
        if states["api-server"].process == "STOPPED" and self.port_in_use():
            self.err(f"ERROR: port {PORT} is in use by a process that is not this runtime's API server.")
            return 1
        check = self.run([self.airflow_bin, "db", "check"], capture_output=True, text=True)
        if check.returncode != 0:
            self.err("ERROR: 'airflow db check' failed; the metadata database is unavailable. "
                     "Nothing was started, and the database was not modified.")
            for line in (check.stderr or check.stdout or "").splitlines()[-15:]:
                self.err(f"    {line[:300]}")
            return 1
        for name in COMPONENTS:
            state = self.states()[name]
            if state.healthy:
                continue
            if state.process == "RUNNING":
                # Running but not yet healthy: wait for it instead of starting a duplicate.
                pid = state.pids[0]
                self.out(f"{name} is running (pid {pid}); waiting for it to become healthy")
            else:
                pid = self._launch(name)
            if not self._wait_healthy(name, pid, timeout):
                self.diagnose(name)
                self.report(self.states())
                self.err(f"Startup stopped at {name}. Components started earlier were left running; "
                         f"logs are in {self.runtime_dir}.")
                return 1
        ok = self.report(self.states())
        if ok:
            self.out(f"Airflow UI/API: http://{HOST}:{PORT}")
        return 0 if ok else 1

    # -- stop -----------------------------------------------------------------------
    def stop(self, timeout: float = 60.0) -> int:
        states = self.states()
        targets = [(n, p) for n in reversed(COMPONENTS) for p in states[n].pids]
        if not targets:
            self.out("No local Airflow runtime process is running.")
            return 0
        for name, pid in targets:
            self.out(f"stopping {name} pid={pid} (SIGTERM)")
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            procs = self.list_procs()
            if not any(_alive(pid, procs) for _, pid in targets):
                self.out("Stopped.")
                return 0
            self.sleep(2.0)
        left = [pid for _, pid in targets if _alive(pid, self.list_procs())]
        self.err(f"ERROR: still running after {timeout:.0f} s: {left}. They were not force-killed.")
        return 1


def _alive(pid: int, procs: Iterable[Proc]) -> bool:
    return any(p.pid == pid for p in procs)


def _port_in_use(host: str = HOST, port: int = PORT) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--airflow-home", required=True)
    parser.add_argument("--airflow-bin", required=True)
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("AIRFLOW_LOCAL_START_TIMEOUT", DEFAULT_TIMEOUT)))
    args = parser.parse_args(argv)
    runtime = Runtime(airflow_home=args.airflow_home, airflow_bin=args.airflow_bin,
                      runtime_dir=Path(args.airflow_home) / "logs" / "local-runtime")
    if args.action == "status":
        return runtime.status()
    if args.action == "stop":
        return runtime.stop()
    return runtime.start(timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
