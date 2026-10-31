from data_platform.contracts.schema_inventory import build_inventory, render_mermaid


def test_inventory_reuses_canonical_table_lists_and_labels_proposals():
    inventory = build_inventory()
    assert inventory["counts"] == {"source": 8, "bronze": 8, "silver": 8, "gold": 15}
    assert "fraud_labels" in inventory["zones"]["source"]
    assert "feat_user_behavior" in inventory["zones"]["gold"]
    assert len(inventory["not_deployed"]) == 2
    diagram = render_mermaid(inventory)
    assert "source_transactions" in diagram
    assert "gold_fact_transactions" in diagram
