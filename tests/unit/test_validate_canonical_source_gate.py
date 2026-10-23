from __future__ import annotations

import unittest

from ops.gcp.validate_canonical_source_gate import (
    PHYSICAL_TABLES,
    validate_canonical_manifest,
    verify_gcs_inventory,
)


def canonical_manifest(*, shard_count: int = 2) -> dict:
    logical_per_shard = 100
    physical_per_shard = 102
    tables = {}
    for table in PHYSICAL_TABLES:
        rows_per_shard = (
            physical_per_shard // 2
            if table in {"transactions_v1", "transactions_v2"}
            else logical_per_shard
        )
        files = [
            {
                "shard_id": shard_id,
                "path": f"{table}/shard-{shard_id:05d}.parquet",
                "rows": rows_per_shard,
                "bytes": 1000,
                "sha256": f"{shard_id:064x}",
                "md5_base64": f"md5-{table}-{shard_id}",
            }
            for shard_id in range(shard_count)
        ]
        tables[table] = {
            "rows": rows_per_shard * shard_count,
            "bytes": 1000 * shard_count,
            "schema_sha256": [f"schema-{table}"],
            "files": files,
        }
    return {
        "status": "COMPLETE",
        "dataset_version": "canonical-100gb-v1",
        "shard_count": shard_count,
        "logical_transactions": logical_per_shard * shard_count,
        "physical_transactions": physical_per_shard * shard_count,
        "fraud_label_rows": logical_per_shard * shard_count,
        "fraud_rows": 4,
        "fraud_scenario_counts": {
            "velocity": 1,
            "amount_anomaly": 1,
            "account_takeover": 1,
            "merchant_burst": 1,
        },
        "total_bytes": 1000 * shard_count * len(PHYSICAL_TABLES),
        "tables": tables,
    }


class ListedBlob:
    def __init__(self, name: str, size: int, md5_hash: str | None) -> None:
        self.name = name
        self.size = size
        self.md5_hash = md5_hash
        self.reload_count = 0
        self.reload_md5: str | None = None

    def reload(self) -> None:
        self.reload_count += 1
        self.md5_hash = self.reload_md5


class Bucket:
    def __init__(self, blobs: list[ListedBlob]) -> None:
        self.blobs = blobs
        self.list_calls: list[str] = []

    def list_blobs(self, *, prefix: str):
        self.list_calls.append(prefix)
        return iter(self.blobs)


def listed_blobs(manifest: dict, prefix: str) -> list[ListedBlob]:
    blobs = [
        ListedBlob(f"{prefix}/_control/plan.json", 10, "control-md5"),
        ListedBlob(f"{prefix}/_control/dataset_manifest.json", 10, "control-md5"),
    ]
    blobs.extend(
        ListedBlob(
            f"{prefix}/_control/shards/shard-{shard_id:05d}.json",
            10,
            "control-md5",
        )
        for shard_id in range(manifest["shard_count"])
    )
    for table in PHYSICAL_TABLES:
        for item in manifest["tables"][table]["files"]:
            blobs.append(
                ListedBlob(
                    f"{prefix}/{item['path']}",
                    item["bytes"],
                    item["md5_base64"],
                )
            )
    return blobs


class CanonicalSourceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = canonical_manifest()
        self.prefix = "source/canonical-100gb-v1"
        self.expected = validate_canonical_manifest(
            self.manifest,
            dataset_version="canonical-100gb-v1",
            shard_count=2,
            logical_transactions=200,
            physical_transactions=204,
            fraud_label_rows=200,
            fraud_rows=4,
            minimum_bytes=18_000,
            maximum_bytes=18_000,
        )

    def test_single_listing_verifies_exact_parquet_inventory_and_ignores_control(self) -> None:
        blobs = listed_blobs(self.manifest, self.prefix)
        bucket = Bucket(blobs)

        result = verify_gcs_inventory(
            bucket,
            dataset_prefix=self.prefix,
            expected_objects=self.expected,
            expected_shard_count=2,
        )

        self.assertEqual([f"{self.prefix}/"], bucket.list_calls)
        self.assertEqual(18, result.parquet_objects)
        self.assertEqual(2, result.shard_manifests)
        self.assertEqual(0, result.md5_fallback_reloads)
        self.assertTrue(all(blob.reload_count == 0 for blob in blobs))

    def test_only_listing_entries_without_md5_are_reloaded(self) -> None:
        blobs = listed_blobs(self.manifest, self.prefix)
        parquet = [blob for blob in blobs if blob.name.endswith(".parquet")]
        missing_md5 = parquet[3]
        missing_md5.reload_md5 = missing_md5.md5_hash
        missing_md5.md5_hash = None
        bucket = Bucket(blobs)

        result = verify_gcs_inventory(
            bucket,
            dataset_prefix=self.prefix,
            expected_objects=self.expected,
            expected_shard_count=2,
        )

        self.assertEqual(1, result.md5_fallback_reloads)
        self.assertEqual(1, missing_md5.reload_count)
        self.assertTrue(
            all(blob.reload_count == 0 for blob in parquet if blob is not missing_md5)
        )

    def test_missing_or_unexpected_parquet_object_fails_exact_set_comparison(self) -> None:
        blobs = listed_blobs(self.manifest, self.prefix)
        blobs.pop(next(i for i, blob in enumerate(blobs) if blob.name.endswith(".parquet")))
        blobs.append(ListedBlob(f"{self.prefix}/users/unexpected.parquet", 1000, "x"))

        with self.assertRaisesRegex(ValueError, "Parquet object inventory mismatch"):
            verify_gcs_inventory(
                Bucket(blobs),
                dataset_prefix=self.prefix,
                expected_objects=self.expected,
                expected_shard_count=2,
            )

    def test_size_or_available_md5_mismatch_fails(self) -> None:
        size_blobs = listed_blobs(self.manifest, self.prefix)
        size_blob = next(blob for blob in size_blobs if blob.name.endswith(".parquet"))
        size_blob.size += 1
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            verify_gcs_inventory(
                Bucket(size_blobs),
                dataset_prefix=self.prefix,
                expected_objects=self.expected,
                expected_shard_count=2,
            )

        md5_blobs = listed_blobs(self.manifest, self.prefix)
        md5_blob = next(blob for blob in md5_blobs if blob.name.endswith(".parquet"))
        md5_blob.md5_hash = "different"
        with self.assertRaisesRegex(ValueError, "MD5 mismatch"):
            verify_gcs_inventory(
                Bucket(md5_blobs),
                dataset_prefix=self.prefix,
                expected_objects=self.expected,
                expected_shard_count=2,
            )

    def test_missing_shard_manifest_fails(self) -> None:
        blobs = listed_blobs(self.manifest, self.prefix)
        blobs = [blob for blob in blobs if "shard-00001.json" not in blob.name]

        with self.assertRaisesRegex(ValueError, "shard manifest inventory mismatch"):
            verify_gcs_inventory(
                Bucket(blobs),
                dataset_prefix=self.prefix,
                expected_objects=self.expected,
                expected_shard_count=2,
            )

    def test_manifest_count_or_byte_range_mismatch_fails_before_listing(self) -> None:
        broken = canonical_manifest()
        broken["physical_transactions"] = 203

        with self.assertRaisesRegex(ValueError, "physical transaction count"):
            validate_canonical_manifest(
                broken,
                dataset_version="canonical-100gb-v1",
                shard_count=2,
                logical_transactions=200,
                physical_transactions=204,
                fraud_label_rows=200,
                fraud_rows=4,
                minimum_bytes=18_000,
                maximum_bytes=18_000,
            )


if __name__ == "__main__":
    unittest.main()
