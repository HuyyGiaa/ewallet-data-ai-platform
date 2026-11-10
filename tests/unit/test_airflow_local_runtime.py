"""Regression tests for the local Airflow runtime manager (scripts/airflow_local_runtime.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "airflow_local_runtime", REPOSITORY_ROOT / "scripts" / "airflow_local_runtime.py")
rt = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = rt
_SPEC.loader.exec_module(rt)

HOME = "/repo/data_platform/orchestration/airflow"
OTHER_HOME = "/elsewhere/airflow"
BIN = "/env/bin/airflow"


def api(pid: int = 100, port: int = 8084) -> rt.Proc:
    return rt.Proc(pid, (f"airflow api_server -- host:127.0.0.1 port:{port}",), None, 1)


def scheduler(pid: int = 200, home: str = HOME) -> rt.Proc:
    return rt.Proc(pid, ("/env/bin/python3.10", BIN, "scheduler"), home, 1)


def dag_processor(pid: int = 300, home: str = HOME, ppid: int = 1) -> rt.Proc:
    return rt.Proc(pid, ("/env/bin/python3.10", BIN, "dag-processor"), home, ppid)


def health(meta: str = "healthy", sched: str | None = "healthy",
           dagp: str | None = "healthy") -> str:
    return json.dumps({
        "metadatabase": {"status": meta},
        "scheduler": {"status": sched, "latest_scheduler_heartbeat": "x"},
        "triggerer": {"status": None},
        "dag_processor": {"status": dagp, "latest_dag_processor_heartbeat": "x"},
    })


class FakeWorld:
    """Simulates processes, the health endpoint, time and process launches."""

    def __init__(self, procs: list[rt.Proc], body: str | None,
                 db_check_rc: int = 0) -> None:
        self.procs = list(procs)
        self.body = body
        self.now = 0.0
        self.launched: list[list[str]] = []
        self.commands: list[list[str]] = []
        self.db_check_rc = db_check_rc
        self.on_launch: dict[str, object] = {}
        self.next_pid = 1000
        self.output: list[str] = []
        self.errors: list[str] = []
        self.port_busy = False

    # injected callables
    def list_procs(self) -> list[rt.Proc]:
        return list(self.procs)

    def health_body(self) -> str | None:
        return self.body

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def run(self, args, **_kwargs):
        self.commands.append(list(args))
        return subprocess.CompletedProcess(args, self.db_check_rc, "",
                                           "" if self.db_check_rc == 0 else "OperationalError: unable to open database file")

    def popen(self, args, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        self.launched.append(list(args))
        component = args[1]
        self.next_pid += 1
        pid = self.next_pid
        effect = self.on_launch.get(component, "healthy")
        if effect == "healthy":
            maker = {"api-server": lambda: api(pid), "scheduler": lambda: scheduler(pid),
                     "dag-processor": lambda: dag_processor(pid)}[component]
            self.procs.append(maker())
            self._set_health(component, "healthy")
        elif effect == "never_healthy":
            maker = {"api-server": lambda: api(pid), "scheduler": lambda: scheduler(pid),
                     "dag-processor": lambda: dag_processor(pid)}[component]
            self.procs.append(maker())
        # "crash": the process never appears
        return type("P", (), {"pid": pid})()

    def _set_health(self, component: str, value: str) -> None:
        data = json.loads(self.body) if self.body else json.loads(health(sched=None, dagp=None))
        key = rt.HEALTH_KEYS[component]
        data[key]["status"] = value
        self.body = json.dumps(data)


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self._tmp.name) / "logs" / "local-runtime"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, world: FakeWorld) -> rt.Runtime:
        return rt.Runtime(
            airflow_home=HOME, airflow_bin=BIN, runtime_dir=self.runtime_dir,
            list_procs=world.list_procs, health_body=world.health_body,
            sleep=world.sleep, clock=world.clock, run=world.run, popen=world.popen,
            port_in_use=lambda: world.port_busy,
            out=world.output.append, err=world.errors.append)

    # -- health parsing ----------------------------------------------------------
    def test_health_is_parsed_as_json(self) -> None:
        self.assertEqual({"metadatabase": "healthy", "scheduler": "healthy",
                          "dag_processor": "unhealthy"},
                         rt.parse_health(health(dagp="unhealthy")))
        compact_but_unhealthy = '{"metadatabase":{"status":"healthy"},"dag_processor":{"status":"unhealthy"}}'
        self.assertEqual("unhealthy", rt.parse_health(compact_but_unhealthy)["dag_processor"])
        self.assertIsNone(rt.parse_health(compact_but_unhealthy)["scheduler"])
        for bad in (None, "", "<html>", "[]", '{"scheduler": "healthy"}'):
            self.assertIsNone(rt.parse_health(bad)["scheduler"], bad)

    # -- status ------------------------------------------------------------------
    def test_healthy_runtime_detection(self) -> None:
        world = FakeWorld([api(), scheduler(), dag_processor()], health())
        self.assertEqual(0, self.make(world).status())
        self.assertIn("HEALTHY", world.output[-1])

    def test_missing_dag_processor_is_reported(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        self.assertEqual(1, self.make(world).status())
        line = next(l for l in world.output if l.startswith("dag-processor"))
        self.assertIn("STOPPED", line)
        self.assertIn("unhealthy", line)

    def test_status_is_read_only(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        self.make(world).status()
        self.assertEqual([], world.launched)
        self.assertEqual([], world.commands)
        self.assertFalse(self.runtime_dir.exists())

    def test_other_runtimes_and_children_are_not_counted(self) -> None:
        procs = [api(), api(pid=101, port=8080), scheduler(), scheduler(pid=201, home=OTHER_HOME),
                 rt.Proc(202, ("airflow worker -- LocalExecutor: <idle>",), HOME, 200),
                 rt.Proc(203, ("airflow serve-logs",), HOME, 200),
                 rt.Proc(204, ("/bin/bash", "-c", "pgrep -f 'airflow api_server -- host:127.0.0.1 port:8084'; "
                               "grep 'airflow scheduler' x; ./scripts/start_airflow_local.sh --status"), HOME, 1),
                 rt.Proc(205, ("grep", "airflow dag-processor", "log"), HOME, 1),
                 rt.Proc(206, ("/env/bin/airflow", "api-server", "--host", "127.0.0.1", "--port", "8084"), None, 99),
                 dag_processor(), dag_processor(pid=301, ppid=300)]
        world = FakeWorld(procs, health())
        states = self.make(world).states()
        self.assertEqual([100, 206], states["api-server"].pids)  # title form and CLI form
        self.assertEqual([200], states["scheduler"].pids)
        self.assertEqual([300], states["dag-processor"].pids)

    def test_process_discovery_tolerates_exiting_processes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            live = proc / "300"
            live.mkdir()
            (live / "cmdline").write_bytes(b"/env/bin/python3.10\0" + BIN.encode() + b"\0dag-processor\0")
            (live / "environ").write_bytes(f"PATH=/bin\0AIRFLOW_HOME={HOME}\0".encode())
            (live / "stat").write_bytes(b"300 (airflow dag-proc) S 1 300 300 0")
            vanished = proc / "301"
            vanished.mkdir()  # directory without files: exited between listing and reading
            (proc / "self").mkdir()
            unreadable = proc / "302"
            unreadable.mkdir()
            (unreadable / "cmdline").write_bytes(BIN.encode() + b"\0scheduler\0")
            procs = rt.list_processes(proc)
        self.assertEqual([300], [p.pid for p in procs if "dag-processor" in p.cmdline])
        self.assertEqual(HOME, next(p for p in procs if p.pid == 300).airflow_home)
        self.assertIsNone(next(p for p in procs if p.pid == 302).airflow_home)

    # -- start ---------------------------------------------------------------------
    def test_existing_healthy_runtime_is_reused(self) -> None:
        world = FakeWorld([api(), scheduler(), dag_processor()], health())
        self.assertEqual(0, self.make(world).start())
        self.assertEqual([], world.launched)
        self.assertIn("reusing", world.output[-1])

    def test_clean_start_runs_components_one_at_a_time(self) -> None:
        world = FakeWorld([], None)
        order: list[str] = []
        original = world.popen

        def popen(args, **kwargs):
            # Each component must only start after the previous one is healthy.
            current = rt.parse_health(world.body)
            if args[1] == "scheduler":
                self.assertEqual("healthy", current["metadatabase"])
            if args[1] == "dag-processor":
                self.assertEqual("healthy", current["scheduler"])
            order.append(args[1])
            return original(args, **kwargs)

        world.popen = popen
        self.assertEqual(0, self.make(world).start())
        self.assertEqual(["api-server", "scheduler", "dag-processor"], order)
        self.assertFalse(any("--daemon" in arg for args in world.launched for arg in args))
        self.assertEqual([[BIN, "db", "check"]], world.commands)
        self.assertIn(["--host", "127.0.0.1", "--port", "8084"], [world.launched[0][2:]])
        for name in rt.COMPONENTS:
            self.assertTrue((self.runtime_dir / f"{name}.pid").exists())

    def test_partial_startup_starts_only_the_missing_component(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        self.assertEqual(0, self.make(world).start())
        self.assertEqual([[BIN, "dag-processor"]], world.launched)

    def test_running_but_not_yet_healthy_component_is_not_duplicated(self) -> None:
        world = FakeWorld([api(), scheduler(), dag_processor()], health(dagp="unhealthy"))
        world.sleep = lambda s: (setattr(world, "now", world.now + s),
                                 world._set_health("dag-processor", "healthy"))
        self.assertEqual(0, self.make(world).start())
        self.assertEqual([], world.launched)

    def test_duplicate_processes_are_refused(self) -> None:
        world = FakeWorld([api(), scheduler(), scheduler(pid=250), dag_processor()], health())
        self.assertEqual(1, self.make(world).start())
        self.assertEqual([], world.launched)
        self.assertTrue(any("duplicate" in e for e in world.errors))

    def test_foreign_process_on_port_is_refused(self) -> None:
        world = FakeWorld([], None)
        world.port_busy = True
        self.assertEqual(1, self.make(world).start())
        self.assertEqual([], world.launched)

    def test_unavailable_metadata_database_stops_before_launch(self) -> None:
        world = FakeWorld([], None, db_check_rc=1)
        self.assertEqual(1, self.make(world).start())
        self.assertEqual([], world.launched)
        self.assertTrue(any("unable to open database file" in e for e in world.errors))
        self.assertFalse(any("reset" in " ".join(c) or "migrate" in " ".join(c) for c in world.commands))

    def test_crashed_dag_processor_reports_sqlite_locking_diagnosis(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        world.on_launch["dag-processor"] = "crash"
        original = world.popen

        def crashing_popen(args, **kwargs):
            process = original(args, **kwargs)
            kwargs["stderr"].write(
                b"Traceback (most recent call last):\n"
                b'    cursor.execute("PRAGMA journal_mode=WAL")\n'
                b"sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) locking protocol\n")
            kwargs["stderr"].flush()
            return process

        world.popen = crashing_popen
        self.assertEqual(1, self.make(world).start())
        errors = "\n".join(world.errors)
        self.assertIn("exited during startup", errors)
        self.assertIn("locking protocol", errors)
        self.assertIn("DIAGNOSIS", errors)
        self.assertEqual(1, len(world.launched))

    def test_previous_logs_are_rotated_on_launch(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        self.runtime_dir.mkdir(parents=True)
        old = self.runtime_dir / "dag-processor.err"
        old.write_text("sqlite3.OperationalError: locking protocol\n")
        self.assertEqual(0, self.make(world).start())
        self.assertEqual("", old.read_text())
        self.assertIn("locking protocol", (self.runtime_dir / "dag-processor.err.prev").read_text())

    def test_startup_timeout_is_bounded_and_reported(self) -> None:
        world = FakeWorld([api(), scheduler()], health(dagp="unhealthy"))
        world.on_launch["dag-processor"] = "never_healthy"
        self.assertEqual(1, self.make(world).start(timeout=30))
        self.assertLessEqual(world.now, 32)
        self.assertTrue(any("within 30 s" in e for e in world.errors))
        self.assertEqual(1, len(world.launched))

    # -- stop and wrapper -----------------------------------------------------------
    def test_stop_with_nothing_running_is_a_no_op(self) -> None:
        world = FakeWorld([scheduler(home=OTHER_HOME)], None)
        self.assertEqual(0, self.make(world).stop())

    def test_shell_wrapper_is_valid_and_safe(self) -> None:
        script = REPOSITORY_ROOT / "scripts" / "start_airflow_local.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        text = script.read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8084/execution/", text)
        self.assertIn('export PYTHONPATH="${REPOSITORY_ROOT}', text)
        source = (REPOSITORY_ROOT / "scripts" / "airflow_local_runtime.py").read_text(encoding="utf-8")
        for forbidden in ('"reset"', "db reset", "dags trigger", "dags unpause", "SIGKILL", '"--daemon"'):
            self.assertNotIn(forbidden, source + text)
        self.assertFalse([line for line in text.splitlines()
                          if "--daemon" in line and not line.lstrip().startswith("#")])


if __name__ == "__main__":
    unittest.main()
