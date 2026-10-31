from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.feast.online import DEFAULT_ONLINE_FRESHNESS
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
        self.assertEqual(
            int(DEFAULT_ONLINE_FRESHNESS.feature_view_ttl.total_seconds()),
            contract["online_sink"]["feature_view_ttl_seconds"],
        )
        self.assertEqual(
            int(DEFAULT_ONLINE_FRESHNESS.max_staleness.total_seconds()),
            contract["online_sink"]["max_staleness_seconds"],
        )
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

    def test_online_feast_repo_uses_push_source_and_exact_schema(self) -> None:
        definitions = (
            ROOT / "ai_platform/features/feast/online_repo/feature_definitions.py"
        ).read_text(encoding="utf-8")
        config = yaml.safe_load(
            (ROOT / "ai_platform/features/feast/online_repo/feature_store.yaml").read_text(
                encoding="utf-8"
            )
        )
        requirements = (
            ROOT / "ai_platform/features/feast/requirements.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("PushSource", definitions)
        self.assertIn("source=push_source", definitions)
        self.assertIn("online=True", definitions)
        self.assertEqual("redis", config["online_store"]["type"])
        self.assertIn("key_ttl_seconds", config["online_store"])
        self.assertIsNone(config["online_store"]["key_ttl_seconds"])
        self.assertIn("feast[redis]==0.66.0", requirements)
        self.assertEqual(31, sum(len(spec["features"]) for spec in FEATURE_SPECS.values()))


if __name__ == "__main__":
    unittest.main()
