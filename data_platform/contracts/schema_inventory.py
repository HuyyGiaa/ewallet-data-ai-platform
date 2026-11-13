"""Table inventory per zone for one approved dataset version.

This is the single source of the expected Source, Bronze, Silver, core Gold and
historical-feature tables. It is derived from the code that builds them, not from
a hand-maintained list:

* Source physical datasets and their logical table come from
  ``data_platform.ingestion.spark.source_to_bronze``. ``transactions_v1`` and
  ``transactions_v2`` are two physical schema versions of one logical table.
* Bronze, Silver, Gold and feature tables come from
  ``data_platform.storage.scripts.config``.
* The ``merchant_history`` source extension, and the SCD Type 2 dimension built
  from it, are included exactly when ``dataset_identity`` registers the dataset
  with that extension.

The DataHub lineage plan, the assertion mapping, the observed-schema exporter and
the diagram renderer all consume this inventory, so a table missing from a cloud
run is detected the same way everywhere.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from data_platform.config.dataset_identity import (
    dataset_has_merchant_history,
    require_approved_dataset,
)
from data_platform.ingestion.spark.source_to_bronze import (
    EXTENDED_PHYSICAL_TABLES,
    PHYSICAL_TABLES,
)
from data_platform.storage.scripts.config import (
    HISTORICAL_FEATURE_TABLES,
    MERCHANT_HISTORY_TABLE,
    core_gold_tables,
    offline_tables,
    silver_tables,
)


# Physical Source datasets whose logical table has another name. Bronze unions the
# two transaction schema versions into one table (schema evolution).
SOURCE_LOGICAL_TABLES: Mapping[str, str] = {
    "transactions_v1": "transactions",
    "transactions_v2": "transactions",
}
ZONES = ("source", "bronze", "silver", "gold", "features")


@dataclass(frozen=True)
class ZoneInventory:
    """Expected tables of one dataset version, zone by zone.

    Attributes:
        dataset_version: Approved dataset version the inventory belongs to.
        source_physical: Physical Source datasets in the source manifest.
        bronze: Bronze tables written by DP1.
        silver: Silver tables written by DP2.
        gold: Core Gold tables written by DP2, including the SCD2 dimension.
        features: Historical feature tables written by DP3.
    """

    dataset_version: str
    source_physical: tuple[str, ...]
    bronze: tuple[str, ...]
    silver: tuple[str, ...]
    gold: tuple[str, ...]
    features: tuple[str, ...]

    @property
    def source_logical(self) -> tuple[str, ...]:
        """Logical Source tables, in first-seen order."""

        return tuple(dict.fromkeys(logical_source_table(name) for name in self.source_physical))

    def tables(self, zone: str) -> tuple[str, ...]:
        """Return the tables of one zone; ``source`` means the physical datasets."""

        if zone == "source":
            return self.source_physical
        if zone not in ZONES:
            raise ValueError(f"unknown zone {zone!r}; expected one of {ZONES}")
        return getattr(self, zone)

    def counts(self) -> dict[str, int]:
        """Table count per zone, with Source split into physical and logical."""

        return {
            "source_physical": len(self.source_physical),
            "source_logical": len(self.source_logical),
            "bronze": len(self.bronze),
            "silver": len(self.silver),
            "gold": len(self.gold),
            "features": len(self.features),
        }

    def require_tables(self, zone: str, observed: Sequence[str]) -> None:
        """Refuse an observed table set that differs from the expected one.

        Raises:
            ValueError: A table is missing or unexpected; the message names both sets.
        """

        expected = set(self.tables(zone))
        found = set(observed)
        if found != expected:
            raise ValueError(
                f"{self.dataset_version} {zone} inventory mismatch: "
                f"missing={sorted(expected - found)} unexpected={sorted(found - expected)}"
            )


def logical_source_table(physical: str) -> str:
    """Return the logical table a physical Source dataset belongs to."""

    return SOURCE_LOGICAL_TABLES.get(physical, physical)


def zone_inventory(dataset_version: str) -> ZoneInventory:
    """Return the expected inventory of an approved dataset version.

    Raises:
        ValueError: The dataset version is not approved in ``dataset_identity``.
    """

    require_approved_dataset(dataset_version)
    extended = dataset_has_merchant_history(dataset_version)
    return ZoneInventory(
        dataset_version=dataset_version,
        source_physical=EXTENDED_PHYSICAL_TABLES if extended else PHYSICAL_TABLES,
        bronze=offline_tables(include_fraud_labels=True, include_merchant_history=extended),
        silver=silver_tables(include_fraud_labels=True, include_merchant_history=extended),
        gold=core_gold_tables(include_merchant_scd2=extended),
        features=HISTORICAL_FEATURE_TABLES,
    )


def inventory_document(inventory: ZoneInventory) -> dict[str, object]:
    """Return a JSON-serialisable view of the inventory."""

    return {
        "dataset_version": inventory.dataset_version,
        "merchant_history_extension": MERCHANT_HISTORY_TABLE in inventory.silver,
        "counts": inventory.counts(),
        "zones": {
            "source": {
                name: logical_source_table(name) for name in inventory.source_physical
            },
            "bronze": list(inventory.bronze),
            "silver": list(inventory.silver),
            "gold": list(inventory.gold),
            "features": list(inventory.features),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Print or write the inventory of one dataset version."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rendered = json.dumps(inventory_document(zone_inventory(args.dataset_version)), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
