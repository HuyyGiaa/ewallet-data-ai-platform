"""Zone inventory per approved dataset version."""

from __future__ import annotations

import pytest

from data_platform.contracts.schema_inventory import inventory_document, zone_inventory
from ops.gcp.validate_spark_output_equivalence import expected_tables


def test_rehearsal_inventory_includes_the_merchant_history_extension():
    inventory = zone_inventory("rehearsal-5gb-v1")
    assert inventory.counts() == {"source_physical": 10, "source_logical": 9, "bronze": 9, "silver": 9,
                                  "gold": 12, "features": 4}
    assert "merchant_history" in inventory.silver
    assert inventory.gold[-1] == "dim_merchant_scd2"
    assert inventory.source_logical.count("transactions") == 1


def test_canonical_100gb_inventory_has_no_extension():
    counts = zone_inventory("canonical-100gb-v1").counts()
    assert counts == {"source_physical": 9, "source_logical": 8, "bronze": 8, "silver": 8, "gold": 11, "features": 4}


@pytest.mark.parametrize("stage,zone", [("dp1-bronze", "bronze"), ("dp2-silver", "silver"),
                                        ("dp2-gold", "gold"), ("dp3-features", "features")])
def test_inventory_equals_the_campaign_stage_inventory(stage, zone):
    for version in ("rehearsal-5gb-v1", "canonical-100gb-v1"):
        assert set(zone_inventory(version).tables(zone)) == expected_tables(stage, version)


def test_an_unapproved_dataset_is_refused():
    with pytest.raises(ValueError, match="not an approved"):
        zone_inventory("canonical-100gb-v2")


def test_a_missing_table_is_named():
    with pytest.raises(ValueError, match=r"missing=\['dim_merchant_scd2'\]"):
        zone_inventory("rehearsal-5gb-v1").require_tables("gold", zone_inventory("canonical-100gb-v1").gold)


def test_document_maps_physical_source_to_logical_tables():
    document = inventory_document(zone_inventory("rehearsal-5gb-v1"))
    assert document["zones"]["source"]["transactions_v1"] == "transactions"
    assert document["merchant_history_extension"] is True
