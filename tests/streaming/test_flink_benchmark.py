"""Offline contracts for the Flink baseline/optimized local benchmark."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import yaml

from data_platform.streaming.benchmark.compare import report
from data_platform.streaming.benchmark.dataset import (
    MAX_EVENTS,
    ReplaySpec,
    build_payloads,
    functional_spec,
    performance_spec,
)
from data_platform.streaming.benchmark.outputs import percentile, summarize
from data_platform.streaming.config.settings import (
    BASELINE,
    OPTIMIZED,
    OperatingMode,
    RuntimeSettings,
    load_profile,
    profile_by_name,
)
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.topics import TOPICS, consumer_group, topic_name


ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "infra/docker/flink-benchmark"


def _profile_yaml(directory: Path, **changes) -> Path:
    values = yaml.safe_load((ROOT / "configs/streaming/flink-optimized.yaml").read_text())
    values.update(changes)
    path = directory / "profile.yaml"
    path.write_text(yaml.safe_dump(values))
    return path


class ProfileLoadingTest(unittest.TestCase):
    def test_yaml_values_are_the_runtime_values(self) -> None:
        self.assertEqual((1, timedelta(seconds=120), False), (BASELINE.parallelism, BASELINE.checkpoint_interval, BASELINE.incremental_checkpoints))
        self.assertEqual((4, timedelta(seconds=30), True), (OPTIMIZED.parallelism, OPTIMIZED.checkpoint_interval, OPTIMIZED.incremental_checkpoints))
        self.assertEqual("rocksdb", BASELINE.state_backend)
        self.assertEqual(BASELINE.state_backend, OPTIMIZED.state_backend)

    def test_profiles_share_semantics_from_block_a_contract(self) -> None:
        contract = yaml.safe_load((ROOT / "configs/streaming/block-a.yaml").read_text())
        for profile in (BASELINE, OPTIMIZED):
            self.assertEqual(contract["event_time"]["allowed_lateness_seconds"], profile.event_time.allowed_lateness.total_seconds())
            self.assertEqual(contract["dedup"]["ttl_seconds"], profile.dedup_ttl.total_seconds())
            self.assertEqual(contract["feature_state"]["history_ttl_seconds"], profile.history_ttl.total_seconds())
        self.assertEqual(
            (BASELINE.event_time, BASELINE.dedup_ttl, BASELINE.history_ttl, BASELINE.checkpoint_mode),
            (OPTIMIZED.event_time, OPTIMIZED.dedup_ttl, OPTIMIZED.history_ttl, OPTIMIZED.checkpoint_mode),
        )
        self.assertNotEqual(BASELINE.config_sha256, OPTIMIZED.config_sha256)

    def test_edited_yaml_changes_loaded_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = load_profile(_profile_yaml(Path(tmp), parallelism=2), repo_root=ROOT)
        self.assertEqual(2, profile.parallelism)

    def test_unsupported_sink_batch_size_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "sink_batch_size is not implemented"):
                load_profile(_profile_yaml(Path(tmp), sink_batch_size=250), repo_root=ROOT)

    def test_incremental_checkpoint_requires_rocksdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "requires the rocksdb"):
                load_profile(_profile_yaml(Path(tmp), state_backend="hashmap"), repo_root=ROOT)
            hashmap = load_profile(
                _profile_yaml(Path(tmp), state_backend="hashmap", incremental_checkpoints=False),
                repo_root=ROOT,
            )
        self.assertFalse(hashmap.incremental_checkpoints)

    def test_unknown_missing_and_mistyped_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for changes in ({"typo_key": 1}, {"state_backend": "memory"}, {"incremental_checkpoints": "yes"}, {"checkpoint_mode": "NONE"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_profile(_profile_yaml(Path(tmp), **changes), repo_root=ROOT)
        with self.assertRaises(ValueError):
            profile_by_name("turbo")

    def test_job_wires_profile_state_backend_and_namespace(self) -> None:
        source = (ROOT / "data_platform/streaming/flink/job.py").read_text()
        body = source[source.index("def build_executable_job"):source.index("def parser")]
        self.assertIn("env.set_state_backend(_state_backend(settings.profile))", body)
        self.assertIn("EmbeddedRocksDBStateBackend(enable_incremental_checkpointing=profile.incremental_checkpoints)", source)
        self.assertNotIn('TOPICS["', body)
        self.assertNotIn("state.backend.incremental", source)
        self.assertIn("settings.profile.restart_attempts", body)


class BenchmarkSettingsTest(unittest.TestCase):
    def settings(self, **changes):
        values = dict(mode=OperatingMode.BENCHMARK_REPLAY, profile=BASELINE, bootstrap_servers="redpanda:29092",
                      kafka_connector_jar=Path("x.jar"), checkpoint_storage="file:///c", namespace="bench-func-baseline-1")
        values.update(changes)
        return RuntimeSettings(**values)

    def test_benchmark_mode_requires_isolated_namespace(self) -> None:
        self.assertEqual("bench-func-baseline-1", self.settings().namespace)
        for namespace in (None, "", "prod", "bench-UPPER", "bench-" + "x" * 60):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                self.settings(namespace=namespace)
        with self.assertRaises(ValueError):
            self.settings(savepoint_path="file:///sp")
        with self.assertRaises(ValueError):
            self.settings(mode=OperatingMode.NORMAL_STREAMING, savepoint_path="file:///sp")

    def test_namespaced_topics_never_equal_production_topics(self) -> None:
        for key, contract in TOPICS.items():
            self.assertEqual(contract.name, topic_name(key))
            self.assertEqual(f"{contract.name}.bench-a", topic_name(key, "bench-a"))
        self.assertTrue(consumer_group("flink", "bench-a").endswith(".bench-a"))


class ReplayDatasetTest(unittest.TestCase):
    def test_functional_replay_is_deterministic_with_exact_targets(self) -> None:
        spec = functional_spec()
        records, manifest = build_payloads(spec)
        again, manifest_again = build_payloads(spec)
        self.assertEqual(records, again)
        self.assertEqual(manifest, manifest_again)
        self.assertEqual(round(600 * 0.12), manifest["late_arrival_transactions"])
        self.assertEqual(round(600 * 0.015), manifest["duplicate_events"])
        self.assertEqual(3, manifest["invalid_records"])
        self.assertEqual(600 + 9 + 3, manifest["emitted_records"])
        valid = invalid = 0
        for _key, payload in records:
            try:
                TransactionEvent.from_json(payload)
                valid += 1
            except (KeyError, TypeError, ValueError):
                invalid += 1
        self.assertEqual((609, 3), (valid, invalid))

    def test_late_events_exceed_watermark_plus_allowed_lateness(self) -> None:
        records, _ = build_payloads(functional_spec())
        bound = BASELINE.event_time.max_out_of_orderness + BASELINE.event_time.allowed_lateness
        lateness, seen = [], set()
        for _key, payload in records:
            try:
                event = TransactionEvent.from_json(payload)
            except (KeyError, TypeError, ValueError):
                continue
            if event.transaction_id in seen:
                continue  # duplicates re-arrive 1 s later and are deduplicated
            seen.add(event.transaction_id)
            if event.arrival_time > event.event_time:
                lateness.append(event.arrival_time - event.event_time)
        self.assertTrue(lateness)
        self.assertTrue(all(item > bound for item in lateness))

    def test_performance_rates_and_bounds(self) -> None:
        self.assertEqual(200_000, performance_spec(2000, 100).count)
        for bad in (lambda: performance_spec(3000), lambda: performance_spec(100, 5),
                    lambda: ReplaySpec("performance", MAX_EVENTS + 1, 1, 1, 2, 1)):
            with self.assertRaises(ValueError):
                bad()

    def test_same_spec_same_input_for_both_profiles(self) -> None:
        _, first = build_payloads(performance_spec(100, 10))
        _, second = build_payloads(performance_spec(100, 10))
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])


class OutputSummaryTest(unittest.TestCase):
    def test_summary_checks_dedup_dlq_and_latency(self) -> None:
        manifest = {"emitted_records": 4, "unique_transactions": 2, "invalid_records": 1, "late_arrival_transactions": 1}
        accepted = lambda tx, disposition: json.dumps({"transaction_id": tx, "disposition": disposition})
        topics = {
            "raw": [("t1", "{}", 1000), ("t1", "{}", 1005), ("t2", "{}", 1010), ("bad", "{}", 1020)],
            "accepted": [("t1", accepted("t1", "ON_TIME"), 1100), ("t2", accepted("t2", "TOO_LATE"), 1300)],
            "late": [("t2", "{}", 1300)], "dlq": [("bad", "{}", 1030)],
            "online": [("u", '{"v":1}', 1400)], "retry": [],
        }
        summary = summarize(topics, manifest)
        self.assertTrue(summary["functional_checks"]["all_passed"], summary["functional_checks"])
        self.assertEqual(100, summary["processing_latency_ms"]["p50"])
        self.assertEqual(290, summary["processing_latency_ms"]["p99"])
        topics["accepted"].append(topics["accepted"][0])
        self.assertFalse(summarize(topics, manifest)["functional_checks"]["dedup_exact_once_per_transaction"])

    def test_percentile_handles_empty(self) -> None:
        self.assertIsNone(percentile([], 50))
        self.assertEqual(3, percentile([1, 2, 3, 4], 75))

    def test_compare_reports_missing_metrics_as_na(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base, opt = Path(tmp, "b"), Path(tmp, "o")
            for run in (base, opt):
                (run / "input").mkdir(parents=True)
                (run / "input/input_manifest.json").write_text(json.dumps({"payload_sha256": "abc"}))
            text = report(base, opt, require_valid=False)
        self.assertIn("Identical logical input (payload SHA-256): **True**", text)
        self.assertIn("| latency_p95_ms | n/a | n/a |", text)


class RunValidityTest(unittest.TestCase):
    def _run(self, root: Path, status: str, passed: bool, completed: int) -> Path:
        run = root / status
        (run / "metrics").mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"status": status, "failures": []}))
        (run / "outputs_summary.json").write_text(json.dumps({"functional_checks": {"all_passed": passed}}))
        (run / "metrics/snapshot_0000.json").write_text(json.dumps({"checkpoints": {"counts": {"completed": completed}}}))
        return run

    def test_compare_rejects_invalid_failed_or_checkpointless_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = self._run(Path(tmp), "complete", True, 1)
            report(good, good)
            for bad in (self._run(Path(tmp), "invalid", True, 1),
                        self._run(Path(tmp) / "f", "complete", False, 1),
                        self._run(Path(tmp) / "c", "complete", True, 0)):
                with self.subTest(run=bad), self.assertRaisesRegex(ValueError, "invalid run"):
                    report(good, bad)

    def test_driver_no_longer_swallows_failures(self) -> None:
        script = (ROOT / "scripts/streaming/flink_bench.sh").read_text()
        body = script[script.index("run_benchmark() {"):script.index("finalize_run() {")]
        self.assertNotIn('|| echo "WARN', body)
        for reason in ("outputs_not_quiesced", "no_completed_checkpoint", "functional_checks_failed"):
            self.assertIn(f'failures+=("{reason}")', script)
        self.assertIn('finalize_run "$run"', script)


class EnvironmentContractTest(unittest.TestCase):
    def test_compose_isolated_and_sized_for_parallelism_4(self) -> None:
        compose = yaml.safe_load((BENCH / "compose.yml").read_text())
        services = compose["services"]
        self.assertTrue({"redpanda", "jobmanager", "taskmanager", "generator", "sink-reader"} <= set(services))
        self.assertIn("taskmanager.numberOfTaskSlots: 4", services["jobmanager"]["environment"]["FLINK_PROPERTIES"])
        self.assertGreaterEqual(TOPICS["raw"].partitions, 4)
        for service in services.values():
            for port in service.get("ports", []):
                self.assertTrue(port.startswith("127.0.0.1:"), port)
            for volume in service.get("volumes", []):
                self.assertFalse(re.match(r"^(/|[A-Za-z]:)", volume), f"absolute host path: {volume}")
            self.assertIn("mem_limit", service)
        self.assertIn("@sha256:", services["redpanda"]["image"])
        self.assertIn("auto_create_topics_enabled=false", " ".join(services["redpanda"]["command"]))

    def test_dockerfile_installs_only_from_committed_lock(self) -> None:
        dockerfile = (BENCH / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r"FROM flink:1\.19\.1-scala_2\.12-java11@sha256:[0-9a-f]{64}")
        self.assertRegex(dockerfile, r"astral-sh/uv:0\.13\.5@sha256:[0-9a-f]{64}")
        self.assertIn("COPY infra/docker/flink-benchmark/pyproject.toml infra/docker/flink-benchmark/uv.lock ./", dockerfile)
        self.assertIn("uv sync --locked --no-dev --extra flink", dockerfile)
        self.assertIn("flink-sql-connector-kafka-3.3.0-1.19.jar", dockerfile)
        for path in (BENCH / "Dockerfile", ROOT / "scripts/streaming/flink_bench.sh"):
            self.assertNotRegex(path.read_text(), r"pip3? install", str(path))

    def test_uv_project_pins_pyflink_matching_connector_and_python(self) -> None:
        project = (BENCH / "pyproject.toml").read_text()
        self.assertIn('flink = [\n  "apache-flink==1.19.1",\n]', project)
        self.assertIn('requires-python = ">=3.10,<3.12"', project)
        lock = (BENCH / "uv.lock").read_text()
        self.assertIn('name = "apache-flink"\nversion = "1.19.1"', lock)
        jar = ROOT / "data_platform/processing/flink/jars/flink-sql-connector-kafka-3.3.0-1.19.jar"
        self.assertTrue(jar.is_file())


if __name__ == "__main__":
    unittest.main()
