"""Fail-closed, single-trigger DP2 Method-2 orchestration primitives.

Airflow invokes one action per task. No Spark transformations are changed.
Only ``quota``, ``submit`` and ``summary`` can launch or wait on GCP work.
Every action creates external, immutable evidence (never inside the Git tree).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from data_platform.config.spark_benchmark import (
    WINNER_SCHEMA, load_pair_manifest, pair_member, winner_artifact_digest,
)
from data_platform.config.spark_profiles import (
    require_profile_binding, validate_benchmark_id, validate_git_sha,
)
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.summarize_spark_event_log import summarize_event_logs

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES = ("baseline", "optimized")
STAGES = {"silver": "dp2-silver", "gold": "dp2-gold"}
TABLES = {
    "silver": ("users", "accounts", "merchants", "devices", "transactions",
               "balance_snapshots", "login_events", "fraud_labels"),
    "gold": ("dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date",
             "fact_transactions", "fact_login_events", "fact_balance_snapshot",
             "obt_transaction_enriched", "feat_user_90d", "opt_merchant_performance"),
}
EVENT_NAME = re.compile(r"/app-(\d{14})\d{3}-\d+\.(?:zstd|gz|jsonl)$")


def _json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return obj


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(dict(payload), f, indent=2, sort_keys=True)
        f.write("\n")


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(list(args), check=True, capture_output=capture, text=True,
                            cwd=REPO_ROOT)
    return result.stdout.strip() if capture else ""


def _module(module: str, *args: str) -> None:
    _run(sys.executable, "-m", module, *args)


def _winner(path: Path, *, stage: str, benchmark: str, bucket: str,
            expected_git_sha: str | None = None) -> dict[str, Any]:
    data = _json(path)
    required = {"schema_version": WINNER_SCHEMA, "stage": stage,
                "benchmark_id": benchmark, "dataset_version": "canonical-100gb-v1"}
    for key, value in required.items():
        if data.get(key) != value:
            raise ValueError(f"Wrong winner {key}: {data.get(key)!r}")
    if data.get("winner_artifact_digest") != winner_artifact_digest(data):
        raise ValueError("Winner artifact digest mismatch")
    if expected_git_sha is not None and data.get("git_sha") != expected_git_sha:
        raise ValueError("Winner source Git SHA mismatch")
    profile = data.get("selected_profile")
    if profile not in PROFILES:
        raise ValueError("Winner has no recognized selected_profile")
    selected = data.get("compared_outputs", {}).get(profile, {})
    if (data.get("selected_output_run_id") != selected.get("output_run_id") or
            data.get("selected_output_root") != selected.get("output_root")):
        raise ValueError("Winner selected output differs from compared output")
    run_id = data["selected_output_run_id"]
    kind = {"dp1-bronze": "bronze", "dp2-silver": "silver"}[stage]
    require_profile_binding(run_id, benchmark_id=benchmark, profile=profile, stage=kind)
    exact_root = f"gs://{bucket}/lakehouse/runs/{run_id}/{kind}"
    if data["selected_output_root"] != exact_root:
        raise ValueError("Winner output root does not match configured data bucket")
    return data


def _require_head(sha: str) -> None:
    head = _run("git", "rev-parse", "HEAD", capture=True)
    if head != sha:
        raise ValueError("Configured Git SHA does not match HEAD")


def _require_clean_head(sha: str) -> None:
    _require_head(sha)
    status = _run("git", "status", "--porcelain", capture=True)
    if status:
        raise ValueError("Execution requires the exact clean committed Git HEAD")


def _pair_path(root: Path, stage: str) -> Path:
    return root / f"dp2-{stage}-pair.json"


def _winner_path(root: Path, stage: str) -> Path:
    return root / f"dp2-{stage}-winner.json"


def _profile_dir(root: Path, stage: str, profile: str) -> Path:
    return root / stage / profile


def _require_pair(args: argparse.Namespace, stage: str) -> tuple[dict[str, Any], str, str, list[str]]:
    manifest = load_pair_manifest(_pair_path(args.root, stage))
    if (manifest["stage"] != STAGES[stage] or
        manifest["benchmark_id"] != args.benchmark_id or
        manifest["dataset_version"] != args.dataset_version or
        manifest["git_sha"] != args.git_sha):
        raise ValueError("DP2 pair identity mismatch")
    upstream = list(manifest["upstream_run_ids"])
    if stage == "silver":
        source = _winner(args.bronze_winner, stage="dp1-bronze",
                         benchmark=args.benchmark_id, bucket=args.data_bucket)
    else:
        source = _winner(_winner_path(args.root, "silver"), stage="dp2-silver",
                         benchmark=args.benchmark_id, bucket=args.data_bucket,
                         expected_git_sha=args.git_sha)
    if upstream != [source["selected_output_run_id"]]:
        raise ValueError("Pair upstream does not match selected immutable winner")
    member = pair_member(manifest, args.profile)
    expected_root = (f"gs://{args.data_bucket}/lakehouse/runs/"
                     f"{member['output_run_id']}/{stage}")
    if member["output_root"] != expected_root:
        raise ValueError("Pair output is outside configured immutable GCS prefix")
    return manifest, str(member["output_run_id"]), str(member["output_root"]), upstream


def _create_pair(args: argparse.Namespace, stage: str, upstream: str) -> None:
    identities = {profile: getattr(args, f"{profile}_{stage}_run_id") for profile in PROFILES}
    for profile, run_id in identities.items():
        require_profile_binding(run_id, benchmark_id=args.benchmark_id,
                                profile=profile, stage=stage)
    payload = build_stage_pair_manifest(
        stage=STAGES[stage], dataset_version=args.dataset_version,
        git_sha=args.git_sha, upstream_run_ids=[upstream],
        baseline_output_run_id=identities["baseline"],
        baseline_output_root=f"gs://{args.data_bucket}/lakehouse/runs/{identities['baseline']}/{stage}",
        candidate_output_run_id=identities["optimized"],
        candidate_output_root=f"gs://{args.data_bucket}/lakehouse/runs/{identities['optimized']}/{stage}",
    )
    _write_once(_pair_path(args.root, stage), payload)
    print(_pair_path(args.root, stage))


def _validation_batch_id(run_id: str, stage: str, profile: str) -> str:
    # Batch IDs have a fixed maximum length; they are distinct from ingestion runs.
    suffix = run_id.rsplit("-r", 1)[-1]
    if not re.fullmatch(r"\d{12}", suffix):
        raise ValueError("Invalid run timestamp")
    return f"abval-{'b' if profile == 'baseline' else 'o'}-{'s' if stage == 'silver' else 'g'}-r{suffix}"


def _summary_uri(args: argparse.Namespace, stage: str, profile: str, run_id: str) -> str:
    return (f"gs://{args.data_bucket}/evidence/spark_benchmark/"
            f"{args.benchmark_id}/{profile}/{STAGES[stage]}/{run_id}-summary.json")


def _describe_batch(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    return json.loads(_run("gcloud", "dataproc", "batches", "describe", run_id,
                           f"--project={args.project}", f"--region={args.region}",
                           "--format=json", capture=True))


def _get_event_metrics(args: argparse.Namespace, stage: str, profile: str,
                       run_id: str, pair_digest: str, batch: Mapping[str, Any],
                       output: Path) -> dict[str, Any]:
    """Find only original-job event logs in the original Dataproc time window.

    This deliberately fails if no *unique* log matches the Method2 identity.
    Never silently uses a validator's event log as ingestion performance.
    """
    def timestamp(s: str) -> dt.datetime:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)

    if batch.get("state") != "SUCCEEDED":
        raise ValueError(f"Original batch {run_id} did not succeed")
    begin = timestamp(str(batch["createTime"])) - dt.timedelta(minutes=30)
    end = timestamp(str(batch["stateTime"])) + dt.timedelta(minutes=30)
    prefix = f"gs://{args.data_bucket}/spark-events/"
    listing = _run("gcloud", "storage", "ls", prefix + "*", capture=True)
    candidates = []
    for uri in listing.splitlines():
        match = EVENT_NAME.search(uri)
        if match:
            event_time = dt.datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
            if begin <= event_time <= end:
                candidates.append(uri)
    if not candidates or len(candidates) > 50:
        raise ValueError(f"Unexpected event-log candidate count ({len(candidates)})")
    matches: list[dict[str, Any]] = []
    temp_dir = output.parent / "event-logs"
    temp_dir.mkdir(parents=True, exist_ok=True)
    for number, uri in enumerate(candidates):
        suffix = Path(uri).suffix
        compressed = temp_dir / f"candidate-{number}{suffix}"
        _run("gcloud", "storage", "cp", uri, str(compressed))
        if suffix == ".zstd":
            decoded = temp_dir / f"candidate-{number}.jsonl"
            with decoded.open("xb") as stream:
                subprocess.run(["zstd", "-dc", str(compressed)], stdout=stream, check=True)
        else:
            decoded = compressed
        try:
            result = summarize_event_logs(
                [decoded], batch_id=run_id, output_run_id=run_id,
                pair_manifest_digest=pair_digest, source_uri=uri,
            )
        except ValueError:
            # Other jobs (including validations) share the same event-log bucket.
            continue
        matches.append(result)
    if len(matches) != 1:
        raise ValueError(f"Expected one original event log for {run_id}, found {len(matches)}")
    _write_once(output, matches[0])
    return matches[0]


def _action(args: argparse.Namespace) -> None:
    if args.action in ("prepare-silver", "prepare-gold", "approve"):
        validate_benchmark_id(args.benchmark_id)
        validate_git_sha(args.git_sha)
        if args.action == "approve":
            if args.approval != "I_APPROVE_DP2_PAID_JOBS":
                raise ValueError("Explicit DP2 cloud execution approval is missing")
            if not args.root.is_absolute() or args.root.resolve().is_relative_to(REPO_ROOT):
                raise ValueError("Artifact root must be an absolute path outside Git repository")
            if args.require_clean:
                _require_clean_head(args.git_sha)
            else:
                _require_head(args.git_sha)
            _winner(args.bronze_winner, stage="dp1-bronze",
                    benchmark=args.benchmark_id, bucket=args.data_bucket)
            for stage in STAGES:
                for profile in PROFILES:
                    require_profile_binding(getattr(args, f"{profile}_{stage}_run_id"),
                                            benchmark_id=args.benchmark_id,
                                            profile=profile, stage=stage)
            print("DP2 APPROVED: source, Bronze Winner and run identities validated")
            return
        _require_clean_head(args.git_sha)
        if args.action == "prepare-silver":
            source = _winner(args.bronze_winner, stage="dp1-bronze",
                             benchmark=args.benchmark_id, bucket=args.data_bucket)
            _create_pair(args, "silver", source["selected_output_run_id"])
        else:
            source = _winner(_winner_path(args.root, "silver"), stage="dp2-silver",
                             benchmark=args.benchmark_id, bucket=args.data_bucket,
                             expected_git_sha=args.git_sha)
            _create_pair(args, "gold", source["selected_output_run_id"])
        return

    if args.action == "finalize":
        # Stage-only finalization uses a baseline member for pair identity checks.
        # Bind the local profile too: downstream path construction requires a string.
        args.profile = "baseline"
    stage, profile = args.stage, args.profile
    pair, run_id, output_root, upstream = _require_pair(args, stage)
    base = _profile_dir(args.root, stage, profile)
    base.mkdir(parents=True, exist_ok=True)

    if args.action == "quota":
        # Strictly sequential profiles mean only one active DP2 ingestion at a time.
        predecessor = (upstream[0] if profile == "baseline" else
                       pair_member(pair, "baseline")["output_run_id"])
        _module("ops.gcp.wait_for_dataproc_quota", "--project", args.project,
                "--region", args.region, "--spark-profile", profile,
                "--benchmark-id", args.benchmark_id,
                "--handoff", "bronze-to-silver" if stage == "silver" else "silver-to-gold",
                "--predecessor-batch-id", str(predecessor),
                "--evidence-output", str(base / "quota.json"), "--execute")
    elif args.action == "submit":
        _require_clean_head(args.git_sha)
        _module("ops.gcp.submit_bronze_to_silver" if stage == "silver" else
                "ops.gcp.submit_silver_to_gold",
                "--dataset-version", args.dataset_version,
                "--benchmark-id", args.benchmark_id,
                "--source-bronze-run-id" if stage == "silver" else "--source-silver-run-id",
                upstream[0], "--run-id", run_id, "--spark-profile", profile,
                "--git-sha", args.git_sha, "--pair-manifest", str(_pair_path(args.root, stage)),
                "--ttl", args.ttl, "--execute")
    elif args.action == "verify":
        _module("ops.gcp.verify_delta_run", "--root", output_root,
                *(x for table in TABLES[stage] for x in ("--expected-table", table)))
    elif args.action == "summary":
        _require_clean_head(args.git_sha)
        # A semantic scan is also a paid Dataproc batch; gate its quota separately.
        _module("ops.gcp.wait_for_dataproc_quota", "--project", args.project,
                "--region", args.region, "--spark-profile", profile,
                "--benchmark-id", args.benchmark_id,
                "--handoff", "bronze-to-silver" if stage == "silver" else "silver-to-gold",
                "--predecessor-batch-id", run_id,
                "--evidence-output", str(base / "validation-quota.json"), "--execute")
        _module("ops.gcp.submit_spark_output_summary",
                "--batch-id", _validation_batch_id(run_id, stage, profile),
                "--benchmark-id", args.benchmark_id, "--spark-profile", profile,
                "--stage", STAGES[stage], "--output-run-id", run_id,
                "--dataset-version", args.dataset_version, "--git-sha", args.git_sha,
                "--validation-git-sha", args.git_sha,
                "--summary-uri", _summary_uri(args, stage, profile, run_id),
                "--source-run-id", upstream[0],
                "--pair-manifest", str(_pair_path(args.root, stage)), "--execute")
    elif args.action == "evidence":
        summary = base / "summary.json"
        performance = base / "original-performance.json"
        validation = base / "validation-evidence.json"
        batch_path = base / "original-batch.json"
        evidence = base / "batch-evidence.json"
        # GCS returns the read-only summary emitted by a distinct validation job.
        if summary.exists() or batch_path.exists():
            raise FileExistsError("Evidence has existing partial artifacts; inspect before retry")
        _run("gcloud", "storage", "cp", _summary_uri(args, stage, profile, run_id),
             str(summary))
        original = _describe_batch(args, run_id)
        _write_once(batch_path, original)
        _get_event_metrics(args, stage, profile, run_id,
                           str(pair["pair_manifest_digest"]), original, performance)
        _module("ops.gcp.build_spark_validation_evidence",
                "--summary-json", str(summary),
                "--original-performance-json", str(performance),
                "--pair-manifest", str(_pair_path(args.root, stage)),
                "--spark-profile", profile, "--output", str(validation))
        _module("ops.gcp.collect_spark_benchmark_evidence",
                "--project", args.project, "--region", args.region,
                "--batch-id", run_id, "--benchmark-id", args.benchmark_id,
                "--spark-profile", profile, "--stage", STAGES[stage],
                "--dataset-version", args.dataset_version, "--git-sha", args.git_sha,
                "--upstream-run-id", upstream[0], "--output-run-id", run_id,
                "--output-root", output_root, "--pair-manifest", str(_pair_path(args.root, stage)),
                "--validation-json", str(validation), "--batch-description-json", str(batch_path),
                "--output", str(evidence))
    elif args.action == "finalize":
        b, o = (_profile_dir(args.root, stage, p) for p in PROFILES)
        eq = args.root / f"dp2-{stage}-equivalence.json"
        _module("ops.gcp.validate_spark_output_equivalence",
                "--baseline-summary", str(b / "summary.json"),
                "--optimized-summary", str(o / "summary.json"),
                "--pair-manifest", str(_pair_path(args.root, stage)),
                "--baseline-evidence", str(b / "batch-evidence.json"),
                "--optimized-evidence", str(o / "batch-evidence.json"),
                "--output", str(eq))
        _module("ops.gcp.compare_spark_benchmark",
                "--pair-manifest", str(_pair_path(args.root, stage)),
                "--baseline-evidence", str(b / "batch-evidence.json"),
                "--optimized-evidence", str(o / "batch-evidence.json"),
                "--equivalence-evidence", str(eq),
                "--output-json", str(args.root / f"dp2-{stage}-comparison.json"),
                "--output-csv", str(args.root / f"dp2-{stage}-comparison.csv"))
        _module("ops.gcp.select_spark_stage_winner",
                "--pair-manifest", str(_pair_path(args.root, stage)),
                "--baseline-evidence", str(b / "batch-evidence.json"),
                "--optimized-evidence", str(o / "batch-evidence.json"),
                "--equivalence-evidence", str(eq),
                "--output", str(_winner_path(args.root, stage)))
        _winner(_winner_path(args.root, stage), stage=STAGES[stage],
                benchmark=args.benchmark_id, bucket=args.data_bucket,
                expected_git_sha=args.git_sha)
        print(f"DP2 {stage.upper()} Method 2 winner confirmed")
    else:
        raise ValueError(f"Unknown action {args.action}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True,
                        choices=("approve", "prepare-silver", "prepare-gold", "quota",
                                 "submit", "verify", "summary", "evidence", "finalize"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bronze-winner", required=True, type=Path)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--dataset-version", default="canonical-100gb-v1")
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--ttl", default="8h")
    parser.add_argument("--approval", default="")
    parser.add_argument("--require-clean", action="store_true")
    for stage in STAGES:
        for profile in PROFILES:
            parser.add_argument(f"--{profile}-{stage}-run-id", required=True)
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--profile", choices=PROFILES)
    args = parser.parse_args(argv)
    if args.dataset_version != "canonical-100gb-v1":
        raise ValueError("DP2 must use canonical-100gb-v1")
    if args.action not in ("approve", "prepare-silver", "prepare-gold") and not (
            args.stage and args.profile or args.action == "finalize" and args.stage):
        raise ValueError("Stage/profile required (finalize needs stage only)")
    if not args.root.is_absolute() or args.root.resolve().is_relative_to(REPO_ROOT):
        raise ValueError("--root must be absolute and outside Git repo")
    _action(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
