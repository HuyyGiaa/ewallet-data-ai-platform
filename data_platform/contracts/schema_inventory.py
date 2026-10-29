"""Render the actual deployed Source→Bronze→Silver→Gold table inventory.

This deliberately reports table-level topology from the canonical inventories;
it does not claim that proposed SCD2 or feature-created columns are deployed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_platform.storage.scripts.config import (
    GOLD_TABLES,
    HISTORICAL_FEATURE_TABLES,
    offline_tables,
    silver_tables,
)


def build_inventory() -> dict[str, object]:
    source = offline_tables(include_fraud_labels=True)
    bronze = offline_tables(include_fraud_labels=True)
    silver = silver_tables(include_fraud_labels=True)
    gold = GOLD_TABLES
    edges = []
    for table in source:
        edges.append({"from": f"source.{table}", "to": f"bronze.{table}"})
        edges.append({"from": f"bronze.{table}", "to": f"silver.{table}"})
    edges += [
        {"from": "silver.users", "to": "gold.dim_user"},
        {"from": "silver.accounts", "to": "gold.dim_account"},
        {"from": "silver.merchants", "to": "gold.dim_merchant"},
        {"from": "silver.devices", "to": "gold.dim_device"},
        {"from": "silver.transactions", "to": "gold.fact_transactions"},
        {"from": "silver.login_events", "to": "gold.fact_login_events"},
        {"from": "silver.balance_snapshots", "to": "gold.fact_balance_snapshot"},
    ]
    for table in HISTORICAL_FEATURE_TABLES:
        edges.append({"from": "silver.transactions", "to": f"gold.{table}"})
    return {
        "classification": "actual canonical inventory at source HEAD; table-level",
        "zones": {
            "source": list(source),
            "bronze": list(bronze),
            "silver": list(silver),
            "gold": list(gold),
        },
        "counts": {
            "source": len(source),
            "bronze": len(bronze),
            "silver": len(silver),
            "gold": len(gold),
        },
        "edges": edges,
        "not_deployed": [
            "SCD2 valid_from_ts/valid_to_ts/is_current contract (Spark owner)",
            "feature created timestamp contract (Spark owner)",
        ],
    }


def render_mermaid(inventory: dict[str, object]) -> str:
    lines = ["flowchart LR"]
    for edge in inventory["edges"]:
        source = edge["from"].replace(".", "_")
        target = edge["to"].replace(".", "_")
        lines.append(f"  {source}[{edge['from']}] --> {target}[{edge['to']}]")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--mermaid-output", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = build_inventory()
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    args.mermaid_output.write_text(render_mermaid(inventory), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
