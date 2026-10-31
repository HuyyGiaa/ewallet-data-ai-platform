from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dask.dataframe as dd
import pandas as pd

from ai_platform.features.feast.retrieval.bounded_dask import (
    BoundedSourcePlan,
    bounded_dask_source,
)


class BoundedDaskSourceTest(unittest.TestCase):
    def plan(self, source: Path) -> BoundedSourcePlan:
        return BoundedSourcePlan(
            view_name="user_behavior",
            source_path=str(source),
            entity_key="user_id",
            entity_values=("u1",),
            timestamp_field="event_timestamp",
            start_timestamp=pd.Timestamp("2025-01-01", tz="UTC").to_pydatetime(),
            end_timestamp=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
            projected_columns=(
                "user_id",
                "event_timestamp",
                "user_tx_count_5m",
            ),
            ttl_seconds=3650 * 24 * 60 * 60,
        )

    def test_reader_projects_and_filters_before_feast_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            pd.DataFrame(
                {
                    "user_id": ["u1", "u1", "u2"],
                    "event_timestamp": pd.to_datetime(
                        ["2024-01-01", "2026-01-01", "2026-01-01"], utc=True
                    ),
                    "user_tx_count_5m": [1, 2, 3],
                    "unneeded": [100, 200, 300],
                }
            ).to_parquet(source / "part.parquet", index=False)
            plan = self.plan(source)

            original_reader = lambda data_source, repo_path: dd.read_parquet(  # noqa: E731
                data_source.path
            )
            fake_source = type("Source", (), {"path": str(source)})()
            with patch(
                "ai_platform.features.feast.retrieval.bounded_dask.feast_dask._read_datasource",
                original_reader,
            ):
                with bounded_dask_source(plan):
                    from feast.infra.offline_stores import dask as feast_dask

                    observed = feast_dask._read_datasource(fake_source, None).compute()

            self.assertEqual(list(plan.projected_columns), list(observed.columns))
            self.assertEqual([2], observed["user_tx_count_5m"].tolist())

    def test_reader_fails_closed_on_wrong_source_binding(self) -> None:
        plan = self.plan(Path("expected"))
        wrong_source = type("Source", (), {"path": "wrong"})()
        with patch(
            "ai_platform.features.feast.retrieval.bounded_dask.feast_dask._read_datasource",
            return_value=dd.from_pandas(pd.DataFrame(), npartitions=1),
        ):
            with self.assertRaisesRegex(ValueError, "source binding mismatch"):
                with bounded_dask_source(plan):
                    from feast.infra.offline_stores import dask as feast_dask

                    feast_dask._read_datasource(wrong_source, None)


if __name__ == "__main__":
    unittest.main()
