"""Fail-closed, single-trigger DP2 Method-2 orchestration primitives.

Airflow invokes one action per task. No Spark transformations are changed.
Only ``quota``, ``submit`` and ``summary`` can launch or wait on GCP work.
Every action creates external, immutable evidence (never inside the Git tree).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
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
from ops.gcp.build_spark_validation_evidence import build_validation_evidence
from ops.gcp.collect_spark_benchmark_evidence import parse_batch_evidence

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
APPLICATION_NAMES = {"silver": "EWallet-DP2-Silver", "gold": "EWallet-DP2-Core-Gold"}


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


def _write_once_or_require_exact(path: Path, payload: Mapping[str, Any]) -> None:
    """Create an immutable JSON artifact or adopt the exact existing value."""

    if path.exists():
        if _json(path) != dict(payload):
            raise ValueError(f"Existing immutable artifact differs: {path}")
        return
    _write_once(path, payload)


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(list(args), check=True, capture_output=capture, text=True,
                            cwd=REPO_ROOT)
    return result.stdout.strip() if capture else ""


def _module(module: str, *args: str) -> None:
    _run(sys.executable, "-m", module, *args)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _winner(path: Path, *, stage: str, benchmark: str, bucket: str,
            expected_git_sha: str | None = None,
            expected_file_sha256: str | None = None) -> dict[str, Any]:
    if expected_file_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_file_sha256):
            raise ValueError("Expected winner SHA-256 is invalid")
        if _sha256(path) != expected_file_sha256:
            raise ValueError("Winner file SHA-256 mismatch")
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


def _bronze_winner(args: argparse.Namespace) -> dict[str, Any]:
    source_benchmark = getattr(args, "bronze_source_benchmark_id", None)
    source_sha256 = getattr(args, "bronze_winner_sha256", None)
    if source_benchmark is None or source_sha256 is None:
        raise ValueError(
            "Bronze source benchmark and trusted winner SHA-256 are required"
        )
    validate_benchmark_id(source_benchmark)
    return _winner(
        args.bronze_winner,
        stage="dp1-bronze",
        benchmark=source_benchmark,
        bucket=args.data_bucket,
        expected_file_sha256=source_sha256,
    )


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
        source = _bronze_winner(args)
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
    _write_once_or_require_exact(_pair_path(args.root, stage), payload)
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


def _describe_batch_if_present(
    args: argparse.Namespace, batch_id: str,
) -> dict[str, Any] | None:
    completed = subprocess.run(
        [
            "gcloud", "dataproc", "batches", "describe", batch_id,
            f"--project={args.project}", f"--region={args.region}", "--format=json",
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0:
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise ValueError("Dataproc batch description must be an object")
        return value
    if "not_found" in (completed.stdout + completed.stderr).lower():
        return None
    raise RuntimeError(
        f"Could not determine whether Dataproc batch exists: {batch_id}"
    )


def _method2_runtime_identity(batch: Mapping[str, Any]) -> dict[str, str]:
    runtime = batch.get("runtimeConfig")
    if not isinstance(runtime, Mapping):
        raise ValueError("Dataproc batch lacks runtimeConfig")
    raw = runtime.get("properties")
    if not isinstance(raw, Mapping):
        raise ValueError("Dataproc batch lacks runtime properties")
    return {
        str(key).split(":", 1)[-1]: str(value)
        for key, value in raw.items()
    }


def _adopt_completed_batch(
    args: argparse.Namespace, *, batch_id: str, output_run_id: str,
    pair_digest: str, profile: str, recovery_git_sha: str | None,
    validation_git_sha: str | None = None,
) -> bool:
    """Adopt one exact completed Dataproc attempt; never resubmit an existing ID."""

    batch = _describe_batch_if_present(args, batch_id)
    if batch is None:
        return False
    state = str(batch.get("state", ""))
    if state != "SUCCEEDED":
        raise ValueError(
            f"Dataproc batch {batch_id} already exists in state {state}; "
            "refusing duplicate submission"
        )
    _batch_cohort(batch, batch_id)
    labels = batch.get("labels")
    if not isinstance(labels, Mapping):
        raise ValueError("Dataproc batch lacks labels")
    expected_labels = {
        "benchmark_id": args.benchmark_id,
        "spark_profile": profile,
        "git_sha": args.git_sha,
    }
    if validation_git_sha is not None:
        expected_labels["validation_git_sha"] = validation_git_sha
    if batch_id != output_run_id and validation_git_sha is None:
        expected_labels.update(
            output_run_id=output_run_id,
            recovery_git_sha=str(recovery_git_sha or ""),
        )
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise ValueError("Existing Dataproc batch labels do not match execution identity")
    properties = _method2_runtime_identity(batch)
    expected_properties = {
        "spark.ewallet.method2.pairManifestDigest": pair_digest,
        "spark.ewallet.method2.outputRunId": output_run_id,
    }
    if any(properties.get(key) != value for key, value in expected_properties.items()):
        raise ValueError("Existing Dataproc batch runtime identity mismatch")
    return True


def _dataproc_batch_id(
    args: argparse.Namespace, stage: str, profile: str, output_run_id: str,
) -> str:
    if stage != "gold":
        return output_run_id
    return str(
        getattr(args, f"{profile}_gold_batch_id", None) or output_run_id
    )


def _batch_cohort(batch: Mapping[str, Any], run_id: str) -> str:
    if str(batch.get("name", "")).rsplit("/", 1)[-1] != run_id:
        raise ValueError("Original batch name does not match Dataproc attempt ID")
    runtime_info = batch.get("runtimeInfo")
    labels = batch.get("labels")
    if not isinstance(runtime_info, Mapping) or not isinstance(labels, Mapping):
        raise ValueError("Original batch lacks runtimeInfo or labels")
    cohort_info = runtime_info.get("cohortInfo")
    if not isinstance(cohort_info, Mapping):
        raise ValueError("Original batch lacks Dataproc cohort identity")
    cohort = str(cohort_info.get("cohort", ""))
    identities = {
        cohort,
        str(batch.get("uuid", "")),
        str(labels.get("goog-dataproc-batch-uuid", "")),
    }
    if "" in identities or len(identities) != 1:
        raise ValueError("Original batch Dataproc cohort identities disagree")
    return cohort


def _epoch_ms(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _get_event_metrics(args: argparse.Namespace, stage: str, profile: str,
                       output_run_id: str, dataproc_batch_id: str,
                       pair_digest: str, batch: Mapping[str, Any],
                       output: Path) -> dict[str, Any]:
    """Find only original-job event logs in the original Dataproc time window.

    This deliberately fails if no *unique* log matches the Method2 identity.
    Never silently uses a validator's event log as ingestion performance.
    """
    def timestamp(s: str) -> dt.datetime:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)

    if batch.get("state") != "SUCCEEDED":
        raise ValueError(f"Original batch {dataproc_batch_id} did not succeed")
    create_time = str(batch["createTime"])
    state_time = str(batch["stateTime"])
    begin = timestamp(create_time) - dt.timedelta(minutes=30)
    end = timestamp(state_time) + dt.timedelta(minutes=30)
    cohort = _batch_cohort(batch, dataproc_batch_id)
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
    with tempfile.TemporaryDirectory(prefix="dp2-event-logs-") as directory:
        temp_dir = Path(directory)
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
                    [decoded], batch_id=dataproc_batch_id,
                    output_run_id=output_run_id,
                    pair_manifest_digest=pair_digest, source_uri=uri,
                    expected_dataproc_cohort_id=cohort,
                    expected_application_name=APPLICATION_NAMES[stage],
                    application_not_before_epoch_ms=_epoch_ms(create_time),
                    application_not_after_epoch_ms=_epoch_ms(state_time),
                )
            except ValueError:
                # Other jobs (including validations) share the event-log bucket.
                continue
            matches.append(result)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one original event log for {dataproc_batch_id}, "
            f"found {len(matches)}"
        )
    _write_once(output, matches[0])
    return matches[0]


def _require_saved_batch(
    saved: Mapping[str, Any], live: Mapping[str, Any], *, run_id: str,
    benchmark_id: str, profile: str, git_sha: str,
    output_run_id: str, recovery_git_sha: str | None,
) -> None:
    """Verify an immutable partial batch description against the live batch."""
    _batch_cohort(saved, run_id)
    _batch_cohort(live, run_id)
    required = ("name", "uuid", "state", "createTime", "stateTime", "runtimeConfig")
    if any(saved.get(field) != live.get(field) for field in required):
        raise ValueError("Saved original batch does not match live Dataproc identity")
    if saved.get("state") != "SUCCEEDED":
        raise ValueError("Saved original batch did not succeed")
    labels = saved.get("labels", {})
    expected = {"benchmark_id": benchmark_id, "spark_profile": profile, "git_sha": git_sha}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValueError("Saved original batch labels do not match execution identity")
    if run_id != output_run_id:
        expected_recovery = {
            "output_run_id": output_run_id,
            "recovery_git_sha": recovery_git_sha,
        }
        if not recovery_git_sha or any(
            labels.get(key) != value for key, value in expected_recovery.items()
        ):
            raise ValueError("Saved recovery batch labels do not match output identity")


def _evidence_payload(
    args: argparse.Namespace, *, stage: str, profile: str, pair: Mapping[str, Any],
    run_id: str, dataproc_batch_id: str, output_root: str,
    upstream: Sequence[str],
    summary: Mapping[str, Any], performance: Mapping[str, Any], batch: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = build_validation_evidence(
        summary, performance, pair_manifest=pair, spark_profile=profile,
    )
    evidence = parse_batch_evidence(
        batch, benchmark_id=args.benchmark_id, spark_profile=profile,
        stage=STAGES[stage], dataset_version=args.dataset_version,
        git_sha=args.git_sha, upstream_run_ids=upstream,
        output_run_id=run_id, output_root=output_root,
        pair_manifest=pair, validation=validation,
        dataproc_batch_id=dataproc_batch_id,
        recovery_git_sha=getattr(args, "recovery_git_sha", None),
    )
    return validation, evidence


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
            _bronze_winner(args)
            for stage in STAGES:
                for profile in PROFILES:
                    require_profile_binding(getattr(args, f"{profile}_{stage}_run_id"),
                                            benchmark_id=args.benchmark_id,
                                            profile=profile, stage=stage)
            print("DP2 APPROVED: source, Bronze Winner and run identities validated")
            return
        _require_clean_head(args.git_sha)
        if args.action == "prepare-silver":
            source = _bronze_winner(args)
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
    dataproc_batch_id = _dataproc_batch_id(args, stage, profile, run_id)
    base = _profile_dir(args.root, stage, profile)
    base.mkdir(parents=True, exist_ok=True)

    if args.action == "quota":
        # Strictly sequential profiles mean only one active DP2 ingestion at a time.
        predecessor = (
            upstream[0] if profile == "baseline" else
            _dataproc_batch_id(
                args, stage, "baseline",
                str(pair_member(pair, "baseline")["output_run_id"]),
            )
        )
        quota_name = (
            f"quota-{dataproc_batch_id}.json"
            if dataproc_batch_id != run_id else "quota.json"
        )
        _module("ops.gcp.wait_for_dataproc_quota", "--project", args.project,
                "--region", args.region, "--spark-profile", profile,
                "--benchmark-id", args.benchmark_id,
                "--handoff", "bronze-to-silver" if stage == "silver" else "silver-to-gold",
                "--predecessor-batch-id", str(predecessor),
                "--evidence-output", str(base / quota_name), "--execute")
    elif args.action in ("submit", "plan-submit"):
        recovery_attempt = dataproc_batch_id != run_id
        if recovery_attempt:
            if stage != "gold":
                raise ValueError("distinct Dataproc attempts are supported only for Gold recovery")
            _require_clean_head(str(args.recovery_git_sha or ""))
        else:
            _require_clean_head(args.git_sha)
        module_args = [
                "--dataset-version", args.dataset_version,
                "--benchmark-id", args.benchmark_id,
                "--source-bronze-run-id" if stage == "silver" else "--source-silver-run-id",
                upstream[0], "--run-id", run_id, "--spark-profile", profile,
                "--git-sha", args.git_sha, "--pair-manifest", str(_pair_path(args.root, stage)),
                "--ttl", args.ttl,
        ]
        if recovery_attempt:
            if args.compute_repository is None:
                raise ValueError("Gold recovery requires compute_repository")
            module_args.extend((
                "--dataproc-batch-id", dataproc_batch_id,
                "--recovery-git-sha", args.recovery_git_sha,
                "--compute-repository", str(args.compute_repository),
            ))
        if args.action == "submit":
            if _adopt_completed_batch(
                args,
                batch_id=dataproc_batch_id,
                output_run_id=run_id,
                pair_digest=str(pair["pair_manifest_digest"]),
                profile=profile,
                recovery_git_sha=getattr(args, "recovery_git_sha", None),
            ):
                print(f"Adopted exact completed Dataproc batch: {dataproc_batch_id}")
                return
            module_args.append("--execute")
        _module(
            "ops.gcp.submit_bronze_to_silver" if stage == "silver" else
            "ops.gcp.submit_silver_to_gold", *module_args,
        )
    elif args.action == "verify":
        _module("ops.gcp.verify_delta_run", "--root", output_root,
                *(x for table in TABLES[stage] for x in ("--expected-table", table)))
    elif args.action == "summary":
        _require_clean_head(
            args.recovery_git_sha if dataproc_batch_id != run_id else args.git_sha
        )
        validation_batch_id = _validation_batch_id(run_id, stage, profile)
        validation_git_sha = (
            args.recovery_git_sha if dataproc_batch_id != run_id else args.git_sha
        )
        if _adopt_completed_batch(
            args,
            batch_id=validation_batch_id,
            output_run_id=run_id,
            pair_digest=str(pair["pair_manifest_digest"]),
            profile=profile,
            recovery_git_sha=None,
            validation_git_sha=validation_git_sha,
        ):
            _run("gcloud", "storage", "ls", _summary_uri(args, stage, profile, run_id))
            print(f"Adopted exact completed semantic scan: {validation_batch_id}")
            return
        # A semantic scan is also a paid Dataproc batch; gate its quota separately.
        _module("ops.gcp.wait_for_dataproc_quota", "--project", args.project,
                "--region", args.region, "--spark-profile", profile,
                "--benchmark-id", args.benchmark_id,
                "--handoff", "bronze-to-silver" if stage == "silver" else "silver-to-gold",
                "--predecessor-batch-id", dataproc_batch_id,
                "--evidence-output", str(
                    base / (
                        f"validation-quota-{dataproc_batch_id}.json"
                        if dataproc_batch_id != run_id else "validation-quota.json"
                    )
                ), "--execute")
        _module("ops.gcp.submit_spark_output_summary",
                "--batch-id", validation_batch_id,
                "--benchmark-id", args.benchmark_id, "--spark-profile", profile,
                "--stage", STAGES[stage], "--output-run-id", run_id,
                "--dataset-version", args.dataset_version, "--git-sha", args.git_sha,
                "--validation-git-sha", validation_git_sha,
                "--summary-uri", _summary_uri(args, stage, profile, run_id),
                "--source-run-id", upstream[0],
                "--pair-manifest", str(_pair_path(args.root, stage)), "--execute")
    elif args.action == "evidence":
        summary = base / "summary.json"
        performance = base / "original-performance.json"
        validation = base / "validation-evidence.json"
        batch_path = base / "original-batch.json"
        evidence = base / "batch-evidence.json"
        # Partial artifacts are reused only after exact validation. No existing
        # file is overwritten, and the action is idempotent after completion.
        if summary.exists():
            summary_data = _json(summary)
        else:
            with tempfile.TemporaryDirectory(prefix="dp2-summary-") as directory:
                downloaded = Path(directory) / "summary.json"
                _run("gcloud", "storage", "cp", _summary_uri(args, stage, profile, run_id),
                     str(downloaded))
                summary_data = _json(downloaded)
                _write_once(summary, summary_data)
        live = _describe_batch(args, dataproc_batch_id)
        if batch_path.exists():
            original = _json(batch_path)
            _require_saved_batch(original, live, run_id=dataproc_batch_id,
                                 benchmark_id=args.benchmark_id, profile=profile,
                                 git_sha=args.git_sha, output_run_id=run_id,
                                 recovery_git_sha=getattr(args, "recovery_git_sha", None))
        else:
            original = live
            _require_saved_batch(original, live, run_id=dataproc_batch_id,
                                 benchmark_id=args.benchmark_id, profile=profile,
                                 git_sha=args.git_sha, output_run_id=run_id,
                                 recovery_git_sha=getattr(args, "recovery_git_sha", None))
            _write_once(batch_path, original)
        if performance.exists():
            performance_data = _json(performance)
        else:
            performance_data = _get_event_metrics(
                args, stage, profile, run_id, dataproc_batch_id,
                str(pair["pair_manifest_digest"]), original, performance,
            )
        expected_validation, expected_evidence = _evidence_payload(
            args, stage=stage, profile=profile, pair=pair, run_id=run_id,
            dataproc_batch_id=dataproc_batch_id,
            output_root=output_root, upstream=upstream, summary=summary_data,
            performance=performance_data, batch=original,
        )
        if validation.exists():
            if _json(validation) != expected_validation:
                raise ValueError("Existing validation evidence is not the expected artifact")
        else:
            _write_once(validation, expected_validation)
        if evidence.exists():
            if _json(evidence) != expected_evidence:
                raise ValueError("Existing batch evidence is not the expected artifact")
        else:
            _write_once(evidence, expected_evidence)
        print(evidence)
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
                                 "submit", "plan-submit", "verify", "summary", "evidence",
                                 "finalize"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bronze-winner", required=True, type=Path)
    parser.add_argument("--bronze-source-benchmark-id", required=True)
    parser.add_argument("--bronze-winner-sha256", required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--dataset-version", default="canonical-100gb-v1")
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--ttl", default="8h")
    parser.add_argument("--approval", default="")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--baseline-gold-batch-id")
    parser.add_argument("--optimized-gold-batch-id")
    parser.add_argument("--recovery-git-sha")
    parser.add_argument("--compute-repository", type=Path)
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
