"""SparkSession startup: the optional AQE settings are logged without a typed fallback.

Regression for benchmark ab-20261110215453: the Bronze validator died at startup with
``spark.sql.adaptive.coalescePartitions.enabled should be boolean, but was
spark-default`` because ``spark.conf.get(key, "spark-default")`` was called for a key
its profile leaves unset. Spark 3.5 converts such a fallback with the key's type.

The unit tests use a configuration fake with Spark 3.5's typed semantics. The
integration class starts real Spark and runs when EWALLET_RUN_SPARK_INTEGRATION=1.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from data_platform.config import load_runtime_config
from data_platform.config.spark_profiles import (
    ADAPTIVE_ADVISORY_SIZE_PROPERTY,
    ADAPTIVE_COALESCE_PROPERTY,
    ADAPTIVE_PARALLELISM_FIRST_PROPERTY,
    resolve_stage_profile,
)
from data_platform.processing.spark.common import spark_session
from data_platform.processing.spark.common.spark_session import (
    create_spark_session,
    describe_optional_sql_properties,
    spark_options,
)
from ops.gcp import campaign_task
from tests.unit.campaign_fakes import RecordingRunner, TerraformFixture, finished_batches, producer


STAGES = ("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features")
OPTIONAL = (ADAPTIVE_COALESCE_PROPERTY, ADAPTIVE_PARALLELISM_FIRST_PROPERTY, ADAPTIVE_ADVISORY_SIZE_PROPERTY)
# Spark 3.5.3's own values for the three keys when nothing sets them (verified locally).
SPARK_DEFAULTS = {ADAPTIVE_COALESCE_PROPERTY: "true", ADAPTIVE_PARALLELISM_FIRST_PROPERTY: "true",
                  ADAPTIVE_ADVISORY_SIZE_PROPERTY: "67108864b"}
REVISED = {("dp2-silver", True), ("dp3-features", True)}
# The validators' fixed session settings (data_platform/quality/validate_*.py), audited:
VALIDATOR_SESSIONS = {
    "bronze": ("EWallet-Bronze-Validation", "dp1-bronze", True),
    "silver": ("EWallet-Silver-Validation", "dp2-silver", True),
    "core-gold": ("EWallet-Gold-Validation", "dp2-gold", True),
    "features": ("EWallet-Gold-Validation", "dp2-gold", True),
}


def gcp_config():
    return load_runtime_config("gcp", environ={
        "GCP_PROJECT_ID": "example-project", "GCP_REGION": "asia-southeast1",
        "EWALLET_DATA_BUCKET": "example-project-data", "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
    })


class TypedConf:
    """Spark 3.5 RuntimeConfig semantics for the keys involved.

    A fallback passed for an unset typed key is converted with the key's type, so an
    invalid fallback raises, exactly as Spark does. Without a fallback Spark returns
    its own default. Writes are recorded so a test can prove logging never writes.
    """

    def __init__(self, values):
        self.values = dict(values)
        self.writes = []

    def get(self, key, *default):
        if key in self.values:
            return self.values[key]
        if default:
            fallback = default[0]
            if key in (ADAPTIVE_COALESCE_PROPERTY, ADAPTIVE_PARALLELISM_FIRST_PROPERTY) \
                    and fallback is not None and fallback not in ("true", "false"):
                raise ValueError(f"{key} should be boolean, but was {fallback}")
            if key == ADAPTIVE_ADVISORY_SIZE_PROPERTY and fallback is not None and not str(fallback).rstrip("bkmgt").isdigit():
                raise ValueError("Size must be specified as bytes (b), kibibytes (k), mebibytes (m) ...")
            return fallback
        return SPARK_DEFAULTS.get(key, "unset-in-fake")

    def set(self, key, value):
        self.writes.append((key, value))


class FakeSpark:
    version = "3.5.3"

    def __init__(self, values):
        self.conf = TypedConf(values)
        self.sparkContext = self
        self.stopped = False

    def setLogLevel(self, level):
        self.level = level

    def stop(self):
        self.stopped = True


class TypedFallbackTest(unittest.TestCase):
    def test_the_fake_reproduces_the_production_error(self):
        conf = TypedConf({})
        with self.assertRaisesRegex(ValueError, "should be boolean, but was spark-default"):
            conf.get(ADAPTIVE_COALESCE_PROPERTY, "spark-default")

    def test_every_stage_and_profile_logs_without_a_typed_fallback(self):
        config = gcp_config()
        for stage in STAGES:
            for optimized in (False, True):
                with self.subTest(stage=stage, optimized=optimized):
                    options = spark_options(config, stage=stage, optimized=optimized)
                    spark = FakeSpark(options)
                    lines = describe_optional_sql_properties(spark, options)
                    self.assertEqual([], spark.conf.writes)
                    self.assertEqual(3, len(lines))
                    if (stage, optimized) in REVISED:
                        self.assertEqual(["[SPARK] Coalesce   : true (profile)",
                                          "[SPARK] Par. first : false (profile)",
                                          "[SPARK] Advisory   : 67108864 (profile)"], lines)
                    else:
                        self.assertEqual(["[SPARK] Coalesce   : true (Spark default)",
                                          "[SPARK] Par. first : true (Spark default)",
                                          "[SPARK] Advisory   : 67108864b (Spark default)"], lines)

    def test_no_typed_fallback_is_passed_anywhere_in_the_session_helper(self):
        source = Path(spark_session.__file__).read_text(encoding="utf-8")
        self.assertNotIn("spark-default'", source)
        self.assertNotIn("conf.get(key, ", source)


class CreateSessionTest(unittest.TestCase):
    """create_spark_session end to end with a typed fake instead of a JVM."""

    def start(self, stage, optimized, app="test"):
        captured = {}

        def fake_get_or_create(builder, config):
            spark = FakeSpark(captured["options"])
            captured["spark"] = spark
            return spark

        real_options = spark_session.spark_options

        def recording_options(*args, **kwargs):
            captured["options"] = real_options(*args, **kwargs)
            return captured["options"]

        out = io.StringIO()
        with patch.object(spark_session, "_get_or_create_spark", fake_get_or_create), \
                patch.object(spark_session, "spark_options", recording_options), \
                patch.object(spark_session, "_prepare_local_event_log_directory"), \
                patch("pyspark.sql.SparkSession"), redirect_stdout(out):
            create_spark_session(app, stage=stage, optimized=optimized, runtime_config=gcp_config())
        return captured, out.getvalue()

    def test_all_eight_transformation_sessions_start(self):
        for stage in STAGES:
            for optimized in (False, True):
                with self.subTest(stage=stage, optimized=optimized):
                    captured, out = self.start(stage, optimized)
                    profile = resolve_stage_profile(stage, "optimized" if optimized else "baseline")
                    self.assertIn(f"[SPARK] AQE         : {str(profile.adaptive_enabled).lower()}", out)
                    self.assertIn(f"[SPARK] Shuffle     : {profile.shuffle_partitions}", out)
                    self.assertIn("[SPARK] Skew Join   : false", out)
                    self.assertIn("[SPARK] Advisory   : ", out)
                    self.assertEqual([], captured["spark"].conf.writes)

    def test_baseline_sessions_keep_aqe_off_200_partitions_no_skew_join(self):
        for stage in STAGES:
            with self.subTest(stage):
                captured, _ = self.start(stage, False)
                options = captured["options"]
                self.assertEqual(("false", "200", "false"), (
                    options["spark.sql.adaptive.enabled"], options["spark.sql.shuffle.partitions"],
                    options["spark.sql.adaptive.skewJoin.enabled"]))
                for key in OPTIONAL:
                    self.assertNotIn(key, options)

    def test_every_validator_session_starts(self):
        for scope, (app, stage, optimized) in VALIDATOR_SESSIONS.items():
            with self.subTest(scope):
                _, out = self.start(stage, optimized, app)
                self.assertIn(f"[SPARK] Application : {app}", out)
                self.assertIn("[SPARK] Coalesce   : ", out)


class ValidatorWiringTest(unittest.TestCase):
    """Each validator's session settings, and fail-closed startup errors."""

    def recorder(self):
        calls = []

        def fake_create(**kwargs):  # the validators pass every argument by keyword
            calls.append(kwargs)
            raise RuntimeError("simulated Spark initialization error")
        return calls, fake_create

    def test_validators_use_the_audited_stage_settings_and_never_pass_on_startup_errors(self):
        from data_platform.quality import validate_bronze, validate_gold, validate_silver

        config = gcp_config()
        cases = {
            "bronze": (validate_bronze, lambda: validate_bronze.run_validation(
                runtime_config=config, report_uri="gs://b/lakehouse/runs/r/evidence/validation-bronze-a1.json")),
            "silver": (validate_silver, lambda: validate_silver.run_validation(
                runtime_config=config, report_uri="gs://b/lakehouse/runs/r/evidence/validation-silver-a1.json")),
            "core-gold": (validate_gold, lambda: validate_gold.run_validation(
                config, scope="core-gold", report_uri="gs://b/lakehouse/runs/r/evidence/validation-core-gold-a1.json")),
            "features": (validate_gold, lambda: validate_gold.run_validation(
                config, scope="features", report_uri="gs://b/lakehouse/runs/r/evidence/validation-features-a1.json")),
        }
        for scope, (module, run) in cases.items():
            with self.subTest(scope):
                calls, fake = self.recorder()
                with patch.object(module, "create_spark_session", fake), \
                        patch.object(module, "write_report") as write_report, redirect_stdout(io.StringIO()):
                    if scope == "bronze":
                        self.assertFalse(run())  # Bronze returns its verdict: FAIL, exit code 1
                    else:
                        with self.assertRaisesRegex(RuntimeError, "simulated Spark initialization error"):
                            run()
                app, stage, optimized = VALIDATOR_SESSIONS[scope]
                self.assertEqual([(app, stage, optimized)],
                                 [(call["app_name"], call["stage"], call["optimized"]) for call in calls])
                write_report.assert_not_called()  # no report, so no PASS can be read downstream


class NoReports(RecordingRunner):
    """A validator batch that died before writing its report (the ab-20261110215453 case)."""

    def listing(self, uri):
        if uri.endswith("-a*.json"):
            return self._done(1, stderr="ERROR: One or more URLs matched no objects.")
        return super().listing(uri)


class MissingReportBlocksHandoffTest(TerraformFixture):
    def test_a_validator_that_wrote_no_report_blocks_the_handoff(self):
        runner = NoReports(existing_batches=finished_batches("baseline"))
        params = self.params(pipeline="dp1")
        with self.assertRaisesRegex(campaign_task.CampaignError, "no validation report found for bronze"):
            campaign_task.run_action("publish-handoff", params, attempt=1, runner=runner, producer=producer("dp1"))
        self.assertNotIn(campaign_task.handoff_uri(params, "dp1"), runner.objects)


@unittest.skipUnless(os.environ.get("EWALLET_RUN_SPARK_INTEGRATION") == "1",
                     "set EWALLET_RUN_SPARK_INTEGRATION=1 to start real Spark")
class RealSparkStartupTest(unittest.TestCase):
    """Real PySpark: every stage/profile and every validator session starts and logs."""

    @classmethod
    def setUpClass(cls):
        cls.directory = Path(tempfile.mkdtemp(prefix="spark-session-startup-"))
        base = load_runtime_config("local", environ={"MINIO_ACCESS_KEY": "unused", "MINIO_SECRET_KEY": "unused"})
        cls.config = replace(base, spark=replace(
            base.spark, master="local[1]", packages=(), event_log_uri=(cls.directory / "events").as_uri(),
            configurations={"spark.ui.enabled": "false", "spark.eventLog.enabled": "false"}))

    def start(self, app, stage, optimized):
        from pyspark.sql import SparkSession

        active = SparkSession.getActiveSession()
        if active is not None:
            active.stop()
        out = io.StringIO()
        with redirect_stdout(out):
            spark = create_spark_session(app, stage=stage, optimized=optimized, runtime_config=self.config)
        return spark, out.getvalue()

    def test_every_stage_and_profile_starts_and_keeps_its_effective_configuration(self):
        for stage in STAGES:
            for optimized in (False, True):
                with self.subTest(stage=stage, optimized=optimized):
                    spark, out = self.start("startup-test", stage, optimized)
                    try:
                        profile = resolve_stage_profile(stage, "optimized" if optimized else "baseline")
                        self.assertEqual(str(profile.adaptive_enabled).lower(), spark.conf.get("spark.sql.adaptive.enabled"))
                        self.assertEqual("false", spark.conf.get("spark.sql.adaptive.skewJoin.enabled"))
                        sql = profile.sql_properties()
                        for key in OPTIONAL:
                            if key in sql:
                                self.assertEqual(sql[key], spark.conf.get(key))
                                self.assertIn(f": {sql[key]} (profile)", out)
                            else:
                                # still unset after logging: Spark's own default applies
                                self.assertIsNone(spark.conf.get(key, None))
                                self.assertEqual(SPARK_DEFAULTS[key], spark.conf.get(key))
                                self.assertIn(f": {SPARK_DEFAULTS[key]} (Spark default)", out)
                    finally:
                        spark.stop()

    def test_every_validator_session_starts_and_records_its_execution(self):
        from data_platform.quality.reporting import write_execution_record

        for scope, (app, stage, optimized) in VALIDATOR_SESSIONS.items():
            with self.subTest(scope):
                spark, out = self.start(app, stage, optimized)
                try:
                    self.assertIn("[SPARK] AQE         : true", out)
                    record_uri = (self.directory / f"execution-{scope}.json").as_uri()
                    record = write_execution_record(spark, uri=record_uri, stage=f"{scope}-validation",
                                                    run_id="run-1", dataset_version="rehearsal-5gb-v1")
                    self.assertEqual("false", record["spark_event_log_enabled"])
                finally:
                    spark.stop()


if __name__ == "__main__":
    unittest.main()
