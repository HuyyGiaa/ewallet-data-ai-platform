"""The Feast export reads Delta feature tables with or without ``created`` and
always writes the established Feast schema (runs in the Feast uv project)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
from deltalake import write_deltalake

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.sources.export_gold_to_parquet import export_gold_snapshots
from data_platform.config import StorageConfig, load_runtime_config


def frame(spec: dict, *, with_created: bool) -> pd.DataFrame:
    times = pd.to_datetime(["2026-01-01 10:00:00", "2026-01-01 11:00:00"], utc=True).as_unit("us")
    data = {spec["entity_key"]: ["e1", "e1"], "event_timestamp": times}
    for feature in spec["features"]:
        data[feature] = [1, 2] if feature in spec["integer_features"] else [1.0, 2.0]
    result = pd.DataFrame(data)
    for feature in spec["integer_features"]:
        result[feature] = result[feature].astype("int64")
    if with_created:
        result["created"] = pd.Timestamp("2026-11-09 08:00:00", tz="UTC").as_unit("us")
    return result


class FeastExportCreatedTest(unittest.TestCase):
    def export(self, *, with_created: bool) -> tuple[dict, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        gold = directory / "gold"
        for spec in FEATURE_SPECS.values():
            write_deltalake(str(gold / spec["table"]), frame(spec, with_created=with_created))
        base = load_runtime_config("local", environ={"MINIO_ACCESS_KEY": "x", "MINIO_SECRET_KEY": "x"})
        storage = StorageConfig(scheme="file", source_root=(directory / "s").as_uri(),
                                bronze_root=(directory / "b").as_uri(), silver_root=(directory / "si").as_uri(),
                                gold_root=gold.as_uri(), feast_root=(directory / "f").as_uri())
        manifest = export_gold_snapshots(replace(base, storage=storage), directory / "feast", immutable=True)
        return manifest, directory / "feast"

    def test_tables_with_created_export_the_legacy_feast_schema(self):
        manifest, root = self.export(with_created=True)
        for view_name, spec in FEATURE_SPECS.items():
            with self.subTest(view_name):
                exported = ds.dataset(root / spec["table"], format="parquet")
                self.assertEqual([spec["entity_key"], "event_timestamp", *spec["features"]], exported.schema.names)
                self.assertEqual(2, exported.count_rows())
                self.assertEqual(["created"], manifest["tables"][view_name]["excluded_audit_columns"])

    def test_legacy_tables_export_unchanged(self):
        manifest, root = self.export(with_created=False)
        for view_name, spec in FEATURE_SPECS.items():
            with self.subTest(view_name):
                self.assertNotIn("excluded_audit_columns", manifest["tables"][view_name])
                exported = ds.dataset(root / spec["table"], format="parquet")
                self.assertNotIn("created", exported.schema.names)


if __name__ == "__main__":
    unittest.main()
