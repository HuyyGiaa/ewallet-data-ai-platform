"""Publish the verification scenario and validate the dual-store result.

``publish`` creates fresh single-partition topics for one namespace and sends the
scenario phase by phase. ``validate`` reads the immutable history and Feast online
store and writes a JSON report; it exits non-zero unless the report says PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator, Sequence

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.benchmark.replay import create_topics
from data_platform.streaming.config.settings import profile_by_name
from data_platform.streaming.contracts.topics import topic_name
from data_platform.streaming.verification.consistency import (
    build_report,
    check_dispositions,
    compare_online_state,
    compare_persisted_offline_online,
    expected_online_state,
    offline_rows_from_feast,
    online_rows_from_feast,
    parse_history,
    validate_offline_identity,
    validate_report_contract,
)
from data_platform.streaming.verification.scenario import DEFAULT_START, build_scenario


def _publish(bootstrap_servers: str, topic: str, records: Sequence[str]) -> None:
    from confluent_kafka import Producer

    producer = Producer({"bootstrap.servers": bootstrap_servers, "enable.idempotence": True})
    for record in records:
        producer.produce(topic, record.encode("utf-8"), partition=0)
    if producer.flush(30) != 0:
        raise RuntimeError("records were not delivered to Redpanda")


def publish(args: argparse.Namespace) -> int:
    """Create isolated topics and publish each phase, pausing for the watermark."""

    profile = profile_by_name(args.profile)
    scenario = build_scenario(args.namespace, event_time=profile.event_time, start=DEFAULT_START)
    if args.manifest.exists():
        raise FileExistsError(f"{args.manifest} exists; each run needs a new namespace")
    if args.create_topics:
        create_topics(args.bootstrap_servers, args.namespace, partitions=1)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(scenario.manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.create_topics_only:
        return 0
    topic = topic_name("raw", args.namespace)
    for phase in scenario.phases:
        _publish(args.bootstrap_servers, topic, phase.records)
        print(json.dumps({"event": "phase_published", "phase": phase.name, "records": len(phase.records)}), flush=True)
        time.sleep(args.phase_delay_seconds)
    return 0


def read_history(root: str, run_id: str) -> Iterator[bytes]:
    """Yield history lines from a ``gs://`` or ``file://`` root."""

    from fsspec.core import url_to_fs

    filesystem, base = url_to_fs(root)
    for path in sorted(filesystem.glob(f"{base.rstrip('/')}/runs/{run_id}/partition=*/*.jsonl")):
        with filesystem.open(path, "rb") as stream:
            yield from stream.read().splitlines()


def _git_sha() -> str:
    configured = os.environ.get("EWALLET_GIT_SHA")
    if configured:
        return configured
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or "unknown"


def validate(args: argparse.Namespace) -> int:
    """Compare history with Feast and write the report."""

    from data_platform.config.dataset_identity import (
        validate_feature_store_storage,
        validate_local_feature_store_storage,
    )

    registry_uri = os.environ.get("EWALLET_FEAST_REGISTRY", "")
    if registry_uri.startswith("gs://"):
        validate_feature_store_storage(
            dataset_version=os.environ.get("EWALLET_DATASET_VERSION", ""),
            offline_feature_root=os.environ.get("EWALLET_OFFLINE_FEATURE_ROOT", ""),
            feast_offline_data_root=args.offline_feature_root,
            history_root=args.history_root,
            online_registry_uri=registry_uri,
            campaign_id=args.campaign_id,
            run_id=args.run_id,
        )
    else:
        validate_local_feature_store_storage(
            feast_offline_data_root=args.offline_feature_root,
            history_root=args.history_root,
            online_registry_uri=registry_uri,
            campaign_id=args.campaign_id,
            run_id=args.run_id,
        )

    from feast import FeatureStore

    from ai_platform.features.feast.registry_mirror import mirror_from_environment

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["namespace"] != args.namespace:
        raise ValueError("manifest belongs to another namespace")
    records, repeats = parse_history(read_history(args.history_root, args.run_id))
    expected = expected_online_state(records, manifest["expected"]["closing"])
    validate_offline_identity(args.offline_feature_root, args.campaign_id, args.run_id)
    mirror_from_environment()
    store = FeatureStore(repo_path=args.feast_repo)
    for view, spec in FEATURE_SPECS.items():
        source = store.get_feature_view(view, allow_registry_cache=False).batch_source
        expected_path = f"{args.offline_feature_root.rstrip('/')}/{spec['table']}"
        if str(source.path).rstrip("/") != expected_path:
            raise ValueError(f"{view} is not bound to the persisted Offline feature root")
    online = online_rows_from_feast(store, expected)
    offline = offline_rows_from_feast(
        store,
        {key: snapshot.feature_timestamp for key, snapshot in expected.items()},
    )
    offline_views = sorted({view for view, _ in offline})
    pit_status_counts: dict[str, int] = {}
    for row in offline.values():
        status = str(row.get("__pit_status", "NOT_VERIFIED"))
        pit_status_counts[status] = pit_status_counts.get(status, 0) + 1
    pit_values_verified = pit_status_counts.get("PIT_VALUES_VERIFIED", 0)
    offline_verification = {
        "status": (
            "PASS"
            if expected
            and set(offline) == set(expected)
            and offline_views == sorted(FEATURE_SPECS)
            and pit_values_verified == len(expected)
            else "FAIL"
        ),
        "campaign_id": args.campaign_id,
        "run_id": args.run_id,
        "offline_feature_root": args.offline_feature_root,
        "rows_retrieved": len(offline),
        "pit_status_counts": pit_status_counts,
        "feature_views": offline_views,
        "historical_features": sum(len(spec["features"]) for spec in FEATURE_SPECS.values()),
    }
    direct_comparison = compare_persisted_offline_online(offline, online)
    report = build_report(
        manifest,
        records,
        repeats,
        expected,
        compare_online_state(expected, online),
        check_dispositions(records, manifest),
        {
            "git_sha": _git_sha(),
            "history_root": args.history_root,
            "offline_feature_root": args.offline_feature_root,
            "campaign_id": args.campaign_id,
            "run_id": args.run_id,
            "feast_registry": os.environ.get("EWALLET_FEAST_REGISTRY"),
            "bootstrap_servers": args.bootstrap_servers,
        },
        offline_verification=offline_verification,
        direct_comparison=direct_comparison,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in (
        "status", "features_compared", "entities_compared", "history_transactions",
        "history_repeats_dropped", "offline_verification",
        "direct_offline_online_comparison")}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


def check_report(args: argparse.Namespace) -> int:
    """Validate the persisted machine report used by the cloud runner verdict."""

    report = json.loads(args.report.read_text(encoding="utf-8"))
    validate_report_contract(report)
    print(json.dumps({"status": "PASS", "report": str(args.report)}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""

    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    send = commands.add_parser("publish")
    send.add_argument("--bootstrap-servers", required=True)
    send.add_argument("--namespace", required=True)
    send.add_argument("--manifest", type=Path, required=True)
    send.add_argument("--profile", choices=("baseline", "optimized"), default="baseline")
    send.add_argument("--phase-delay-seconds", type=float, default=10.0)
    send.add_argument("--create-topics", action="store_true")
    send.add_argument("--create-topics-only", action="store_true")
    send.set_defaults(handler=publish)
    check = commands.add_parser("validate")
    check.add_argument("--bootstrap-servers", default="")
    check.add_argument("--namespace", required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--history-root", required=True)
    check.add_argument("--offline-feature-root", required=True)
    check.add_argument("--campaign-id", required=True)
    check.add_argument("--run-id", required=True)
    check.add_argument("--feast-repo", required=True)
    check.add_argument("--report", type=Path, required=True)
    check.set_defaults(handler=validate)
    report = commands.add_parser("check-report")
    report.add_argument("--report", type=Path, required=True)
    report.set_defaults(handler=check_report)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""

    args = parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
