"""Run one task of the EDAI-1 DP1-DP3 campaign (``edai1_data_pipeline_campaign``).

One DagRun is one profile: Source -> Bronze -> Silver -> Core Gold -> Features, each
stage reading only the outputs of the same profile. There is no winner selection.

Every parameter arrives through the environment (``EDAI1_CAMPAIGN_*``) so no value
is interpolated into a shell. Every task re-validates all parameters and the
explicit owner approval before doing anything, so a default or edited DagRun can
never reach a paid job. Failures raise or exit non-zero, which fails the Airflow
task and keeps everything downstream from running.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import (
    APPROVED_DATASETS,
    dataset_has_merchant_history,
    isolated_data_bucket,
    resolve_storage_context,
)
from data_platform.config.spark_profiles import (
    SPARK_PROFILES,
    require_campaign_lineage,
    validate_benchmark_id,
    validate_git_sha,
)
from data_platform.generation.src.sharded.storage import GcloudObjectStore, ObjectStoreError
from ops.gcp import (
    submit_bronze_to_silver,
    submit_historical_features,
    submit_silver_to_gold,
    submit_source_to_bronze,
    submit_stage_validation,
    verify_delta_run,
    wait_for_dataproc_quota,
)
from ops.gcp import spark_provenance
from ops.gcp.rehearsal_storage import StorageNotReady, StoragePlan, check_bucket
from ops.gcp.submit_silver_to_gold import _load_outputs, assert_batch_id_absent, require_clean_repository
from ops.gcp.submit_source_to_bronze import (
    CloudRuntime,
    apply_dataset_bucket,
    execution_record_uri,
    load_cloud_runtime,
)
from ops.gcp.submit_stage_validation import ValidationRuns, validated_run_id
from ops.gcp.validate_spark_output_equivalence import expected_tables
from ops.gcp.verify_source_dataset import verify_source_dataset


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENV_PREFIX = "EDAI1_CAMPAIGN_"
PARAMETER_NAMES = (
    "dataset_version", "benchmark_id", "spark_profile", "git_sha", "source_manifest_sha256",
    "bronze_run_id", "silver_run_id", "gold_run_id", "feature_run_id",
    "project_id", "region", "data_bucket", "dataproc_ttl", "terraform_output_json",
    "execution_approval",
)
NOT_APPROVED = "NOT_APPROVED"
REQUIRED_TTL = "8h"  # the existing benchmark contract for full-scale submissions
_PATTERNS = {
    "source_manifest_sha256": re.compile(r"^[0-9a-f]{64}$"),
    "project_id": re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$"),
    "region": re.compile(r"^[a-z]+-[a-z]+[0-9]+$"),
    "data_bucket": re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$"),
    "terraform_output_json": re.compile(r"^/[A-Za-z0-9_./-]+\.json$"),
}
STAGES = (
    # (name, spark stage for inventory, zone, run field)
    ("bronze", "dp1-bronze", "bronze", "bronze_run_id"),
    ("silver", "dp2-silver", "silver", "silver_run_id"),
    ("gold", "dp2-gold", "gold", "gold_run_id"),
    ("features", "dp3-features", "gold", "feature_run_id"),
)
VALIDATION_SCOPE = {"bronze": "bronze", "silver": "silver", "gold": "core-gold", "features": "features"}
QUOTA_HANDOFF = {
    "bronze": "external-to-bronze",
    "silver": "bronze-to-silver",
    "gold": "silver-to-gold",
    "features": "gold-to-features",
}


class CampaignError(ValueError):
    """A campaign parameter or gate refused the task."""


def expected_approval(values: Mapping[str, str]) -> str:
    """The exact token the owner types, bound to this one campaign and code revision."""

    return (f"EXECUTE-PAID-DATAPROC:{values['benchmark_id']}:{values['spark_profile']}:"
            f"{values['dataset_version']}:{values['git_sha'][:12]}")


@dataclass(frozen=True)
class CampaignParams:
    dataset_version: str
    benchmark_id: str
    spark_profile: str
    git_sha: str
    source_manifest_sha256: str
    bronze_run_id: str
    silver_run_id: str
    gold_run_id: str
    feature_run_id: str
    project_id: str
    region: str
    data_bucket: str
    dataproc_ttl: str
    terraform_output_json: str
    execution_approval: str

    @property
    def runs(self) -> ValidationRuns:
        return ValidationRuns(self.bronze_run_id, self.silver_run_id, self.gold_run_id, self.feature_run_id)

    def stage_run_id(self, stage: str) -> str:
        return {"bronze": self.bronze_run_id, "silver": self.silver_run_id,
                "gold": self.gold_run_id, "features": self.feature_run_id}[stage]

    def run_root(self, stage: str) -> str:
        zone = next(item[2] for item in STAGES if item[0] == stage)
        return f"gs://{self.data_bucket}/lakehouse/runs/{self.stage_run_id(stage)}/{zone}"


def parse_parameters(values: Mapping[str, str]) -> CampaignParams:
    """Validate every parameter and the approval; refuse anything else."""

    missing = sorted(name for name in PARAMETER_NAMES if name not in values)
    if missing:
        raise CampaignError(f"missing campaign parameters: {missing}")
    if values["dataset_version"] not in APPROVED_DATASETS:
        raise CampaignError(f"dataset_version is not an approved campaign dataset: {values['dataset_version']!r}")
    if values["spark_profile"] not in SPARK_PROFILES:
        raise CampaignError(f"spark_profile must be one of {SPARK_PROFILES}")
    try:
        validate_benchmark_id(values["benchmark_id"])
        validate_git_sha(values["git_sha"])
    except ValueError as error:
        raise CampaignError(str(error)) from error
    for name, pattern in _PATTERNS.items():
        value = values[name]
        if name == "terraform_output_json" and value == "":
            continue
        if not pattern.fullmatch(value):
            raise CampaignError(f"{name} has an unsafe or invalid value")
    if values["dataproc_ttl"] != REQUIRED_TTL:
        raise CampaignError(f"dataproc_ttl must be {REQUIRED_TTL}")
    run_ids = [values["bronze_run_id"], values["silver_run_id"], values["gold_run_id"], values["feature_run_id"]]
    if len(set(run_ids)) != 4:
        raise CampaignError("stage run IDs must be distinct")
    try:
        for stage, run_id in zip(("bronze", "silver", "gold", "feat"), run_ids):
            require_campaign_lineage(run_id, benchmark_id=values["benchmark_id"],
                                     profile=values["spark_profile"], stage=stage)
        resolve_storage_context(values["dataset_version"], values["data_bucket"])
    except ValueError as error:
        raise CampaignError(str(error)) from error
    if values["execution_approval"] != expected_approval(values):
        raise CampaignError(
            "explicit owner approval is missing or does not match this campaign: "
            f"expected {expected_approval(values)!r}"
        )
    return CampaignParams(**{name: values[name] for name in PARAMETER_NAMES})


def parameters_from_environment(environ: Mapping[str, str] = os.environ) -> CampaignParams:
    return parse_parameters({
        name: environ[f"{ENV_PREFIX}{name.upper()}"]
        for name in PARAMETER_NAMES
        if f"{ENV_PREFIX}{name.upper()}" in environ
    })


def attempt_from_environment(environ: Mapping[str, str] = os.environ) -> int:
    value = environ.get(f"{ENV_PREFIX}ATTEMPT", "")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        raise CampaignError("Airflow try number is required for validation attempts")
    return int(value)


def load_runtime(params: CampaignParams, runner: Callable[..., Any]) -> CloudRuntime:
    """The submitter runtime must be exactly the deployment the DagRun names."""

    path = Path(params.terraform_output_json) if params.terraform_output_json else None
    runtime = apply_dataset_bucket(load_cloud_runtime(_load_outputs(path, runner)), params.dataset_version)
    actual = (runtime.project_id, runtime.region, runtime.data_bucket)
    expected = (params.project_id, params.region, params.data_bucket)
    if actual != expected:
        raise CampaignError(f"terraform runtime {actual} differs from the DagRun parameters {expected}")
    return runtime


def _terraform_arguments(params: CampaignParams) -> tuple[str, ...]:
    return (f"--terraform-output-json={params.terraform_output_json}",) if params.terraform_output_json else ()


def _require_absent_output_prefixes(params: CampaignParams, runtime: CloudRuntime, runner: Callable[..., Any]) -> None:
    submit_source_to_bronze.assert_bronze_prefix_absent(runtime, run_id=params.bronze_run_id, runner=runner)
    submit_bronze_to_silver.assert_silver_prefix_absent(runtime, run_id=params.silver_run_id, runner=runner)
    submit_silver_to_gold.assert_output_prefix_absent(runtime, run_id=params.gold_run_id, runner=runner)
    submit_historical_features.assert_output_prefix_absent(runtime, run_id=params.feature_run_id, runner=runner)


def action_preflight(params: CampaignParams, runner: Callable[..., Any]) -> None:
    """Identity gate before any paid job: code revision, deployment, output absence."""

    require_clean_repository(REPOSITORY_ROOT, params.git_sha, runner)
    runtime = load_runtime(params, runner)
    if isolated_data_bucket(params.dataset_version) is not None:
        try:
            check_bucket(StoragePlan(runtime.project_id, runtime.data_bucket, runtime.service_account_email), runner)
        except StorageNotReady as error:
            raise CampaignError(str(error)) from error
    _require_absent_output_prefixes(params, runtime, runner)
    for run_id in (params.bronze_run_id, params.silver_run_id, params.gold_run_id, params.feature_run_id):
        assert_batch_id_absent(runtime, batch_id=run_id, runner=runner)
    print(json.dumps({
        "status": "PASS",
        "campaign": {name: value for name, value in asdict(params).items() if name != "execution_approval"},
        "merchant_history": dataset_has_merchant_history(params.dataset_version),
        "expected_tables": {stage: sorted(expected_tables(spark, params.dataset_version))
                            for stage, spark, _, _ in STAGES},
    }, indent=2, sort_keys=True))


def action_verify_source(params: CampaignParams, runner: Callable[..., Any]) -> None:
    report = verify_source_dataset(
        bucket=params.data_bucket,
        dataset_version=params.dataset_version,
        expected_manifest_sha256=params.source_manifest_sha256,
        store=GcloudObjectStore(runner=runner),
        lister=lambda root: _list_objects(root, runner),
    )
    print(json.dumps(report, indent=2, sort_keys=True))


def _list_objects(root: str, runner: Callable[..., Any]) -> Mapping[str, Mapping[str, Any]]:
    from ops.gcp.verify_source_dataset import list_objects

    return list_objects(root, runner)


def _quota(params: CampaignParams, stage: str, handoff: str, predecessor: str, suffix: str) -> None:
    evidence = f"docs/evidence/quota_handoffs/{params.benchmark_id}/{params.stage_run_id(stage)}-{suffix}.json"
    code = wait_for_dataproc_quota.main([
        "--project", params.project_id, "--region", params.region,
        "--spark-profile", params.spark_profile, "--benchmark-id", params.benchmark_id,
        "--handoff", handoff, "--predecessor-batch-id", predecessor,
        "--evidence-output", evidence, "--execute",
    ])
    if code != 0:
        raise CampaignError(f"Dataproc quota is not available for {stage} ({handoff})")


def action_quota(params: CampaignParams, stage: str) -> None:
    previous = {"bronze": "", "silver": params.bronze_run_id, "gold": params.silver_run_id,
                "features": params.gold_run_id}[stage]
    _quota(params, stage, QUOTA_HANDOFF[stage], previous, "quota")


def _common_submit_arguments(params: CampaignParams) -> list[str]:
    return [
        f"--dataset-version={params.dataset_version}",
        f"--benchmark-id={params.benchmark_id}",
        f"--spark-profile={params.spark_profile}",
        f"--git-sha={params.git_sha}",
        f"--ttl={params.dataproc_ttl}",
        "--campaign",
        "--execute",
        *_terraform_arguments(params),
    ]


def _execution_context(params: CampaignParams, stage: str, kind: str) -> spark_provenance.ExecutionContext:
    storage = resolve_storage_context(params.dataset_version, params.data_bucket)
    runs = {"bronze": params.bronze_run_id, "silver": params.silver_run_id,
            "gold": params.gold_run_id, "features": params.feature_run_id}
    transformation = kind == "transformation"
    return spark_provenance.ExecutionContext(
        campaign_id=spark_provenance.campaign_id(params.benchmark_id, params.spark_profile),
        dataset_version=params.dataset_version, spark_profile=params.spark_profile,
        stage_name=stage, kind=kind, git_sha=params.git_sha,
        source_manifest_sha256=params.source_manifest_sha256,
        project_id=params.project_id, region=params.region, data_bucket=params.data_bucket,
        run_id=params.stage_run_id(stage),
        input_roots=(spark_provenance.stage_inputs(storage, stage, runs) if transformation
                     else spark_provenance.validation_inputs(storage, stage, runs)),
        output_roots=(spark_provenance.stage_output(storage, stage, runs),) if transformation else (),
    )


def _collect(params: CampaignParams, stage: str, kind: str, batch_id: str, report_uri: str | None,
             runner: Callable[..., Any]) -> dict[str, Any]:
    return spark_provenance.collect_execution(
        _execution_context(params, stage, kind), batch_id=batch_id,
        record_uri=execution_record_uri(params.data_bucket, params.stage_run_id(stage), batch_id),
        report_uri=report_uri, runner=runner)


def _record_provenance(params: CampaignParams, stage: str, kind: str, batch_id: str, report_uri: str | None,
                       runner: Callable[..., Any]) -> None:
    """Keep one record per batch beside the run, also for failed batches.

    Runs after the submission whatever its outcome. A batch that was never
    submitted leaves no record, and a collection problem never hides the
    submission's own result; finalization collects again and fails closed.
    """

    try:
        record = _collect(params, stage, kind, batch_id, report_uri, runner)
        if not record["dataproc_batch_found"]:
            return
        uri = f"{resolve_storage_context(params.dataset_version).evidence_root(params.stage_run_id(stage))}/execution_provenance-{batch_id}.json"
        GcloudObjectStore(runner=runner).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", uri, if_absent=True)
    except (ValueError, ObjectStoreError) as error:
        print(f"WARNING: execution provenance not recorded for {batch_id}: {error}", file=sys.stderr)


def action_submit(params: CampaignParams, stage: str, runner: Callable[..., Any]) -> None:
    """Submit one transformation. Submitters refuse existing output prefixes."""

    try:
        _submit_transformation(params, stage, runner)
    finally:
        _record_provenance(params, stage, "transformation", params.stage_run_id(stage), None, runner)


def _submit_transformation(params: CampaignParams, stage: str, runner: Callable[..., Any]) -> None:
    common = _common_submit_arguments(params)
    if stage == "bronze":
        code = submit_source_to_bronze.main([*common, f"--run-id={params.bronze_run_id}"], runner=runner)
    elif stage == "silver":
        code = submit_bronze_to_silver.main(
            [*common, f"--source-bronze-run-id={params.bronze_run_id}", f"--run-id={params.silver_run_id}"],
            runner=runner)
    elif stage == "gold":
        code = submit_silver_to_gold.main(
            [*common, f"--source-silver-run-id={params.silver_run_id}", f"--run-id={params.gold_run_id}"],
            runner=runner)
    elif stage == "features":
        code = submit_historical_features.main(
            [*common, f"--source-silver-run-id={params.silver_run_id}",
             f"--source-core-gold-run-id={params.gold_run_id}", f"--run-id={params.feature_run_id}"],
            runner=runner)
    else:
        raise CampaignError(f"unknown stage: {stage}")
    if code != 0:
        raise CampaignError(f"{stage} submission failed with exit code {code}")


def action_physical(params: CampaignParams, stage: str, runner: Callable[..., Any]) -> None:
    spark_stage = next(item[1] for item in STAGES if item[0] == stage)
    tables = expected_tables(spark_stage, params.dataset_version)
    verify_delta_run.verify_delta_run(params.run_root(stage), tables, runner=runner)
    print(f"PASS: {len(tables)} Delta tables at {params.run_root(stage)}")


def action_validate(params: CampaignParams, stage: str, attempt: int, runner: Callable[..., Any]) -> None:
    """Submit the read-only semantic validator for one stage and require a PASS report."""

    scope = VALIDATION_SCOPE[stage]
    _quota(params, stage, "stage-to-validation", params.stage_run_id(stage), f"validation-quota-a{attempt}")
    arguments = [
        f"--scope={scope}",
        f"--dataset-version={params.dataset_version}",
        f"--benchmark-id={params.benchmark_id}",
        f"--spark-profile={params.spark_profile}",
        f"--git-sha={params.git_sha}",
        f"--bronze-run-id={params.bronze_run_id}",
        f"--silver-run-id={params.silver_run_id}",
        f"--gold-run-id={params.gold_run_id}",
        f"--features-run-id={params.feature_run_id}",
        f"--attempt={attempt}",
        f"--ttl={params.dataproc_ttl}",
        "--execute",
        *_terraform_arguments(params),
    ]
    run_id = params.stage_run_id(stage)
    batch_id = submit_stage_validation.validation_batch_id(run_id, attempt)
    report = submit_stage_validation.report_uri(params.data_bucket, run_id, scope, attempt)
    try:
        code = submit_stage_validation.main(arguments, runner=runner)
    finally:
        _record_provenance(params, stage, "validation", batch_id, report, runner)
    if code != 0:
        raise CampaignError(f"{scope} validation failed with exit code {code}")


def latest_validation_report(
    params: CampaignParams, stage: str, runner: Callable[..., Any]
) -> tuple[str, Mapping[str, Any]]:
    """Return the report of the highest attempt for one stage."""

    scope = VALIDATION_SCOPE[stage]
    run_id = validated_run_id(scope, params.runs)
    prefix = f"gs://{params.data_bucket}/lakehouse/runs/{run_id}/evidence/validation-{scope}-a"
    completed = runner(["gcloud", "storage", "ls", f"{prefix}*.json"], check=False, text=True, capture_output=True)
    uris = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not uris:
        raise CampaignError(f"no validation report found for {scope} of {run_id}")
    latest = max(uris, key=lambda uri: int(uri.rsplit("-a", 1)[1].removesuffix(".json")))
    return latest, submit_stage_validation.read_report(latest, runner=runner)


def _validation_attempts(
    params: CampaignParams, stage: str, latest: int, runner: Callable[..., Any]
) -> list[dict[str, Any]]:
    """Every validator attempt up to the latest, failed ones included."""

    scope = VALIDATION_SCOPE[stage]
    run_id = params.stage_run_id(stage)
    return [
        _collect(params, stage, "validation", submit_stage_validation.validation_batch_id(run_id, attempt),
                 submit_stage_validation.report_uri(params.data_bucket, run_id, scope, attempt), runner)
        for attempt in range(1, latest + 1)
    ]


def _require_succeeded(record: Mapping[str, Any]) -> None:
    if record["dataproc_batch_state"] != "SUCCEEDED":
        raise CampaignError(
            f"Dataproc batch {record['dataproc_batch_id']} is {record['dataproc_batch_state']}, expected SUCCEEDED")


def action_finalize(params: CampaignParams, runner: Callable[..., Any]) -> None:
    """Write the immutable campaign manifest; no promotion and no winner."""

    source = verify_source_dataset(
        bucket=params.data_bucket,
        dataset_version=params.dataset_version,
        expected_manifest_sha256=params.source_manifest_sha256,
        store=GcloudObjectStore(runner=runner),
        lister=lambda root: _list_objects(root, runner),
    )
    storage = resolve_storage_context(params.dataset_version, params.data_bucket)
    stages: dict[str, Any] = {}
    try:
        for stage, _, _, _ in STAGES:
            run_id = params.stage_run_id(stage)
            scope = VALIDATION_SCOPE[stage]
            uri, report = latest_validation_report(params, stage, runner)
            submit_stage_validation.require_passing_report(
                report, scope=scope, run_id=run_id, dataset_version=params.dataset_version)
            latest = int(uri.rsplit("-a", 1)[1].removesuffix(".json"))
            transformation = _collect(params, stage, "transformation", run_id, None, runner)
            attempts = _validation_attempts(params, stage, latest, runner)
            _require_succeeded(transformation)
            _require_succeeded(attempts[-1])
            stages[stage] = {
                "run_id": run_id,
                "output_root": params.run_root(stage),
                "execution_metadata": transformation,
                "validation": {
                    "report_uri": uri,
                    "status": report["status"],
                    "execution_metadata": attempts[-1],
                    "attempts": attempts,
                },
            }
    except spark_provenance.ProvenanceError as error:
        raise CampaignError(str(error)) from error
    manifest = {
        "schema_version": "edai1-pipeline-campaign-v2",
        "status": "COMPLETE",
        "benchmark_id": params.benchmark_id,
        "spark_profile": params.spark_profile,
        "dataset_version": params.dataset_version,
        "git_sha": params.git_sha,
        "storage": {"data_bucket": storage.data_bucket, "source_root": storage.source_root,
                    "spark_event_log_root": storage.spark_event_log_root},
        "source": {"manifest_uri": source["manifest_uri"], "manifest_sha256": source["manifest_sha256"],
                   "plan_digest": source["plan_digest"], "physical_bytes": source["physical_bytes"]},
        "lineage": ["source", *[stage for stage, _, _, _ in STAGES]],
        "stages": stages,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }
    uri = (f"gs://{params.data_bucket}/lakehouse/campaigns/{params.benchmark_id}/"
           f"{params.spark_profile}/campaign_manifest.json")
    GcloudObjectStore(runner=runner).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", uri, if_absent=True)
    print(json.dumps({"manifest_uri": uri, **manifest}, indent=2, sort_keys=True))


ACTIONS = (
    "preflight", "verify-source", "finalize",
    *[f"{verb}-{stage}" for verb in ("quota", "submit", "physical", "validate") for stage, _, _, _ in STAGES],
)


def run_action(action: str, params: CampaignParams, *, attempt: int, runner: Callable[..., Any]) -> None:
    verb, _, stage = action.partition("-")
    if action == "preflight":
        action_preflight(params, runner)
    elif action == "verify-source":
        action_verify_source(params, runner)
    elif action == "finalize":
        action_finalize(params, runner)
    elif verb == "quota":
        action_quota(params, stage)
    elif verb == "submit":
        action_submit(params, stage, runner)
    elif verb == "physical":
        action_physical(params, stage, runner)
    elif verb == "validate":
        action_validate(params, stage, attempt, runner)
    else:
        raise CampaignError(f"unknown campaign action: {action}")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        params = parameters_from_environment(environ)
        attempt = attempt_from_environment(environ)
        run_action(args.action, params, attempt=attempt, runner=runner)
    except CampaignError as error:
        print(f"CAMPAIGN TASK REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
