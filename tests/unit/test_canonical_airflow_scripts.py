from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"


class CanonicalAirflowScriptsTest(unittest.TestCase):
    def _fake_airflow(self, root: Path) -> tuple[Path, Path]:
        log = root / "airflow-calls.log"
        binary = root / "airflow"
        binary.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_AIRFLOW_LOG\"\n"
            "if [[ ${1:-} == version ]]; then printf '3.3.1\\n'; fi\n",
            encoding="utf-8",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary, log

    def _run_dry(self, script: str, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            airflow, log = self._fake_airflow(root)
            env = dict(os.environ)
            env.update(
                AIRFLOW_BIN=str(airflow),
                FAKE_AIRFLOW_LOG=str(log),
                EWALLET_DATA_BUCKET="example-canonical-data",
            )
            result = subprocess.run(
                [str(SCRIPTS_ROOT / script), *args],
                cwd=REPOSITORY_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            return result, calls

    def test_dp2_dry_run_is_safe_and_self_contained(self) -> None:
        result, calls = self._run_dry(
            "run_dp2_canonical.sh", "canonical-bronze-20261025-010101"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MODE: DRY-RUN", result.stdout)
        self.assertIn("canonical-bronze-20261025-010101", result.stdout)
        self.assertIn('"dataset_version":"canonical-100gb-v1"', result.stdout)
        self.assertIn('"dataproc_ttl":"8h"', result.stdout)
        self.assertIn(
            '"dataproc_performance_profile":"canonical"', result.stdout
        )
        self.assertIn("airflow dags trigger dp2_bronze_to_curated", result.stdout)
        self.assertIn("airflow tasks states-for-dag-run", result.stdout)
        self.assertNotIn("--limit", result.stdout)
        self.assertEqual("version\n", calls)

    def test_dp3_dry_run_binds_exact_dp2_outputs(self) -> None:
        result, calls = self._run_dry(
            "run_dp3_canonical.sh",
            "canonical-silver-20261025-020202",
            "canonical-gold-20261025-020202",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MODE: DRY-RUN", result.stdout)
        self.assertIn("canonical-silver-20261025-020202", result.stdout)
        self.assertIn("canonical-gold-20261025-020202", result.stdout)
        self.assertIn('"dataset_version":"canonical-100gb-v1"', result.stdout)
        self.assertIn(
            '"dataproc_performance_profile":"canonical"', result.stdout
        )
        self.assertIn("airflow dags trigger dp3_historical_features", result.stdout)
        self.assertNotIn("--limit", result.stdout)
        self.assertEqual("version\n", calls)

    def test_runner_rejects_noncanonical_upstream_before_airflow(self) -> None:
        result, calls = self._run_dry(
            "run_dp2_canonical.sh", "r4-bronze-20261022-095608"
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("canonical Bronze", result.stderr)
        self.assertEqual("", calls)

    def test_local_launcher_is_non_destructive_and_covers_three_components(self) -> None:
        script = SCRIPTS_ROOT / "start_airflow_local.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("api-server", text)
        self.assertIn("scheduler", text)
        self.assertIn("dag-processor", text)
        self.assertIn("http://127.0.0.1:8084/execution/", text)
        self.assertIn("db check", text)
        self.assertNotIn("db reset", text)
        self.assertNotIn("db migrate", text)
        syntax = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, syntax.returncode, syntax.stderr)


if __name__ == "__main__":
    unittest.main()
