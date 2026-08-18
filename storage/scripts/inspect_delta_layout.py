from statistics import median
from deltalake import DeltaTable


STORAGE_OPTIONS = {
    "AWS_ACCESS_KEY_ID": "minioadmin",
    "AWS_SECRET_ACCESS_KEY": "minioadmin",
    "AWS_REGION": "us-east-1",
    "AWS_ENDPOINT_URL": "http://localhost:9000",
    "AWS_VIRTUAL_HOSTED_STYLE_REQUEST": "false",
    "allow_http": "true",
}

TABLES = {
    "silver.transactions": "s3://silver-zone/transactions",
    "gold.fact_transactions": "s3://gold-zone/fact_transactions",
    "gold.obt_transaction_enriched": (
        "s3://gold-zone/obt_transaction_enriched"
    ),
}


def bytes_to_mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


def inspect_table(name: str, uri: str) -> None:
    table = DeltaTable(
        uri,
        storage_options=STORAGE_OPTIONS,
    )

    actions = table.get_add_actions(flatten=True)

    file_sizes = [
        int(size)
        for size in actions.column("size_bytes").to_pylist()
        if size is not None
    ]

    print(f"\n{name}")
    print(f"URI: {uri}")
    print(f"Delta version: {table.version()}")
    print(
        "Partition columns: "
        f"{table.metadata().partition_columns}"
    )
    print(f"Active data files: {len(file_sizes)}")

    if not file_sizes:
        return

    total_size = sum(file_sizes)

    print(
        f"Total size: {bytes_to_mb(total_size):.2f} MiB"
    )
    print(
        "Average file size: "
        f"{bytes_to_mb(total_size / len(file_sizes)):.2f} MiB"
    )
    print(
        "Median file size: "
        f"{bytes_to_mb(median(file_sizes)):.2f} MiB"
    )
    print(
        f"Min file size: {bytes_to_mb(min(file_sizes)):.2f} MiB"
    )
    print(
        f"Max file size: {bytes_to_mb(max(file_sizes)):.2f} MiB"
    )

    if "num_records" in actions.column_names:
        row_counts = [
            int(count)
            for count in actions.column("num_records").to_pylist()
            if count is not None
        ]

        if row_counts:
            total_rows = sum(row_counts)

            print(f"Rows from Delta stats: {total_rows:,}")
            print(
                "Average rows/file: "
                f"{total_rows / len(row_counts):,.0f}"
            )


def main() -> None:
    for name, uri in TABLES.items():
        inspect_table(name, uri)


if __name__ == "__main__":
    main()