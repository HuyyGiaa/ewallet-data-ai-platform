from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from data_platform.storage.cloud_optimization import (
    PINNED_CAPABILITIES,
    TableAccessPattern,
    classify_runtime_change,
    compare_equivalence,
    compare_layouts,
    recommend_layout,
    rewrite_outcome,
    workload_verdict,
    decide_partitioning,
    decide_zorder,
    select_table,
    summarize_layout,
    validate_benchmark_paths,
)
from ops.gcp.run_storage_benchmark import (
    _assert_tool_git_state,
    build_submission_command,
    main,
)
from ops.gcp.inspect_storage_layout import (
    _action_rows,
    build_plan,
    choose_parameters,
    load_verified_campaign,
)
from ops.gcp.storage_benchmark_job import (
    REQUIRED_COLUMNS,
    _assert_representative_result,
    _create_shallow_clone,
    _query_sql,
    _run_layout_queries,
    effective_conf,
    scan_metrics,
)
from ops.gcp.submit_source_to_bronze import CloudRuntime


MIB = 1024 * 1024


class LayoutSummaryTests(unittest.TestCase):
    def test_delta_add_actions_flattening_is_metadata_only(self) -> None:
        import pyarrow as pa

        actions = pa.record_batch(
            [
                pa.array(["event_date=2026-01-01/a.parquet"]),
                pa.array([123]),
                pa.array([7]),
                pa.array(["2026-01-01"]),
                pa.array(["m1"]),
                pa.array(["m9"]),
            ],
            names=[
                "path", "size_bytes", "num_records", "partition.event_date",
                "min.merchant_id", "max.merchant_id",
            ],
        )
        rows = _action_rows(actions)
        self.assertEqual(rows[0]["size_bytes"], 123)
        self.assertEqual(rows[0]["partition_values"], {"event_date": "2026-01-01"})
        self.assertEqual(rows[0]["min_values"], {"merchant_id": "m1"})

    def test_metadata_summary_includes_distribution_and_partitions(self) -> None:
        actions = [
            {
                "path": "event_date=2026-01-01/a.parquet",
                "size_bytes": MIB,
                "num_records": 10,
                "partition_values": {"event_date": "2026-01-01"},
                "min_values": {"merchant_id": "m1"},
                "max_values": {"merchant_id": "m9"},
            },
            {
                "path": "event_date=2026-01-01/b.parquet",
                "size_bytes": 3 * MIB,
                "num_records": 30,
                "partition_values": {"event_date": "2026-01-01"},
                "min_values": {"merchant_id": "m2"},
                "max_values": {"merchant_id": "m8"},
            },
            {
                "path": "event_date=2026-01-02/c.parquet",
                "size_bytes": 20 * MIB,
                "num_records": 60,
                "partition_values": {"event_date": "2026-01-02"},
                "min_values": {"merchant_id": "m3"},
                "max_values": {"merchant_id": "m7"},
            },
        ]
        result = summarize_layout(
            table="fact_transactions",
            stage="dp2-gold",
            uri="gs://bucket/lakehouse/runs/run/gold/fact_transactions",
            delta_version=0,
            partition_columns=("event_date",),
            properties={"delta.dataSkippingNumIndexedCols": "32"},
            actions=actions,
            small_file_threshold_bytes=4 * MIB,
        )

        self.assertEqual(result["row_count"], 100)
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(result["partition_count"], 2)
        self.assertEqual(result["very_small_file_count"], 2)
        self.assertEqual(result["file_size_bytes"]["median"], 3 * MIB)
        self.assertEqual(result["file_size_bytes"]["p95"], 20 * MIB)
        self.assertEqual(result["partitions"][0]["file_count"], 2)
        self.assertIn("merchant_id", result["statistics_columns"])
        self.assertEqual(
            result["global_statistics"]["event_date"],
            {"minimum": "2026-01-01", "maximum": "2026-01-02"},
        )
        self.assertEqual(result["small_partition_count"], 2)
        self.assertTrue(result["partitions"][0]["row_count_complete"])
        self.assertEqual(
            result["statistics_profiles"]["merchant_id"]["coverage_ratio"],
            1.0,
        )

    def test_selection_requires_layout_evidence_and_query_importance(self) -> None:
        summary = {
            "total_bytes": 2 * 1024**3,
            "file_count": 200,
            "very_small_file_count": 180,
            "file_size_bytes": {"median": 2 * MIB},
        }
        decision = select_table(summary, query_importance="high")
        self.assertTrue(decision["selected"])
        self.assertIn("small-file", decision["reasons"][0])
        self.assertFalse(select_table(summary, query_importance="none")["selected"])

    def test_partial_partition_row_statistics_are_not_reported_as_complete(self) -> None:
        result = summarize_layout(
            table="transactions",
            stage="dp2-silver",
            uri="gs://bucket/table",
            delta_version=0,
            partition_columns=("event_date",),
            properties={},
            actions=(
                {
                    "path": "a",
                    "size_bytes": MIB,
                    "num_records": 10,
                    "partition_values": {"event_date": "2026-01-01"},
                },
                {
                    "path": "b",
                    "size_bytes": MIB,
                    "num_records": None,
                    "partition_values": {"event_date": "2026-01-01"},
                },
            ),
        )
        self.assertIsNone(result["row_count"])
        self.assertIsNone(result["partitions"][0]["rows"])
        self.assertFalse(result["partitions"][0]["row_count_complete"])


class StrategyTests(unittest.TestCase):
    def test_partition_decision_keeps_existing_date_partition(self) -> None:
        result = decide_partitioning(
            partition_columns=("event_date",),
            partition_count=151,
            total_bytes=100 * 1024**3,
            files_per_partition_p95=8,
            partition_size_bytes={
                "minimum": 512 * MIB,
                "median": 700 * MIB,
                "p95": 800 * MIB,
                "maximum": 900 * MIB,
            },
            small_partition_count=0,
            access=TableAccessPattern(
                query_importance="high",
                partition_predicates=("event_date",),
                zorder_candidates=("merchant_id",),
            ),
        )
        self.assertEqual(result["action"], "KEEP")
        self.assertEqual(result["columns"], ["event_date"])

    def test_partition_decision_rejects_high_cardinality_identifier(self) -> None:
        result = decide_partitioning(
            partition_columns=(),
            partition_count=1,
            total_bytes=100 * 1024**3,
            files_per_partition_p95=100,
            partition_size_bytes={"median": 100 * 1024**3, "maximum": 100 * 1024**3},
            small_partition_count=0,
            access=TableAccessPattern(
                query_importance="high",
                partition_predicates=("user_id",),
                zorder_candidates=("user_id",),
            ),
        )
        self.assertEqual(result["action"], "KEEP_UNPARTITIONED")
        self.assertIn("high-cardinality", result["reason"])

    def test_partition_decision_flags_skew_instead_of_forcing_keep(self) -> None:
        result = decide_partitioning(
            partition_columns=("event_date",),
            partition_count=100,
            total_bytes=100 * 1024**3,
            files_per_partition_p95=20,
            partition_size_bytes={
                "minimum": MIB,
                "median": 100 * MIB,
                "p95": 2 * 1024**3,
                "maximum": 20 * 1024**3,
            },
            small_partition_count=20,
            access=TableAccessPattern(
                query_importance="high", partition_predicates=("event_date",)
            ),
        )
        self.assertEqual(result["action"], "REVIEW_AFTER_A_B")

    def test_zorder_requires_stats_and_non_partition_predicate(self) -> None:
        access = TableAccessPattern(
            query_importance="high",
            partition_predicates=("event_date",),
            zorder_candidates=("merchant_id", "event_date"),
        )
        result = decide_zorder(
            access=access,
            partition_columns=("event_date",),
            statistics_columns=("merchant_id", "event_date"),
            statistics_profiles={
                "merchant_id": {
                    "coverage_ratio": 1.0,
                    "distinct_file_minimums": 12,
                    "distinct_file_maximums": 12,
                }
            },
            equality_predicate_columns=("merchant_id",),
        )
        self.assertEqual(result["action"], "ZORDER")
        self.assertEqual(result["columns"], ["merchant_id"])
        self.assertEqual(
            decide_zorder(
                access=access,
                partition_columns=("event_date",),
                statistics_columns=("event_date",),
                statistics_profiles={},
                equality_predicate_columns=("merchant_id",),
            )["action"],
            "COMPACTION_ONLY",
        )

    def test_zorder_rejects_unqueried_or_constant_stats(self) -> None:
        access = TableAccessPattern(
            query_importance="high", zorder_candidates=("merchant_id",)
        )
        common = {
            "access": access,
            "partition_columns": (),
            "statistics_columns": ("merchant_id",),
        }
        self.assertEqual(
            decide_zorder(
                **common,
                statistics_profiles={
                    "merchant_id": {
                        "coverage_ratio": 1.0,
                        "distinct_file_minimums": 1,
                        "distinct_file_maximums": 1,
                    }
                },
                equality_predicate_columns=("merchant_id",),
            )["action"],
            "COMPACTION_ONLY",
        )
        self.assertEqual(
            decide_zorder(
                **common,
                statistics_profiles={
                    "merchant_id": {
                        "coverage_ratio": 1.0,
                        "distinct_file_minimums": 20,
                        "distinct_file_maximums": 20,
                    }
                },
                equality_predicate_columns=(),
            )["action"],
            "COMPACTION_ONLY",
        )


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456"
SPEC = ROOT / "images/edai1-campaign-verifier/workload-5gb.json"
BUCKET = "ewallet-data-ai-platform-hg26-rehearsal-5gb-data"
SOURCE_SHA = "aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c"
GOLD_RUN = "ab-20261110225456-optimized-gold-r261110225458"
PARAMETERS = {"window_start": "2026-05-25", "window_end": "2026-05-31", "merchant_id": "m-0001",
              "status_counts_in_window": {"failed": 3, "success": 10}}


def binding():
    return load_verified_campaign(EVIDENCE, SPEC, dataset_version="rehearsal-5gb-v1",
                                  source_spark_git_sha=SOURCE_SHA)


def gold_layout(**overrides):
    layout = {
        "table": "fact_transactions",
        "stage": "dp2-gold",
        "uri": f"gs://{BUCKET}/lakehouse/runs/{GOLD_RUN}/gold/fact_transactions",
        "delta_version": 0,
        "total_bytes": 3_667_633_194,
        "file_count": 151,
        "very_small_file_count": 0,
        "file_size_bytes": {"median": 24 * MIB, "p95": 24 * MIB},
        "partition_columns": ["event_date"],
        "partition_count": 151,
        "files_per_partition_p95": 1,
        "partition_size_bytes": {"minimum": 23 * MIB, "median": 24 * MIB, "p95": 24 * MIB, "maximum": 24 * MIB},
        "small_partition_count": 151,
        "statistics_columns": ["merchant_id", "timestamp"],
        "statistics_profiles": {"merchant_id": {"coverage_ratio": 1.0, "distinct_file_minimums": 1,
                                                "distinct_file_maximums": 8}},
        "global_statistics": {"event_date": {"minimum": "2026-01-01", "maximum": "2026-05-31"}},
        "table_properties": {},
        "partitions": [],
        "inspection_mode": "delta_log_metadata_only",
    }
    layout.update(overrides)
    return layout


class SafetyAndComparisonTests(unittest.TestCase):
    def test_campaign_binding_comes_from_committed_verified_evidence(self) -> None:
        value = binding()
        self.assertEqual(value.benchmark_id, "ab-20261110225456")
        self.assertEqual(value.run_ids["dp2-gold"], GOLD_RUN)
        self.assertTrue(value.output_roots["dp3-features"].endswith("-optimized-feat-r261110225459/gold"))

    def test_campaign_binding_refuses_another_dataset_or_source_sha(self) -> None:
        with self.assertRaisesRegex(ValueError, "workload spec"):
            load_verified_campaign(EVIDENCE, SPEC, dataset_version="canonical-100gb-v1",
                                   source_spark_git_sha=SOURCE_SHA)
        with self.assertRaisesRegex(ValueError, "workload spec"):
            load_verified_campaign(EVIDENCE, SPEC, dataset_version="rehearsal-5gb-v1",
                                   source_spark_git_sha="7e6f050028706a2a930a733771d97e04f1df2864")

    def test_plan_binds_tables_to_the_campaign_and_three_isolated_arms(self) -> None:
        plan = build_plan(benchmark_id="storage-5gb-test", binding=binding(),
                          tool_git_sha="8e6f050028706a2a930a733771d97e04f1df2865", data_bucket=BUCKET,
                          layouts=(gold_layout(),), parameters={"dp2-gold.fact_transactions": PARAMETERS})
        table = plan["tables"][0]
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["source_campaign"]["run_ids"]["dp2-gold"], GOLD_RUN)
        two_tables = build_plan(benchmark_id="storage-5gb-test", binding=binding(), tool_git_sha="8" * 40,
                                data_bucket=BUCKET, layouts=(gold_layout(), gold_layout(
                                    table="obt_transaction_enriched",
                                    uri=f"gs://{BUCKET}/lakehouse/runs/{GOLD_RUN}/gold/obt_transaction_enriched")),
                                parameters={"dp2-gold.fact_transactions": PARAMETERS,
                                            "dp2-gold.obt_transaction_enriched": PARAMETERS})
        self.assertEqual(two_tables["source_run_ids"], [GOLD_RUN])
        self.assertEqual(plan["source_spark_git_sha"], SOURCE_SHA)
        self.assertNotEqual(plan["git_sha"], plan["source_spark_git_sha"])
        for arm in ("baseline", "compacted", "optimized"):
            self.assertIn(f"/benchmarks/storage/storage-5gb-test/{arm}/dp2-gold/", table[f"{arm}_uri"])
        self.assertEqual(table["optimization"]["action"], "ZORDER")
        self.assertEqual(table["optimization"]["columns"], ["merchant_id"])
        # Every file starts at the same merchant_id: metadata predicts no skipping benefit.
        self.assertEqual(table["optimization"]["metadata_prediction"]["action"], "COMPACTION_ONLY")
        self.assertEqual(table["partition_decision"]["action"], "REVIEW_AFTER_A_B")
        self.assertEqual(table["workload_parameters"]["merchant_id"], "m-0001")

    def test_plan_rejects_a_table_outside_the_campaign_or_bucket(self) -> None:
        other = gold_layout(uri=f"gs://{BUCKET}/lakehouse/runs/ab-20261110225456-baseline-gold-r1/gold/fact_transactions")
        with self.assertRaisesRegex(ValueError, "not the verified campaign table"):
            build_plan(benchmark_id="storage-5gb-test", binding=binding(), tool_git_sha="8" * 40,
                       data_bucket=BUCKET, layouts=(other,), parameters={"dp2-gold.fact_transactions": PARAMETERS})
        with self.assertRaisesRegex(ValueError, "must use bucket"):
            build_plan(benchmark_id="storage-5gb-test", binding=binding(), tool_git_sha="8" * 40,
                       data_bucket="ewallet-data-ai-platform-hg26-data", layouts=(gold_layout(),),
                       parameters={"dp2-gold.fact_transactions": PARAMETERS})

    def test_parameters_are_a_typical_merchant_and_require_failed_rows(self) -> None:
        chosen = choose_parameters(("2026-05-25", "2026-05-31"), {"a": 1, "b": 2, "c": 2, "d": 9},
                                   {"failed": 4, "success": 10}, 14)
        self.assertEqual(chosen["merchant_id"], "b")
        self.assertEqual(chosen["merchant_rows_in_window"], 2)
        with self.assertRaisesRegex(ValueError, "no failed"):
            choose_parameters(("2026-05-25", "2026-05-31"), {"a": 1}, {"success": 1}, 1)
        with self.assertRaisesRegex(ValueError, "no merchant"):
            choose_parameters(("2026-05-25", "2026-05-31"), {}, {"failed": 1}, 1)

    def test_queries_use_data_derived_parameters_and_three_pruning_sides(self) -> None:
        table = {"table": "fact_transactions", "workload_parameters": PARAMETERS}
        date_sql = _query_sql(table, {"class": "date_range"}, relation="baseline_table")
        merchant_sql = _query_sql(table, {"class": "date_entity_lookup"}, relation="optimized_table")
        failed_sql = _query_sql(table, {"class": "date_status_aggregation"}, relation="compacted_table")
        stats_sql = _query_sql(table, {"class": "date_range_stats_only"}, relation="baseline_table")
        full_sql = _query_sql(table, {"class": "date_range_full_scan"}, relation="baseline_table")
        self.assertIn("event_date BETWEEN DATE '2026-05-25' AND DATE '2026-05-31'", date_sql)
        self.assertIn("DECIMAL(38,6)", date_sql)
        self.assertIn("merchant_id = 'm-0001'", merchant_sql)
        self.assertIn("status = 'failed'", failed_sql)
        self.assertIn("timestamp < TIMESTAMP '2026-06-01 00:00:00'", stats_sql)
        self.assertNotIn("event_date", stats_sql.split("WHERE", 1)[1])
        self.assertIn("date_format(timestamp, 'yyyy-MM-dd')", full_sql)
        with self.assertRaisesRegex(ValueError, "workload window"):
            _query_sql({"table": "x", "workload_parameters": {}}, {"class": "date_range"}, relation="baseline_table")

    def test_trino_queries_pin_the_window_to_utc(self) -> None:
        # The Spark job runs in UTC; a zone-less Trino literal would follow the DBeaver
        # session zone (+07:00 on the owner's desktop) and shift the stats-only window.
        from ops.gcp.storage_benchmark_report import query_sql

        plan = {"tables": [{"table": "fact_transactions", "uri": "gs://b/t", "delta_version": 0,
                            "workload_parameters": PARAMETERS}]}
        sql = query_sql(plan)
        stats = next(line for line in sql.splitlines() if "\"timestamp\" >= TIMESTAMP" in line)
        self.assertIn("\"timestamp\" >= TIMESTAMP '2026-05-25 00:00:00 UTC'", stats)
        self.assertIn("\"timestamp\" < TIMESTAMP '2026-06-01 00:00:00 UTC'", stats)
        self.assertIn("format_datetime(\"timestamp\" AT TIME ZONE 'UTC', 'yyyy-MM-dd')", sql)
        self.assertNotRegex(sql, r"TIMESTAMP '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'")

    def test_trino_script_is_deterministic_and_covers_both_gold_tables_once(self) -> None:
        from ops.gcp.storage_benchmark_report import query_sql

        plan = {"tables": [
            {"table": name, "uri": f"gs://b/{name}", "delta_version": 0, "workload_parameters": PARAMETERS}
            for name in ("fact_transactions", "obt_transaction_enriched")]}
        first, second = query_sql(plan), query_sql(plan)
        self.assertEqual(first, second)
        for name in ("fact_transactions", "obt_transaction_enriched"):
            block = first.split(f"-- ===== {name} ")[1].split("-- ===== ")[0]
            for side in ("pruned", "stats_only", "full_scan"):
                self.assertEqual(block.count(f"-- ST09 partition pruning: {side}\n"), 1, (name, side))
                self.assertEqual(block.count(f"-- ST09 result: {side}\n"), 1, (name, side))
                result = block.split(f"-- ST09 result: {side}\n")[1].splitlines()[0]
                explain = block.split(f"-- ST09 partition pruning: {side}\n")[1].splitlines()[0]
                self.assertEqual(explain, f"EXPLAIN ANALYZE {result}")
            st09 = block.split("-- ST09 result: pruned")[1]
            self.assertEqual(st09.count(f"FROM delta.storage_bench.{name}_baseline WHERE"), 6)
            self.assertIn("UNION ALL SELECT 'optimized' AS arm", block)
        # The pruned side keeps the partition predicate; the controls never mention it.
        stats = next(line for line in first.splitlines() if "\"timestamp\" >= TIMESTAMP" in line)
        full = next(line for line in first.splitlines() if "format_datetime" in line)
        for line in (stats, full):
            self.assertNotIn("event_date", line.split("WHERE", 1)[1])

    def test_job_refuses_a_non_utc_session(self) -> None:
        from ops.gcp.storage_benchmark_job import require_utc_session

        class Conf:
            def __init__(self, zone):
                self.zone = zone

            def get(self, key):
                assert key == "spark.sql.session.timeZone"
                return self.zone

        class Spark:
            def __init__(self, zone):
                self.conf = Conf(zone)

        require_utc_session(Spark("UTC"))
        with self.assertRaisesRegex(ValueError, "must be UTC"):
            require_utc_session(Spark("Asia/Ho_Chi_Minh"))

    def test_three_arm_workload_is_measured_with_equal_query_results(self) -> None:
        class Reader:
            def format(self, _):
                return self

            def load(self, _):
                return self

            def createOrReplaceTempView(self, _):
                return None

        class Spark:
            read = Reader()

        table = {"table": "fact_transactions", "workload_parameters": PARAMETERS,
                 "queries": [{"name": "window_aggregation", "class": "date_range", "require_non_empty": True}]}
        import ops.gcp.storage_benchmark_job as job

        original = job._execute_query
        job._execute_query = lambda *_: ([{"row_count": 1}], 0.1, "plan", {"available": False, "reason": "test"})
        try:
            results = _run_layout_queries(Spark(), table, layout_uris={
                "baseline": "gs://bucket/baseline", "compacted": "gs://bucket/compacted",
                "optimized": "gs://bucket/optimized"})
        finally:
            job._execute_query = original
        self.assertEqual(set(results), {"baseline", "compacted", "optimized"})
        self.assertEqual(len(results["compacted"][0]["runtime_samples_seconds"]), 5)
        self.assertEqual(results["baseline"][0]["result_json"], results["optimized"][0]["result_json"])
        self.assertEqual(results["baseline"][0]["position_in_rotation"], [0, 2, 1, 0, 2])

    def test_clone_pins_inspected_source_version(self) -> None:
        class Result:
            def collect(self):
                return []

        class Spark:
            def __init__(self):
                self.statements = []

            def sql(self, statement):
                self.statements.append(statement)
                return Result()

        spark = Spark()
        import ops.gcp.storage_benchmark_job as job

        original = job._path_exists
        job._path_exists = lambda *_: False
        try:
            _create_shallow_clone(spark, "gs://bucket/source", "gs://bucket/target", source_version=17)
        finally:
            job._path_exists = original
        self.assertIn("SHALLOW CLONE", spark.statements[0])
        self.assertIn("VERSION AS OF 17", spark.statements[0])

    def test_runtime_change_has_four_honest_verdicts(self) -> None:
        self.assertEqual(classify_runtime_change([10, 11, 10.5], [5, 5.2, 5.1])["verdict"], "IMPROVED")
        self.assertEqual(classify_runtime_change([10, 11, 10.5], [14, 15, 16])["verdict"], "REGRESSED")
        self.assertEqual(classify_runtime_change([10, 10.1, 10.2], [10.1, 10.3, 9.9])["verdict"],
                         "NO_CLEAR_IMPROVEMENT")
        self.assertEqual(classify_runtime_change([10, 20, 12], [8, 15, 9])["verdict"], "INCONCLUSIVE")
        with self.assertRaises(ValueError):
            classify_runtime_change([], [1.0])

    def test_workload_verdict_combines_queries_conservatively(self) -> None:
        self.assertEqual(workload_verdict(["IMPROVED", "REGRESSED"]), "REGRESSED")
        self.assertEqual(workload_verdict(["IMPROVED", "INCONCLUSIVE"]), "INCONCLUSIVE")
        self.assertEqual(workload_verdict(["IMPROVED", "NO_CLEAR_IMPROVEMENT"]), "IMPROVED")
        self.assertEqual(workload_verdict(["NO_CLEAR_IMPROVEMENT"]), "NO_CLEAR_IMPROVEMENT")
        with self.assertRaises(ValueError):
            workload_verdict(["IMPROVEMENT"])

    def test_rewrite_outcome_detects_a_no_op_optimize(self) -> None:
        noop = rewrite_outcome([{"path": "p", "metrics": {"numFilesAdded": 0, "numFilesRemoved": 0,
                                                          "totalConsideredFiles": 151, "totalFilesSkipped": 151}}])
        self.assertTrue(noop["no_op"])
        rewrite = rewrite_outcome([{"metrics": {"numFilesAdded": 151, "numFilesRemoved": 151,
                                                "filesAdded": {"totalSize": 10}, "filesRemoved": {"totalSize": 12},
                                                "partitionsOptimized": 151}}])
        self.assertFalse(rewrite["no_op"])
        self.assertEqual((rewrite["bytes_added"], rewrite["bytes_removed"]), (10, 12))

    def test_recommendation_attributes_each_effect_to_its_own_arm(self) -> None:
        noop = recommend_layout(overall_verdict="IMPROVED", compaction_verdict="NO_CLEAR_IMPROVEMENT",
                                zorder_incremental_verdict="IMPROVED", compaction_no_op=True, zorder_no_op=False)
        self.assertTrue(noop["compaction"].startswith("NOT_NEEDED"))
        self.assertEqual(noop["zorder"], "KEEP")
        mixed = recommend_layout(overall_verdict="REGRESSED", compaction_verdict="IMPROVED",
                                 zorder_incremental_verdict="IMPROVED", compaction_no_op=False, zorder_no_op=False)
        self.assertEqual(mixed["compaction"], "KEEP")
        self.assertTrue(mixed["zorder"].startswith("DO_NOT_ADOPT"))

    def test_typed_spark_settings_are_read_without_a_fallback(self) -> None:
        class Conf:
            def get(self, key, *default):
                if default:
                    raise AssertionError("a typed fallback raises on Spark 3.5")
                if key == "plain.unset":
                    raise RuntimeError("java.util.NoSuchElementException")
                return "false"

        spark = SimpleNamespace(conf=Conf())
        self.assertEqual(effective_conf(spark, "spark.sql.adaptive.enabled"), "false")
        self.assertTrue(effective_conf(spark, "plain.unset").startswith("<unset"))

    def test_scan_metrics_report_unavailability_instead_of_guessing(self) -> None:
        class Broken:
            @property
            def _jdf(self):
                raise RuntimeError("no plan")

        self.assertEqual(scan_metrics(Broken())["available"], False)

    def test_job_never_vacuums_or_writes_outside_create_only_uploads(self) -> None:
        source = (ROOT / "ops/gcp/storage_benchmark_job.py").read_text(encoding="utf-8")
        self.assertNotIn(".vacuum(", source)
        self.assertNotRegex(source, r"(?i)['\"]VACUUM ")
        self.assertIn("if_generation_match=0", source)

    def test_representative_query_must_match_rows(self) -> None:
        query = {"name": "merchant_lookup", "require_non_empty": True}
        with self.assertRaisesRegex(ValueError, "matched no rows"):
            _assert_representative_result(query, [{"row_count": 0}])
        _assert_representative_result(query, [{"row_count": 1}])

    def test_required_columns_come_from_explicit_table_contracts(self) -> None:
        self.assertIn("status", REQUIRED_COLUMNS["transactions"])
        self.assertIn("amount", REQUIRED_COLUMNS["fact_transactions"])
        self.assertIn("new_balance", REQUIRED_COLUMNS["obt_transaction_enriched"])
        self.assertIn("user_amount_sum_1h", REQUIRED_COLUMNS["feat_user_behavior"])

    def test_capabilities_match_pinned_runtime(self) -> None:
        self.assertEqual(PINNED_CAPABILITIES["managed_spark_runtime"], "2.2")
        self.assertEqual(PINNED_CAPABILITIES["delta"], "3.2.0")
        self.assertTrue(PINNED_CAPABILITIES["zorder"])
        self.assertFalse(PINNED_CAPABILITIES["liquid_clustering_in_scope"])

    def test_output_paths_are_isolated_from_source_and_canonical_runs(self) -> None:
        validate_benchmark_paths(
            source_uri="gs://bucket/lakehouse/runs/ab-1-optimized-gold/gold/fact_transactions",
            benchmark_root="gs://bucket/lakehouse/benchmarks/storage/storage-ab-1",
            baseline_uri="gs://bucket/lakehouse/benchmarks/storage/storage-ab-1/baseline/dp2-gold/fact_transactions",
            optimized_uri="gs://bucket/lakehouse/benchmarks/storage/storage-ab-1/optimized/dp2-gold/fact_transactions",
            compacted_uri="gs://bucket/lakehouse/benchmarks/storage/storage-ab-1/compacted/dp2-gold/fact_transactions",
        )
        with self.assertRaises(ValueError):
            validate_benchmark_paths(
                source_uri="gs://bucket/lakehouse/runs/source/gold/fact_transactions",
                benchmark_root="gs://bucket/lakehouse/runs/source",
                baseline_uri="gs://bucket/lakehouse/runs/source/baseline",
                optimized_uri="gs://bucket/lakehouse/runs/source/optimized",
            )

    def test_equivalence_requires_schema_rows_grain_and_fingerprint(self) -> None:
        before = {"schema_sha256": "schema", "row_count": 10, "grain_unique": True, "business_invariants": True,
                  "content_fingerprint": "content"}
        self.assertTrue(compare_equivalence(before, dict(before))["equivalent"])
        self.assertFalse(compare_equivalence(before, dict(before, content_fingerprint="other"))["equivalent"])

    def test_layout_comparison_uses_samples_not_a_single_number(self) -> None:
        result = compare_layouts(
            before={"file_count": 151, "median_file_size": 1, "runtime_samples_seconds": [10, 10.5, 11]},
            after={"file_count": 151, "median_file_size": 1, "runtime_samples_seconds": [12, 12.5, 13]},
        )
        self.assertEqual(result["file_count_change"], 0)
        self.assertEqual(result["verdict"], "REGRESSED")


class SubmitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = CloudRuntime(
            project_id="project",
            region="asia-southeast1",
            data_bucket="data-bucket",
            temporary_bucket="temp-bucket",
            subnet="projects/project/regions/asia-southeast1/subnetworks/subnet",
            service_account_email="spark@project.iam.gserviceaccount.com",
            performance_tier="standard",
        )

    def test_command_is_standard_runtime_22_and_uses_isolated_root(self) -> None:
        command = build_submission_command(
            self.runtime,
            benchmark_id="storage-ab-20261025",
            dataset_version="canonical-100gb-v1",
            source_run_ids=("ab-20261025112811-optimized-gold-r261025120000",),
            git_sha="7e6f050028706a2a930a733771d97e04f1df2864",
            plan_path=Path("layout-plan.json"),
            jar_paths=(Path("delta.jar"), Path("storage.jar")),
            archive_path=Path("python.zip"),
        )
        rendered = " ".join(command)
        self.assertIn("--version=2.2", command)
        self.assertIn("dataproc.tier=standard", rendered)
        self.assertIn(
            "gs://data-bucket/lakehouse/benchmarks/storage/storage-ab-20261025",
            rendered,
        )
        self.assertNotIn("/lakehouse/runs/storage-ab-20261025", rendered)

    def test_dry_run_never_invokes_runner(self) -> None:
        root = f"gs://{BUCKET}/lakehouse/benchmarks/storage/storage-5gb-20261112"
        plan = build_plan(benchmark_id="storage-5gb-20261112", binding=binding(),
                          tool_git_sha="7e6f050028706a2a930a733771d97e04f1df2864", data_bucket=BUCKET,
                          layouts=(gold_layout(),), parameters={"dp2-gold.fact_transactions": PARAMETERS})
        self.assertEqual(plan["benchmark_root"], root)
        terraform = {
            "project_id": {"value": "project"},
            "region": {"value": "asia-southeast1"},
            "zone": {"value": "asia-southeast1-b"},
            "data_bucket_name": {"value": "data-bucket"},
            "temporary_bucket_name": {"value": "temp-bucket"},
            "dataproc_subnet_uri": {"value": "projects/project/regions/asia-southeast1/subnetworks/subnet"},
            "service_account_emails": {
                "value": {"spark": "spark@project.iam.gserviceaccount.com"}
            },
            "dataproc_batch_runtime": {
                "value": {
                    "project_id": "project",
                    "region": "asia-southeast1",
                    "staging_bucket": "temp-bucket",
                    "service_account_email": "spark@project.iam.gserviceaccount.com",
                    "execution_mode": "serverless-batch",
                    "performance_tier": "standard",
                    "subnet": "projects/project/regions/asia-southeast1/subnetworks/subnet",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            tf_path = Path(directory) / "terraform.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            tf_path.write_text(json.dumps(terraform), encoding="utf-8")
            calls = []
            def clean_git(command, **_):
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return SimpleNamespace(stdout=plan["git_sha"] + "\n")
                if "ls-files" in command:
                    candidates = sorted(
                        path.relative_to(Path.cwd()).as_posix()
                        for root in (Path.cwd() / "data_platform", Path.cwd() / "ops")
                        for path in root.rglob("*.py")
                    )
                    return SimpleNamespace(stdout="\n".join(candidates) + "\n")
                return SimpleNamespace(stdout="")

            rc = main(
                ["--plan", str(plan_path), "--terraform-output-json", str(tf_path)],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                git_runner=clean_git,
            )
            broken = dict(plan, schema_version=1)
            plan_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema"):
                main(["--plan", str(plan_path), "--terraform-output-json", str(tf_path)],
                     runner=lambda *args, **kwargs: calls.append((args, kwargs)), git_runner=clean_git)
        self.assertEqual(rc, 0)
        # Terraform names the canonical bucket; the rehearsal dataset still resolves its own bucket.
        self.assertEqual(calls, [])

    def test_tool_git_state_rejects_sha_drift_and_tracked_changes(self) -> None:
        expected = "7e6f050028706a2a930a733771d97e04f1df2864"

        def wrong_sha(command, **_):
            return SimpleNamespace(stdout="8e6f050028706a2a930a733771d97e04f1df2865\n")

        with self.assertRaisesRegex(ValueError, "SHA differs"):
            _assert_tool_git_state(expected, wrong_sha)

        def dirty(command, **_):
            return SimpleNamespace(
                stdout=expected + "\n"
                if command[-2:] == ["rev-parse", "HEAD"]
                else " M ops/gcp/storage_benchmark_job.py\n"
            )

        with self.assertRaisesRegex(ValueError, "tracked worktree"):
            _assert_tool_git_state(expected, dirty)

        def package_outside_git(command, **_):
            if command[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(stdout=expected + "\n")
            if "ls-files" in command:
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout="")

        with self.assertRaisesRegex(ValueError, "outside Git"):
            _assert_tool_git_state(expected, package_outside_git)


if __name__ == "__main__":
    unittest.main()
