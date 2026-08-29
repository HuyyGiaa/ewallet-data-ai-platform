import argparse
import statistics
import time

import trino


TRINO_HOST = "localhost"
TRINO_PORT = 8081
CATALOG = "delta"
SCHEMA = "gold_zone"
TABLE = "fact_transactions"


def create_connection():
    return trino.dbapi.connect(
        host=TRINO_HOST,
        port=TRINO_PORT,
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


def benchmark(start_date: str, end_date: str, runs: int) -> None:
    query = f"""
        SELECT
            COUNT(*) AS transaction_count,
            SUM(amount) AS total_amount,
            AVG(amount) AS avg_amount
        FROM {CATALOG}.{SCHEMA}.{TABLE}
        WHERE event_date BETWEEN DATE '{start_date}'
                             AND DATE '{end_date}'
    """

    connection = create_connection()
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


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--runs", type=int, default=5)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    benchmark(
        start_date=args.start_date,
        end_date=args.end_date,
        runs=args.runs,
    )