from data_platform.contracts.schema_inventory import build_inventory, render_mermaid


def test_inventory_reuses_canonical_table_lists_and_labels_proposals():
    inventory = build_inventory()
    assert inventory["counts"] == {"source": 8, "bronze": 8, "silver": 8, "gold": 15}
    assert "fraud_labels" in inventory["zones"]["source"]
    assert "feat_user_behavior" in inventory["zones"]["gold"]
    assert len(inventory["not_deployed"]) == 2
    assert "dim_merchant_scd2" not in inventory["zones"]["gold"]
    diagram = render_mermaid(inventory)
    assert "source_transactions" in diagram
    assert "gold_fact_transactions" in diagram


def test_extended_inventory_is_reported_separately():
    extended = build_inventory()["extended"]
    assert extended["counts"] == {"source": 9, "bronze": 9, "silver": 9, "gold": 16}
    assert extended["zones"]["gold"][-1] == "dim_merchant_scd2"
    assert {"from": "silver.merchant_history", "to": "gold.dim_merchant_scd2"} in extended["edges"]
