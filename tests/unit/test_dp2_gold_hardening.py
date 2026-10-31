"""Gold recovery fail-closed regression tests; all cloud and Airflow access mocked."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.gcp import recover_dp2_method2 as rec


def config(root: Path):
    return {
        "airflow_url": "http://airflow", "dag_id": "dp2_bronze_to_curated",
        "dag_run_id": "dp2-method2-ab-20261029174838",
        "execution_git_sha": "a" * 40, "controller_git_sha": "b" * 40,
        "evidence_root": str(root), "benchmark_id": "ab-20261029174838",
        "data_bucket": "bucket", "project": "project", "region": "region",
        "baseline_gold_run_id": "baseline-gold",
        "optimized_gold_run_id": "optimized-gold",
        "cancelled_baseline_gold_batch_id": "baseline-gold",
        "baseline_gold_batch_id": "baseline-gold-recovery",
        "optimized_gold_batch_id": "optimized-gold-recovery",
    }


class GoldHardeningTest(unittest.TestCase):
    def _plan(self, cfg):
        cancelled = {"state": "CANCELLED", "name": cfg["cancelled_baseline_gold_batch_id"]}
        return {
            "schema_version": rec.RECOVERY_SCHEMA,
            "status": "GOLD_RECOVERY_PLAN_VERIFIED_NOT_SUBMITTED",
            "dag_id": cfg["dag_id"], "dag_run_id": cfg["dag_run_id"],
            "execution_git_sha": cfg["execution_git_sha"],
            "controller_git_sha": cfg["controller_git_sha"],
            "pair_manifest_digest": "frozen-digest",
            "cancelled_original_batch_id": cfg["cancelled_baseline_gold_batch_id"],
            "cancelled_original_batch": cancelled,
            "cancelled_original_batch_sha256": hashlib.sha256(
                json.dumps(cancelled, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "attempts": {
                profile: {
                    "dataproc_batch_id": cfg[f"{profile}_gold_batch_id"],
                    "output_run_id": cfg[f"{profile}_gold_run_id"],
                } for profile in ("baseline", "optimized")
            },
        }

    def test_gold_plan_detects_changed_attempt_and_cancelled_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(Path(folder))
            plan = self._plan(cfg)
            rec._plan_path(cfg).write_text(json.dumps(plan))
            with patch.object(rec, "_pair_for_gold", return_value={
                "pair_manifest_digest": "frozen-digest",
            }):
                self.assertEqual(rec._validate_gold_plan(cfg), plan)
                altered = dict(plan)
                altered["attempts"] = dict(plan["attempts"])
                altered["attempts"]["baseline"] = dict(plan["attempts"]["baseline"])
                altered["attempts"]["baseline"]["dataproc_batch_id"] = "rogue"
                rec._plan_path(cfg).write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, "immutable"):
                    rec._validate_gold_plan(cfg)
                altered = dict(plan)
                altered["cancelled_original_batch_sha256"] = "bad"
                rec._plan_path(cfg).write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, "hash"):
                    rec._validate_gold_plan(cfg)

    def test_new_controller_uses_new_create_only_plan_filename(self):
        cfg = config(Path("/tmp/gold-test-artifacts"))
        self.assertIn(cfg["controller_git_sha"][:12], rec._plan_path(cfg).name)
        self.assertNotEqual(rec._plan_path(cfg).name, "gold-recovery-plan.json")

    def _batch(self, cfg):
        return {
            "name": "projects/project/locations/region/batches/" + cfg["baseline_gold_batch_id"],
            "state": "SUCCEEDED", "uuid": "uuid-123", "createTime": "2026-10-30T19:20:00Z",
            "labels": {
                "component": "core-gold", "phase": "s1b", "benchmark_id": cfg["benchmark_id"],
                "spark_profile": "baseline", "git_sha": cfg["execution_git_sha"],
                "recovery_git_sha": cfg["controller_git_sha"],
                "output_run_id": cfg["baseline_gold_run_id"],
            },
            "runtimeConfig": {"version": "2.2", "properties": {"properties": "stubbed"}},
        }

    def test_batch_identity_rejects_wrong_resource_name_or_labels(self):
        cfg = config(Path("/tmp/evidence"))
        d = self._batch(cfg)
        with patch.object(rec, "_pair_for_gold", return_value={"pair_manifest_digest": "digest"}), patch.object(
            rec, "_runtime_properties", return_value={
                rec.METHOD2_PAIR_DIGEST_PROPERTY: "digest",
                rec.METHOD2_OUTPUT_RUN_PROPERTY: cfg["baseline_gold_run_id"],
            },
        ), patch.object(rec, "is_managed_spark_22", return_value=True), patch.object(
            rec, "validate_runtime_profile_properties"
        ), patch.object(rec, "_batch_cohort", return_value="uuid-123"):
            rec._validate_gold_batch_identity(cfg, profile="baseline", description=d)
            d["labels"]["git_sha"] = "wrong"
            with self.assertRaisesRegex(ValueError, "labels"):
                rec._validate_gold_batch_identity(cfg, profile="baseline", description=d)
            d["labels"]["git_sha"] = cfg["execution_git_sha"]
            d["name"] = "projects/project/locations/region/batches/rogue"
            with self.assertRaisesRegex(ValueError, "name"):
                rec._validate_gold_batch_identity(cfg, profile="baseline", description=d)

    def test_batch_identity_rejects_wrong_spark_pair_properties(self):
        cfg = config(Path("/tmp/evidence"))
        d = self._batch(cfg)
        with patch.object(rec, "_pair_for_gold", return_value={"pair_manifest_digest": "digest"}), patch.object(
            rec, "_runtime_properties", return_value={
                rec.METHOD2_PAIR_DIGEST_PROPERTY: "rogue",
                rec.METHOD2_OUTPUT_RUN_PROPERTY: cfg["baseline_gold_run_id"],
            },
        ), patch.object(rec, "is_managed_spark_22", return_value=True):
            with self.assertRaisesRegex(ValueError, "properties"):
                rec._validate_gold_batch_identity(cfg, profile="baseline", description=d)

    def test_quota_evidence_requires_correct_identity_and_recent_file(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(Path(folder))
            file = Path(folder) / "gold" / "baseline" / f"quota-{cfg['baseline_gold_batch_id']}.json"
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps({
                "status": "READY", "project": cfg["project"], "region": cfg["region"],
                "benchmark_id": cfg["benchmark_id"], "spark_profile": "baseline",
            }))
            rec._validate_fresh_quota(cfg, "baseline")
            os.utime(file, (file.stat().st_atime - 600, file.stat().st_mtime - 600))
            with self.assertRaisesRegex(ValueError, "stale"):
                rec._validate_fresh_quota(cfg, "baseline")
            file.write_text(json.dumps({
                "status": "READY", "project": "rogue", "region": cfg["region"],
                "benchmark_id": cfg["benchmark_id"], "spark_profile": "baseline",
            }))
            with self.assertRaisesRegex(ValueError, "exact"):
                rec._validate_fresh_quota(cfg, "baseline")

    def test_gold_step_fails_before_airflow_when_plan_is_inconsistent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = config(root)
            with patch.object(rec, "_validate_gold_plan", side_effect=ValueError("stale")), patch.object(
                rec, "_request"
            ) as api:
                with self.assertRaisesRegex(ValueError, "stale"):
                    rec.run_gold_step(cfg, "token", step="baseline-submit",
                                      authorization="I_APPROVE_DP2_GOLD_RECOVERY")
                api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
