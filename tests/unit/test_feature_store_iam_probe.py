"""The Workload Identity probe passes only when reads work and forbidden writes fail."""

from __future__ import annotations

import hashlib
import unittest

from google.api_core import exceptions as gexc

from ai_platform.features.feast.validation.iam_probe import ProbeConfig, run_checks

MANIFEST = b'{"dataset_version": "rehearsal-5gb-v1"}'
DIGEST = hashlib.sha256(MANIFEST).hexdigest()
SNAPSHOT = "features/offline/snapshots/rehearsal-5gb-v1/run/snap"


class FakeBlob:
    def __init__(self, store: "FakeStore", path: str) -> None:
        self.store, self.path = store, path

    def download_as_bytes(self) -> bytes:
        if not self.store.readable(self.path):
            raise gexc.Forbidden("read")
        return self.store.objects[self.path]

    def upload_from_string(self, payload, if_generation_match=None) -> None:
        if not self.store.writable(self.path):
            raise gexc.Forbidden("write")
        if if_generation_match == 0 and self.path in self.store.objects:
            raise gexc.PreconditionFailed("exists")
        self.store.objects[self.path] = payload


class FakeStore:
    """A bucket whose policy mirrors the conditional bindings."""

    def __init__(self, read=("features/",), write=("features/_iam-probe/",), list_=("features/",)) -> None:
        self.objects: dict[str, object] = {f"{SNAPSHOT}/_control/export_manifest.json": MANIFEST}
        self.read, self.write, self.list_ = read, write, list_

    def readable(self, path: str) -> bool:
        return path.startswith(self.read)

    def writable(self, path: str) -> bool:
        return path.startswith(self.write)

    def bucket(self, _name: str):
        store = self

        class _Bucket:
            def blob(self, path: str) -> FakeBlob:
                return FakeBlob(store, path)

        return _Bucket()

    def list_blobs(self, _bucket: str, prefix: str, max_results: int):
        if not prefix.startswith(self.list_):
            raise gexc.Forbidden("list")
        return [path for path in self.objects if path.startswith(prefix)][:max_results]


def config(**overrides) -> ProbeConfig:
    values = dict(bucket="bucket", snapshot_prefix=SNAPSHOT, expected_manifest_sha256=DIGEST, probe_id="probe-1")
    values.update(overrides)
    return ProbeConfig(**values)


class IamProbeTest(unittest.TestCase):
    def test_least_privilege_policy_passes(self):
        report = run_checks(FakeStore(), config())
        self.assertEqual(report["status"], "PASS", report)
        results = {check["name"]: check["result"] for check in report["checks"]}
        self.assertEqual(results["recreate_same_object_refused"], "PRECONDITION_FAILED")
        self.assertEqual(results["list_denied"], "FORBIDDEN")
        self.assertEqual(len(report["checks"]), 7)

    def test_overly_broad_write_access_fails(self):
        report = run_checks(FakeStore(write=("",)), config())
        self.assertEqual(report["status"], "FAIL")
        failed = {check["name"] for check in report["checks"] if not check["pass"]}
        self.assertIn("create_denied_lakehouse__iam_negative_probe", failed)

    def test_overly_broad_list_access_fails(self):
        report = run_checks(FakeStore(list_=("",)), config())
        self.assertFalse(next(c for c in report["checks"] if c["name"] == "list_denied")["pass"])

    def test_wrong_manifest_digest_fails(self):
        report = run_checks(FakeStore(), config(expected_manifest_sha256="0" * 64))
        check = report["checks"][0]
        self.assertEqual((check["result"], report["status"]), ("ERROR", "FAIL"))

    def test_missing_read_access_fails(self):
        report = run_checks(FakeStore(read=("other/",)), config())
        self.assertEqual(report["checks"][0]["result"], "FORBIDDEN")
        self.assertEqual(report["status"], "FAIL")

    def test_config_rejects_absolute_or_malformed_values(self):
        with self.assertRaises(ValueError):
            config(expected_manifest_sha256="abc")
        with self.assertRaises(ValueError):
            config(snapshot_prefix="gs://bucket/features")
        with self.assertRaises(ValueError):
            config(probe_id="")


if __name__ == "__main__":
    unittest.main()
