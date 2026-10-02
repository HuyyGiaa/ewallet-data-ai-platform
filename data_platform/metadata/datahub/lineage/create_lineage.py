from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from datahub.metadata.urns import DatasetUrn


def dataset(name: str) -> DatasetUrn:
    return DatasetUrn(
        platform="trino",
        name=name,
        env="PROD",
    )


lineage = {
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


def publish_lineage(emitter: DatahubRestEmitter) -> None:
    for downstream, upstreams in lineage.items():
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


def main() -> None:
    emitter = DatahubRestEmitter(gms_server="http://localhost:8080")
    publish_lineage(emitter)


if __name__ == "__main__":
    main()
