"""Terminal workflow for the three EDAI-1 pipeline DAGs (behind ``scripts/edai1/*.sh``).

Subcommands:

``prepare``          Plan one Baseline/Optimized benchmark: pin the Git SHA, the Source
                     manifest and the deployment, and write the six DagRun configurations
                     to an ignored local directory. Reads GCS only to verify the Source.
``run``              Check one pipeline/profile and, only with ``--execute --confirm-paid``,
                     trigger exactly that one DAG in the local Airflow. Dry run by default.
``status``           Read-only table of the six planned DagRuns and their handoffs.
``verify-handoffs``  Read-only verification of DP1 -> DP2 -> DP3 for one profile.

Nothing here submits Dataproc work directly, writes or deletes GCS objects, resets
Airflow metadata or changes Git. The only state-changing calls are, in ``run
--execute --confirm-paid`` only, ``airflow dags unpause`` and ``airflow dags trigger``
for the one requested DAG; the DAG's own gated tasks do the paid work.

Exit codes: 0 ready or done, 1 refused or not ready, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import REHEARSAL_5GB, require_approved_dataset, resolve_storage_context
from data_platform.config.spark_profiles import SPARK_PROFILES, validate_benchmark_id
from data_platform.generation.src.sharded.storage import ObjectStoreError
from ops.gcp import campaign_task
from ops.gcp.campaign_task import PIPELINES, CampaignError, CampaignParams, parse_parameters
from ops.gcp.dataproc_batch_properties import profile_summary
from ops.gcp.submit_silver_to_gold import TERRAFORM_DEV_ROOT
from ops.gcp.submit_source_to_bronze import apply_dataset_bucket, load_cloud_runtime


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLAN_SCHEMA = "edai1-local-campaign-plan-v1"
AIRFLOW_UI = "http://127.0.0.1:8084"
DEFAULT_PROJECT = "ewallet-data-ai-platform-hg26"
DEFAULT_REGION = "asia-southeast1"
REQUIRED_BRANCH = "main"
# Published and verified Source manifests (docs/runbooks/edai1/cloud/edai1_campaign_runbook.md).
KNOWN_SOURCE_MANIFESTS = {
    REHEARSAL_5GB: "ac2dfb0eac16e173261b3c3f65e83a03d388528538daefd9ec669bef7123eb80",
}
# Benchmarks that must never be reused: troubleshooting evidence and the interrupted
# 2026-11-11 integrated-campaign attempt (abandoned; its run IDs are never reused).
ABANDONED_BENCHMARKS = frozenset({"ab-20261025112811", "ab-20261111033139"})
PIPELINE_ORDER = ("dp1", "dp2", "dp3")
STAGE_OFFSETS = {"bronze_run_id": ("bronze", 0), "silver_run_id": ("silver", 1),
                 "gold_run_id": ("gold", 2), "feature_run_id": ("feat", 3)}
SPARK_STAGES = {"bronze": "dp1-bronze", "silver": "dp2-silver", "gold": "dp2-gold", "features": "dp3-features"}
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class CliError(Exception):
    """A refused or not-ready request (exit 1)."""


# --- Small helpers -----------------------------------------------------------------

def campaign_base(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("EDAI1_CAMPAIGN_DIR") or REPOSITORY_ROOT / "tmp/edai1-campaign").resolve()


def campaign_dir(benchmark_id: str, environ: Mapping[str, str] = os.environ) -> Path:
    return campaign_base(environ) / validate_benchmark_id(benchmark_id)


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"not found: {path}; run scripts/edai1/prepare_campaign.sh first") from None
    except json.JSONDecodeError as error:
        raise CliError(f"not valid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise CliError(f"not a JSON object: {path}")
    return value


def json_payload(stdout: str) -> Any:
    """Airflow CLI JSON output, skipping warning lines printed before it."""

    for index, line in enumerate(stdout.splitlines()):
        if line.startswith(("[", "{")):
            return json.loads("\n".join(stdout.splitlines()[index:]))
    raise ValueError("no JSON document in Airflow CLI output")


def _git(runner: Runner, *args: str) -> str:
    completed = runner(["git", "-C", str(REPOSITORY_ROOT), *args], check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise CliError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


@dataclass(frozen=True)
class GitState:
    branch: str
    head: str
    dirty: tuple[str, ...]


def git_state(runner: Runner) -> GitState:
    status = _git(runner, "status", "--porcelain")
    return GitState(_git(runner, "rev-parse", "--abbrev-ref", "HEAD"), _git(runner, "rev-parse", "HEAD"),
                    tuple(line for line in status.splitlines() if line.strip()))


def git_problems(state: GitState, pinned_sha: str | None = None) -> list[str]:
    problems = []
    if state.branch != REQUIRED_BRANCH:
        problems.append(f"current branch is {state.branch!r}; the campaign runs from {REQUIRED_BRANCH!r}")
    if state.dirty:
        problems.append(f"working tree is not clean ({len(state.dirty)} changed paths)")
    if pinned_sha is not None and state.head != pinned_sha:
        problems.append(f"HEAD {state.head[:12]} differs from the benchmark's pinned Git SHA {pinned_sha[:12]}; "
                        "the benchmark never follows main automatically")
    return problems


# --- Airflow (local runtime on 127.0.0.1:8084) -------------------------------------

@dataclass(frozen=True)
class Airflow:
    binary: str
    runner: Runner

    def environment(self) -> dict[str, str]:
        home = REPOSITORY_ROOT / "data_platform/orchestration/airflow"
        python_path = os.environ.get("PYTHONPATH", "")
        return {
            **os.environ,
            "AIRFLOW_HOME": str(home),
            "AIRFLOW__CORE__DAGS_FOLDER": str(home / "dags"),
            "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": f"{AIRFLOW_UI}/execution/",
            "PYTHONPATH": f"{REPOSITORY_ROOT}{':' + python_path if python_path else ''}",
        }

    def call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner([self.binary, *args], check=False, text=True, capture_output=True, env=self.environment())

    def json(self, *args: str) -> Any:
        completed = self.call(*args, "-o", "json")
        if completed.returncode != 0:
            raise CliError(f"airflow {' '.join(args)} failed: {completed.stderr.strip()[-500:]}")
        try:
            return json_payload(completed.stdout)
        except (ValueError, json.JSONDecodeError) as error:
            raise CliError(f"airflow {' '.join(args)} returned no JSON: {error}") from error

    def dag_runs(self, dag_id: str) -> list[Mapping[str, Any]]:
        value = self.json("dags", "list-runs", dag_id)
        return list(value) if isinstance(value, list) else []

    def task_states(self, dag_id: str, run_id: str) -> list[Mapping[str, Any]]:
        value = self.json("tasks", "states-for-dag-run", dag_id, run_id)
        return list(value) if isinstance(value, list) else []


def resolve_airflow(runner: Runner, environ: Mapping[str, str] = os.environ) -> Airflow:
    binary = environ.get("AIRFLOW_BIN") or str(Path.home() / "miniconda3/envs/airflow/bin/airflow")
    if not (Path(binary).is_file() and os.access(binary, os.X_OK)):
        raise CliError(f"Airflow CLI not found: {binary}; set AIRFLOW_BIN")
    return Airflow(binary, runner)


def dag_run_link(dag_id: str, run_id: str) -> str:
    return f"{AIRFLOW_UI}/dags/{dag_id}/runs/{run_id}"


# --- prepare -----------------------------------------------------------------------

def _stamp(benchmark_id: str, offset: int) -> str:
    moment = datetime.strptime(benchmark_id[3:], "%Y%m%d%H%M%S") + timedelta(seconds=offset)
    return f"r{moment:%y%m%d%H%M%S}"


def plan_run_ids(benchmark_id: str, profile: str) -> dict[str, str]:
    """Distinct, profile-bound run IDs of one profile; never reused across benchmarks."""

    return {name: f"{benchmark_id}-{profile}-{alias}-{_stamp(benchmark_id, offset)}"
            for name, (alias, offset) in STAGE_OFFSETS.items()}


def dag_run_id(benchmark_id: str, profile: str, pipeline: str) -> str:
    return f"{benchmark_id}-{profile}-{pipeline}"


def pipeline_conf(plan: Mapping[str, Any], profile: str, pipeline: str) -> dict[str, str]:
    """The Airflow conf of one DagRun, non-executing until ``run`` adds the approval."""

    runs = plan["profiles"][profile]["run_ids"]
    conf = {name: plan[name] for name in ("dataset_version", "benchmark_id", "git_sha", "source_manifest_sha256",
                                          "project_id", "region", "data_bucket", "dataproc_ttl",
                                          "terraform_output_json")}
    conf["spark_profile"] = profile
    conf.update({name: runs[name] for name in PIPELINES[pipeline].run_parameters})
    conf["execution_approval"] = campaign_task.NOT_APPROVED
    return conf


def _load_terraform(path: Path | None, runner: Runner) -> dict[str, Any]:
    if path is not None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CliError(f"Terraform outputs are not readable: {path}: {error}") from error
    else:
        completed = runner(["terraform", f"-chdir={TERRAFORM_DEV_ROOT}", "output", "-json"],
                           check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise CliError("terraform output failed; pass --terraform-output-json with a saved copy: "
                           + completed.stderr.strip()[-300:])
        value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise CliError("Terraform outputs must be a JSON object")
    return value


def command_prepare(args: argparse.Namespace, runner: Runner, environ: Mapping[str, str]) -> int:
    dataset = args.dataset_version
    identity = require_approved_dataset(dataset)
    storage = resolve_storage_context(dataset)
    benchmark_id = args.benchmark_id or f"ab-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    validate_benchmark_id(benchmark_id)
    if benchmark_id in ABANDONED_BENCHMARKS:
        raise CliError(f"benchmark {benchmark_id} is abandoned evidence and is never reused")
    expected_sha = args.source_manifest_sha256 or KNOWN_SOURCE_MANIFESTS.get(dataset)
    if not expected_sha or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise CliError(f"--source-manifest-sha256 is required for {dataset}")

    state = git_state(runner)
    problems = git_problems(state)
    if problems:
        raise CliError("; ".join(problems))
    directory = campaign_dir(benchmark_id, environ)
    if directory.exists():
        raise CliError(f"campaign directory already exists, nothing was changed: {directory}")
    if not _SAFE_PATH.fullmatch(str(directory)):
        raise CliError(f"campaign directory path is not a safe DagRun parameter: {directory}")

    outputs = _load_terraform(Path(args.terraform_output_json).resolve() if args.terraform_output_json else None,
                              runner)
    try:
        runtime = apply_dataset_bucket(load_cloud_runtime(outputs), dataset)
    except (KeyError, TypeError, ValueError) as error:
        raise CliError(f"Terraform outputs do not describe a usable deployment: {error}") from error
    if (runtime.project_id, runtime.region, runtime.data_bucket) != (args.project_id, args.region, storage.data_bucket):
        raise CliError(f"Terraform deployment {(runtime.project_id, runtime.region, runtime.data_bucket)} differs "
                       f"from {(args.project_id, args.region, storage.data_bucket)}")

    if args.skip_source_check:
        source = {"status": "NOT_VERIFIED", "reason": "--skip-source-check; DP1 verify_source_dataset still gates"}
    else:
        report = campaign_task.verify_source_dataset(
            bucket=storage.data_bucket, dataset_version=dataset, expected_manifest_sha256=expected_sha,
            store=campaign_task.GcloudObjectStore(runner=runner),
            lister=lambda root: campaign_task._list_objects(root, runner))
        source = {"status": report["status"], "manifest_uri": report["manifest_uri"],
                  "manifest_sha256": report["manifest_sha256"], "plan_digest": report["plan_digest"],
                  "physical_bytes": report["physical_bytes"]}

    terraform_copy = directory / "terraform-outputs.json"
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "benchmark_id": benchmark_id,
        "dataset_version": dataset,
        "dataset_target_source_bytes": identity.target_source_bytes,
        "source_manifest_sha256": expected_sha,
        "source_verification": source,
        "git_sha": state.head,
        "git_branch": state.branch,
        "project_id": runtime.project_id,
        "region": runtime.region,
        "data_bucket": runtime.data_bucket,
        "dataproc_ttl": campaign_task.REQUIRED_TTL,
        "terraform_output_json": str(terraform_copy),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profiles": {
            profile: {
                "run_ids": plan_run_ids(benchmark_id, profile),
                "dag_runs": {pipeline: dag_run_id(benchmark_id, profile, pipeline) for pipeline in PIPELINE_ORDER},
            }
            for profile in SPARK_PROFILES
        },
        "handoff_root": f"gs://{runtime.data_bucket}/lakehouse/campaigns/{benchmark_id}",
    }
    # Validate every conf before anything is written.
    for profile in SPARK_PROFILES:
        for pipeline in PIPELINE_ORDER:
            parse_parameters(pipeline_conf(plan, profile, pipeline), pipeline=pipeline, require_approval=False)
    directory.mkdir(mode=0o700, parents=True)
    _write_private_json(terraform_copy, outputs)
    _write_private_json(directory / "campaign.json", plan)
    for profile in SPARK_PROFILES:
        for pipeline in PIPELINE_ORDER:
            _write_private_json(directory / profile / f"{pipeline}.json", pipeline_conf(plan, profile, pipeline))

    print(f"PREPARED benchmark {benchmark_id} (nothing was triggered or submitted)")
    print(f"  directory       {directory}")
    print(f"  dataset         {dataset}  source manifest {expected_sha}")
    print(f"  source check    {source['status']}")
    print(f"  git sha         {state.head} (pinned; main may move on, this benchmark will not)")
    print(f"  deployment      {runtime.project_id} / {runtime.region} / gs://{runtime.data_bucket}")
    print(f"  handoffs        {plan['handoff_root']}/<profile>/")
    for profile in SPARK_PROFILES:
        print(f"  {profile}:")
        for name, run in plan["profiles"][profile]["run_ids"].items():
            print(f"    {name:<15} {run}")
    print("Next:")
    print(f"  bash scripts/edai1/run_dp1.sh --benchmark-id {benchmark_id} --profile baseline")
    return 0


# --- shared loading ----------------------------------------------------------------

@dataclass(frozen=True)
class Planned:
    plan: Mapping[str, Any]
    profile: str
    pipeline: str
    conf: Mapping[str, str]
    params: CampaignParams

    @property
    def dag_id(self) -> str:
        return PIPELINES[self.pipeline].dag_id

    @property
    def dag_run_id(self) -> str:
        return self.plan["profiles"][self.profile]["dag_runs"][self.pipeline]


def load_planned(benchmark_id: str, profile: str, pipeline: str, environ: Mapping[str, str]) -> Planned:
    """One DagRun conf, re-validated and checked against its campaign plan."""

    if profile not in SPARK_PROFILES:
        raise CliError(f"--profile must be one of {SPARK_PROFILES}")
    directory = campaign_dir(benchmark_id, environ)
    plan = _read_json(directory / "campaign.json")
    conf = _read_json(directory / profile / f"{pipeline}.json")
    if plan.get("schema_version") != PLAN_SCHEMA or plan.get("benchmark_id") != benchmark_id:
        raise CliError(f"{directory / 'campaign.json'} is not the plan of {benchmark_id}")
    if conf != pipeline_conf(plan, profile, pipeline):
        raise CliError(f"{directory / profile / (pipeline + '.json')} differs from its campaign plan; "
                       "prepared files must not be edited")
    try:
        params = parse_parameters(conf, pipeline=pipeline, require_approval=False)
    except CampaignError as error:
        raise CliError(str(error)) from error
    return Planned(plan, profile, pipeline, conf, params)


# --- run ---------------------------------------------------------------------------

def _cloud_readiness(planned: Planned, runner: Runner) -> list[str]:
    """Read-only GCS/Dataproc checks: upstream handoff, own outputs absent."""

    problems: list[str] = []
    params = planned.params
    spec = params.spec
    if spec.upstream is not None:
        try:
            verified = campaign_task.require_upstream(params, runner)
            print(f"  upstream        {verified.uri} PASS (sha256 {verified.sha256[:16]}…, "
                  f"DagRun {verified.document['producer']['dag_run_id']})")
        except CampaignError as error:
            problems.append(str(error))
    try:
        runtime = campaign_task.load_runtime(params, runner)
        if campaign_task._object_exists(campaign_task.handoff_uri(params, spec.name), runner):
            problems.append(f"{spec.label} handoff already published: {campaign_task.handoff_uri(params, spec.name)}")
        for stage in spec.stages:
            try:
                campaign_task._PREFIX_ABSENT[stage](runtime, run_id=params.stage_run_id(stage), runner=runner)
                campaign_task.assert_batch_id_absent(runtime, batch_id=params.stage_run_id(stage), runner=runner)
            except (ValueError, RuntimeError) as error:
                problems.append(f"{stage}: {error}")
    except (CampaignError, ValueError, OSError) as error:
        problems.append(str(error))
    return problems


def _airflow_readiness(planned: Planned, airflow: Airflow) -> list[str]:
    problems = []
    details = airflow.call("dags", "details", planned.dag_id, "-o", "json")
    if details.returncode != 0:
        problems.append(f"DAG {planned.dag_id} is not registered in the local Airflow; start it with "
                        "./scripts/start_airflow_local.sh and check `airflow dags list-import-errors`")
        return problems
    runs = airflow.dag_runs(planned.dag_id)
    if any(run.get("run_id") == planned.dag_run_id for run in runs):
        problems.append(f"DagRun {planned.dag_run_id} already exists; a benchmark run is never triggered twice")
    active = [run["run_id"] for run in runs if run.get("state") in ("queued", "running")]
    if active:
        problems.append(f"{planned.dag_id} has active DagRuns {active}; wait for them or stop them in the UI")
    return problems


def _print_run_summary(planned: Planned, approval: str) -> None:
    params = planned.params
    spec = params.spec
    print(f"{spec.label} {planned.profile.upper()}  dag={planned.dag_id}  dag_run_id={planned.dag_run_id}")
    print(f"  benchmark       {params.benchmark_id}")
    print(f"  dataset         {params.dataset_version}  source manifest {params.source_manifest_sha256}")
    print(f"  git sha         {params.git_sha}")
    print(f"  deployment      {params.project_id} / {params.region} / gs://{params.data_bucket}")
    for stage in spec.stages:
        print(f"  {stage:<15} run {params.stage_run_id(stage)} -> {params.run_root(stage)}")
        summary = dict(item.split(": ", 1) for item in profile_summary(planned.profile, stage=SPARK_STAGES[stage]))
        print(f"  {'':<15} AQE={summary['AQE']} SHUFFLE_PARTITIONS={summary['SHUFFLE_PARTITIONS']} "
              f"SKEW_JOIN={summary['SKEW_JOIN']} COALESCE={summary['COALESCE_PARTITIONS']} "
              f"ADVISORY={summary['ADVISORY_PARTITION_SIZE_BYTES']} "
              f"ALLOCATION_PROFILE={summary['DYNAMIC_ALLOCATION_PROFILE']}")
    if spec.upstream:
        print(f"  reads handoff   {campaign_task.handoff_uri(params, spec.upstream)}")
    print(f"  publishes       {campaign_task.handoff_uri(params, spec.name)}")
    print(f"  approval token  {approval}")


def command_run(args: argparse.Namespace, runner: Runner, environ: Mapping[str, str]) -> int:
    if args.confirm_paid and not args.execute:
        raise CliError("--confirm-paid is only meaningful with --execute")
    if args.execute and not args.confirm_paid:
        raise CliError("--execute triggers paid Dataproc work through Airflow; add --confirm-paid to approve it")
    if args.execute and args.offline:
        raise CliError("--offline skips required checks and is only allowed for a dry run")
    planned = load_planned(args.benchmark_id, args.profile, args.pipeline, environ)
    approval = campaign_task.expected_approval(planned.conf, planned.pipeline)
    print(f"MODE: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    _print_run_summary(planned, approval)

    problems = git_problems(git_state(runner), planned.params.git_sha)
    try:
        airflow = resolve_airflow(runner, environ)
        problems += _airflow_readiness(planned, airflow)
    except CliError as error:
        problems.append(str(error))
        airflow = None
    if args.offline:
        print("  cloud checks    SKIPPED (--offline): upstream handoff and output absence were not read")
    else:
        problems += _cloud_readiness(planned, runner)

    if problems:
        sys.stdout.flush()
        print(f"NOT READY: {planned.dag_id} {planned.profile} was not triggered:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    conf = {**planned.conf, "execution_approval": approval}
    if not args.execute:
        print("READY. DRY-RUN only: nothing was triggered. To execute exactly this DAG run:")
        print(f"  bash scripts/edai1/run_{planned.pipeline}.sh --benchmark-id {planned.params.benchmark_id} "
              f"--profile {planned.profile} --execute --confirm-paid")
        return 0

    assert airflow is not None
    status = runner([str(REPOSITORY_ROOT / "scripts/start_airflow_local.sh"), "--status"],
                    check=False, text=True, capture_output=True, env={**os.environ, "AIRFLOW_BIN": airflow.binary})
    if status.returncode != 0:
        print(status.stdout, end="")
        raise CliError("the local Airflow runtime is not healthy; run ./scripts/start_airflow_local.sh")
    unpause = airflow.call("dags", "unpause", planned.dag_id)
    if unpause.returncode != 0:
        raise CliError(f"could not unpause {planned.dag_id}: {unpause.stderr.strip()[-300:]}")
    trigger = airflow.call("dags", "trigger", planned.dag_id, "--run-id", planned.dag_run_id,
                           "--conf", json.dumps(conf, sort_keys=True), "-o", "json")
    if trigger.returncode != 0:
        raise CliError(f"airflow dags trigger failed: {trigger.stderr.strip()[-500:]}")
    print(f"TRIGGERED {planned.dag_id} DagRun {planned.dag_run_id}")
    print(f"  {dag_run_link(planned.dag_id, planned.dag_run_id)}")
    print(f"  follow with: bash scripts/edai1/status_campaign.sh --benchmark-id {planned.params.benchmark_id}")
    return 0


# --- status ------------------------------------------------------------------------

def _current_task(states: Sequence[Mapping[str, Any]]) -> str:
    for wanted in (("running", "queued", "up_for_retry", "scheduled", "deferred"), ("failed", "upstream_failed")):
        for item in states:
            if item.get("state") in wanted:
                return f"{item['task_id']} ({item['state']})"
    done = [item for item in states if item.get("state") == "success"]
    return f"{done[-1]['task_id']} (success)" if done else "NOT_AVAILABLE"


def _handoff_details(params: CampaignParams, pipeline: str, runner: Runner) -> tuple[str, list[str]]:
    uri = campaign_task.handoff_uri(params, pipeline)
    store = campaign_task.GcloudObjectStore(runner=runner)
    try:
        if not store.exists(uri):
            return "ABSENT", []
        document = json.loads(store.read_text(uri))
    except Exception as error:  # status is best effort and never invents a state
        return "UNKNOWN", [f"handoff unreadable: {error}"]
    lines = []
    for stage, record in (document.get("stages") or {}).items():
        execution = record.get("execution_metadata") or {}
        validation = record.get("validation") or {}
        lines.append(f"{stage}: batch={execution.get('dataproc_batch_id') or 'UNKNOWN'} "
                     f"state={execution.get('dataproc_batch_state') or 'UNKNOWN'} "
                     f"app={execution.get('spark_application_id') or 'NOT_AVAILABLE'} "
                     f"validation={validation.get('status') or 'UNKNOWN'}")
        lines.append(f"{'':<{len(stage) + 2}}spark_ui={execution.get('history_server_uri') or 'NOT_AVAILABLE'} "
                     f"event_log={execution.get('spark_event_log_uri') or 'NOT_AVAILABLE'}")
    return ("PUBLISHED" if document.get("status") == "SUCCESS" else "UNKNOWN"), lines


def _batch_state(params: CampaignParams, run_id: str, runner: Runner) -> str:
    completed = runner(["gcloud", "dataproc", "batches", "describe", run_id, f"--project={params.project_id}",
                        f"--region={params.region}", "--format=json"], check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        return "NOT_SUBMITTED" if "not_found" in (completed.stdout + completed.stderr).lower() else "UNKNOWN"
    try:
        return str(json.loads(completed.stdout).get("state") or "UNKNOWN")
    except json.JSONDecodeError:
        return "UNKNOWN"


def command_status(args: argparse.Namespace, runner: Runner, environ: Mapping[str, str]) -> int:
    try:
        airflow: Airflow | None = resolve_airflow(runner, environ)
    except CliError as error:
        print(f"WARNING: {error}; Airflow states are UNKNOWN", file=sys.stderr)
        airflow = None
    rows, details = [], []
    for profile in SPARK_PROFILES:
        for pipeline in PIPELINE_ORDER:
            planned = load_planned(args.benchmark_id, profile, pipeline, environ)
            state, task = "UNKNOWN", "NOT_AVAILABLE"
            if airflow is not None:
                try:
                    runs = {run.get("run_id"): run for run in airflow.dag_runs(planned.dag_id)}
                    if planned.dag_run_id in runs:
                        state = str(runs[planned.dag_run_id].get("state") or "UNKNOWN").upper()
                        task = _current_task(airflow.task_states(planned.dag_id, planned.dag_run_id))
                    else:
                        state = "NOT_STARTED"
                except CliError as error:
                    details.append(f"{profile} {pipeline}: Airflow unavailable: {error}")
            handoff, lines = ("NOT_CHECKED", []) if args.offline else _handoff_details(planned.params, pipeline, runner)
            rows.append((profile, pipeline.upper(), state, handoff, planned.dag_run_id, task))
            if state != "NOT_STARTED":
                details.append(f"{profile} {pipeline.upper()}  {dag_run_link(planned.dag_id, planned.dag_run_id)}")
            if args.dataproc and not args.offline and handoff != "PUBLISHED" and state != "NOT_STARTED":
                for stage in planned.params.spec.stages:
                    run_id = planned.params.stage_run_id(stage)
                    details.append(f"  {stage}: dataproc batch {run_id} {_batch_state(planned.params, run_id, runner)}")
            details += [f"  {line}" for line in lines]
    print(f"Benchmark {args.benchmark_id}")
    header = ("PROFILE", "DAG", "STATUS", "HANDOFF", "DAG RUN ID", "CURRENT/LAST TASK")
    widths = [max(len(str(row[index])) for row in (header, *rows)) for index in range(len(header))]
    for row in (header, tuple("-" * width for width in widths), *rows):
        print("  ".join(str(value).ljust(width) for value, width in zip(row, widths)).rstrip())
    if details:
        print()
        print("\n".join(details))
    return 0


# --- verify-handoffs ---------------------------------------------------------------

def command_verify_handoffs(args: argparse.Namespace, runner: Runner, environ: Mapping[str, str]) -> int:
    """DP1 -> DP2 and DP2 -> DP3 for one profile; read-only."""

    params = load_planned(args.benchmark_id, args.profile, "dp3", environ).params
    failures = 0
    for pipeline, consumer in (("dp1", "DP2"), ("dp2", "DP3"), ("dp3", "semantic comparison")):
        spec = PIPELINES[pipeline]
        uri = campaign_task.handoff_uri(params, pipeline)
        print(f"{spec.label} -> {consumer}")
        print(f"  manifest        {uri}")
        try:
            exists = campaign_task._object_exists(uri, runner)
        except CampaignError as error:
            print(f"  exists          UNKNOWN ({error})")
            failures += 1
            continue
        print(f"  exists          {'YES' if exists else 'NO'}")
        if not exists:
            print(f"  ready for {consumer:<5} NO ({spec.dag_id} has not published a successful handoff)")
            continue
        try:
            verified = campaign_task.verify_handoff(params, pipeline, runner)
        except CampaignError as error:
            failures += 1
            print(f"  verdict         FAIL: {error}")
            print(f"  ready for {consumer:<5} NO")
            continue
        document = verified.document
        print(f"  sha256          {verified.sha256}")
        for name in ("dataset_version", "source_manifest_sha256", "git_sha", "spark_profile", "benchmark_id"):
            print(f"  {name:<15} {document[name]}")
        print(f"  producer        {document['producer']['dag_id']} / {document['producer']['dag_run_id']}")
        for stage, record in document["stages"].items():
            print(f"  {stage:<15} run {record['run_id']} validation {record['validation']['status']} "
                  f"({record['validation']['report_uri']})")
        if pipeline == "dp3":
            manifest = document.get("campaign_manifest") or {}
            print(f"  campaign        {manifest.get('uri', 'NOT_AVAILABLE')}")
        print(f"  verdict         PASS")
        print(f"  ready for {consumer:<5} YES")
    return 1 if failures else 0


# --- entry point -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edai1-campaign", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="plan one Baseline/Optimized benchmark")
    prepare.add_argument("--dataset-version", default=REHEARSAL_5GB)
    prepare.add_argument("--source-manifest-sha256")
    prepare.add_argument("--terraform-output-json", help="saved `terraform output -json`; default runs terraform output")
    prepare.add_argument("--project-id", default=DEFAULT_PROJECT)
    prepare.add_argument("--region", default=DEFAULT_REGION)
    prepare.add_argument("--benchmark-id", help="default: ab-<UTC now>")
    prepare.add_argument("--skip-source-check", action="store_true",
                         help="do not read GCS now; DP1 still verifies the Source before any paid job")

    run = commands.add_parser("run", help="check, and optionally trigger, one pipeline DagRun")
    run.add_argument("--pipeline", required=True, choices=PIPELINE_ORDER)
    run.add_argument("--benchmark-id", required=True)
    run.add_argument("--profile", required=True, choices=SPARK_PROFILES)
    run.add_argument("--execute", action="store_true")
    run.add_argument("--confirm-paid", action="store_true")
    run.add_argument("--offline", action="store_true", help="dry run without the read-only GCS/Dataproc checks")

    status = commands.add_parser("status", help="read-only status of the six DagRuns")
    status.add_argument("--benchmark-id", required=True)
    status.add_argument("--dataproc", action="store_true", help="also describe unfinished Dataproc batches")
    status.add_argument("--offline", action="store_true", help="Airflow only; do not read GCS")

    verify = commands.add_parser("verify-handoffs", help="read-only DP1 -> DP2 -> DP3 verification")
    verify.add_argument("--benchmark-id", required=True)
    verify.add_argument("--profile", required=True, choices=SPARK_PROFILES)
    return parser


COMMANDS = {"prepare": command_prepare, "run": command_run, "status": command_status,
            "verify-handoffs": command_verify_handoffs}


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run,
         environ: Mapping[str, str] = os.environ) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, runner, environ)
    except (CliError, ValueError, ObjectStoreError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
