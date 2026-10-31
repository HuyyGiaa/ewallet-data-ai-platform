from __future__ import annotations

import shutil
import tempfile
import unittest
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import ops.gcp.generate_source_dataset as source_cli
from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.planner import build_shard_plan


class RecordingPublisher:
    def __init__(self, completed: set[int] | None = None) -> None:
        self.completed = completed or set()
        self.published: list[int] = []

    def completed_shard(self, shard_id: int):
        return object() if shard_id in self.completed else None

    def publish_shard(self, local_directory, manifest, *, cleanup_local):
        self.published.append(manifest.shard_id)
        if cleanup_local:
            shutil.rmtree(local_directory, ignore_errors=True)


class ImmediateExecutor:
    instances: list["ImmediateExecutor"] = []

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.submitted: list[int] = []
        self.shutdown_cancel_futures = False
        self.active = 0
        self.max_active = 0
        self.__class__.instances.append(self)

    def submit(self, function, base_config, plan, shard_id, staging_root):
        self.submitted.append(shard_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        future = CountingFuture(self)
        try:
            future.set_result(function(base_config, plan, shard_id, staging_root))
        except Exception as exc:  # pragma: no cover - exercised by failure test
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdown_cancel_futures = cancel_futures

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.shutdown(cancel_futures=exc_type is not None)


class FakeManifest:
    def __init__(self, shard_id: int) -> None:
        self.shard_id = shard_id


class CountingFuture(Future):
    def __init__(self, executor: ImmediateExecutor) -> None:
        super().__init__()
        self.executor = executor
        self.consumed = False

    def result(self, timeout=None):
        try:
            return super().result(timeout=timeout)
        finally:
            if not self.consumed:
                self.executor.active -= 1
                self.consumed = True


class ParallelSourceGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        ImmediateExecutor.instances.clear()

    def test_cli_defaults_to_one_worker_and_accepts_bounded_workers(self) -> None:
        required = [
            "--config", "config/settings.yaml",
            "--dataset-version", "parallel-test-v1",
            "--global-seed", "7001",
            "--shard-count", "4",
            "--users-per-shard", "30",
            "--merchants-per-shard", "8",
            "--transactions-per-shard", "600",
        ]
        self.assertEqual(1, source_cli.parse_args(required).workers)
        self.assertEqual(
            4,
            source_cli.parse_args([*required, "--workers", "4"]).workers,
        )

    def test_main_process_skips_completed_shards_before_scheduling(self) -> None:
        publisher = RecordingPublisher(completed={1, 3})
        stdout = StringIO()

        with redirect_stdout(stdout):
            missing = source_cli.find_missing_shards(
                publisher, range(5), stdout=stdout
            )

        self.assertEqual([0, 2, 4], missing)
        self.assertIn("shard 00001: verified remote COMPLETE; skip", stdout.getvalue())
        self.assertIn("shard 00003: verified remote COMPLETE; skip", stdout.getvalue())

    def test_workers_one_uses_exact_sequential_path(self) -> None:
        publisher = RecordingPublisher()
        calls: list[int] = []

        def fake_worker(base_config, plan, shard_id, staging_root):
            calls.append(shard_id)
            directory = Path(staging_root) / f"shard-{shard_id:05d}"
            directory.mkdir(parents=True)
            return FakeManifest(shard_id)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(source_cli, "_generate_shard_worker", fake_worker):
                source_cli.generate_and_publish_shards(
                    base_config={},
                    plan=object(),
                    shard_ids=[0, 2, 4],
                    staging_root=Path(temporary_directory),
                    publisher=publisher,
                    workers=1,
                    stdout=StringIO(),
                    executor_factory=lambda **_: self.fail(
                        "workers=1 must not create an executor"
                    ),
                )

        self.assertEqual([0, 2, 4], calls)
        self.assertEqual([0, 2, 4], publisher.published)

    def test_parallel_scheduling_is_unique_ordered_and_bounded(self) -> None:
        publisher = RecordingPublisher()

        def fake_worker(base_config, plan, shard_id, staging_root):
            directory = Path(staging_root) / f"shard-{shard_id:05d}"
            directory.mkdir(parents=True)
            return FakeManifest(shard_id)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(source_cli, "_generate_shard_worker", fake_worker):
                source_cli.generate_and_publish_shards(
                    base_config={},
                    plan=object(),
                    shard_ids=[0, 1, 2, 3, 4],
                    staging_root=Path(temporary_directory),
                    publisher=publisher,
                    workers=2,
                    stdout=StringIO(),
                    executor_factory=ImmediateExecutor,
                )

        executor = ImmediateExecutor.instances[0]
        self.assertEqual(2, executor.max_workers)
        self.assertLessEqual(executor.max_active, 2)
        self.assertEqual([0, 1, 2, 3, 4], executor.submitted)
        self.assertEqual([0, 1, 2, 3, 4], publisher.published)

    def test_duplicate_shard_ids_are_rejected_before_scheduling(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate shard IDs"):
            source_cli.generate_and_publish_shards(
                base_config={},
                plan=object(),
                shard_ids=[0, 1, 1],
                staging_root=Path("unused"),
                publisher=RecordingPublisher(),
                workers=2,
                stdout=StringIO(),
                executor_factory=ImmediateExecutor,
            )
        self.assertEqual([], ImmediateExecutor.instances)

    def test_worker_failure_stops_publish_and_cancels_queued_work(self) -> None:
        publisher = RecordingPublisher()

        def failing_worker(base_config, plan, shard_id, staging_root):
            if shard_id == 1:
                raise RuntimeError("intentional worker failure")
            directory = Path(staging_root) / f"shard-{shard_id:05d}"
            directory.mkdir(parents=True, exist_ok=True)
            return FakeManifest(shard_id)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(source_cli, "_generate_shard_worker", failing_worker):
                with self.assertRaisesRegex(RuntimeError, "intentional"):
                    source_cli.generate_and_publish_shards(
                        base_config={},
                        plan=object(),
                        shard_ids=[0, 1, 2],
                        staging_root=Path(temporary_directory),
                        publisher=publisher,
                        workers=2,
                        stdout=StringIO(),
                        executor_factory=ImmediateExecutor,
                    )

        self.assertEqual([0], publisher.published)
        self.assertTrue(ImmediateExecutor.instances[0].shutdown_cancel_futures)

    def test_real_processes_preserve_manifests_and_checksums(self) -> None:
        base_config = load_config("config/settings_fraud_dev.yaml")
        plan = build_shard_plan(
            base_config,
            "parallel-parity-v1",
            7001,
            4,
            30,
            8,
            600,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sequential = {
                shard_id: source_cli._generate_shard_worker(
                    base_config, plan, shard_id, root / "sequential"
                ).to_dict()
                for shard_id in range(4)
            }
            with ProcessPoolExecutor(max_workers=2) as executor:
                futures = {
                    shard_id: executor.submit(
                        source_cli._generate_shard_worker,
                        base_config,
                        plan,
                        shard_id,
                        root / "workers-two",
                    )
                    for shard_id in range(4)
                }
                workers_two = {
                    shard_id: future.result().to_dict()
                    for shard_id, future in futures.items()
                }
            with ProcessPoolExecutor(max_workers=4) as executor:
                futures = {
                    shard_id: executor.submit(
                        source_cli._generate_shard_worker,
                        base_config,
                        plan,
                        shard_id,
                        root / "workers-four",
                    )
                    for shard_id in range(4)
                }
                workers_four = {
                    shard_id: future.result().to_dict()
                    for shard_id, future in futures.items()
                }

            for shard_id in range(4):
                for item in sequential[shard_id]["files"]:
                    relative_path = Path(item["relative_path"])
                    expected = root / "sequential" / relative_path
                    self.assertEqual(
                        expected.read_bytes(),
                        (root / "workers-two" / relative_path).read_bytes(),
                    )
                    self.assertEqual(
                        expected.read_bytes(),
                        (root / "workers-four" / relative_path).read_bytes(),
                    )

        self.assertEqual(sequential, workers_two)
        self.assertEqual(sequential, workers_four)


if __name__ == "__main__":
    unittest.main()
