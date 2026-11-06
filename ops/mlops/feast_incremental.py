"""Run one stage of the incremental Feast Online materialization workflow.

The Airflow DAG calls this module once per task. Each stage writes one JSON
artifact into the run directory, and a retried stage reuses a finished artifact,
so a window decision cannot change halfway through a run. Stages in order:

``config`` validates the request; ``source`` checks the immutable snapshot
identity; ``window`` asks the operator for registry state and decides the window;
``materialize`` runs the existing operator Job; ``completion`` confirms that the
registry advanced; ``readback`` runs online parity; ``provenance`` records the run.

Inputs arrive only through ``C1_*`` environment variables, so Airflow params are
never rendered into shell source. Kubernetes work goes through
``scripts/mlops/feast_online_job.sh``. A run without ``C1_MODE=execute`` and the
matching approval token renders the Job manifests and changes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from ai_platform.features.feast.cloud_boundary import FEATURE_TABLES
from ai_platform.features.feast.materialization_plan import (
    PROVENANCE_SCHEMA,
    ExistingJob,
    MaterializationRequest,
    ViewState,
    WindowError,
    WindowState,
    decide_window,
    existing_job_from_kubernetes,
    job_name,
    parse_utc,
    provenance_digest,
    resolve_materialize_job,
    verify_completion,
    verify_readback,
)
from ai_platform.features.feast.schema import FEATURE_SPECS


LOGGER = logging.getLogger("ewallet.feast_incremental")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
JOB_SCRIPT = REPOSITORY_ROOT / "scripts/mlops/feast_online_job.sh"
STAGES = ("config", "source", "window", "materialize", "completion", "readback", "provenance")
ARTIFACTS = {stage: f"{index:02d}-{stage}.json" for index, stage in enumerate(STAGES, 1)}
PASS, SKIPPED, DRY_RUN = "PASS", "SKIPPED", "DRY_RUN"


class StageError(RuntimeError):
    """A stage could not prove its result; the task must fail."""


@dataclass(frozen=True)
class RunContext:
    """Everything one stage needs, read once from the environment."""

    request: MaterializationRequest
    run_dir: Path
    run_id: str
    execute: bool
    approval: str
    attempt: int
    expected_manifest_sha256: str
    sample_size: int
    environment: Mapping[str, str]

    @property
    def expected_approval(self) -> str:
        """Token an owner copies from a dry run to authorize this exact window."""

        return f"approve-{self.request.window_id}"

    @property
    def fingerprint(self) -> str:
        """Digest that ties artifacts to one request."""

        encoded = json.dumps(self.request.as_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class FeastCluster(Protocol):
    """Boundary to Kubernetes; tests replace it with a fake."""

    def list_window_jobs(self, window_id: str) -> Sequence[ExistingJob]:
        """Return Jobs labelled with this window."""

    def run_operator(self, name: str, window_id: str, arguments: Sequence[str]) -> dict[str, object]:
        """Run or join one operator Job and return its evidence document."""

    def render_operator(self, name: str, window_id: str, arguments: Sequence[str]) -> str:
        """Return the Job manifest without applying it."""


class SnapshotReader(Protocol):
    """Boundary to the immutable snapshot's control file."""

    def read(self, uri: str) -> bytes:
        """Return the bytes of one object."""


class GsutilReader:
    """Read one object with the ambient gcloud credentials."""

    def read(self, uri: str) -> bytes:
        """Return the object's bytes, or raise :class:`StageError`."""

        result = subprocess.run(["gsutil", "cat", uri], capture_output=True, check=False)
        if result.returncode != 0:
            raise StageError(f"cannot read {uri}: {result.stderr.decode(errors='replace')[:400]}")
        return result.stdout


class KubectlFeastCluster:
    """Run the existing operator through ``feast_online_job.sh``."""

    def __init__(self, context: RunContext) -> None:
        self.context = context
        self.kube_context = context.environment.get("KUBE_CONTEXT", "")

    def _environment(self, window_id: str) -> dict[str, str]:
        request = self.context.request
        return {
            **os.environ,
            **self.context.environment,
            "ONLINE_REGISTRY_URI": request.registry_uri,
            "FEAST_OFFLINE_DATA_ROOT": request.snapshot_root,
            "FEAST_WINDOW_ID": window_id,
        }

    def list_window_jobs(self, window_id: str) -> Sequence[ExistingJob]:
        """Return Jobs labelled with this window."""

        command = ["kubectl"]
        if self.kube_context:
            command += ["--context", self.kube_context]
        command += [
            "-n", "ml-platform", "get", "jobs",
            "-l", f"ewallet.feast/window-id={window_id}", "-o", "json",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise StageError(f"cannot list Jobs: {result.stderr[:400]}")
        return [
            existing_job_from_kubernetes(item)
            for item in json.loads(result.stdout).get("items", [])
            if str(item["metadata"]["name"]).startswith("feast-inc-")
        ]

    def run_operator(self, name: str, window_id: str, arguments: Sequence[str]) -> dict[str, object]:
        """Run or join one operator Job and return its evidence document."""

        result = subprocess.run(
            [str(JOB_SCRIPT), name, "--execute", "--", *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=self._environment(window_id),
        )
        if result.returncode != 0:
            raise StageError(f"operator Job {name} failed: {result.stderr[-1200:]}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise StageError(f"operator Job {name} printed no evidence")
        return parse_operator_evidence(lines[-1], name)

    def render_operator(self, name: str, window_id: str, arguments: Sequence[str]) -> str:
        """Return the Job manifest without applying it."""

        result = subprocess.run(
            [str(JOB_SCRIPT), name, "--", *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=self._environment(window_id),
        )
        if result.returncode != 0:
            raise StageError(f"cannot render Job {name}: {result.stderr[-600:]}")
        return result.stdout


def parse_operator_evidence(line: str, name: str) -> dict[str, object]:
    """Parse the evidence line a Job printed, failing on anything else."""

    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise StageError(f"operator Job {name} did not print JSON evidence") from error
    if not isinstance(value, dict) or value.get("schema_version") != "feast-online-operation-evidence-v1":
        raise StageError(f"operator Job {name} printed an unknown evidence document")
    return value


def context_from_environment(environment: Mapping[str, str]) -> RunContext:
    """Build a run context from ``C1_*`` variables.

    Raises:
        WindowError: A value breaks the request contract.
        StageError: A required variable is missing or invalid.
    """

    def required(name: str) -> str:
        value = environment.get(name, "").strip()
        if not value:
            raise StageError(f"{name} is required")
        return value

    cutover = environment.get("C1_STREAMING_CUTOVER", "").strip()
    request = MaterializationRequest(
        bucket=required("C1_DATA_BUCKET"),
        dataset_version=required("C1_DATASET_VERSION"),
        feature_run_id=required("C1_FEATURE_RUN_ID"),
        feast_snapshot_id=required("C1_FEAST_SNAPSHOT_ID"),
        online_registry_id=required("C1_ONLINE_REGISTRY_ID"),
        target_end=parse_utc(required("C1_TARGET_END"), "target_end"),
        streaming_cutover=parse_utc(cutover, "streaming_cutover") if cutover else None,
        max_window=timedelta(hours=float(environment.get("C1_MAX_WINDOW_HOURS", "6"))),
    )
    mode = environment.get("C1_MODE", "dry_run")
    if mode not in {"dry_run", "execute"}:
        raise StageError("C1_MODE must be dry_run or execute")
    run_id = required("C1_RUN_ID")
    root = Path(required("C1_EVIDENCE_ROOT")).expanduser()
    safe_run_id = "".join(char if char.isalnum() or char in "-_." else "_" for char in run_id)
    return RunContext(
        request=request,
        run_dir=root / safe_run_id,
        run_id=run_id,
        execute=mode == "execute",
        approval=environment.get("C1_APPROVAL", ""),
        attempt=max(1, int(environment.get("C1_ATTEMPT", "1"))),
        expected_manifest_sha256=environment.get("C1_EXPECTED_MANIFEST_SHA256", "").strip(),
        sample_size=int(environment.get("C1_SAMPLE_SIZE", "5")),
        environment={
            key: environment[key]
            for key in ("FEAST_ONLINE_IMAGE", "FEAST_REDIS_SECRET", "KUBE_CONTEXT", "FEAST_ONLINE_GIT_SHA")
            if environment.get(key)
        },
    )


def _load(context: RunContext, stage: str) -> dict[str, object]:
    path = context.run_dir / ARTIFACTS[stage]
    if not path.is_file():
        raise StageError(f"stage {stage} has no artifact; an upstream task did not finish")
    return json.loads(path.read_text(encoding="utf-8"))


def _finished(context: RunContext, stage: str) -> dict[str, object] | None:
    """Return a stage's artifact when it exists, after checking it fits this run."""

    path = context.run_dir / ARTIFACTS[stage]
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("run_fingerprint") != context.fingerprint:
        raise StageError(f"{path} belongs to a different request; use a new run id")
    return value


def _write(context: RunContext, stage: str, value: Mapping[str, object]) -> dict[str, object]:
    record = {
        "stage": stage,
        "run_id": context.run_id,
        "window_id": context.request.window_id,
        "run_fingerprint": context.fingerprint,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **value,
    }
    context.run_dir.mkdir(parents=True, exist_ok=True)
    path = context.run_dir / ARTIFACTS[stage]
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
    LOGGER.info(json.dumps({"event": "stage_finished", "stage": stage, "status": record.get("status"),
                            "run_id": context.run_id, "window_id": context.request.window_id}))
    return record


def _require_execution(context: RunContext) -> None:
    if context.approval != context.expected_approval:
        raise StageError(
            f"execute mode needs C1_APPROVAL={context.expected_approval}, "
            "copied from a dry run of this exact window"
        )


def stage_config(context: RunContext, _cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Validate the request and, in execute mode, its approval token."""

    if context.execute:
        _require_execution(context)
    return _write(
        context,
        "config",
        {
            "status": PASS,
            "mode": "execute" if context.execute else "dry_run",
            "request": context.request.as_dict(),
            "approval_token_for_this_window": context.expected_approval,
        },
    )


def validate_export_manifest(
    request: MaterializationRequest, raw: bytes, expected_sha256: str
) -> dict[str, object]:
    """Check that a snapshot's control file describes exactly this snapshot.

    Args:
        request: Requested window identity.
        raw: Bytes of ``_control/export_manifest.json``.
        expected_sha256: Pinned manifest digest, or empty when none is pinned.

    Returns:
        The manifest digest and per-table row counts.

    Raises:
        StageError: Identity, table set, row counts or digest do not match.
    """

    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise StageError(f"snapshot manifest digest {digest} is not the pinned digest")
    manifest = json.loads(raw)
    if manifest.get("dataset_version") != request.dataset_version:
        raise StageError("snapshot manifest belongs to another dataset version")
    tables = manifest.get("tables")
    if not isinstance(tables, dict) or sorted(tables) != sorted(FEATURE_SPECS):
        raise StageError("snapshot manifest does not list the four FeatureViews")
    rows = {}
    for view, spec in FEATURE_SPECS.items():
        table = tables[view]
        if table.get("target") != f"{request.snapshot_root}/{spec['table']}":
            raise StageError(f"{view}: manifest target is outside this snapshot")
        if table.get("entity_key") != spec["entity_key"]:
            raise StageError(f"{view}: manifest entity key drifted")
        if not str(table.get("event_timestamp_type", "")).endswith("tz=UTC]"):
            raise StageError(f"{view}: event_timestamp is not stored as UTC")
        if int(table.get("output_rows", -1)) != int(table.get("source_rows", -2)) or int(table["output_rows"]) <= 0:
            raise StageError(f"{view}: exported rows differ from source rows")
        rows[view] = int(table["output_rows"])
    if sorted(spec["table"] for spec in FEATURE_SPECS.values()) != sorted(FEATURE_TABLES):
        raise StageError("Feast and cloud boundary table lists disagree")
    return {"manifest_sha256": digest, "rows": rows}


def stage_source(context: RunContext, _cluster: FeastCluster, reader: SnapshotReader) -> dict[str, object]:
    """Validate the immutable snapshot identity. Read-only in both modes."""

    uri = f"{context.request.snapshot_root}/_control/export_manifest.json"
    summary = validate_export_manifest(
        context.request, reader.read(uri), context.expected_manifest_sha256
    )
    return _write(context, "source", {"status": PASS, "manifest_uri": uri, **summary})


def _state_job(context: RunContext, cluster: FeastCluster, phase: str) -> dict[str, object]:
    window_id = context.request.window_id
    return cluster.run_operator(
        job_name(f"state-{phase}", window_id, context.attempt), window_id, ["state"]
    )


def _views(evidence: Mapping[str, object]) -> list[ViewState]:
    return [ViewState.from_evidence(item) for item in evidence["views"]]


def stage_window(context: RunContext, cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Read registry state through a read-only Job and decide the window."""

    if not context.execute:
        window_id = context.request.window_id
        manifest = cluster.render_operator(job_name("state-pre", window_id, 1), window_id, ["state"])
        return _write(context, "window", {"status": DRY_RUN, "state_job_manifest": manifest})
    _require_execution(context)
    evidence = _state_job(context, cluster, "pre")
    decision = decide_window(context.request, _views(evidence))
    return _write(
        context,
        "window",
        {
            "status": PASS,
            "decision": decision.as_dict(),
            "state_evidence": evidence,
            "registry_generation": evidence.get("registry_generation_after"),
        },
    )


def stage_materialize(context: RunContext, cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Run, join or reuse the materialization Job for a pending window."""

    window_id = context.request.window_id
    arguments = ["materialize-incremental", "--end", context.request.target_end.isoformat()]
    if not context.execute:
        manifest = cluster.render_operator(job_name("inc", window_id, 1), window_id, arguments)
        return _write(context, "materialize", {"status": DRY_RUN, "job_manifest": manifest})
    _require_execution(context)
    decision = _load(context, "window")["decision"]
    if decision["state"] in {WindowState.COMPLETED.value, WindowState.NO_NEW_DATA.value}:
        return _write(context, "materialize", {"status": SKIPPED, "reason": decision["reason"]})
    action, name = resolve_materialize_job(window_id, cluster.list_window_jobs(window_id))
    evidence = cluster.run_operator(name, window_id, arguments)
    return _write(
        context,
        "materialize",
        {"status": PASS, "job_action": action.value, "job_name": name, "evidence": evidence},
    )


def stage_completion(context: RunContext, cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Confirm through a fresh registry read that the window was recorded."""

    if not context.execute:
        return _write(context, "completion", {"status": DRY_RUN})
    decision = _load(context, "window")["decision"]
    if decision["state"] == WindowState.NO_NEW_DATA.value:
        return _write(context, "completion", {"status": SKIPPED, "reason": decision["reason"]})
    evidence = _state_job(context, cluster, "post")
    try:
        verify_completion(context.request, _views(evidence))
    except WindowError as error:
        raise StageError(str(error)) from error
    return _write(context, "completion", {"status": PASS, "state_evidence": evidence})


def stage_readback(context: RunContext, cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Compare Redis with offline point-in-time values for the loaded window."""

    if not context.execute:
        return _write(context, "readback", {"status": DRY_RUN})
    decision = _load(context, "window")["decision"]
    if decision["state"] == WindowState.NO_NEW_DATA.value:
        return _write(context, "readback", {"status": SKIPPED, "reason": decision["reason"]})
    end = context.request.target_end
    if decision["start"]:
        start = parse_utc(str(decision["start"]))
    else:
        start = end - context.request.max_window
    window_id = context.request.window_id
    evidence = cluster.run_operator(
        job_name("parity", window_id, context.attempt),
        window_id,
        ["parity", "--cutover", end.isoformat(), "--sample-window-start", start.isoformat(),
         "--sample-size", str(context.sample_size)],
    )
    summary = verify_readback(evidence, start=start, end=end)
    return _write(context, "readback", {"status": PASS, "summary": summary, "evidence": evidence})


def stage_provenance(context: RunContext, _cluster: FeastCluster, _reader: SnapshotReader) -> dict[str, object]:
    """Record the run identity and a digest of every stage artifact."""

    artifacts = {}
    for stage in STAGES[:-1]:
        path = context.run_dir / ARTIFACTS[stage]
        if not path.is_file():
            raise StageError(f"cannot record provenance without stage {stage}")
        artifacts[ARTIFACTS[stage]] = hashlib.sha256(path.read_bytes()).hexdigest()
    outcome = "DRY_RUN"
    if context.execute:
        outcome = str(_load(context, "window")["decision"]["state"])
    record: dict[str, object] = {
        "schema_version": PROVENANCE_SCHEMA,
        "status": PASS,
        "outcome": outcome,
        "request": context.request.as_dict(),
        "manifest_sha256": _load(context, "source")["manifest_sha256"],
        "operator_image": context.environment.get("FEAST_ONLINE_IMAGE"),
        "git_sha": context.environment.get("FEAST_ONLINE_GIT_SHA"),
        "airflow": {
            "dag_id": os.environ.get("C1_AIRFLOW_DAG_ID"),
            "run_id": context.run_id,
        },
        "artifacts": artifacts,
    }
    record["provenance_digest"] = provenance_digest(record)
    return _write(context, "provenance", record)


_RUNNERS = {
    "config": stage_config,
    "source": stage_source,
    "window": stage_window,
    "materialize": stage_materialize,
    "completion": stage_completion,
    "readback": stage_readback,
    "provenance": stage_provenance,
}


def run_stage(
    stage: str, context: RunContext, cluster: FeastCluster, reader: SnapshotReader
) -> dict[str, object]:
    """Run one stage, or return its artifact when an earlier attempt finished it.

    Args:
        stage: One of :data:`STAGES`.
        context: Validated run context.
        cluster: Kubernetes boundary.
        reader: Snapshot control-file boundary.

    Returns:
        The stage artifact.
    """

    finished = _finished(context, stage)
    if finished is not None:
        LOGGER.info(json.dumps({"event": "stage_reused", "stage": stage, "run_id": context.run_id}))
        return finished
    return _RUNNERS[stage](context, cluster, reader)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one stage from the command line and return a process exit code."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    context = context_from_environment(os.environ)
    result = run_stage(args.stage, context, KubectlFeastCluster(context), GsutilReader())
    print(json.dumps({key: result.get(key) for key in ("stage", "status", "window_id")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
