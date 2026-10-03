import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from datahub.metadata.urns import DatasetUrn

from data_platform.storage.scripts.config import BRONZE_SCHEMA, SILVER_SCHEMA
from data_platform.storage.scripts.trino_client import (
    check_trino_connection,
    list_schema_tables,
    trino_cursor,
)


FRAUD_LINEAGE_TABLES = {
    BRONZE_SCHEMA: "fraud_labels",
    SILVER_SCHEMA: "fraud_labels",
}


def dataset(name: str) -> DatasetUrn:
    return DatasetUrn(
        platform="trino",
        name=name,
        env="PROD",
    )


BASE_LINEAGE = {
    dataset("delta.silver_zone.transactions"): [
        dataset("delta.bronze_zone.transactions"),
    ],
    dataset("delta.gold_zone.fact_transactions"): [
        dataset("delta.silver_zone.transactions"),
    ],
    dataset("delta.gold_zone.obt_transaction_enriched"): [
        dataset("delta.gold_zone.fact_transactions"),
        dataset("delta.gold_zone.dim_user"),
        dataset("delta.gold_zone.dim_account"),
        dataset("delta.gold_zone.dim_device"),
        dataset("delta.gold_zone.dim_merchant"),
        dataset("delta.gold_zone.dim_date"),
    ],
    dataset("delta.gold_zone.feat_user_90d"): [
        dataset("delta.gold_zone.fact_transactions"),
        dataset("delta.gold_zone.dim_user"),
    ],
}


def get_lineage(include_fraud_labels: bool = False):
    lineage_mapping = {
        downstream: list(upstreams)
        for downstream, upstreams in BASE_LINEAGE.items()
    }
    if include_fraud_labels:
        lineage_mapping[dataset("delta.silver_zone.fraud_labels")] = [
            dataset("delta.bronze_zone.fraud_labels"),
        ]
    return lineage_mapping


# Backward-compatible legacy definition for imports and reconciliation checks.
lineage = BASE_LINEAGE


def validate_fraud_table_inventory(
    tables_by_schema: dict[str, set[str]],
) -> None:
    missing = [
        f"{schema}.{table}"
        for schema, table in FRAUD_LINEAGE_TABLES.items()
        if table not in tables_by_schema.get(schema, set())
    ]
    if missing:
        raise RuntimeError(
            "Fraud lineage requires registered Trino tables: "
            + ", ".join(missing)
        )


def verify_fraud_tables_registered() -> None:
    """Fail before DataHub publication when fraud tables are not canonical."""
    with trino_cursor() as cursor:
        check_trino_connection(cursor)
        tables_by_schema = {
            schema: list_schema_tables(cursor, schema)
            for schema in FRAUD_LINEAGE_TABLES
        }
    validate_fraud_table_inventory(tables_by_schema)


def publish_lineage(emitter: DatahubRestEmitter, lineage_mapping=None) -> None:
    if lineage_mapping is None:
        lineage_mapping = BASE_LINEAGE

    for downstream, upstreams in lineage_mapping.items():
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=str(downstream),
                aspect=UpstreamLineageClass(
                    upstreams=[
                        UpstreamClass(
                            dataset=str(upstream),
                            type=DatasetLineageTypeClass.TRANSFORMED,
                        )
                        for upstream in upstreams
                    ]
                ),
            )
        )

        for upstream in upstreams:
            print(f"{upstream} -> {downstream}")

    print("Lineage created successfully.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish selected DataHub lineage.")
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help=(
            "Include bronze.fraud_labels -> silver.fraud_labels after both "
            "datasets exist in canonical DataHub metadata."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.include_fraud_labels:
        verify_fraud_tables_registered()
    emitter = DatahubRestEmitter(gms_server="http://localhost:8080")
    publish_lineage(emitter, get_lineage(args.include_fraud_labels))


if __name__ == "__main__":
    main()
