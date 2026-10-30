"""Recovery controller safety tests; no Airflow or GCP calls are real."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from ops.gcp.recover_dp2_method2 import (
    GOLD_TASKS, airflow_status, reconcile_silver, recover_silver, resume_gold,
)


def config(root: Path):
    return {
        "airflow_url": "http://airflow", "dag_id": "dp2_bronze_to_curated",
        "dag_run_id": "dp2-method2-ab-20261029174838",
        "execution_git_sha": "a" * 40, "controller_git_sha": "b" * 40,
        "evidence_root": str(root), "benchmark_id": "ab-20261029174838",
        "data_bucket": "bucket", "project": "project", "region": "region",
        "baseline_gold_run_id": "baseline-gold",
        "optimized_gold_run_id": "optimized-gold",
    }


class RecoverySafetyTest(unittest.TestCase):
    def test_airflow_status_rejects_wrong_execution_source(self):
        cfg = config(Path("/tmp/evidence"))
        responses = [
            {"state": "failed", "conf": {"git_sha": "wrong"}},
            {"task_instances": []},
        ]
        with patch("ops.gcp.recover_dp2_method2._request", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "original execution Git SHA"):
                airflow_status(cfg, "token")

    def test_reconcile_marks_only_verified_silver_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(Path(directory))
            before = {"dag_run_state": "failed", "task_states": {
                "silver_optimized_evidence": "failed",
                "validate_and_select_silver_winner": "upstream_failed",
                **{task: "upstream_failed" for task in GOLD_TASKS},
            }}
            after = {"dag_run_state": "failed", "task_states": {
                **before["task_states"], "silver_optimized_evidence": "success",
                "validate_and_select_silver_winner": "success",
            }}
            requester = Mock(return_value={})
            with patch("ops.gcp.recover_dp2_method2._artifacts_complete"), patch(
                "ops.gcp.recover_dp2_method2.airflow_status", side_effect=[before, after]
            ), patch("ops.gcp.recover_dp2_method2._request", requester):
                with patch("ops.gcp.recover_dp2_method2._set_dag_paused"):
                    result = reconcile_silver(cfg, "token")
            self.assertEqual(result["status"], "SILVER_AIRFLOW_RECONCILED_GOLD_BLOCKED")
            patched_paths = [item.args[2] for item in requester.call_args_list]
            self.assertEqual(len(patched_paths), 2)
            self.assertTrue(all("silver" in path or "validate_and_select_silver" in path
                                for path in patched_paths))

    def test_reconcile_holds_auto_queued_dag_before_gold_can_run(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(Path(directory))
            tasks = {"silver_optimized_evidence": "failed",
                     "validate_and_select_silver_winner": "upstream_failed",
                     **{task: "upstream_failed" for task in GOLD_TASKS}}
            before = {"dag_run_state": "failed", "task_states": tasks}
            cascaded = {"dag_run_state": "queued", "task_states": {
                **tasks, "silver_optimized_evidence": "success",
                "validate_and_select_silver_winner": "success",
                **{task: None for task in GOLD_TASKS},
            }}
            held = {**cascaded, "dag_run_state": "failed"}
            requester = Mock(return_value={})
            pauser = Mock(return_value={"is_paused": True})
            with patch("ops.gcp.recover_dp2_method2._artifacts_complete"), patch(
                "ops.gcp.recover_dp2_method2.airflow_status",
                side_effect=[before, cascaded, held],
            ), patch("ops.gcp.recover_dp2_method2._request", requester), patch(
                "ops.gcp.recover_dp2_method2._set_dag_paused", pauser
            ):
                result = reconcile_silver(cfg, "token")
            pauser.assert_called_once_with(cfg, "token", True)
            self.assertEqual(result["states_after"]["dag_run_state"], "failed")
            self.assertTrue(any(item.args[1] == "PATCH" and
                                item.args[2].endswith(cfg["dag_run_id"])
                                for item in requester.call_args_list))

    def test_resume_gold_requires_explicit_approval_before_any_read(self):
        cfg = config(Path("/tmp/evidence"))
        with self.assertRaisesRegex(ValueError, "authorization"):
            resume_gold(cfg, "token", Path("missing.json"), "")

    def test_completed_recovery_is_idempotent_and_does_not_run_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cfg = config(root)
            audit = {
                "schema_version": "dp2-method2-recovery-v1",
                "status": "SILVER_RECOVERED", "dag_id": cfg["dag_id"],
                "dag_run_id": cfg["dag_run_id"], "benchmark_id": cfg["benchmark_id"],
                "execution_git_sha": cfg["execution_git_sha"],
                "controller_git_sha": cfg["controller_git_sha"],
                "completed_artifact_sha256": {"artifact": "digest"},
            }
            import json
            (root / "recovery-audit.json").write_text(json.dumps(audit))
            with patch("ops.gcp.recover_dp2_method2._inventory",
                       return_value={"artifact": "digest"}), patch(
                "ops.gcp.recover_dp2_method2._artifacts_complete"
            ), patch("ops.gcp.recover_dp2_method2._action") as action:
                self.assertEqual(recover_silver(cfg), audit)
            action.assert_not_called()

    @patch("ops.gcp.recover_dp2_method2.subprocess.run")
    def test_existing_gold_batch_blocks_resume(self, run):
        from ops.gcp.recover_dp2_method2 import _gold_outputs_absent
        run.return_value = Mock(returncode=0, stdout="{}", stderr="")
        with self.assertRaisesRegex(ValueError, "already exists"):
            _gold_outputs_absent(config(Path("/tmp/evidence")))

    @patch("ops.gcp.recover_dp2_method2.subprocess.run")
    def test_uncertain_gold_batch_lookup_fails_closed(self, run):
        from ops.gcp.recover_dp2_method2 import _gold_outputs_absent
        run.return_value = Mock(returncode=1, stdout="", stderr="permission denied")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify Gold batch absence"):
            _gold_outputs_absent(config(Path("/tmp/evidence")))


if __name__ == "__main__":
    unittest.main()
