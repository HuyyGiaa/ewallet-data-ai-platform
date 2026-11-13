"""Import the verified snapshot into the isolated PostgreSQL and record its provenance."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import TABLE
from .postgres import Psql, PostgresError
from .schema import PROVENANCE_DDL, create_table_sql
from .source import PROVENANCE_NAME, SNAPSHOT_NAME


def read_provenance(snapshot_dir: Path) -> dict[str, Any]:
    """Provenance of a snapshot directory, after checking the snapshot's SHA-256.

    Raises:
        ValueError: The snapshot file differs from the one the provenance describes.
    """

    provenance = json.loads((snapshot_dir / PROVENANCE_NAME).read_text(encoding="utf-8"))
    digest = hashlib.sha256((snapshot_dir / SNAPSHOT_NAME).read_bytes()).hexdigest()
    if digest != provenance["snapshot"]["sha256"]:
        raise ValueError("snapshot SHA-256 differs from its provenance; refusing to load")
    return provenance


def provenance_rows(provenance: dict[str, Any]) -> dict[str, str]:
    """Flat key/value rows shown in DBeaver for WH01."""

    source, sample, snapshot = provenance["source"], provenance["sample"], provenance["snapshot"]
    return {
        "source.benchmark_id": source["benchmark_id"],
        "source.spark_profile": source["spark_profile"],
        "source.dataset_version": source["dataset_version"],
        "source.source_spark_git_sha": source["source_spark_git_sha"],
        "source.table": source["table"],
        "source.uri": source["uri"],
        "source.delta_version": str(source["delta_version"]),
        "source.table_rows_total": str(source["table_rows_total"]),
        "source.table_partitions_total": str(source["table_partitions_total"]),
        "sample.method": sample["method"],
        "sample.first_partition": sample["partitions"][0],
        "sample.last_partition": sample["partitions"][-1],
        "sample.files": str(len(sample["files"])),
        "sample.expected_rows": str(sample["expected_rows"]),
        "snapshot.sha256": snapshot["sha256"],
        "snapshot.rows": str(snapshot["rows"]),
    }


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load(snapshot_dir: Path, psql: Psql, *, batch_rows: int = 200_000) -> dict[str, Any]:
    """Create the tables, stream the snapshot in, then ``VACUUM ANALYZE``.

    Raises:
        PostgresError: The database already holds benchmark tables, or a row count differs.
    """

    import pyarrow.csv as pa_csv
    import pyarrow.parquet as pq

    provenance = read_provenance(snapshot_dir)
    existing = psql.scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public';")
    if existing != "0":
        raise PostgresError("the benchmark database is not empty; start from a fresh volume")
    parquet = pq.ParquetFile(snapshot_dir / SNAPSHOT_NAME)
    fields = [(field.name, str(field.type)) for field in parquet.schema_arrow]
    psql.run(create_table_sql(fields) + "\n" + PROVENANCE_DDL, tuples=False)
    columns = ", ".join(name for name, _ in fields)
    process = subprocess.Popen(
        psql._args("-c", f"\\copy {TABLE} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)"),
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    writer = None
    for batch in parquet.iter_batches(batch_size=batch_rows):
        if writer is None:
            writer = pa_csv.CSVWriter(process.stdin, batch.schema)
        writer.write_batch(batch)
    if writer is not None:
        writer.close()
    process.stdin.close()
    assert process.stderr is not None
    stderr = process.stderr.read()
    process.wait()
    if process.returncode != 0:
        raise PostgresError(f"COPY failed: {stderr.decode(errors='replace')[:600]}")
    rows = provenance_rows(provenance)
    values = ",\n".join(f"({_sql_literal(key)}, {_sql_literal(value)})" for key, value in sorted(rows.items()))
    psql.run(f"INSERT INTO benchmark_provenance (key, value) VALUES\n{values};", tuples=False)
    psql.run(f"VACUUM (ANALYZE) {TABLE};", tuples=False)
    loaded = int(psql.scalar(f"SELECT count(*) FROM {TABLE};"))
    if loaded != int(provenance["snapshot"]["rows"]):
        raise PostgresError(f"loaded {loaded} rows, snapshot has {provenance['snapshot']['rows']}")
    return {"rows_loaded": loaded, "columns": fields, "provenance_keys": len(rows)}
