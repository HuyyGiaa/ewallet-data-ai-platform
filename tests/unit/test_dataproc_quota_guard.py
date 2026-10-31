from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from data_platform.config.spark_profiles import quota_requirements
from ops.gcp.dataproc_quota import (
    GcloudQuotaReader,
    PollingPolicy,
    PredecessorBatch,
    QuotaSnapshot,
    QuotaTimeoutError,
    parse_quota_documents,
    wait_for_quota,
)
from ops.gcp.wait_for_dataproc_quota import main


UTC = timezone.utc


def snapshot(*, cpu_available: float, disk_available: float) -> QuotaSnapshot:
    return QuotaSnapshot(
        checked_at=datetime(2026, 10, 25, 13, 8, tzinfo=UTC),
        cpu_limit=32,
        cpu_usage=32 - cpu_available,
        disk_limit_gb=4096,
        disk_usage_gb=4096 - disk_available,
    )


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds

    def now(self) -> datetime:
        return datetime(2026, 10, 25, 13, 8, tzinfo=UTC)


class DataprocQuotaGuardTest(unittest.TestCase):
    def test_requirement_is_derived_from_shared_profile_for_both_ab_profiles(self) -> None:
        baseline = quota_requirements("baseline")
        optimized = quota_requirements("optimized")
        self.assertEqual(baseline, optimized)
        self.assertEqual(
            {
                "process_count": 7,
                "vcpus": 28,
                "disk_gb": 2800,
            },
            baseline["initial"],
        )
        self.assertEqual(
            {
                "process_count": 8,
                "vcpus": 32,
                "disk_gb": 3200,
            },
            baseline["maximum"],
        )

    def test_quota_already_available_is_immediately_ready(self) -> None:
        clock = FakeClock()
        result = wait_for_quota(
            project="example-project",
            region="asia-southeast1",
            requirement=quota_requirements("baseline"),
            read_snapshot=lambda: snapshot(cpu_available=32, disk_available=4096),
            policy=PollingPolicy(initial_interval_seconds=15, max_interval_seconds=30, timeout_seconds=900),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            now=clock.now,
        )
        self.assertEqual("READY", result["status"])
        self.assertEqual(1, result["poll_count"])
        self.assertEqual(0.0, result["wait_seconds"])

    def test_each_insufficient_quota_state_polls_until_ready(self) -> None:
        for first in (
            snapshot(cpu_available=0, disk_available=4096),
            snapshot(cpu_available=32, disk_available=896),
            snapshot(cpu_available=0, disk_available=896),
        ):
            with self.subTest(first=first):
                clock = FakeClock()
                values = iter((first, snapshot(cpu_available=32, disk_available=4096)))
                result = wait_for_quota(
                    project="example-project",
                    region="asia-southeast1",
                    requirement=quota_requirements("optimized"),
                    read_snapshot=lambda: next(values),
                    policy=PollingPolicy(15, 30, 900),
                    sleeper=clock.sleep,
                    monotonic=clock.monotonic,
                    now=clock.now,
                )
                self.assertEqual("READY", result["status"])
                self.assertEqual(2, result["poll_count"])
                self.assertEqual(15.0, result["wait_seconds"])

    def test_partial_release_continues_until_both_resources_are_ready(self) -> None:
        clock = FakeClock()
        reported: list[tuple[int, float]] = []
        values = iter(
            (
                snapshot(cpu_available=0, disk_available=896),
                snapshot(cpu_available=32, disk_available=896),
                snapshot(cpu_available=32, disk_available=4096),
            )
        )
        result = wait_for_quota(
            project="example-project",
            region="asia-southeast1",
            requirement=quota_requirements("baseline"),
            read_snapshot=lambda: next(values),
            policy=PollingPolicy(15, 30, 900),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            now=clock.now,
            on_observation=lambda _snapshot, poll, elapsed: reported.append((poll, elapsed)),
        )
        self.assertEqual(3, result["poll_count"])
        self.assertEqual(37.5, result["wait_seconds"])
        self.assertEqual([(1, 0.0), (2, 15.0), (3, 37.5)], reported)

    def test_timeout_is_explicit_and_keeps_last_observation(self) -> None:
        clock = FakeClock()
        with self.assertRaises(QuotaTimeoutError) as raised:
            wait_for_quota(
                project="example-project",
                region="asia-southeast1",
                requirement=quota_requirements("baseline"),
                read_snapshot=lambda: snapshot(cpu_available=0, disk_available=896),
                policy=PollingPolicy(15, 30, 20),
                sleeper=clock.sleep,
                monotonic=clock.monotonic,
                now=clock.now,
            )
        result = raised.exception.evidence
        self.assertEqual("TIMEOUT", result["status"])
        self.assertEqual(0, result["observed"]["cpu_available"])
        self.assertEqual(896, result["observed"]["disk_available_gb"])
        self.assertGreaterEqual(result["wait_seconds"], 20)

    def test_missing_or_malformed_quota_metric_fails_closed(self) -> None:
        valid_cpu = {"quotas": [{"metric": "CPUS_ALL_REGIONS", "limit": 32, "usage": 0}]}
        valid_disk = {"quotas": [{"metric": "DISKS_TOTAL_GB", "limit": 4096, "usage": 0}]}
        cases = (
            ({"quotas": []}, valid_disk),
            (valid_cpu, {"quotas": []}),
            ({"quotas": [{"metric": "CPUS_ALL_REGIONS", "limit": "bad", "usage": 0}]}, valid_disk),
        )
        for project_payload, region_payload in cases:
            with self.subTest(project_payload=project_payload, region_payload=region_payload):
                with self.assertRaises(ValueError):
                    parse_quota_documents(project_payload, region_payload)

    def test_gcloud_reader_requests_structured_project_and_region_quota(self) -> None:
        commands: list[tuple[str, ...]] = []

        class Completed:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        def runner(command: tuple[str, ...], **_: object) -> Completed:
            commands.append(command)
            metric = "CPUS_ALL_REGIONS" if "project-info" in command else "DISKS_TOTAL_GB"
            limit = 32 if metric == "CPUS_ALL_REGIONS" else 4096
            return Completed(json.dumps({"quotas": [{"metric": metric, "limit": limit, "usage": 0}]}))

        value = GcloudQuotaReader(
            "example-project", "asia-southeast1", runner=runner
        ).snapshot()
        self.assertEqual(32, value.cpu_available)
        self.assertEqual(4096, value.disk_available_gb)
        self.assertEqual("--format=json(quotas)", commands[0][-1])
        self.assertEqual("--format=json(quotas)", commands[1][-1])
        self.assertIn("project-info", commands[0])
        self.assertIn("regions", commands[1])

    def test_release_latency_uses_predecessor_terminal_timestamp(self) -> None:
        clock = FakeClock()
        predecessor = PredecessorBatch(
            batch_id="ab-20261025112811-baseline-silver-r261025122554",
            state="SUCCEEDED",
            terminal_time=datetime(2026, 10, 25, 13, 7, 53, tzinfo=UTC),
        )
        result = wait_for_quota(
            project="example-project",
            region="asia-southeast1",
            requirement=quota_requirements("baseline"),
            read_snapshot=lambda: snapshot(cpu_available=32, disk_available=4096),
            policy=PollingPolicy(15, 30, 900),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
            now=lambda: datetime(2026, 10, 25, 13, 8, 45, tzinfo=UTC),
            predecessor=predecessor,
        )
        self.assertEqual(52.0, result["release_latency_seconds"])
        self.assertEqual(predecessor.batch_id, result["predecessor_batch"]["batch_id"])

    def test_cli_dry_run_does_not_read_cloud_or_write_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "quota.json"
            stdout = io.StringIO()
            with patch("ops.gcp.wait_for_dataproc_quota.GcloudQuotaReader") as reader, patch(
                "sys.stdout", stdout
            ):
                code = main(
                    [
                        "--project", "example-project",
                        "--region", "asia-southeast1",
                        "--spark-profile", "baseline",
                        "--benchmark-id", "ab-20261025123456",
                        "--handoff", "silver-to-gold",
                        "--evidence-output", str(output),
                    ]
                )
            self.assertEqual(0, code)
            reader.assert_not_called()
            self.assertFalse(output.exists())
            payload = json.loads(stdout.getvalue())
            self.assertEqual("DRY_RUN", payload["status"])
            self.assertEqual(28, payload["required"]["initial"]["vcpus"])

    def test_cli_execute_writes_machine_readable_error_when_quota_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "quota.json"
            with patch("ops.gcp.wait_for_dataproc_quota.GcloudQuotaReader") as reader, patch(
                "sys.stderr", io.StringIO()
            ):
                reader.return_value.snapshot.side_effect = ValueError("missing quota metric")
                code = main(
                    [
                        "--project", "example-project",
                        "--region", "asia-southeast1",
                        "--spark-profile", "baseline",
                        "--benchmark-id", "ab-20261025123456",
                        "--handoff", "external-to-bronze",
                        "--evidence-output", str(output),
                        "--execute",
                    ]
                )
            self.assertEqual(1, code)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("ERROR", payload["status"])
            self.assertIn("missing quota metric", payload["error"])


if __name__ == "__main__":
    unittest.main()
