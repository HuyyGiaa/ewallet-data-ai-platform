import argparse
from statistics import median

from deltalake import DeltaTable

from data_platform.config import RuntimeConfig, load_runtime_config
from data_platform.storage.scripts.config import (
    deltalake_storage_options,
    require_local_runtime,
    storage_table_uri,
)

TABLES = {
    "silver.transactions": ("silver", "transactions"),
    "gold.fact_transactions": ("gold", "fact_transactions"),
    "gold.obt_transaction_enriched": ("gold", "obt_transaction_enriched"),
}


def bytes_to_mb(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


def inspect_table(
    name: str,
    uri: str,
    storage_options: dict[str, str],
) -> None:
    table = DeltaTable(
        uri,
        storage_options=storage_options,
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


def inspect_runtime_tables(runtime_config: RuntimeConfig) -> None:
    require_local_runtime(runtime_config, "inspect_delta_layout")
    storage_options = deltalake_storage_options(runtime_config)
    for name, (layer, table_name) in TABLES.items():
        uri = storage_table_uri(
            runtime_config.storage,
            layer,
            table_name,
            delta_rs=True,
        )
        inspect_table(name, uri, storage_options)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect file layout for local MinIO Delta tables."
    )
    parser.add_argument(
        "--environment",
        default="local",
        choices=("local", "dev", "gcp"),
        help="Runtime profile. This command only permits local.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.environment != "local":
        raise RuntimeError(
            "inspect_delta_layout is a local-only command; "
            f"selected environment={args.environment!r}"
        )
    inspect_runtime_tables(load_runtime_config(args.environment))


if __name__ == "__main__":
    main()
