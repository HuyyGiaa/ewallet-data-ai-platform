from __future__ import annotations

import unittest
from unittest.mock import MagicMock, sentinel

from data_platform.config import StorageConfig
from data_platform.processing.spark.gold.delta_gold_io import (
    read_silver,
    write_gold,
)
from data_platform.processing.spark.silver.delta_silver_io import (
    read_bronze,
    write_silver,
)


def gcs_storage() -> StorageConfig:
    return StorageConfig(
        scheme="gs",
        source_root="gs://contract-bucket/source",
        bronze_root="gs://contract-bucket/lakehouse/bronze/",
        silver_root="gs://contract-bucket/lakehouse/silver/",
        gold_root="gs://contract-bucket/lakehouse/gold/",
        feast_root="gs://contract-bucket/features/offline",
        options={},
    )


def local_storage() -> StorageConfig:
    return StorageConfig(
        scheme="file",
        source_root="runtime/source",
        bronze_root="runtime/bronze",
        silver_root="runtime/silver",
        gold_root="runtime/gold",
        feast_root="runtime/feast",
        options={},
    )


class DeltaReadPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = gcs_storage()

    def test_read_bronze_loads_profile_bronze_uri_as_delta(self) -> None:
        spark = MagicMock()
        spark.read.format.return_value.load.return_value = sentinel.bronze_df

        result = read_bronze(
            spark,
            "transactions",
            storage=self.storage,
        )

        spark.read.format.assert_called_once_with("delta")
        spark.read.format.return_value.load.assert_called_once_with(
            "gs://contract-bucket/lakehouse/bronze/transactions"
        )
        self.assertIs(result, sentinel.bronze_df)

    def test_read_silver_loads_profile_silver_uri_as_delta(self) -> None:
        spark = MagicMock()
        spark.read.format.return_value.load.return_value = sentinel.silver_df

        result = read_silver(
            spark,
            "accounts",
            storage=self.storage,
        )

        spark.read.format.assert_called_once_with("delta")
        spark.read.format.return_value.load.assert_called_once_with(
            "gs://contract-bucket/lakehouse/silver/accounts"
        )
        self.assertIs(result, sentinel.silver_df)


class DeltaWritePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = gcs_storage()

    def test_write_silver_refuses_existing_gcs_table_and_preserves_partitions(
        self,
    ) -> None:
        dataframe = MagicMock()
        planned = dataframe.repartition.return_value
        delta_writer = planned.write.format.return_value
        overwrite_writer = delta_writer.mode.return_value
        schema_writer = overwrite_writer.option.return_value
        bounded_writer = schema_writer.option.return_value
        partitioned_writer = bounded_writer.partitionBy.return_value

        write_silver(
            dataframe,
            "transactions",
            partition_columns=["event_date", "country"],
            storage=self.storage,
        )

        dataframe.repartition.assert_called_once_with(
            128, "event_date", "country"
        )
        planned.write.format.assert_called_once_with("delta")
        delta_writer.mode.assert_called_once_with("errorifexists")
        overwrite_writer.option.assert_called_once_with("mergeSchema", "true")
        schema_writer.option.assert_called_once_with(
            "maxRecordsPerFile", "2500000"
        )
        bounded_writer.partitionBy.assert_called_once_with(
            "event_date", "country"
        )
        partitioned_writer.save.assert_called_once_with(
            "gs://contract-bucket/lakehouse/silver/transactions"
        )

    def test_write_gold_refuses_existing_gcs_table_without_partitions(
        self,
    ) -> None:
        dataframe = MagicMock()
        planned = dataframe.repartition.return_value
        delta_writer = planned.write.format.return_value
        overwrite_writer = delta_writer.mode.return_value
        schema_writer = overwrite_writer.option.return_value
        bounded_writer = schema_writer.option.return_value

        write_gold(
            dataframe,
            "dim_user",
            storage=self.storage,
        )

        dataframe.repartition.assert_called_once_with(16)
        planned.write.format.assert_called_once_with("delta")
        delta_writer.mode.assert_called_once_with("errorifexists")
        overwrite_writer.option.assert_called_once_with("mergeSchema", "true")
        schema_writer.option.assert_called_once_with(
            "maxRecordsPerFile", "2500000"
        )
        bounded_writer.partitionBy.assert_not_called()
        bounded_writer.save.assert_called_once_with(
            "gs://contract-bucket/lakehouse/gold/dim_user"
        )

    def test_local_write_keeps_existing_overwrite_behavior(self) -> None:
        dataframe = MagicMock()
        write_silver(dataframe, "users", storage=local_storage())
        dataframe.repartition.return_value.write.format.return_value.mode.assert_called_once_with(
            "overwrite"
        )


if __name__ == "__main__":
    unittest.main()
