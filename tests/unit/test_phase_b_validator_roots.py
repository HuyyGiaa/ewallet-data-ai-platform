"""Stage validators read explicit run roots on gcp and never fall back to defaults."""

from __future__ import annotations

import unittest

from data_platform.config import load_runtime_config
from data_platform.quality import validate_gold, validate_silver


ENV = {"GCP_PROJECT_ID": "p", "EWALLET_DATA_BUCKET": "ewallet-rehearsal-5gb-data",
       "DATAHUB_GMS_URL": "http://datahub.invalid:8080"}
RUNS = "gs://ewallet-rehearsal-5gb-data/lakehouse/runs"
SILVER, GOLD, FEATURES = f"{RUNS}/s1/silver", f"{RUNS}/g1/gold", f"{RUNS}/f1/gold"


class GoldValidatorRootsTest(unittest.TestCase):
    def setUp(self):
        self.gcp = load_runtime_config("gcp", environ=ENV)

    def resolve(self, scope, **roots):
        values = {"silver_root": None, "gold_root": None, "feature_gold_root": None, **roots}
        return validate_gold.resolve_storage(self.gcp, scope=scope, **values)

    def test_gcp_requires_explicit_run_roots(self):
        for scope, roots in (("core-gold", {}), ("core-gold", {"silver_root": SILVER}),
                             ("features", {"silver_root": SILVER, "gold_root": GOLD}),
                             ("all", {"silver_root": SILVER, "gold_root": GOLD})):
            with self.subTest(scope=scope, roots=roots), self.assertRaises(ValueError):
                self.resolve(scope, **roots)

    def test_default_or_non_run_roots_are_refused(self):
        for bad in (self.gcp.storage.gold_root, "gs://x/lakehouse/runs/g1/silver", "/tmp/lakehouse/runs/g1/gold"):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.resolve("core-gold", silver_root=SILVER, gold_root=bad)

    def test_scopes_bind_the_core_and_feature_roots(self):
        core, features = self.resolve("core-gold", silver_root=SILVER, gold_root=GOLD)
        self.assertEqual((SILVER, GOLD), (core.silver_root, core.gold_root))
        self.assertIs(core, features)
        core, features = self.resolve("features", silver_root=SILVER, gold_root=GOLD, feature_gold_root=FEATURES)
        self.assertEqual((GOLD, FEATURES, SILVER), (core.gold_root, features.gold_root, features.silver_root))
        with self.assertRaisesRegex(ValueError, "own run root"):
            self.resolve("features", silver_root=SILVER, gold_root=GOLD, feature_gold_root=GOLD)
        with self.assertRaisesRegex(ValueError, "only read by the features scope"):
            self.resolve("core-gold", silver_root=SILVER, gold_root=GOLD, feature_gold_root=FEATURES)

    def test_local_runs_keep_the_profile_roots(self):
        local = load_runtime_config("local", environ={"MINIO_ACCESS_KEY": "x", "MINIO_SECRET_KEY": "x"})
        core, features = validate_gold.resolve_storage(local, scope="all", silver_root=None, gold_root=None,
                                                       feature_gold_root=None)
        self.assertEqual(local.storage, core)
        self.assertEqual(local.storage, features)

    def test_cli_exposes_the_stage_scopes(self):
        args = validate_gold.parse_args(["--scope", "features", "--pit-sample-modulus", "7"])
        self.assertEqual(("features", 7, False), (args.scope, args.pit_sample_modulus, args.include_merchant_scd2))
        self.assertEqual(("all", "core-gold", "features"), validate_gold.VALIDATION_SCOPES)


class SilverValidatorRootsTest(unittest.TestCase):
    def test_gcp_requires_both_run_roots(self):
        gcp = load_runtime_config("gcp", environ=ENV)
        with self.assertRaises(ValueError):
            validate_silver.apply_explicit_roots(gcp, bronze_root=None, silver_root=SILVER)
        with self.assertRaises(ValueError):
            validate_silver.apply_explicit_roots(gcp, bronze_root=f"{RUNS}/b1/bronze", silver_root=GOLD)
        bound = validate_silver.apply_explicit_roots(gcp, bronze_root=f"{RUNS}/b1/bronze", silver_root=SILVER)
        self.assertEqual((f"{RUNS}/b1/bronze", SILVER), (bound.storage.bronze_root, bound.storage.silver_root))


if __name__ == "__main__":
    unittest.main()
