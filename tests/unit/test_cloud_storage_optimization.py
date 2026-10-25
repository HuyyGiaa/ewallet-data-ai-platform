from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from data_platform.storage.cloud_optimization import (
    PINNED_CAPABILITIES,
    TableAccessPattern,
    compare_equivalence,
    compare_layouts,
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
    _load_verified_spark_evidence,
    build_plan,
)
from ops.gcp.storage_benchmark_job import (
    REQUIRED_COLUMNS,
    _assert_representative_result,
    _create_shallow_clone,
    _query_sql,
    _recommend_layout,
    _run_layout_queries,
    _workload_verdict,
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


class SafetyAndComparisonTests(unittest.TestCase):
    def test_plan_binds_selected_table_to_isolated_paths(self) -> None:
        layout = {
            "table": "fact_transactions",
            "stage": "dp2-gold",
            "uri": "gs://bucket/lakehouse/runs/ab-20261025112811-optimized-gold-r261025120000/gold/fact_transactions",
            "delta_version": 0,
            "total_bytes": 2 * 1024**3,
            "file_count": 200,
            "very_small_file_count": 190,
            "file_size_bytes": {"median": MIB},
            "partition_columns": ["event_date"],
            "partition_count": 10,
            "files_per_partition_p95": 20,
            "partition_size_bytes": {
                "minimum": 128 * MIB,
                "median": 200 * MIB,
                "p95": 300 * MIB,
                "maximum": 400 * MIB,
            },
            "small_partition_count": 0,
            "statistics_columns": ["merchant_id", "event_date"],
            "statistics_profiles": {
                "merchant_id": {
                    "coverage_ratio": 1.0,
                    "distinct_file_minimums": 20,
                    "distinct_file_maximums": 20,
                }
            },
            "global_statistics": {
                "event_date": {"minimum": "2026-01-01", "maximum": "2026-06-01"},
                "merchant_id": {"minimum": "merchant-1", "maximum": "merchant-9"},
            },
            "table_properties": {},
            "inspection_mode": "delta_log_metadata_only",
        }
        plan = build_plan(
            benchmark_id="storage-ab-1",
            dataset_version="canonical-100gb-v1",
            source_spark_git_sha="7e6f050028706a2a930a733771d97e04f1df2864",
            tool_git_sha="8e6f050028706a2a930a733771d97e04f1df2865",
            source_run_ids=("ab-20261025112811-optimized-gold-r261025120000",),
            spark_benchmark_id="ab-20261025112811",
            data_bucket="bucket",
            layouts=(layout,),
        )
        table = plan["tables"][0]
        self.assertTrue(table["selected"])
        self.assertIn("/benchmarks/storage/storage-ab-1/baseline/", table["baseline_uri"])
        self.assertIn("/benchmarks/storage/storage-ab-1/compacted/", table["compacted_uri"])
        self.assertEqual(table["optimization"]["columns"], ["merchant_id"])

    def test_plan_rejects_wrong_stage_or_table_source_uri(self) -> None:
        layout = {
            "table": "fact_transactions",
            "stage": "dp2-gold",
            "uri": "gs://bucket/lakehouse/runs/ab-20261025112811-optimized-silver-r261025120000/gold/fact_transactions",
            "delta_version": 0,
            "total_bytes": 2 * 1024**3,
            "file_count": 100,
            "very_small_file_count": 100,
            "file_size_bytes": {"median": MIB},
            "partition_columns": [],
            "partition_count": 1,
            "files_per_partition_p95": 100,
            "partition_size_bytes": {"median": 2 * 1024**3, "maximum": 2 * 1024**3},
            "small_partition_count": 0,
            "statistics_columns": [],
            "statistics_profiles": {},
            "global_statistics": {},
            "table_properties": {},
            "inspection_mode": "delta_log_metadata_only",
        }
        with self.assertRaisesRegex(ValueError, "exactly bound"):
            build_plan(
                benchmark_id="storage-ab-1",
                dataset_version="canonical-100gb-v1",
                source_spark_git_sha="7e6f050028706a2a930a733771d97e04f1df2864",
                tool_git_sha="8e6f050028706a2a930a733771d97e04f1df2865",
                source_run_ids=(
                    "ab-20261025112811-optimized-silver-r261025120000",
                ),
                spark_benchmark_id="ab-20261025112811",
                data_bucket="bucket",
                layouts=(layout,),
            )

    def test_queries_use_decimal_math_lowercase_status_and_zorder_predicate(self) -> None:
        table = {
            "table": "fact_transactions",
            "global_statistics": {
                "event_date": {"maximum": "2026-06-10"},
                "merchant_id": {"minimum": "merchant-0001"},
            },
        }
        date_sql = _query_sql(
            table, {"class": "date_range"}, relation="baseline_table"
        )
        merchant_sql = _query_sql(
            table, {"class": "date_entity_lookup"}, relation="optimized_table"
        )
        failed_sql = _query_sql(
            table, {"class": "date_status_aggregation"}, relation="baseline_table"
        )
        self.assertIn("DECIMAL(38,6)", date_sql)
        self.assertNotIn("DOUBLE", date_sql)
        self.assertIn("merchant_id = 'merchant-0001'", merchant_sql)
        self.assertIn("status = 'failed'", failed_sql)

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

        table = {
            "table": "fact_transactions",
            "global_statistics": {
                "event_date": {"maximum": "2026-06-10"},
            },
            "queries": [
                {
                    "name": "bounded_date_aggregation",
                    "class": "date_range",
                    "require_non_empty": True,
                }
            ],
        }
        import ops.gcp.storage_benchmark_job as job

        original = job._execute_query
        job._execute_query = lambda *_: ([{"row_count": 1}], 0.1, "plan")
        try:
            results = _run_layout_queries(
                Spark(),
                table,
                layout_uris={
                    "baseline": "gs://bucket/baseline",
                    "compacted": "gs://bucket/compacted",
                    "optimized": "gs://bucket/optimized",
                },
            )
        finally:
            job._execute_query = original
        self.assertEqual(set(results), {"baseline", "compacted", "optimized"})
        self.assertEqual(len(results["compacted"][0]["runtime_samples_seconds"]), 5)
        self.assertEqual(results["baseline"][0]["result_json"], results["optimized"][0]["result_json"])

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
            _create_shallow_clone(
                spark,
                "gs://bucket/source",
                "gs://bucket/target",
                source_version=17,
            )
        finally:
            job._path_exists = original
        self.assertIn("VERSION AS OF 17", spark.statements[0])

    def test_workload_verdict_rejects_any_material_query_regression(self) -> None:
        comparisons = [
            {
                "before_runtime_seconds": 10,
                "after_runtime_seconds": 5,
                "runtime_change_percent": -50,
            },
            {
                "before_runtime_seconds": 10,
                "after_runtime_seconds": 11,
                "runtime_change_percent": 10,
            },
        ]
        self.assertEqual(_workload_verdict(comparisons), "REGRESSION")

    def test_layout_recommendation_requires_fair_ablation_benefit(self) -> None:
        self.assertEqual(
            _recommend_layout(
                overall_verdict="REGRESSION",
                compaction_verdict="IMPROVEMENT",
                zorder_incremental_verdict="IMPROVEMENT",
            ),
            "KEEP_COMPACTION_ONLY",
        )
        self.assertEqual(
            _recommend_layout(
                overall_verdict="IMPROVEMENT",
                compaction_verdict="REGRESSION",
                zorder_incremental_verdict="REGRESSION",
            ),
            "DO_NOT_ADOPT",
        )
        self.assertEqual(
            _recommend_layout(
                overall_verdict="IMPROVEMENT",
                compaction_verdict="IMPROVEMENT",
                zorder_incremental_verdict="IMPROVEMENT",
            ),
            "KEEP_ZORDER",
        )

    def test_representative_query_must_match_rows(self) -> None:
        query = {"name": "merchant_lookup", "require_non_empty": True}
        with self.assertRaisesRegex(ValueError, "matched no rows"):
            _assert_representative_result(query, [{"row_count": 0}])
        _assert_representative_result(query, [{"row_count": 1}])

    def test_spark_evidence_binding_requires_complete_equivalent_run(self) -> None:
        benchmark = "ab-20261025112811"
        git_sha = "7e6f050028706a2a930a733771d97e04f1df2864"
        stages = ("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "optimized").mkdir()
            (root / "comparison.json").write_text(
                json.dumps(
                    {
                        "benchmark_id": benchmark,
                        "dataset_version": "canonical-100gb-v1",
                        "git_sha": git_sha,
                        "stages": [
                            {"stage": stage, "output_equivalent": True}
                            for stage in stages
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for stage, suffix in (
                ("dp2-silver", "silver-r261025120000"),
                ("dp2-gold", "gold-r261025120001"),
                ("dp3-features", "feat-r261025120002"),
            ):
                (root / "optimized" / f"{stage}.json").write_text(
                    json.dumps(
                        {
                            "benchmark_id": benchmark,
                            "dataset_version": "canonical-100gb-v1",
                            "git_sha": git_sha,
                            "spark_profile": "optimized",
                            "stage": stage,
                            "batch_state": "SUCCEEDED",
                            "output_run_ids": [f"{benchmark}-optimized-{suffix}"],
                        }
                    ),
                    encoding="utf-8",
                )
            actual_benchmark, run_by_stage = _load_verified_spark_evidence(
                root,
                dataset_version="canonical-100gb-v1",
                git_sha=git_sha,
            )
        self.assertEqual(actual_benchmark, benchmark)
        self.assertEqual(
            run_by_stage["dp2-gold"], f"{benchmark}-optimized-gold-r261025120001"
        )

    def test_required_columns_come_from_explicit_table_contracts(self) -> None:
        self.assertIn("status", REQUIRED_COLUMNS["transactions"])
        self.assertIn("amount", REQUIRED_COLUMNS["fact_transactions"])
        self.assertIn("new_balance", REQUIRED_COLUMNS["obt_transaction_enriched"])
        self.assertIn("user_amount_sum_1h", REQUIRED_COLUMNS["feat_user_behavior"])

    def test_capabilities_match_pinned_runtime(self) -> None:
        self.assertEqual(PINNED_CAPABILITIES["managed_spark_runtime"], "2.2")
        self.assertEqual(PINNED_CAPABILITIES["spark"], "3.5.3")
        self.assertEqual(PINNED_CAPABILITIES["delta"], "3.2.0")
        self.assertTrue(PINNED_CAPABILITIES["optimize"])
        self.assertTrue(PINNED_CAPABILITIES["zorder"])
        self.assertTrue(PINNED_CAPABILITIES["data_skipping"])
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
        before = {
            "schema_sha256": "schema",
            "row_count": 10,
            "grain_unique": True,
            "business_invariants": True,
            "content_fingerprint": "content",
        }
        self.assertTrue(compare_equivalence(before, dict(before))["equivalent"])
        changed = dict(before, content_fingerprint="other")
        self.assertFalse(compare_equivalence(before, changed)["equivalent"])

    def test_comparison_math_reports_regression_honestly(self) -> None:
        result = compare_layouts(
            before={"file_count": 100, "median_file_size": 1, "runtime_seconds": 10},
            after={"file_count": 10, "median_file_size": 8, "runtime_seconds": 12},
        )
        self.assertEqual(result["runtime_change_percent"], 20.0)
        self.assertEqual(result["verdict"], "REGRESSION")


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
        plan = {
            "benchmark_id": "storage-ab-20261025",
            "dataset_version": "canonical-100gb-v1",
            "git_sha": "7e6f050028706a2a930a733771d97e04f1df2864",
            "source_spark_git_sha": "6e6f050028706a2a930a733771d97e04f1df2863",
            "source_run_ids": ["ab-20261025112811-optimized-gold-r261025120000"],
            "spark_benchmark_id": "ab-20261025112811",
            "benchmark_root": "gs://data-bucket/lakehouse/benchmarks/storage/storage-ab-20261025",
            "tables": [
                {
                    "table": "fact_transactions",
                    "selected": True,
                    "uri": "gs://data-bucket/lakehouse/runs/ab-20261025112811-optimized-gold-r261025120000/gold/fact_transactions",
                    "baseline_uri": "gs://data-bucket/lakehouse/benchmarks/storage/storage-ab-20261025/baseline/dp2-gold/fact_transactions",
                    "optimized_uri": "gs://data-bucket/lakehouse/benchmarks/storage/storage-ab-20261025/optimized/dp2-gold/fact_transactions",
                    "stage": "dp2-gold",
                    "delta_version": 0,
                    "file_count": 100,
                    "total_bytes": 1000,
                    "file_size_bytes": {"median": 10, "p95": 20},
                    "partition_decision": {"action": "KEEP"},
                    "optimization": {"action": "COMPACTION_ONLY", "columns": []},
                    "estimated_rewrite_bytes": 1000,
                }
            ],
        }
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
        self.assertEqual(rc, 0)
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
