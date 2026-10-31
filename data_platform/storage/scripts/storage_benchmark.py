import argparse
import statistics
import time

import trino

from data_platform.config import RuntimeConfig, load_runtime_config
from data_platform.storage.scripts.config import require_local_runtime

CATALOG = "delta"
SCHEMA = "gold_zone"
TABLE = "fact_transactions"


def create_connection(runtime_config: RuntimeConfig):
    host = runtime_config.services.trino_host
    port = runtime_config.services.trino_port
    if host is None or port is None:
        raise RuntimeError("Trino is not configured for this runtime profile.")
    return trino.dbapi.connect(
        host=host,
        port=port,
        user="benchmark",
        catalog=CATALOG,
        schema=SCHEMA,
    )


def run_query(cursor, query: str):
    start = time.perf_counter()

    cursor.execute(query)
    result = cursor.fetchall()

    elapsed = time.perf_counter() - start

    return result, elapsed


def benchmark(
    start_date: str,
    end_date: str,
    runs: int,
    runtime_config: RuntimeConfig,
) -> None:
    require_local_runtime(runtime_config, "storage_benchmark")
    query = f"""
        SELECT
            COUNT(*) AS transaction_count,
            SUM(amount) AS total_amount,
            AVG(amount) AS avg_amount
        FROM {CATALOG}.{SCHEMA}.{TABLE}
        WHERE event_date BETWEEN DATE '{start_date}'
                             AND DATE '{end_date}'
    """

    connection = create_connection(runtime_config)
    cursor = connection.cursor()

    print(f"Table: {CATALOG}.{SCHEMA}.{TABLE}")
    print(f"Date range: {start_date} -> {end_date}")

    result, elapsed = run_query(cursor, query)

    print(f"Warm-up: {elapsed:.4f}s")
    print(f"Result: {result[0]}")

    runtimes = []

    for run in range(1, runs + 1):
        _, elapsed = run_query(cursor, query)
        runtimes.append(elapsed)

        print(f"Run {run}: {elapsed:.4f}s")

    print(f"Median: {statistics.median(runtimes):.4f}s")
    print(f"Average: {statistics.mean(runtimes):.4f}s")
    print(f"Min: {min(runtimes):.4f}s")
    print(f"Max: {max(runtimes):.4f}s")

    cursor.close()
    connection.close()


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime profile; this command only accepts local.",
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--runs", type=int, default=5)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.environment != "local":
        raise RuntimeError(
            "storage_benchmark is a local-only command; "
            f"selected environment={args.environment!r}"
        )
    config = load_runtime_config(args.environment)

    benchmark(
        start_date=args.start_date,
        end_date=args.end_date,
        runs=args.runs,
        runtime_config=config,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
