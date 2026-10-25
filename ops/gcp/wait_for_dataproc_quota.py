"""CLI quota-readiness gate placed immediately before Dataproc submission."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from data_platform.config.spark_profiles import (
    quota_requirements,
    resolve_spark_profile,
    validate_benchmark_id,
)
from ops.gcp.dataproc_quota import (
    GcloudQuotaReader,
    PollingPolicy,
    QuotaTimeoutError,
    wait_for_quota,
)


_SAFE_BATCH_ID = re.compile(r"^[a-z][a-z0-9-]{2,48}[a-z0-9]$")
HANDOFFS = (
    "external-to-bronze",
    "bronze-to-silver",
    "silver-to-gold",
    "gold-to-features",
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--spark-profile", required=True, choices=("baseline", "optimized"))
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--handoff", required=True, choices=HANDOFFS)
    parser.add_argument("--predecessor-batch-id", default="")
    parser.add_argument("--evidence-output", required=True, type=Path)
    parser.add_argument("--initial-poll-seconds", type=float, default=15)
    parser.add_argument("--max-poll-seconds", type=float, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_benchmark_id(args.benchmark_id)
    profile = resolve_spark_profile(args.spark_profile)
    requirement = quota_requirements(profile.name)
    if args.predecessor_batch_id and not _SAFE_BATCH_ID.fullmatch(args.predecessor_batch_id):
        raise ValueError("predecessor batch ID is not a safe GCP identifier")
    policy = PollingPolicy(
        args.initial_poll_seconds,
        args.max_poll_seconds,
        args.timeout_seconds,
    )
    base = {
        "project": args.project,
        "region": args.region,
        "benchmark_id": args.benchmark_id,
        "spark_profile": profile.name,
        "handoff": args.handoff,
        "required": requirement,
        "polling": {
            "initial_interval_seconds": policy.initial_interval_seconds,
            "maximum_interval_seconds": policy.max_interval_seconds,
            "timeout_seconds": policy.timeout_seconds,
        },
        "quota_sources": {
            "cpu": "compute.project-info/CPUS_ALL_REGIONS",
            "disk": "compute.regions/DISKS_TOTAL_GB",
        },
        "evidence_output": str(args.evidence_output),
    }
    if args.predecessor_batch_id:
        base["predecessor_batch_id"] = args.predecessor_batch_id
    if not args.execute:
        print(json.dumps({"status": "DRY_RUN", **base}, indent=2, sort_keys=True))
        return 0

    reader = GcloudQuotaReader(args.project, args.region)
    try:
        predecessor = (
            reader.predecessor(args.predecessor_batch_id)
            if args.predecessor_batch_id
            else None
        )
        result = wait_for_quota(
            project=args.project,
            region=args.region,
            requirement=requirement,
            read_snapshot=reader.snapshot,
            policy=policy,
            predecessor=predecessor,
            on_observation=lambda snapshot, poll, elapsed: print(
                json.dumps(
                    {
                        "event": "quota_observation",
                        "poll_count": poll,
                        "elapsed_seconds": elapsed,
                        "observed": snapshot.as_dict(),
                    },
                    sort_keys=True,
                ),
                flush=True,
            ),
        )
    except QuotaTimeoutError as exc:
        result = {**base, **exc.evidence}
        _write_json(args.evidence_output, result)
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    except (ValueError, subprocess.CalledProcessError) as exc:
        result = {**base, "status": "ERROR", "error": str(exc)}
        _write_json(args.evidence_output, result)
        print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    result = {**base, **result}
    _write_json(args.evidence_output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
