from __future__ import annotations

import unittest
from unittest.mock import MagicMock, sentinel

from data_platform.config import StorageConfig
from data_platform.quality.validate_bronze import read_bronze as read_validated_bronze
from data_platform.quality.validate_gold import read_gold, read_silver as read_gold_input
from data_platform.quality.validate_silver import (
    read_bronze as read_silver_input,
    read_silver as read_validated_silver,
)


def gcs_storage() -> StorageConfig:
    return StorageConfig(
        scheme="gs",
        source_root="gs://contract-bucket/source",
        bronze_root="gs://contract-bucket/lakehouse/bronze",
        silver_root="gs://contract-bucket/lakehouse/silver",
        gold_root="gs://contract-bucket/lakehouse/gold",
        feast_root="gs://contract-bucket/features/offline",
        options={},
    )


class ValidatorPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spark = MagicMock()
        self.spark.read.format.return_value.load.return_value = sentinel.dataframe
        self.storage = gcs_storage()

    def assert_delta_path(self, expected_path: str) -> None:
        self.spark.read.format.assert_called_once_with("delta")
        self.spark.read.format.return_value.load.assert_called_once_with(expected_path)

    def test_bronze_validator_uses_profile_bronze_root(self) -> None:
        result = read_validated_bronze(
            self.spark,
            "transactions",
            storage=self.storage,
        )

        self.assert_delta_path(
            "gs://contract-bucket/lakehouse/bronze/transactions"
        )
        self.assertIs(result, sentinel.dataframe)

    def test_silver_validator_uses_profile_silver_root(self) -> None:
        result = read_validated_silver(
            self.spark,
            "fraud_labels",
            storage=self.storage,
        )

        self.assert_delta_path(
            "gs://contract-bucket/lakehouse/silver/fraud_labels"
        )
        self.assertIs(result, sentinel.dataframe)

    def test_silver_validator_reads_bronze_relationship_input_from_profile(self) -> None:
        result = read_silver_input(
            self.spark,
            "fraud_labels",
            storage=self.storage,
        )

        self.assert_delta_path(
            "gs://contract-bucket/lakehouse/bronze/fraud_labels"
        )
        self.assertIs(result, sentinel.dataframe)

    def test_gold_validator_uses_profile_gold_root(self) -> None:
        result = read_gold(
            self.spark,
            "fact_transactions",
            storage=self.storage,
        )

        self.assert_delta_path(
            "gs://contract-bucket/lakehouse/gold/fact_transactions"
        )
        self.assertIs(result, sentinel.dataframe)

    def test_gold_validator_reads_silver_relationship_input_from_profile(self) -> None:
        result = read_gold_input(
            self.spark,
            "transactions",
            storage=self.storage,
        )

        self.assert_delta_path(
            "gs://contract-bucket/lakehouse/silver/transactions"
        )
        self.assertIs(result, sentinel.dataframe)


if __name__ == "__main__":
    unittest.main()
