"""Materialization clock of feature tables: one UTC instant per run, recorded with the write.

``created`` on a feature row is the run's materialization instant, not a value
derived from the row. The pipelines capture that instant once, write it into
``created`` and record it in the Delta commit's ``userMetadata``. A validator can
then check provenance directly: every ``created`` value must equal the clock that
the commit recorded. That holds whatever the event times are, so ``created`` may
be earlier than, equal to or later than ``event_timestamp``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


MATERIALIZATION_KEY = "ewallet_materialized_at"


def require_utc(value: datetime) -> datetime:
    """Refuse naive or non-UTC instants; PySpark would read a naive one as local time."""

    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("materialized_at must be a timezone-aware UTC datetime")
    return value


def materialization_metadata(materialized_at: datetime) -> str:
    """Delta ``userMetadata`` recording the clock written to ``created``."""

    instant = require_utc(materialized_at).astimezone(timezone.utc)
    return json.dumps({MATERIALIZATION_KEY: instant.isoformat(timespec="microseconds")}, sort_keys=True)


def parse_materialization_metadata(user_metadata: str | None) -> datetime | None:
    """Return the recorded clock, or None if the commit did not record one."""

    if not user_metadata:
        return None
    try:
        value = json.loads(user_metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get(MATERIALIZATION_KEY), str):
        return None
    return require_utc(datetime.fromisoformat(value[MATERIALIZATION_KEY]))


def read_materialization_clock(spark, table_uri: str) -> datetime | None:
    """Read the clock recorded by the latest commit of one Delta table."""

    latest = spark.sql(f"DESCRIBE HISTORY delta.`{table_uri}` LIMIT 1").select("userMetadata").first()
    return parse_materialization_metadata(latest["userMetadata"] if latest else None)
