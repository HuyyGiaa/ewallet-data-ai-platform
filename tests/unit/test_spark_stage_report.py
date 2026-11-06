"""Stage metrics, skew flags and controlled comparison from Spark event logs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ops.gcp.spark_stage_report import build_report, compare_reports, read_events


def task(stage, duration, *, failed=False, shuffle_read=0, shuffle_write=0, spill=0, run_time=None):
    return {
        "Event": "SparkListenerTaskEnd",
        "Stage ID": stage,
        "Task Info": {"Launch Time": 1000, "Finish Time": 1000 + duration, "Failed": failed},
        "Task Metrics": {
            "Executor Run Time": duration if run_time is None else run_time,
            "Memory Bytes Spilled": spill,
            "Disk Bytes Spilled": spill // 2,
            "Shuffle Read Metrics": {"Remote Bytes Read": shuffle_read, "Local Bytes Read": 1},
            "Shuffle Write Metrics": {"Shuffle Bytes Written": shuffle_write},
            "Input Metrics": {"Bytes Read": 5},
        },
    }


def log(*extra):
    return [
        {"Event": "SparkListenerApplicationStart", "Timestamp": 0, "App ID": "app-1", "App Name": "gold"},
        {"Event": "SparkListenerExecutorAdded", "Timestamp": 0, "Executor ID": "1",
         "Executor Info": {"Total Cores": 2}},
        *extra,
        {"Event": "SparkListenerStageCompleted",
         "Stage Info": {"Stage ID": 1, "Stage Name": "join", "Submission Time": 0, "Completion Time": 4000}},
        {"Event": "SparkListenerApplicationEnd", "Timestamp": 10_000},
    ]


class ReportTest(unittest.TestCase):
    def test_application_totals_are_summed(self):
        report = build_report(log(task(1, 1000, shuffle_read=9, shuffle_write=3, spill=4),
                                  task(1, 1000, shuffle_read=9, shuffle_write=3, spill=4)))
        self.assertEqual("app-1", report["application_id"])
        self.assertEqual(10.0, report["runtime_seconds"])
        self.assertEqual(2, report["task_count"])
        self.assertEqual(20, report["shuffle_read_bytes"])
        self.assertEqual(6, report["shuffle_write_bytes"])
        self.assertEqual(8, report["memory_spill_bytes"])
        self.assertEqual(4, report["disk_spill_bytes"])
        self.assertEqual(10, report["input_bytes"])

    def test_stage_duration_and_name_come_from_the_stage_event(self):
        stage = build_report(log(task(1, 1000)))["slowest_stages"][0]
        self.assertEqual("join", stage["name"])
        self.assertEqual(4.0, stage["duration_seconds"])

    def test_failed_tasks_are_counted(self):
        self.assertEqual(1, build_report(log(task(1, 10, failed=True)))["failed_task_count"])

    def test_utilization_is_busy_time_over_core_time(self):
        report = build_report(log(task(1, 5000)))
        self.assertEqual(20.0, report["executor_core_seconds"])
        self.assertEqual(0.25, report["executor_utilization"])

    def test_a_removed_executor_stops_counting(self):
        removed = {"Event": "SparkListenerExecutorRemoved", "Timestamp": 5000, "Executor ID": "1"}
        self.assertEqual(10.0, build_report(log(task(1, 5000), removed))["executor_core_seconds"])

    def test_a_long_straggler_is_flagged_as_skew(self):
        report = build_report(log(*[task(1, 5000) for _ in range(9)], task(1, 40_000)))
        self.assertTrue(report["slowest_stages"][0]["skew_flag"])
        self.assertEqual(1, report["skewed_stages"])

    def test_even_tasks_are_not_skewed(self):
        self.assertFalse(build_report(log(*[task(1, 20_000) for _ in range(5)]))["slowest_stages"][0]["skew_flag"])

    def test_a_high_ratio_on_tiny_tasks_is_not_skew(self):
        report = build_report(log(*[task(1, 10) for _ in range(9)], task(1, 500)))
        self.assertFalse(report["slowest_stages"][0]["skew_flag"])

    def test_sql_plans_are_fingerprinted_not_copied(self):
        plan = {"Event": "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart",
                "physicalPlanDescription": "== Physical Plan =="}
        report = build_report(log(plan, plan))
        self.assertEqual(1, len(report["sql_plan_sha256_prefixes"]))
        self.assertNotIn("Physical Plan", json.dumps(report))

    def test_an_incomplete_log_is_rejected(self):
        with self.assertRaises(ValueError):
            build_report([{"Event": "SparkListenerApplicationStart", "Timestamp": 0}])

    def test_plain_logs_are_read_line_by_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.log"
            path.write_text('{"Event": "a"}\n\n{"Event": "b"}\n', encoding="utf-8")
            self.assertEqual(["a", "b"], [item["Event"] for item in read_events(path)])


class ComparisonTest(unittest.TestCase):
    BASE = {"application_id": "a", "runtime_seconds": 100.0, "skewed_stages": 0}
    OPT = {"application_id": "b", "runtime_seconds": 120.0, "skewed_stages": 2}

    def test_a_slower_optimized_run_is_reported_not_hidden(self):
        result = compare_reports(self.BASE, self.OPT, "silver-run")
        self.assertEqual(20.0, result["metrics"]["runtime_seconds"]["change_percent"])
        self.assertNotIn("winner", json.dumps(result))

    def test_the_shared_input_must_be_named(self):
        with self.assertRaises(ValueError):
            compare_reports(self.BASE, self.OPT, "  ")
        self.assertEqual("silver-run", compare_reports(self.BASE, self.OPT, "silver-run")["input_identity"])

    def test_an_application_cannot_be_compared_with_itself(self):
        with self.assertRaises(ValueError):
            compare_reports(self.BASE, self.BASE, "silver-run")

    def test_a_zero_baseline_has_no_percentage(self):
        self.assertIsNone(compare_reports(self.BASE, self.OPT, "x")["metrics"]["skewed_stages"]["change_percent"])


if __name__ == "__main__":
    unittest.main()
