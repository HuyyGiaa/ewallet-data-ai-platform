"""Arrow-to-PostgreSQL type mapping and the benchmark DDL."""

from __future__ import annotations

from typing import Sequence

from . import TABLE

ARROW_TO_POSTGRES = {
    "string": "text",
    "large_string": "text",
    "double": "double precision",
    "float": "real",
    "int32": "integer",
    "int64": "bigint",
    "bool": "boolean",
    "date32[day]": "date",
    "timestamp[us, tz=UTC]": "timestamptz",
    "timestamp[us]": "timestamp",
}
PRIMARY_KEY = "transaction_id"


def postgres_type(arrow_type: str) -> str:
    """PostgreSQL type of one Arrow type string.

    Raises:
        ValueError: The type has no reviewed mapping (never guessed).
    """

    try:
        return ARROW_TO_POSTGRES[arrow_type]
    except KeyError as exc:
        raise ValueError(f"no reviewed PostgreSQL mapping for Arrow type {arrow_type!r}") from exc


def create_table_sql(fields: Sequence[tuple[str, str]], *, table: str = TABLE) -> str:
    """``CREATE TABLE`` with the snapshot's columns, NOT NULL key and a primary key only.

    The primary key mirrors the Gold grain; no workload-specific secondary index exists.
    """

    columns = [f"    {name} {postgres_type(kind)}{' NOT NULL' if name == PRIMARY_KEY else ''}"
               for name, kind in fields]
    columns.append(f"    CONSTRAINT {table}_pkey PRIMARY KEY ({PRIMARY_KEY})")
    return f"CREATE TABLE {table} (\n" + ",\n".join(columns) + "\n);"


PROVENANCE_DDL = """CREATE TABLE benchmark_provenance (
    key text PRIMARY KEY,
    value text NOT NULL
);"""
