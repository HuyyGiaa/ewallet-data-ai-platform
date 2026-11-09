"""Current feature builders reproduce the pre-Phase-B outputs (permanent regression).

The golden fixture in ``tests/fixtures/feature_equivalence`` was produced by the
builders of the pre-Phase-B commit named in ``PROVENANCE.json``. See
``build_golden.py`` there. This test runs the current builders on the same
committed inputs and compares every pre-existing column.

Equivalence policy:
* same column names and order (``created`` and the write-time
  ``created_timestamp`` are excluded; they are the only intended additions/volatile);
* same keys and row counts, keys unique;
* integer, string and timestamp (epoch microsecond) columns are exactly equal;
* NULL positions are identical;
* doubles agree within ``rel_tol=1e-12, abs_tol=1e-9``. These are sums, averages
  and standard deviations whose last bits can depend on Spark's aggregation
  order. Any real feature change (a different window, an included current row)
  moves values by far more.
"""

from __future__ import annotations

import hashlib
import json
import math
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from pyspark.sql import SparkSession

from data_platform.processing.spark.gold.feature_builder import build_feat_user_90d
from data_platform.processing.spark.gold.features import (
    build_feat_account_behavior,
    build_feat_device_behavior,
    build_feat_merchant_behavior,
    build_feat_user_behavior,
)
from tests.fixtures.feature_equivalence.build_golden import GOLDEN_KEYS, spark_inputs, to_golden


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "feature_equivalence"
CLOCK = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
REL_TOL, ABS_TOL = 1e-12, 1e-9


def assert_equivalent(case: unittest.TestCase, name: str, golden: pd.DataFrame, current: pd.DataFrame) -> None:
    case.assertEqual(list(golden.columns), list(current.columns), f"{name}: columns")
    case.assertEqual(len(golden), len(current), f"{name}: row count")
    key = list(GOLDEN_KEYS[name])
    case.assertFalse(golden.duplicated(key).any(), f"{name}: golden key not unique")
    pd.testing.assert_frame_equal(golden[key], current[key], check_dtype=False, obj=f"{name} keys")
    for column in golden.columns:
        left, right = golden[column], current[column]
        case.assertTrue((left.isna() == right.isna()).all(), f"{name}.{column}: NULL positions differ")
        mask = left.notna()
        if pd.api.types.is_float_dtype(left) or pd.api.types.is_float_dtype(right):
            bad = [i for i in left[mask].index
                   if not math.isclose(float(left[i]), float(right[i]), rel_tol=REL_TOL, abs_tol=ABS_TOL)]
            case.assertEqual([], bad[:5], f"{name}.{column}: values differ beyond the policy")
        else:
            case.assertTrue((left[mask].astype(object) == right[mask].astype(object)).all(),
                            f"{name}.{column}: values differ")


class FeatureEquivalenceGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provenance = json.loads((FIXTURE / "PROVENANCE.json").read_text(encoding="utf-8"))
        cls.spark = (
            SparkSession.builder.master("local[2]").appName("feature-equivalence")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "4")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("ERROR")
        inputs = {name: pd.read_parquet(FIXTURE / f"input_{name}.parquet")
                  for name in ("transactions", "devices", "users")}
        transactions, devices, users = spark_inputs(cls.spark, inputs)
        current = {
            "feat_user_behavior": build_feat_user_behavior(transactions, materialized_at=CLOCK),
            "feat_account_behavior": build_feat_account_behavior(transactions, materialized_at=CLOCK),
            "feat_device_behavior": build_feat_device_behavior(transactions, devices, materialized_at=CLOCK),
            "feat_merchant_behavior": build_feat_merchant_behavior(transactions, materialized_at=CLOCK),
            "feat_user_90d": build_feat_user_90d(transactions, users, materialized_at=CLOCK),
        }
        cls.created_columns = {name: [c for c in frame.columns if c not in to_golden_columns(frame)]
                               for name, frame in current.items()}
        cls.current = {name: to_golden(frame, GOLDEN_KEYS[name]) for name, frame in current.items()}

    def test_fixture_files_match_their_recorded_checksums(self):
        self.assertRegex(self.provenance["base_commit"], r"^[0-9a-f]{40}$")
        for name, item in self.provenance["files"].items():
            with self.subTest(name):
                self.assertEqual(item["sha256"], hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest())

    def test_all_five_tables_match_the_pre_phase_b_outputs(self):
        for name in GOLDEN_KEYS:
            with self.subTest(name):
                golden = pd.read_parquet(FIXTURE / f"{name}.parquet")
                assert_equivalent(self, name, golden, self.current[name])

    def test_only_audit_columns_were_added(self):
        self.assertEqual({name: ["created"] for name in GOLDEN_KEYS if name != "feat_user_90d"}
                         | {"feat_user_90d": ["created_timestamp", "created"]}, self.created_columns)

    def test_the_policy_rejects_a_meaningful_drift(self):
        golden = pd.read_parquet(FIXTURE / "feat_user_behavior.parquet")
        for column, change in (("user_tx_count_1h", lambda s: s + 1),
                               ("user_avg_amount_30d", lambda s: s * (1 + 1e-9))):
            with self.subTest(column):
                drifted = golden.copy()
                row = drifted[column].first_valid_index()
                drifted.loc[row, column] = change(drifted.loc[[row], column]).iloc[0]
                with self.assertRaises(AssertionError):
                    assert_equivalent(self, "feat_user_behavior", golden, drifted)
        nulled = golden.copy()
        nulled.loc[nulled["user_avg_amount_30d"].first_valid_index(), "user_avg_amount_30d"] = None
        with self.assertRaises(AssertionError):
            assert_equivalent(self, "feat_user_behavior", golden, nulled)


def to_golden_columns(frame) -> set[str]:
    return {c for c in frame.columns if c not in ("created", "created_timestamp")}


if __name__ == "__main__":
    unittest.main()
