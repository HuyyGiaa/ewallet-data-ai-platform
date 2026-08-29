from datahub.metadata.urns import DatasetUrn
from datahub.sdk import DataHubClient


client = DataHubClient(
    server="http://localhost:8080",
)


def dataset(name: str) -> DatasetUrn:
    return DatasetUrn(
        platform="trino",
        name=name,
        env="PROD",
    )


edges = [
    (
        dataset("delta.bronze_zone.transactions"),
        dataset("delta.silver_zone.transactions"),
    ),
    (
        dataset("delta.silver_zone.transactions"),
        dataset("delta.gold_zone.fact_transactions"),
    ),
    (
        dataset("delta.gold_zone.fact_transactions"),
        dataset("delta.gold_zone.obt_transaction_enriched"),
    ),
    (
        dataset("delta.gold_zone.obt_transaction_enriched"),
        dataset("delta.gold_zone.feat_user_90d"),
    ),
]


for upstream, downstream in edges:
    client.lineage.add_lineage(
        upstream=upstream,
        downstream=downstream,
    )

    print(f"{upstream} -> {downstream}")


print("Lineage created successfully.")