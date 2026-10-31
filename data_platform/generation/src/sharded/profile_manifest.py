"""Produce a truthful offline-profile report from immutable control metadata.

Manifest-only metrics are reported as measured.  Distribution metrics that
require reading Parquet columns remain explicitly PENDING instead of being
inferred from generator configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_profile(plan: dict[str, object], manifest: dict[str, object]) -> dict[str, object]:
    logical = int(manifest["logical_transactions"])
    physical = int(manifest["physical_transactions"])
    duplicates = physical - logical
    tables = manifest["tables"]
    if manifest["status"] != "COMPLETE" or plan["digest"] != manifest["plan_digest"]:
        raise ValueError("manifest is incomplete or does not match the immutable plan")
    return {
        "dataset_version": manifest["dataset_version"],
        "plan_digest": manifest["plan_digest"],
        "global_seed": plan["global_seed"],
        "shard_count": manifest["shard_count"],
        "total_bytes": manifest["total_bytes"],
        "logical_transactions": logical,
        "physical_transactions": physical,
        "duplicate_rows": duplicates,
        "duplicate_rate_percent": round(duplicates / logical * 100, 6),
        "schema_evolution": {
            "transactions_v1_rows": tables["transactions_v1"]["rows"],
            "transactions_v2_rows": tables["transactions_v2"]["rows"],
            "transactions_v1_schema_sha256": tables["transactions_v1"]["schema_sha256"],
            "transactions_v2_schema_sha256": tables["transactions_v2"]["schema_sha256"],
            "different_schema_hash": (
                tables["transactions_v1"]["schema_sha256"]
                != tables["transactions_v2"]["schema_sha256"]
            ),
        },
        "column_distributions": {
            "merchant_skew_top_n_share": {"status": "PENDING_COLUMN_SCAN"},
            "merchant_max_to_median_ratio": {"status": "PENDING_COLUMN_SCAN"},
            "transaction_id_unique_ratio": {"status": "PENDING_COLUMN_SCAN"},
            "login_id_unique_ratio": {"status": "PENDING_COLUMN_SCAN"},
        },
        "claim_boundary": (
            "No skew or cardinality claim is made from manifest/configuration alone. "
            "Run a bounded read-only column aggregation after billing authorization."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    manifest_value = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest = manifest_value.get("manifest_without_file_lists", manifest_value)
    payload = build_profile(plan, manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
