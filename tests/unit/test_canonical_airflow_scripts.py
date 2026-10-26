from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
BENCHMARK = "ab-20261025123456"
ATTEMPT = "r261025123456"


class CanonicalAirflowScriptsTest(unittest.TestCase):
    def _fake_airflow(self, root: Path) -> tuple[Path, Path]:
        log = root / "airflow-calls.log"; binary = root / "airflow"
        binary.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_AIRFLOW_LOG\"\n"
                          "if [[ ${1:-} == version ]]; then printf '3.3.1\\n'; fi\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary, log

    def _run_dry(self, script: str, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); airflow, log = self._fake_airflow(root)
            env = dict(os.environ)
            env.update(AIRFLOW_BIN=str(airflow), FAKE_AIRFLOW_LOG=str(log),
                       EWALLET_DATA_BUCKET="example-canonical-data")
            result = subprocess.run([str(SCRIPTS_ROOT / script), *args], cwd=REPOSITORY_ROOT,
                                    env=env, capture_output=True, text=True, check=False)
            return result, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_dp1_dry_run_prints_full_baseline_profile(self) -> None:
        result, calls = self._run_dry("run_dp1_canonical.sh", "--profile", "baseline",
                                      "--benchmark-id", BENCHMARK)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MODE: DRY-RUN", result.stdout)
        self.assertIn("SPARK_PROFILE: BASELINE", result.stdout)
        self.assertIn("AQE: false", result.stdout)
        self.assertIn("SHUFFLE_PARTITIONS: 200", result.stdout)
        self.assertIn("INITIAL_EXECUTORS: 6", result.stdout)
        self.assertIn("MAX_EXECUTORS: 7", result.stdout)
        self.assertIn('"status": "DRY_RUN"', result.stdout)
        self.assertIn('"vcpus": 28', result.stdout)
        self.assertIn('"disk_gb": 2800', result.stdout)
        self.assertIn('"handoff": "external-to-bronze"', result.stdout)
        self.assertIn('"spark_profile":"baseline"', result.stdout)
        self.assertEqual("version\n", calls)

    def test_dp2_and_dp3_dry_runs_bind_same_profile(self) -> None:
        bronze = f"{BENCHMARK}-optimized-bronze-{ATTEMPT}"
        silver = f"{BENCHMARK}-optimized-silver-{ATTEMPT}"
        gold = f"{BENCHMARK}-optimized-gold-{ATTEMPT}"
        dp2, calls2 = self._run_dry("run_dp2_canonical.sh", bronze, "--profile", "optimized")
        dp3, calls3 = self._run_dry("run_dp3_canonical.sh", silver, gold, "--profile", "optimized")
        for result in (dp2, dp3):
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("SPARK_PROFILE: OPTIMIZED", result.stdout)
            self.assertIn('"benchmark_id":"ab-20261025123456"', result.stdout)
        self.assertIn("SPARK_STAGE: dp2-silver", dp2.stdout)
        self.assertIn("SHUFFLE_PARTITIONS: 304", dp2.stdout)
        self.assertIn("SPARK_STAGE: dp2-gold", dp2.stdout)
        self.assertIn("SHUFFLE_PARTITIONS: 400", dp2.stdout)
        self.assertIn("SPARK_STAGE: dp3-features", dp3.stdout)
        self.assertIn("SHUFFLE_PARTITIONS: 200", dp3.stdout)
        self.assertNotIn("SKEW_JOIN: true", dp2.stdout + dp3.stdout)
        self.assertEqual("version\n", calls2); self.assertEqual("version\n", calls3)

    def test_dp2_and_dp3_dry_runs_render_each_quota_handoff_without_cloud_submit(self) -> None:
        bronze = f"{BENCHMARK}-optimized-bronze-{ATTEMPT}"
        silver = f"{BENCHMARK}-optimized-silver-{ATTEMPT}"
        gold = f"{BENCHMARK}-optimized-gold-{ATTEMPT}"
        dp2, calls2 = self._run_dry("run_dp2_canonical.sh", bronze, "--profile", "optimized")
        dp3, calls3 = self._run_dry("run_dp3_canonical.sh", silver, gold, "--profile", "optimized")
        self.assertIn('"handoff": "bronze-to-silver"', dp2.stdout)
        self.assertIn('"handoff": "silver-to-gold"', dp2.stdout)
        self.assertIn(f'"predecessor_batch_id": "{bronze}"', dp2.stdout)
        self.assertIn(f'"predecessor_batch_id": "{BENCHMARK}-optimized-silver-r', dp2.stdout)
        self.assertIn('"handoff": "gold-to-features"', dp3.stdout)
        self.assertIn(f'"predecessor_batch_id": "{gold}"', dp3.stdout)
        self.assertEqual("version\n", calls2)
        self.assertEqual("version\n", calls3)

    def test_abandoned_incident_benchmark_is_not_reused(self) -> None:
        result, calls = self._run_dry(
            "run_dp1_canonical.sh",
            "--profile", "baseline",
            "--benchmark-id", "ab-20261025112811",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("troubleshooting evidence only", result.stderr)
        self.assertEqual("", calls)

    def test_selected_cross_profile_upstream_is_accepted_for_method_2(self) -> None:
        optimized = f"{BENCHMARK}-optimized-bronze-{ATTEMPT}"
        result, calls = self._run_dry("run_dp2_canonical.sh", optimized, "--profile", "baseline")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f'"bronze_run_id":"{optimized}"', result.stdout)
        self.assertEqual("version\n", calls)

    def test_non_benchmark_upstream_is_rejected_before_airflow(self) -> None:
        abandoned, calls = self._run_dry("run_dp2_canonical.sh",
                                          "canonical-bronze-20261025-100451",
                                          "--profile", "optimized")
        self.assertNotEqual(0, abandoned.returncode); self.assertEqual("", calls)

    def test_execute_rejects_uncommitted_benchmark_code_before_airflow(self) -> None:
        dirty_marker = REPOSITORY_ROOT / ".spark-ab-test-dirty"
        dirty_marker.write_text("test-only\n", encoding="utf-8")
        try:
            result, calls = self._run_dry(
                "run_dp1_canonical.sh",
                "--profile",
                "optimized",
                "--benchmark-id",
                BENCHMARK,
                "--execute",
            )
        finally:
            dirty_marker.unlink(missing_ok=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "benchmark execution requires a clean committed worktree",
            result.stderr,
        )
        self.assertEqual("version\n", calls)

    def test_scripts_are_dry_run_by_default_and_shell_valid(self) -> None:
        for name in ("run_dp1_canonical.sh", "run_dp2_canonical.sh", "run_dp3_canonical.sh",
                     "lib/spark_ab_common.sh", "start_airflow_local.sh"):
            result = subprocess.run(["bash", "-n", str(SCRIPTS_ROOT / name)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
        launcher = (SCRIPTS_ROOT / "start_airflow_local.sh").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8084/execution/", launcher)
        self.assertNotIn("db reset", launcher)


if __name__ == "__main__":
    unittest.main()
