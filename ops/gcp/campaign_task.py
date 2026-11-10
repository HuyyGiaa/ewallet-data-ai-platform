"""Run one task of an EDAI-1 pipeline DAG: DP1, DP2 or DP3.

Three independent DAGs share this runner. One DagRun is one pipeline and one Spark
profile; each stage reads only outputs of its own profile, and there is no winner
selection or promotion.

=====  ==========================  ==============================  ==========================
Name   DAG                         Stages                          Publishes on success
=====  ==========================  ==============================  ==========================
dp1    ``dp1_source_to_bronze``    Source -> Bronze                ``dp1_handoff.json``
dp2    ``dp2_bronze_to_curated``   Bronze -> Silver -> Core Gold   ``dp2_handoff.json``
dp3    ``dp3_historical_features`` Silver + Core Gold -> Features  ``dp3_completion.json`` and
                                                                   ``campaign_manifest.json``
=====  ==========================  ==============================  ==========================

The pipelines exchange state only through immutable manifests under
``gs://<bucket>/lakehouse/campaigns/<benchmark_id>/<profile>/``. A pipeline publishes its
manifest only after every stage it owns passed the physical and the semantic
validation, and a downstream pipeline verifies that manifest, the upstream validation
reports and the upstream Delta inventory before any paid work.

Every parameter arrives through the environment (``EDAI1_CAMPAIGN_*``) so no value is
interpolated into a shell. Every task re-validates all parameters and the explicit
owner approval, which is bound to one pipeline, so a default or edited DagRun can
never reach a paid job. Failures raise or exit non-zero, which fails the Airflow task
and keeps everything downstream from running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
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
NOT_APPROVED = "NOT_APPROVED"
REQUIRED_TTL = "8h"  # the existing benchmark contract for full-scale submissions
HANDOFF_SCHEMA = "edai1-pipeline-handoff-v1"
CAMPAIGN_SCHEMA = "edai1-pipeline-campaign-v2"  # read by ops/gcp/compare_campaign_outputs.py
COMMON_PARAMETERS = (
    "dataset_version", "benchmark_id", "spark_profile", "git_sha", "source_manifest_sha256",
    "project_id", "region", "data_bucket", "dataproc_ttl", "terraform_output_json",
    "execution_approval",
)
RUN_PARAMETERS = ("bronze_run_id", "silver_run_id", "gold_run_id", "feature_run_id")
# Identity every handoff carries and every downstream pipeline compares exactly.
IDENTITY_FIELDS = (
    "benchmark_id", "dataset_version", "spark_profile", "git_sha", "source_manifest_sha256",
    "data_bucket", "project_id", "region",
)
_PATTERNS = {
    "source_manifest_sha256": re.compile(r"^[0-9a-f]{64}$"),
    "project_id": re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$"),
    "region": re.compile(r"^[a-z]+-[a-z]+[0-9]+$"),
    "data_bucket": re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$"),
    "terraform_output_json": re.compile(r"^/[A-Za-z0-9_./-]+\.json$"),
}
_DAG_RUN_ID = re.compile(r"^[A-Za-z0-9_.:+~-]{1,250}$")
STAGES = (
    # (name, spark stage for inventory, zone, run field)
    ("bronze", "dp1-bronze", "bronze", "bronze_run_id"),
    ("silver", "dp2-silver", "silver", "silver_run_id"),
    ("gold", "dp2-gold", "gold", "gold_run_id"),
    ("features", "dp3-features", "gold", "feature_run_id"),
)
STAGE_NAMES = tuple(item[0] for item in STAGES)
SPARK_STAGE = {item[0]: item[1] for item in STAGES}
STAGE_ZONE = {item[0]: item[2] for item in STAGES}
RUN_FIELD = {item[0]: item[3] for item in STAGES}
LINEAGE_ALIAS = {"bronze": "bronze", "silver": "silver", "gold": "gold", "features": "feat"}
VALIDATION_SCOPE = {"bronze": "bronze", "silver": "silver", "gold": "core-gold", "features": "features"}
QUOTA_HANDOFF = {
    "bronze": "external-to-bronze",
    "silver": "bronze-to-silver",
    "gold": "silver-to-gold",
    "features": "gold-to-features",
}


@dataclass(frozen=True)
class Pipeline:
    """One independent DAG: the stages it owns and the handoff it consumes."""

    name: str
    dag_id: str
    stages: tuple[str, ...]
    run_parameters: tuple[str, ...]
    upstream: str | None
    handoff_name: str

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return (*COMMON_PARAMETERS, *self.run_parameters)

    @property
    def label(self) -> str:
        return self.name.upper()


PIPELINES: dict[str, Pipeline] = {
    "dp1": Pipeline("dp1", "dp1_source_to_bronze", ("bronze",), ("bronze_run_id",), None, "dp1_handoff.json"),
    "dp2": Pipeline("dp2", "dp2_bronze_to_curated", ("silver", "gold"),
                    ("bronze_run_id", "silver_run_id", "gold_run_id"), "dp1", "dp2_handoff.json"),
    "dp3": Pipeline("dp3", "dp3_historical_features", ("features",), RUN_PARAMETERS, "dp2",
                    "dp3_completion.json"),
}


def upstream_stages(pipeline: Pipeline) -> tuple[str, ...]:
    """Stages owned by every pipeline before this one, oldest first."""

    stages: list[str] = []
    current = pipeline.upstream
    while current is not None:
        stages[:0] = PIPELINES[current].stages
        current = PIPELINES[current].upstream
    return tuple(stages)


def direct_inputs(pipeline: Pipeline) -> tuple[str, ...]:
    """Upstream stages whose Delta outputs this pipeline's transformations read."""

    return {"dp1": (), "dp2": ("bronze",), "dp3": ("silver", "gold")}[pipeline.name]


class CampaignError(ValueError):
    """A campaign parameter or gate refused the task."""


def require_pipeline(name: str) -> Pipeline:
    try:
        return PIPELINES[name]
    except KeyError:
        raise CampaignError(f"pipeline must be one of {sorted(PIPELINES)}, got {name!r}") from None


def expected_approval(values: Mapping[str, str], pipeline: str) -> str:
    """The exact token for one pipeline of one campaign profile at one code revision."""

    return (f"EXECUTE-PAID-DATAPROC:{require_pipeline(pipeline).label}:{values['benchmark_id']}:"
            f"{values['spark_profile']}:{values['dataset_version']}:{values['git_sha'][:12]}")


@dataclass(frozen=True)
class CampaignParams:
    pipeline: str
    dataset_version: str
    benchmark_id: str
    spark_profile: str
    git_sha: str
    source_manifest_sha256: str
    project_id: str
    region: str
    data_bucket: str
    dataproc_ttl: str
    terraform_output_json: str
    execution_approval: str = field(repr=False)
    bronze_run_id: str | None = None
    silver_run_id: str | None = None
    gold_run_id: str | None = None
    feature_run_id: str | None = None

    @property
    def spec(self) -> Pipeline:
        return PIPELINES[self.pipeline]

    @property
    def runs(self) -> ValidationRuns:
        return ValidationRuns(self.bronze_run_id, self.silver_run_id, self.gold_run_id, self.feature_run_id)

    @property
    def run_map(self) -> dict[str, str]:
        """Stage name to run ID for the runs this pipeline knows."""

        return {stage: run for stage in STAGE_NAMES if (run := getattr(self, RUN_FIELD[stage]))}

    def stage_run_id(self, stage: str) -> str:
        run = getattr(self, RUN_FIELD[stage])
        if not run:
            raise CampaignError(f"{stage} run ID is not a parameter of {self.pipeline}")
        return run

    def run_root(self, stage: str) -> str:
        return f"gs://{self.data_bucket}/lakehouse/runs/{self.stage_run_id(stage)}/{STAGE_ZONE[stage]}"

    def identity(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in IDENTITY_FIELDS}


def parse_parameters(
    values: Mapping[str, str], *, pipeline: str, require_approval: bool = True
) -> CampaignParams:
    """Validate every parameter of one pipeline and its approval; refuse anything else.

    ``require_approval=False`` is for read-only tooling (status and handoff checks),
    which never submits anything.
    """

    spec = require_pipeline(pipeline)
    missing = sorted(name for name in spec.parameter_names if name not in values)
    if missing:
        raise CampaignError(f"missing {spec.label} parameters: {missing}")
    foreign = sorted(name for name in RUN_PARAMETERS if name in values and name not in spec.run_parameters)
    if foreign:
        raise CampaignError(f"parameters {foreign} do not belong to {spec.label}")
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
    run_ids = [values[name] for name in spec.run_parameters]
    if len(set(run_ids)) != len(run_ids):
        raise CampaignError("stage run IDs must be distinct")
    try:
        for stage in STAGE_NAMES:
            if RUN_FIELD[stage] in spec.run_parameters:
                require_campaign_lineage(values[RUN_FIELD[stage]], benchmark_id=values["benchmark_id"],
                                         profile=values["spark_profile"], stage=LINEAGE_ALIAS[stage])
        resolve_storage_context(values["dataset_version"], values["data_bucket"])
    except ValueError as error:
        raise CampaignError(str(error)) from error
    if require_approval and values["execution_approval"] != expected_approval(values, pipeline):
        raise CampaignError(
            f"explicit owner approval is missing or does not match this {spec.label} run: "
            f"expected {expected_approval(values, pipeline)!r}"
        )
    return CampaignParams(pipeline=pipeline, **{name: values[name] for name in spec.parameter_names})


def pipeline_from_environment(environ: Mapping[str, str] = os.environ) -> str:
    return require_pipeline(environ.get(f"{ENV_PREFIX}PIPELINE", "")).name


def parameters_from_environment(environ: Mapping[str, str] = os.environ) -> CampaignParams:
    pipeline = pipeline_from_environment(environ)
    return parse_parameters({
        name: environ[f"{ENV_PREFIX}{name.upper()}"]
        for name in PIPELINES[pipeline].parameter_names
        if f"{ENV_PREFIX}{name.upper()}" in environ
    }, pipeline=pipeline)


def attempt_from_environment(environ: Mapping[str, str] = os.environ) -> int:
    value = environ.get(f"{ENV_PREFIX}ATTEMPT", "")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        raise CampaignError("Airflow try number is required for validation attempts")
    return int(value)


@dataclass(frozen=True)
class Producer:
    """The Airflow DagRun that published a manifest."""

    dag_id: str
    dag_run_id: str


def producer_from_environment(params: CampaignParams, environ: Mapping[str, str] = os.environ) -> Producer:
    dag_id = environ.get(f"{ENV_PREFIX}DAG_ID", "")
    dag_run_id = environ.get(f"{ENV_PREFIX}DAG_RUN_ID", "")
    if dag_id != params.spec.dag_id:
        raise CampaignError(f"publishing DAG is {dag_id!r}, expected {params.spec.dag_id!r}")
    if not _DAG_RUN_ID.fullmatch(dag_run_id):
        raise CampaignError("the Airflow DagRun ID is required to publish a handoff")
    return Producer(dag_id, dag_run_id)


# --- Manifest locations ------------------------------------------------------------

def campaign_root(params: CampaignParams) -> str:
    return f"gs://{params.data_bucket}/lakehouse/campaigns/{params.benchmark_id}/{params.spark_profile}"


def handoff_uri(params: CampaignParams, pipeline: str) -> str:
    return f"{campaign_root(params)}/{require_pipeline(pipeline).handoff_name}"


def campaign_manifest_uri(params: CampaignParams) -> str:
    return f"{campaign_root(params)}/campaign_manifest.json"


# --- Shared gates ------------------------------------------------------------------

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


_PREFIX_ABSENT: dict[str, Callable[..., None]] = {
    "bronze": submit_source_to_bronze.assert_bronze_prefix_absent,
    "silver": submit_bronze_to_silver.assert_silver_prefix_absent,
    "gold": submit_silver_to_gold.assert_output_prefix_absent,
    "features": submit_historical_features.assert_output_prefix_absent,
}


def _object_exists(uri: str, runner: Callable[..., Any]) -> bool:
    try:
        return GcloudObjectStore(runner=runner).exists(uri)
    except ObjectStoreError as error:
        raise CampaignError(str(error)) from error


def action_preflight(params: CampaignParams, runner: Callable[..., Any]) -> None:
    """Pipeline-scoped identity gate before any paid job.

    Checks the code revision, the deployment and the bucket, and that the outputs,
    batch IDs and manifests this pipeline would create are all absent. Valid upstream
    outputs are expected to exist and are verified by the upstream tasks instead.
    """

    spec = params.spec
    require_clean_repository(REPOSITORY_ROOT, params.git_sha, runner)
    runtime = load_runtime(params, runner)
    if isolated_data_bucket(params.dataset_version) is not None:
        try:
            check_bucket(StoragePlan(runtime.project_id, runtime.data_bucket, runtime.service_account_email), runner)
        except StorageNotReady as error:
            raise CampaignError(str(error)) from error
    owned = [handoff_uri(params, spec.name)] + ([campaign_manifest_uri(params)] if spec.name == "dp3" else [])
    for uri in owned:
        if _object_exists(uri, runner):
            raise CampaignError(f"{spec.label} manifest already exists and is immutable: {uri}; use a new benchmark")
    for stage in spec.stages:
        _PREFIX_ABSENT[stage](runtime, run_id=params.stage_run_id(stage), runner=runner)
        assert_batch_id_absent(runtime, batch_id=params.stage_run_id(stage), runner=runner)
    print(json.dumps({
        "status": "PASS",
        "pipeline": spec.name,
        "dag_id": spec.dag_id,
        "campaign": {name: value for name, value in asdict(params).items() if name != "execution_approval"},
        "merchant_history": dataset_has_merchant_history(params.dataset_version),
        "upstream_handoff": handoff_uri(params, spec.upstream) if spec.upstream else None,
        "publishes": owned,
        "expected_tables": {stage: sorted(expected_tables(SPARK_STAGE[stage], params.dataset_version))
                            for stage in spec.stages},
    }, indent=2, sort_keys=True))


def _list_objects(root: str, runner: Callable[..., Any]) -> Mapping[str, Mapping[str, Any]]:
    from ops.gcp.verify_source_dataset import list_objects

    return list_objects(root, runner)


def _verify_source(params: CampaignParams, runner: Callable[..., Any]) -> dict[str, Any]:
    return verify_source_dataset(
        bucket=params.data_bucket,
        dataset_version=params.dataset_version,
        expected_manifest_sha256=params.source_manifest_sha256,
        store=GcloudObjectStore(runner=runner),
        lister=lambda root: _list_objects(root, runner),
    )


def action_verify_source(params: CampaignParams, runner: Callable[..., Any]) -> None:
    print(json.dumps(_verify_source(params, runner), indent=2, sort_keys=True))


# --- Handoff verification ----------------------------------------------------------

@dataclass(frozen=True)
class VerifiedHandoff:
    pipeline: str
    uri: str
    sha256: str
    document: Mapping[str, Any]


def handoff_problems(document: Any, params: CampaignParams, pipeline: str) -> list[str]:
    """Every reason one handoff cannot feed ``params``; empty when it can.

    Pure: compares the document with the parameters only. Upstream validation reports
    and Delta outputs are re-read separately.
    """

    spec = require_pipeline(pipeline)
    if not isinstance(document, Mapping):
        return [f"{spec.label} handoff is not a JSON object"]
    problems = []
    expected = {"schema_version": HANDOFF_SCHEMA, "pipeline": spec.name, "status": "SUCCESS", **params.identity()}
    for name, value in expected.items():
        if document.get(name) != value:
            problems.append(f"{spec.label} handoff {name} is {document.get(name)!r}, expected {value!r}")
    producer = document.get("producer") or {}
    if producer.get("dag_id") != spec.dag_id or not producer.get("dag_run_id"):
        problems.append(f"{spec.label} handoff was not produced by a {spec.dag_id} DagRun")
    run_ids = document.get("run_ids") or {}
    stages = document.get("stages") or {}
    for stage in spec.stages:
        run_id = getattr(params, RUN_FIELD[stage])
        record = stages.get(stage) or {}
        validation = record.get("validation") or {}
        if not run_id:
            problems.append(f"{stage} run ID is not a parameter of this run")
            continue
        if run_ids.get(stage) != run_id or record.get("run_id") != run_id:
            problems.append(f"{spec.label} handoff {stage} run is {record.get('run_id')!r}, expected {run_id!r}")
        if record.get("output_root") != params.run_root(stage):
            problems.append(f"{spec.label} handoff {stage} output is {record.get('output_root')!r}")
        if validation.get("status") != "PASS" or not validation.get("report_uri"):
            problems.append(f"{spec.label} handoff {stage} validation is not PASS")
        for label, execution in (("transformation", record.get("execution_metadata") or {}),
                                 ("validation", validation.get("execution_metadata") or {})):
            if execution.get("dataproc_batch_state") != "SUCCEEDED":
                problems.append(f"{spec.label} handoff {stage} {label} batch is "
                                f"{execution.get('dataproc_batch_state')!r}, expected 'SUCCEEDED'")
    if set(stages) != set(spec.stages) or set(run_ids) != set(spec.stages):
        problems.append(f"{spec.label} handoff covers {sorted(stages)}, expected {sorted(spec.stages)}")
    upstream = document.get("upstream")
    if spec.upstream is None and upstream is not None:
        problems.append(f"{spec.label} handoff names an upstream it cannot have")
    if spec.upstream is not None and not (isinstance(upstream, Mapping) and upstream.get("pipeline") == spec.upstream
                                          and upstream.get("uri") == handoff_uri(params, spec.upstream)
                                          and re.fullmatch(r"[0-9a-f]{64}", str(upstream.get("sha256", "")))):
        problems.append(f"{spec.label} handoff does not reference the {spec.upstream.upper()} handoff")
    return problems


def verify_handoff(
    params: CampaignParams, pipeline: str, runner: Callable[..., Any], *, recheck_reports: bool = True
) -> VerifiedHandoff:
    """Read one handoff and its upstream chain; refuse anything missing or inconsistent."""

    spec = require_pipeline(pipeline)
    uri = handoff_uri(params, pipeline)
    store = GcloudObjectStore(runner=runner)
    try:
        if not store.exists(uri):
            raise CampaignError(f"{spec.label} handoff is missing: {uri}; {spec.dag_id} has not completed successfully")
        text = store.read_text(uri)
    except ObjectStoreError as error:
        raise CampaignError(f"{spec.label} handoff could not be read: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise CampaignError(f"{spec.label} handoff is not valid JSON: {uri}") from error
    problems = handoff_problems(document, params, pipeline)
    if problems:
        raise CampaignError(f"{spec.label} handoff {uri} refused: " + "; ".join(problems))
    if recheck_reports:
        for stage in spec.stages:
            report_uri = document["stages"][stage]["validation"]["report_uri"]
            try:
                report = submit_stage_validation.read_report(report_uri, runner=runner)
                submit_stage_validation.require_passing_report(
                    report, scope=VALIDATION_SCOPE[stage], run_id=params.stage_run_id(stage),
                    dataset_version=params.dataset_version)
            except (ValueError, subprocess.CalledProcessError) as error:
                raise CampaignError(f"{spec.label} {stage} validation report {report_uri} refused: {error}") from error
    if spec.upstream is not None:
        upstream = verify_handoff(params, spec.upstream, runner, recheck_reports=recheck_reports)
        if document["upstream"]["sha256"] != upstream.sha256:
            raise CampaignError(f"{spec.label} handoff was built on a different {upstream.pipeline.upper()} handoff")
    return VerifiedHandoff(spec.name, uri, hashlib.sha256(text.encode("utf-8")).hexdigest(), document)


def require_upstream(params: CampaignParams, runner: Callable[..., Any], *, recheck_reports: bool = True) -> VerifiedHandoff:
    upstream = params.spec.upstream
    if upstream is None:
        raise CampaignError("DP1 has no upstream handoff")
    return verify_handoff(params, upstream, runner, recheck_reports=recheck_reports)


def action_verify_upstream_handoff(params: CampaignParams, runner: Callable[..., Any]) -> None:
    verified = require_upstream(params, runner)
    print(json.dumps({"status": "PASS", "handoff_uri": verified.uri, "handoff_sha256": verified.sha256,
                      "upstream_dag_run_id": verified.document["producer"]["dag_run_id"],
                      "run_ids": params.run_map}, indent=2, sort_keys=True))


def action_verify_upstream_inputs(params: CampaignParams, runner: Callable[..., Any]) -> None:
    """The upstream Delta runs this pipeline reads must exist with their exact inventory."""

    for stage in direct_inputs(params.spec):
        tables = expected_tables(SPARK_STAGE[stage], params.dataset_version)
        try:
            verify_delta_run.verify_delta_run(params.run_root(stage), tables, runner=runner)
        except ValueError as error:
            raise CampaignError(f"upstream {stage} output is incomplete: {error}") from error
        print(f"PASS: upstream {stage}: {len(tables)} Delta tables at {params.run_root(stage)}")


# --- Stage tasks -------------------------------------------------------------------

def _require_stage(params: CampaignParams, stage: str) -> None:
    if stage not in params.spec.stages:
        raise CampaignError(f"stage {stage} is not part of {params.spec.label}")


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
    _require_stage(params, stage)
    previous = {"bronze": None, "silver": "bronze", "gold": "silver", "features": "gold"}[stage]
    _quota(params, stage, QUOTA_HANDOFF[stage], params.stage_run_id(previous) if previous else "", "quota")


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
    runs = params.run_map
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
    submission's own result; publication collects again and fails closed.
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
    """Submit one transformation after re-checking its upstream. Submitters refuse existing outputs."""

    _require_stage(params, stage)
    if params.spec.upstream is not None:
        require_upstream(params, runner, recheck_reports=False)
    for earlier in params.spec.stages[:params.spec.stages.index(stage)]:
        _require_passing_validation(params, earlier, runner)
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
    _require_stage(params, stage)
    tables = expected_tables(SPARK_STAGE[stage], params.dataset_version)
    verify_delta_run.verify_delta_run(params.run_root(stage), tables, runner=runner)
    print(f"PASS: {len(tables)} Delta tables at {params.run_root(stage)}")


def action_validate(params: CampaignParams, stage: str, attempt: int, runner: Callable[..., Any]) -> None:
    """Submit the read-only semantic validator for one stage and require a PASS report."""

    _require_stage(params, stage)
    scope = VALIDATION_SCOPE[stage]
    _quota(params, stage, "stage-to-validation", params.stage_run_id(stage), f"validation-quota-a{attempt}")
    arguments = [
        f"--scope={scope}",
        f"--dataset-version={params.dataset_version}",
        f"--benchmark-id={params.benchmark_id}",
        f"--spark-profile={params.spark_profile}",
        f"--git-sha={params.git_sha}",
        *[f"--{name}-run-id={run}" for name, run in params.run_map.items()],
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


def _require_passing_validation(params: CampaignParams, stage: str, runner: Callable[..., Any]) -> None:
    """A later stage of one DagRun starts only after the earlier stage's validator passed."""

    _, report = latest_validation_report(params, stage, runner)
    submit_stage_validation.require_passing_report(
        report, scope=VALIDATION_SCOPE[stage], run_id=params.stage_run_id(stage),
        dataset_version=params.dataset_version)


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


def stage_record(params: CampaignParams, stage: str, runner: Callable[..., Any]) -> dict[str, Any]:
    """The verified record of one finished stage: output, provenance and validation."""

    run_id = params.stage_run_id(stage)
    scope = VALIDATION_SCOPE[stage]
    try:
        uri, report = latest_validation_report(params, stage, runner)
        submit_stage_validation.require_passing_report(
            report, scope=scope, run_id=run_id, dataset_version=params.dataset_version)
        latest = int(uri.rsplit("-a", 1)[1].removesuffix(".json"))
        transformation = _collect(params, stage, "transformation", run_id, None, runner)
        attempts = _validation_attempts(params, stage, latest, runner)
    except spark_provenance.ProvenanceError as error:
        raise CampaignError(str(error)) from error
    _require_succeeded(transformation)
    _require_succeeded(attempts[-1])
    return {
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


# --- Publication -------------------------------------------------------------------

def _stable(document: Mapping[str, Any], volatile: Sequence[str]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in volatile}


def publish_immutable(uri: str, document: Mapping[str, Any], runner: Callable[..., Any], *,
                      volatile: Sequence[str]) -> str:
    """Write once; a retry may only find the same content. Returns the stored SHA-256.

    An existing object whose content differs (other than ``volatile`` timestamps) is a
    conflict and is never overwritten.
    """

    store = GcloudObjectStore(runner=runner)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        if store.exists(uri):
            existing_text = store.read_text(uri)
            try:
                existing = json.loads(existing_text)
            except json.JSONDecodeError as error:
                raise CampaignError(f"conflicting manifest (not JSON) already exists: {uri}") from error
            if not isinstance(existing, Mapping) or _stable(existing, volatile) != _stable(document, volatile):
                raise CampaignError(f"a different manifest already exists and is immutable: {uri}")
            print(f"Manifest already published with identical content: {uri}")
            return hashlib.sha256(existing_text.encode("utf-8")).hexdigest()
        store.write_text(text, uri, if_absent=True)
    except ObjectStoreError as error:
        raise CampaignError(f"manifest could not be published to {uri}: {error}") from error
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_summary(source: Mapping[str, Any]) -> dict[str, Any]:
    return {"manifest_uri": source["manifest_uri"], "manifest_sha256": source["manifest_sha256"],
            "plan_digest": source["plan_digest"], "physical_bytes": source["physical_bytes"]}


def build_campaign_manifest(params: CampaignParams, source: Mapping[str, Any], stages: Mapping[str, Any],
                            handoffs: Mapping[str, VerifiedHandoff]) -> dict[str, Any]:
    """The completed-campaign manifest read by ``compare_campaign_outputs.py``."""

    storage = resolve_storage_context(params.dataset_version, params.data_bucket)
    return {
        "schema_version": CAMPAIGN_SCHEMA,
        "status": "COMPLETE",
        "benchmark_id": params.benchmark_id,
        "spark_profile": params.spark_profile,
        "dataset_version": params.dataset_version,
        "git_sha": params.git_sha,
        "storage": {"data_bucket": storage.data_bucket, "source_root": storage.source_root,
                    "spark_event_log_root": storage.spark_event_log_root},
        "source": _source_summary(source),
        "lineage": ["source", *STAGE_NAMES],
        "stages": dict(stages),
        "handoffs": {name: {"uri": item.uri, "sha256": item.sha256,
                            "dag_run_id": item.document["producer"]["dag_run_id"]}
                     for name, item in handoffs.items()},
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }


def action_publish(params: CampaignParams, producer: Producer, runner: Callable[..., Any]) -> None:
    """Publish this pipeline's immutable handoff after every owned stage passed.

    DP3 also publishes the completed campaign manifest, built exactly as the integrated
    campaign did: the Source re-verified and every stage collected and checked again.
    """

    spec = params.spec
    upstream = require_upstream(params, runner) if spec.upstream else None
    stages = {stage: stage_record(params, stage, runner) for stage in spec.stages}
    document: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA,
        "pipeline": spec.name,
        "status": "SUCCESS",
        **params.identity(),
        "producer": {"dag_id": producer.dag_id, "dag_run_id": producer.dag_run_id},
        "upstream": None if upstream is None else {
            "pipeline": upstream.pipeline, "uri": upstream.uri, "sha256": upstream.sha256,
            "dag_id": upstream.document["producer"]["dag_id"],
            "dag_run_id": upstream.document["producer"]["dag_run_id"],
        },
        "run_ids": {stage: params.stage_run_id(stage) for stage in spec.stages},
        "stages": stages,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if spec.name in ("dp1", "dp3"):
        source = _verify_source(params, runner)
        document["source"] = _source_summary(source)
    if spec.name == "dp3":
        dp2 = upstream
        dp1 = verify_handoff(params, "dp1", runner, recheck_reports=False)
        every_stage = {stage: stage_record(params, stage, runner) for stage in upstream_stages(spec)}
        every_stage.update(stages)
        manifest = build_campaign_manifest(params, source, every_stage, {"dp1": dp1, "dp2": dp2})
        manifest_sha = publish_immutable(campaign_manifest_uri(params), manifest, runner, volatile=("finalized_at",))
        document["campaign_manifest"] = {"uri": campaign_manifest_uri(params), "sha256": manifest_sha}
    uri = handoff_uri(params, spec.name)
    sha = publish_immutable(uri, document, runner, volatile=("completed_at",))
    print(json.dumps({"handoff_uri": uri, "handoff_sha256": sha, **document}, indent=2, sort_keys=True))


# --- Dispatch ----------------------------------------------------------------------

def actions_for(pipeline: str) -> tuple[str, ...]:
    """Task actions of one pipeline, in DAG order."""

    spec = require_pipeline(pipeline)
    head = ("preflight", "verify-source") if spec.upstream is None else (
        "preflight", "verify-upstream-handoff", "verify-upstream-inputs")
    stages = tuple(f"{verb}-{stage}" for stage in spec.stages for verb in ("quota", "submit", "physical", "validate"))
    return (*head, *stages, "publish-handoff")


ACTIONS = tuple(dict.fromkeys(action for name in PIPELINES for action in actions_for(name)))


def run_action(action: str, params: CampaignParams, *, attempt: int, runner: Callable[..., Any],
               producer: Producer | None = None) -> None:
    if action not in actions_for(params.pipeline):
        raise CampaignError(f"action {action} is not part of {params.spec.label}")
    verb, _, stage = action.partition("-")
    if action == "preflight":
        action_preflight(params, runner)
    elif action == "verify-source":
        action_verify_source(params, runner)
    elif action == "verify-upstream-handoff":
        action_verify_upstream_handoff(params, runner)
    elif action == "verify-upstream-inputs":
        action_verify_upstream_inputs(params, runner)
    elif action == "publish-handoff":
        if producer is None:
            raise CampaignError("the publishing DagRun identity is required")
        action_publish(params, producer, runner)
    elif verb == "quota":
        action_quota(params, stage)
    elif verb == "submit":
        action_submit(params, stage, runner)
    elif verb == "physical":
        action_physical(params, stage, runner)
    elif verb == "validate":
        action_validate(params, stage, attempt, runner)
    else:  # pragma: no cover - guarded by actions_for
        raise CampaignError(f"unknown campaign action: {action}")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        params = parameters_from_environment(environ)
        attempt = attempt_from_environment(environ)
        producer = producer_from_environment(params, environ) if args.action == "publish-handoff" else None
        run_action(args.action, params, attempt=attempt, runner=runner, producer=producer)
    except CampaignError as error:
        print(f"CAMPAIGN TASK REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
