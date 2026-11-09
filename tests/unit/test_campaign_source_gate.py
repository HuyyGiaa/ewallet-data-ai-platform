"""The campaign source gate and the publication guards fail closed."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from data_platform.config.dataset_identity import APPROVED_DATASETS, CANONICAL_100GB, CANONICAL_DATA_BUCKET, REHEARSAL_5GB
from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.generator import generate_shard, physical_tables
from data_platform.generation.src.sharded.manifest import DatasetPublisher
from data_platform.generation.src.sharded.planner import build_shard_plan
from ops.gcp.generate_source_dataset import validate_publication_target
from ops.gcp.verify_source_dataset import SourceGateError, check_registry_contract, verify_source_dataset
from tests.unit.test_source_dataset_publisher import FakeObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data_platform/generation/config/settings_merchant_history.yaml"
BUCKET = "ewallet-rehearsal-5gb-data"
PREFIX = f"gs://{BUCKET}/source/{REHEARSAL_5GB}"
MANIFEST_URI = f"{PREFIX}/_control/dataset_manifest.json"
PLAN_URI = f"{PREFIX}/_control/plan.json"
SMALL_IDENTITY = replace(APPROVED_DATASETS[REHEARSAL_5GB], source_shard_count=2, size_tolerance_ratio=None)


def listing(store: FakeObjectStore) -> dict[str, dict[str, object]]:
    return {
        uri.split(f"gs://{BUCKET}/", 1)[1]: {
            "size": len(body),
            "md5_hash": base64.b64encode(hashlib.md5(body).digest()).decode("ascii"),
        }
        for uri, body in store.objects.items()
    }


class SourceGateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = load_config(CONFIG)
        plan = build_shard_plan(config, REHEARSAL_5GB, 7001, 2, 12, 4, 80)
        store = FakeObjectStore()
        publisher = DatasetPublisher(store, PREFIX, plan, physical_tables=physical_tables(config))
        publisher.ensure_plan()
        with tempfile.TemporaryDirectory() as directory:
            for shard in range(2):
                manifest = generate_shard(config, plan, shard, Path(directory))
                publisher.publish_shard(Path(directory) / f"shard-{shard:05d}", manifest, cleanup_local=True)
        cls.manifest = publisher.finalize()
        cls.objects = dict(store.objects)

    def setUp(self):
        self.store = FakeObjectStore()
        self.store.objects.update(self.objects)
        patcher = patch.dict(APPROVED_DATASETS, {REHEARSAL_5GB: SMALL_IDENTITY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def verify(self, **kwargs):
        return verify_source_dataset(bucket=BUCKET, dataset_version=REHEARSAL_5GB, store=self.store,
                                     lister=lambda root: listing(self.store), **kwargs)

    def rewrite_manifest(self, change):
        manifest = json.loads(self.store.objects[MANIFEST_URI])
        change(manifest)
        self.store.objects[MANIFEST_URI] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def rewrite_plan(self, change):
        plan = json.loads(self.store.objects[PLAN_URI])
        change(plan)
        self.store.objects[PLAN_URI] = json.dumps(plan).encode("utf-8")


class SourceGateTest(SourceGateBase):
    def test_a_complete_dataset_passes_and_reports_its_identity(self):
        report = self.verify()
        self.assertEqual("PASS", report["status"])
        self.assertEqual(2, report["shard_count"])
        self.assertEqual(20, report["parquet_objects"])
        self.assertEqual(["merchant_history"], report["source_extensions"])
        self.assertEqual(10, len(report["tables"]))
        self.assertEqual(self.manifest["tables"]["merchant_history"]["rows"], report["merchant_history_rows"])
        approved = hashlib.sha256(self.store.objects[MANIFEST_URI]).hexdigest()
        self.assertEqual(approved, self.verify(expected_manifest_sha256=approved)["manifest_sha256"])

    def test_missing_manifest_is_refused(self):
        del self.store.objects[MANIFEST_URI]
        with self.assertRaisesRegex(SourceGateError, "not finalized"):
            self.verify()

    def test_a_manifest_other_than_the_approved_one_is_refused(self):
        with self.assertRaisesRegex(SourceGateError, "differs from the approved"):
            self.verify(expected_manifest_sha256="0" * 64)

    def test_incomplete_manifest_status_is_refused(self):
        self.rewrite_manifest(lambda m: m.update(status="PARTIAL"))
        with self.assertRaisesRegex(ValueError, "COMPLETE"):
            self.verify()

    def test_missing_merchant_history_extension_is_refused(self):
        def strip(manifest):
            manifest.pop("source_extensions")
        self.rewrite_manifest(strip)
        with self.assertRaisesRegex(ValueError, "must carry the merchant_history"):
            self.verify()

    def test_unexpected_table_inventory_is_refused(self):
        def drop(manifest):
            manifest["tables"].pop("merchant_history")
        self.rewrite_manifest(drop)
        with self.assertRaisesRegex(ValueError, "not exactly ten tables"):
            self.verify()

    def test_wrong_dataset_version_is_refused(self):
        self.rewrite_manifest(lambda m: m.update(dataset_version="rehearsal-5gb-v2"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.verify()

    def test_plan_mismatch_is_refused(self):
        self.rewrite_plan(lambda plan: plan.update(digest="0" * 64))
        with self.assertRaisesRegex(SourceGateError, "plan digest"):
            self.verify()

    def test_wrong_seed_or_generator_config_is_refused(self):
        self.rewrite_plan(lambda plan: plan.update(global_seed=9))
        with self.assertRaisesRegex(SourceGateError, "global seed"):
            self.verify()
        self.setUp()
        self.rewrite_plan(lambda plan: plan.update(base_config_sha256="0" * 64))
        with self.assertRaisesRegex(SourceGateError, "not generated with"):
            self.verify()

    def test_missing_object_or_shard_manifest_is_refused(self):
        for uri in (f"{PREFIX}/merchant_history/shard-00001.parquet", f"{PREFIX}/_control/shards/shard-00001.json"):
            with self.subTest(uri):
                self.setUp()
                del self.store.objects[uri]
                with self.assertRaisesRegex((SourceGateError, KeyError), "missing|shard"):
                    self.verify()

    def test_unexpected_object_is_refused(self):
        self.store.objects[f"{PREFIX}/users/shard-00099.parquet"] = b"x"
        with self.assertRaisesRegex(SourceGateError, "unexpected"):
            self.verify()

    def test_a_changed_object_is_refused(self):
        uri = f"{PREFIX}/users/shard-00000.parquet"
        self.store.objects[uri] = self.store.objects[uri] + b"tampered"
        with self.assertRaisesRegex(SourceGateError, "does not match"):
            self.verify()

    def test_canonical_bucket_and_unknown_versions_are_refused(self):
        with self.assertRaisesRegex(ValueError, "must not use bucket"):
            verify_source_dataset(bucket=CANONICAL_DATA_BUCKET, dataset_version=REHEARSAL_5GB,
                                  store=self.store, lister=lambda root: {})
        with self.assertRaisesRegex(ValueError, "not an approved"):
            verify_source_dataset(bucket=BUCKET, dataset_version="rehearsal-5gb-v9", store=self.store,
                                  lister=lambda root: {})

    def test_registry_size_and_shard_count_are_enforced(self):
        manifest = dict(self.manifest, total_bytes=1)
        plan = json.loads(self.objects[PLAN_URI])
        with self.assertRaisesRegex(SourceGateError, "outside the accepted range"):
            check_registry_contract(manifest, plan, replace(SMALL_IDENTITY, size_tolerance_ratio=0.1))
        with self.assertRaisesRegex(SourceGateError, "expected 186 shards"):
            check_registry_contract(self.manifest, plan, replace(SMALL_IDENTITY, source_shard_count=186))


class PublicationTargetTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def target(self, **overrides):
        values = dict(dataset_version=REHEARSAL_5GB, source_root=f"gs://{BUCKET}/source", base_config=self.config,
                      global_seed=7001, shard_count=186)
        values.update(overrides)
        validate_publication_target(**values)

    def test_the_rehearsal_plan_is_accepted_in_its_own_bucket(self):
        self.target()

    def test_the_canonical_bucket_is_refused_for_the_rehearsal(self):
        with self.assertRaisesRegex(ValueError, "must not use bucket"):
            self.target(source_root=f"gs://{CANONICAL_DATA_BUCKET}/source")

    def test_publishing_canonical_again_is_refused(self):
        with self.assertRaisesRegex(ValueError, "immutable canonical"):
            self.target(dataset_version=CANONICAL_100GB)

    def test_wrong_config_seed_or_geometry_is_refused(self):
        legacy = yaml.safe_load((ROOT / "data_platform/generation/config/settings.yaml").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "merchant_history"):
            self.target(base_config=legacy)
        changed = dict(self.config, n_merchants=1)
        with self.assertRaisesRegex(ValueError, "must be generated with"):
            self.target(base_config=changed)
        with self.assertRaisesRegex(ValueError, "global seed"):
            self.target(global_seed=1)
        with self.assertRaisesRegex(ValueError, "186 shards"):
            self.target(shard_count=185)

    def test_unregistered_versions_keep_their_existing_freedom(self):
        self.target(dataset_version="cloud-smoke-v1", global_seed=1, shard_count=3,
                    source_root=f"gs://{CANONICAL_DATA_BUCKET}/source")


class ResumeTest(SourceGateBase):
    """Resumable publication of the extended table contract."""

    def publisher(self, store):
        config = load_config(CONFIG)
        plan = build_shard_plan(config, REHEARSAL_5GB, 7001, 2, 12, 4, 80)
        return DatasetPublisher(store, PREFIX, plan, physical_tables=physical_tables(config))

    def test_a_partial_dataset_cannot_finalize_and_completed_shards_are_recognized(self):
        store = FakeObjectStore()
        store.objects.update({k: v for k, v in self.objects.items() if "dataset_manifest" not in k})
        for key in [k for k in store.objects if "shard-00001" in k]:
            del store.objects[key]
        publisher = self.publisher(store)
        self.assertIsNotNone(publisher.completed_shard(0))
        self.assertIsNone(publisher.completed_shard(1))
        with self.assertRaisesRegex(Exception, "missing shard manifests"):
            publisher.finalize()

    def test_a_tampered_completed_shard_is_a_conflict(self):
        store = FakeObjectStore()
        store.objects.update(self.objects)
        uri = f"{PREFIX}/merchant_history/shard-00000.parquet"
        store.objects[uri] = store.objects[uri] + b"x"
        with self.assertRaisesRegex(Exception, "content changed"):
            self.publisher(store).completed_shard(0)

    def test_finalizing_again_returns_the_identical_manifest(self):
        store = FakeObjectStore()
        store.objects.update(self.objects)
        before = store.objects[MANIFEST_URI]
        self.assertEqual(self.manifest["plan_digest"], self.publisher(store).finalize()["plan_digest"])
        self.assertEqual(before, store.objects[MANIFEST_URI])


if __name__ == "__main__":
    unittest.main()
