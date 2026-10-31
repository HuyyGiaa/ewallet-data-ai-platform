from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from data_platform.streaming.flink.job import build_job_plan


ROOT = Path(__file__).resolve().parents[2]


class RuntimeContractTest(unittest.TestCase):
    def test_yaml_contract_matches_python_topology(self) -> None:
        contract = yaml.safe_load(
            (ROOT / "configs/streaming/block-a.yaml").read_text(encoding="utf-8")
        )
        baseline = yaml.safe_load(
            (ROOT / "configs/streaming/flink-baseline.yaml").read_text(encoding="utf-8")
        )
        optimized = yaml.safe_load(
            (ROOT / "configs/streaming/flink-optimized.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual("transaction_id", contract["dedup"]["key"])
        self.assertEqual("[T-window,T)", contract["feature_state"]["temporal_contract"])
        self.assertFalse(contract["online_sink"]["direct_redis_key_writes"])
        self.assertIsNone(contract["online_sink"]["redis_key_ttl_seconds"])
        self.assertFalse(contract["bootstrap"]["accepted_topic_is_initial_source"])
        self.assertEqual(
            "ewallet.transactions.late.v1",
            contract["retry"]["too_late_destination"],
        )
        self.assertEqual(1, baseline["parallelism"])
        self.assertEqual(4, optimized["parallelism"])

    def test_flink_plans_change_performance_not_semantics(self) -> None:
        baseline = build_job_plan("baseline")
        optimized = build_job_plan("optimized")
        self.assertEqual(baseline.source_topic, optimized.source_topic)
        self.assertEqual(baseline.keyed_operators, optimized.keyed_operators)
        self.assertEqual(baseline.profile.event_time, optimized.profile.event_time)
        self.assertNotEqual(baseline.profile.parallelism, optimized.profile.parallelism)


if __name__ == "__main__":
    unittest.main()
