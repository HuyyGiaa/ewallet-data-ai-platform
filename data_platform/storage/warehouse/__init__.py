"""PostgreSQL index benchmark on real 5GB Gold data (EDAI-1 rubric row 21).

Pipeline, one module per responsibility:

``source``     verified, read-only extraction of Gold ``fact_transactions`` rows of
               the 5GB campaign into an immutable local Parquet snapshot;
``schema``     Delta/Arrow to PostgreSQL type mapping and DDL;
``postgres``   ``psql`` adapter for the isolated benchmark container;
``load``       snapshot import, ``VACUUM ANALYZE`` and the provenance table;
``workload``   data profile, data-derived query parameters, query and index SQL;
``benchmark``  WH-A read, WH-B index, WH-C write and WH-D equivalence experiments;
``dbeaver``    the SQL session the owner runs in DBeaver for WH01-WH06.

No module generates rows: every benchmark row comes from the campaign's Gold table.
"""

import os

CONTAINER = "edai1-wh5gb-postgres"  # default; EDAI1_WH_CONTAINER selects another instance
CONTAINER_ENV = "EDAI1_WH_CONTAINER"


def container_name() -> str:
    """Target PostgreSQL container: ``$EDAI1_WH_CONTAINER`` if set, else :data:`CONTAINER`."""

    return os.environ.get(CONTAINER_ENV, "").strip() or CONTAINER


DATABASE = "warehouse"
USER = "warehouse"
TABLE = "fact_transactions"
REPORT_SCHEMA = "edai1-warehouse-benchmark-v2"
