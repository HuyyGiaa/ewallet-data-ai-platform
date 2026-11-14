"""Port-forward lifecycle of scripts/llm_agent/rag_runtime_run.sh (RAG_TARGET=gke), without a cluster.

A fake ``kubectl`` on PATH listens on the forward port the way ``kubectl port-forward`` does, so the
runner's own functions are exercised: one forward per invocation (trigger -> preflight -> collect),
reuse without taking ownership, refusal of a foreign listener, reopening after a crash, and cleanup on
exit, on SIGTERM and by ``stop``.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "llm_agent" / "rag_runtime_run.sh"
CONTEXT = "gke_test_context"

FAKE_KUBECTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Test double: `port-forward` listens on the local port and keeps the kubectl arguments in its cmdline.
    for a; do [[ "$a" == *:6379 ]] && port=${a%%:*}; done
    if [[ " $* " == *" get secret redis "* ]]; then printf %s "$FAKE_REDIS_PASSWORD" | base64; exit 0; fi
    if [[ " $* " == *" port-forward "* ]]; then
      exec python3 -c 'import socket, sys, time
    s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(600)' "$port" "$@"
    fi
    exit 1
""")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


@unittest.skipUnless(shutil.which("ss") and shutil.which("python3"), "needs ss and python3")
class ForwardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-forward-"))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "kubectl").write_text(FAKE_KUBECTL)
        (bin_dir / "kubectl").chmod(0o755)
        self.port = free_port()
        self.home = self.tmp / "airflow"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RAG_TARGET": "gke",
                    "RAG_KUBE_CONTEXT": CONTEXT, "RAG_REDIS_LOCAL_PORT": str(self.port),
                    "RAG_AIRFLOW_HOME": str(self.home), "EWALLET_RAG_ENV_FILE": str(self.tmp / "none.env")}
        self.pid_file = Path(f"{self.home}.forward.pid")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        subprocess.run(["pkill", "-f", f"svc/redis {self.port}:6379"], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bash(self, script: str, *, check: bool = True) -> subprocess.CompletedProcess:
        body = f"source {RUNNER}\ntrap close_forward EXIT\n{script}"
        result = subprocess.run(["bash", "-c", body], env=self.env, capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_one_forward_per_invocation_and_closed_on_exit(self):
        # trigger -> preflight -> trigger wait loop -> collect: every step asks for the forward.
        out = self.bash("""
            start_forward; first=$FORWARD_PID
            start_forward; start_forward
            [[ $FORWARD_PID == "$first" && $FORWARD_OWNED == 1 ]] || exit 9
            [[ $(cat "$FORWARD_PID_FILE") == "$first" ]] || exit 8
            echo "listeners=$(port_listeners "$GKE_FORWARD_PORT" | wc -l) pid=$first"
        """).stdout
        self.assertIn("listeners=1 ", out)
        pid = int(out.split("pid=")[1])
        time.sleep(0.3)
        self.assertFalse(listening(self.port))
        self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertFalse(self.pid_file.exists())

    def test_second_invocation_reuses_without_taking_ownership_and_stop_closes_it(self):
        holder = subprocess.Popen(["bash", str(RUNNER), "forward"], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        for _ in range(50):
            if self.pid_file.exists() and listening(self.port):
                break
            time.sleep(0.1)
        owner = self.pid_file.read_text().strip()
        out = self.bash('start_forward; echo "pid=$FORWARD_PID owned=$FORWARD_OWNED"').stdout
        self.assertIn(f"pid={owner} owned=0", out)
        time.sleep(0.3)
        self.assertTrue(listening(self.port), "a reusing invocation must not close the owner's forward")
        self.assertEqual(self.pid_file.read_text().strip(), owner)
        # stop (e.g. after the owner was killed with SIGKILL) closes a recorded runner forward.
        self.bash("stop_recorded_forward")
        holder.wait(timeout=10)
        time.sleep(0.3)
        self.assertFalse(listening(self.port))

    def test_sigterm_closes_the_owned_forward(self):
        holder = subprocess.Popen(["bash", str(RUNNER), "forward"], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            if listening(self.port):
                break
            time.sleep(0.1)
        child = int(self.pid_file.read_text())
        holder.send_signal(signal.SIGTERM)
        self.assertEqual(holder.wait(timeout=10), 143)
        time.sleep(0.3)
        self.assertFalse(Path(f"/proc/{child}").exists(), "port-forward orphaned after SIGTERM")
        self.assertFalse(listening(self.port))

    def test_foreign_listener_is_refused_and_left_alone(self):
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", self.port))
            foreign.listen()
            result = self.bash("start_forward", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("held by another process", result.stderr)
            self.assertFalse(self.pid_file.exists())

    def test_stale_pid_file_of_an_unrelated_process_is_neither_reused_nor_killed(self):
        unrelated = subprocess.Popen(["sleep", "30"])
        self.addCleanup(unrelated.kill)
        self.pid_file.write_text(f"{unrelated.pid}\n")
        out = self.bash('start_forward; echo "pid=$FORWARD_PID owned=$FORWARD_OWNED"; stop_recorded_forward').stdout
        self.assertIn("owned=1", out)
        self.assertNotIn(f"pid={unrelated.pid} ", out)
        self.assertIsNone(unrelated.poll(), "an unrelated process was killed")

    def test_a_forward_that_never_opens_leaves_no_process_or_pid_file(self):
        # e.g. a sleeping cluster: kubectl exits at once.
        (Path(self.env["PATH"].split(":")[0]) / "kubectl").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.bash("start_forward", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not open", result.stderr)
        self.assertFalse(self.pid_file.exists())

    def test_a_dropped_forward_is_reopened(self):
        out = self.bash("""
            start_forward; first=$FORWARD_PID
            kill "$first"; wait "$first" 2>/dev/null || true
            start_forward
            [[ $FORWARD_PID != "$first" && $FORWARD_OWNED == 1 ]] || exit 9
            echo "listeners=$(port_listeners "$GKE_FORWARD_PORT" | wc -l)"
        """).stdout
        self.assertIn("listeners=1", out)

    def test_another_context_on_the_port_is_not_this_runners_forward(self):
        holder = subprocess.Popen(["bash", str(RUNNER), "forward"], env={**self.env, "RAG_KUBE_CONTEXT": "other"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        for _ in range(50):
            if listening(self.port):
                break
            time.sleep(0.1)
        result = self.bash("start_forward", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("held by another process", result.stderr)



@unittest.skipUnless(shutil.which("base64"), "needs base64")
class ConfigureTests(unittest.TestCase):
    """`configure DB PROJECT` writes the gke settings from the Secret without printing the password."""

    PASSWORD = "Sup3r-secret_value"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-configure-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "kubectl").write_text(FAKE_KUBECTL)
        (bin_dir / "kubectl").chmod(0o755)
        self.settings = self.tmp / "cfg" / "runtime.env"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RAG_TARGET": "gke",
                    "RAG_KUBE_CONTEXT": CONTEXT, "RAG_AIRFLOW_HOME": str(self.tmp / "airflow"),
                    "EWALLET_RAG_ENV_FILE": str(self.settings), "RAG_PY": sys.executable,
                    "FAKE_REDIS_PASSWORD": self.PASSWORD}

    def configure(self, *args, **env):
        return subprocess.run(["bash", str(RUNNER), "configure", *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def test_writes_a_private_verification_target_keyed_by_the_project(self):
        result = self.configure("5", "ewallet_rag_hybrid_20261115")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.PASSWORD, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        values = dict(line.split("=", 1) for line in self.settings.read_text().splitlines()
                      if line and not line.startswith("#"))
        self.assertEqual(values["EWALLET_RAG_ONLINE_TARGET"], "verification")
        self.assertEqual(values["EWALLET_REDIS_CONNECTION_STRING"], f"127.0.0.1:16380,password={self.PASSWORD},db=5")
        self.assertEqual(values["EWALLET_RAG_FEAST_REGISTRY"], "gs://ewallet-data-ai-platform-hg26-data/features/"
                         "registries/rag/ewallet_rag_hybrid_20261115/registry.db")
        self.assertEqual(values["EWALLET_RAG_PUBLICATION_BASE"],
                         "gs://ewallet-data-ai-platform-hg26-data/features/rag/ewallet_rag_hybrid_20261115")
        self.assertEqual(values["DATAHUB_GMS_URL"], "http://127.0.0.1:18080")
        # An existing file is kept unless --replace, which keeps it as a private backup.
        again = self.configure("6", "ewallet_rag_hybrid_20261115")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("--replace", again.stderr)
        replaced = self.configure("6", "ewallet_rag_hybrid_20261115", "--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        backups = list(self.settings.parent.glob("runtime.env.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertIn(",db=6", self.settings.read_text())

    def test_production_canonical_and_shared_targets_are_refused_before_the_secret_is_read(self):
        for db, project in (("0", "ewallet_rag_x"), ("1", "ewallet_rag_x"), ("16", "ewallet_rag_x"),
                            ("4", "ewallet_rag"), ("4", "fraud")):
            with self.subTest(db=db, project=project):
                result = self.configure(db, project)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not a valid verification target", result.stderr)
                self.assertFalse(self.settings.exists())

    def test_a_password_the_feast_connection_string_cannot_carry_is_refused(self):
        result = self.configure("5", "ewallet_rag_hybrid", FAKE_REDIS_PASSWORD="a,b")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains ','", result.stderr)
        self.assertFalse(self.settings.exists())
        self.assertNotIn("a,b", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
