from __future__ import annotations

import unittest
from pathlib import Path

import yaml


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "data_platform" / "contracts"


class DataContractLocationTest(unittest.TestCase):
    def test_contracts_use_environment_independent_logical_locations(self) -> None:
        contract_paths = sorted(CONTRACT_ROOT.glob("*.yml"))
        self.assertEqual(9, len(contract_paths))

        for contract_path in contract_paths:
            with self.subTest(contract=contract_path.name):
                text = contract_path.read_text(encoding="utf-8")
                document = yaml.safe_load(text)
                dataset_layer, dataset_table = document["dataset"].split(".", 1)

                self.assertNotIn("storage_location", document)
                self.assertEqual(dataset_layer, document["storage_layer"])
                self.assertEqual(dataset_table, document["storage_table"])
                self.assertEqual(dataset_layer, document["layer"])

                lowered = text.lower()
                for forbidden in ("s3a://", "s3://", "gs://", "localhost", "/home/"):
                    self.assertNotIn(forbidden, lowered)

                self.assertTrue(document.get("grain"))
                self.assertTrue(document.get("logical_key"))
                self.assertTrue(document.get("columns"))
                self.assertTrue(document.get("quality_rules"))


if __name__ == "__main__":
    unittest.main()
