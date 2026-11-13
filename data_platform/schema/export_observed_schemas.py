"""Export the observed schemas of one verified campaign profile, metadata only.

Owner-run, read-only. The command takes the local byte copies of one profile's
campaign documents (``campaign_evidence.load_campaign`` verifies their checksum
chain and identity), derives every GCS location from them, and reads:

* the Source dataset manifest. Its SHA-256 must equal the campaign's pinned
  ``source_manifest_sha256``, and its physical datasets must equal the dataset's
  expected inventory. Each Source schema comes from one Parquet footer, read with a
  byte-range request for the file's last bytes (metadata only); the footer must
  hash to the manifest's ``schema_sha256``;
* each Bronze, Silver, Gold and feature run root of the campaign. The table
  directories must equal the expected inventory exactly, and each table's schema is
  the last ``metaData`` action in its contiguous ``_delta_log`` JSON commits.

No data file is read, nothing is written to GCS, and the output carries no customer
value: column names, types, nullability, partition columns and Delta versions. The
output's ``provenance`` binds it to the campaign, so the diagram renderer can refuse a
snapshot of any other dataset (for example the historical 100GB runs).

GCS is read through ``gsutil``; independent reads run in a small thread pool.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from data_platform.contracts.schema_inventory import ZoneInventory, logical_source_table, zone_inventory
from data_platform.metadata.datahub.campaign_evidence import CampaignEvidence, load_campaign


OBSERVED_SCHEMA_VERSION = "ewallet-observed-schemas-v2"
FOOTER_TAIL_BYTES = 65536
_COMMIT = re.compile(r"/_delta_log/(\d{20})\.json$")
# comparison.json stage -> observed zone
COMPARISON_ZONES = {"dp1-bronze": "bronze", "dp2-silver": "silver", "dp2-gold": "gold", "dp3-features": "features"}

ReadBytes = Callable[[str], bytes]
ListObjects = Callable[[str], list[str]]
ReadTail = Callable[[str, int], bytes]


class ExportError(RuntimeError):
    """The cloud metadata does not match the verified campaign."""


def _run(command: Sequence[str]) -> bytes:
    result = subprocess.run(list(command), capture_output=True, check=False)
    if result.returncode != 0:
        raise ExportError(f"{' '.join(command[:3])} failed: {result.stderr[:300]!r}")
    return result.stdout


def gsutil_read(uri: str) -> bytes:
    """Return one object's bytes."""

    return _run(["gsutil", "cat", uri])


def gsutil_list(pattern: str) -> list[str]:
    """Return the object or prefix URIs matching ``pattern``."""

    return sorted(line.strip() for line in _run(["gsutil", "ls", pattern]).decode("utf-8").splitlines()
                  if line.strip())


def gsutil_read_tail(uri: str, size: int) -> bytes:
    """Return the last ``size`` bytes of one object (a byte-range read)."""

    return _run(["gsutil", "cat", "-r", f"-{size}", uri])


def parse_delta_metadata(commit_text: str) -> dict[str, Any] | None:
    """Return the columns and partition columns of a commit's ``metaData`` action, if any."""

    for line in commit_text.splitlines():
        if not line.strip():
            continue
        action = json.loads(line)
        if "metaData" in action:
            schema = json.loads(action["metaData"]["schemaString"])
            return {
                "columns": [
                    {"name": item["name"], "type": item["type"], "nullable": bool(item["nullable"])}
                    for item in schema["fields"]
                ],
                "partition_columns": list(action["metaData"].get("partitionColumns", [])),
            }
    return None


def delta_table_schema(table_root: str, commits: Sequence[str], read: ReadBytes) -> dict[str, Any]:
    """Return the current schema of one Delta table from its JSON commits.

    Raises:
        ExportError: The log is empty, not contiguous from version 0, or holds no
            ``metaData`` action.
    """

    versions = sorted((int(match.group(1)), uri) for uri in commits if (match := _COMMIT.search(uri)))
    if not versions or [number for number, _ in versions] != list(range(len(versions))):
        raise ExportError(f"{table_root}: _delta_log JSON commits are missing or not contiguous from 0")
    current: dict[str, Any] | None = None
    for _, uri in versions:
        found = parse_delta_metadata(read(uri).decode("utf-8"))
        if found is not None:
            current = found
    if current is None:
        raise ExportError(f"{table_root}: no metaData action in the Delta log")
    return {**current, "delta_version": versions[-1][0], "commit_count": len(versions)}


def export_zone(
    root: str,
    run_id: str,
    zone: str,
    inventory: ZoneInventory,
    list_objects: ListObjects,
    read: ReadBytes,
    workers: int = 4,
) -> dict[str, Any]:
    """Return the schema of every Delta table under one run root.

    Raises:
        ExportError: The table set differs from the inventory, or a log is unreadable.
    """

    tables = sorted(uri.rstrip("/").rsplit("/", 1)[-1] for uri in list_objects(f"{root}/") if uri.endswith("/"))
    try:
        inventory.require_tables(zone, tables)
    except ValueError as exc:
        raise ExportError(str(exc)) from exc
    commits = list_objects(f"{root}/*/_delta_log/*.json")
    by_table = {table: [uri for uri in commits if uri.startswith(f"{root}/{table}/_delta_log/")] for table in tables}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        schemas = dict(zip(tables, pool.map(
            lambda table: delta_table_schema(f"{root}/{table}", by_table[table], read), tables)))
    return {"run_id": run_id, "root": root, "tables": schemas}


def parse_parquet_footer(tail: bytes) -> tuple[list[dict[str, object]], str]:
    """Return the columns of a Parquet file and the schema hash the generator records.

    Raises:
        ValueError: The bytes are not a Parquet tail or the footer is truncated.
    """

    import pyarrow as pa
    import pyarrow.parquet as pq

    if len(tail) < 12 or tail[-4:] != b"PAR1":
        raise ValueError("not the tail of a Parquet file")
    footer_length = struct.unpack("<I", tail[-8:-4])[0]
    if footer_length + 8 > len(tail):
        raise ValueError(f"footer of {footer_length} bytes is longer than the tail read")
    metadata = pq.read_metadata(pa.BufferReader(b"PAR1" + tail[-(footer_length + 8):]))
    schema = metadata.schema.to_arrow_schema()
    columns = [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]
    return columns, hashlib.sha256(str(schema).encode("utf-8")).hexdigest()


def export_source(
    campaign: CampaignEvidence,
    inventory: ZoneInventory,
    read: ReadBytes,
    read_tail: ReadTail,
) -> dict[str, Any]:
    """Return the Source inventory: manifest counts plus each dataset's footer schema.

    Raises:
        ExportError: The manifest bytes differ from the campaign pin, the manifest is
            incomplete or names other datasets, or a footer disagrees with it.
    """

    raw = read(campaign.source_manifest_uri)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != campaign.source_manifest_sha256:
        raise ExportError(f"source manifest SHA-256 {digest} is not the campaign's {campaign.source_manifest_sha256}")
    manifest = json.loads(raw)
    if manifest.get("status") != "COMPLETE" or manifest.get("dataset_version") != campaign.dataset_version:
        raise ExportError("source manifest is not the COMPLETE manifest of the campaign dataset")
    try:
        inventory.require_tables("source", list(manifest["tables"]))
    except ValueError as exc:
        raise ExportError(str(exc)) from exc
    tables = {}
    for physical, entry in sorted(manifest["tables"].items()):
        if len(entry["schema_sha256"]) != 1:
            raise ExportError(f"{physical} has {len(entry['schema_sha256'])} schemas across its shards")
        first = min(entry["files"], key=lambda item: item["path"])
        columns, schema_digest = parse_parquet_footer(read_tail(f"{campaign.source_root}/{first['path']}",
                                                                FOOTER_TAIL_BYTES))
        if schema_digest != entry["schema_sha256"][0]:
            raise ExportError(f"{physical} footer schema does not match the manifest schema_sha256")
        tables[physical] = {
            "logical_table": logical_source_table(physical),
            "rows": entry["rows"],
            "files": len(entry["files"]),
            "bytes": entry["bytes"],
            "schema_sha256": schema_digest,
            "columns": columns,
        }
    return {"dataset_version": manifest["dataset_version"], "plan_digest": manifest["plan_digest"],
            "manifest_sha256": digest, "root": campaign.source_root, "tables": tables}


def provenance(campaign: CampaignEvidence, exported_at: str) -> dict[str, Any]:
    """Identity of the campaign profile the snapshot was exported from."""

    return {
        **campaign.identity(),
        "source_plan_digest": campaign.source_plan_digest,
        "data_bucket": campaign.data_bucket,
        "run_ids": {stage.zone: stage.run_id for stage in campaign.stages.values()},
        "exported_at": exported_at,
        "method": "Delta _delta_log metaData and Parquet footer byte-range reads; no data file read",
    }


def export_campaign(
    campaign: CampaignEvidence,
    *,
    read: ReadBytes = gsutil_read,
    list_objects: ListObjects = gsutil_list,
    read_tail: ReadTail = gsutil_read_tail,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Return the observed-schema snapshot of one verified campaign profile."""

    inventory = zone_inventory(campaign.dataset_version)
    zones = {
        stage.zone: export_zone(stage.output_root, stage.run_id, stage.zone, inventory, list_objects, read)
        for stage in campaign.stages.values()
    }
    return {
        "schema_version": OBSERVED_SCHEMA_VERSION,
        "provenance": provenance(campaign, exported_at or datetime.now(timezone.utc).isoformat()),
        "source": export_source(campaign, inventory, read, read_tail),
        "zones": zones,
    }


def comparison_problems(observed: Mapping[str, Any], comparison: Mapping[str, Any]) -> list[str]:
    """Compare observed column names with the semantic comparison's column lists.

    ``comparison.json`` of the campaign lists, for all 34 Delta tables, the columns
    Spark read from both profiles (compared plus excluded audit columns). Observed
    columns must contain exactly those names, so a snapshot of another run fails.
    """

    problems = []
    if comparison.get("source_manifest_sha256") != (observed.get("provenance") or {}).get("source_manifest_sha256"):
        problems.append("comparison and observed snapshot name different source manifests")
    for stage in comparison.get("stages", []):
        zone = COMPARISON_ZONES.get(stage.get("stage"))
        if zone is None:
            problems.append(f"unknown comparison stage {stage.get('stage')!r}")
            continue
        for table in stage.get("tables", []):
            entry = observed["zones"].get(zone, {}).get("tables", {}).get(table["table"])
            if entry is None:
                problems.append(f"{zone}.{table['table']} is in the comparison but not observed")
                continue
            expected = set(table.get("compared_columns", [])) | set(table.get("excluded_audit_columns", []))
            found = {column["name"] for column in entry["columns"]}
            if found != expected:
                problems.append(f"{zone}.{table['table']} columns differ from the comparison: "
                                f"missing={sorted(expected - found)} extra={sorted(found - expected)}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Export one campaign profile to a local JSON file."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True,
                        help="local copies of one profile's four campaign documents")
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", required=True, choices=("baseline", "optimized"))
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--comparison", type=Path, help="campaign comparison.json for a column cross-check")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    campaign = load_campaign(args.manifest_dir, benchmark_id=args.benchmark_id, spark_profile=args.spark_profile,
                             dataset_version=args.dataset_version,
                             source_manifest_sha256=args.source_manifest_sha256)
    document = export_campaign(campaign)
    if args.comparison:
        problems = comparison_problems(document, json.loads(args.comparison.read_text(encoding="utf-8")))
        if problems:
            raise ExportError("observed schema disagrees with the campaign comparison: " + "; ".join(problems))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = {zone: len(value["tables"]) for zone, value in document["zones"].items()}
    print(json.dumps({"output": str(args.output), "source_physical": len(document["source"]["tables"]), **counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
