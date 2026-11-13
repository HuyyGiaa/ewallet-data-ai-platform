"""Load and verify the local evidence of one EDAI-1 batch campaign profile.

The DataHub lineage plan and the assertion artifact are built only from a
campaign that this module has verified. The input is a directory holding byte
copies of the four write-once campaign documents of one Spark profile::

    <manifest dir>/
        dp1_handoff.json        written by dp1_source_to_bronze
        dp2_handoff.json        written by dp2_bronze_to_curated
        dp3_completion.json     written by dp3_historical_features
        campaign_manifest.json  written by dp3_historical_features

Verification is offline and read-only:

* the checksum chain: ``dp3_completion.json`` pins the campaign manifest, which pins
  both handoffs, so a copy edited after download is refused;
* the identity: dataset version, bucket, benchmark, profile, Git SHA and source
  manifest SHA-256 agree across all four documents and with ``dataset_identity``;
* every stage: a SUCCEEDED transformation batch writing exactly one run root, and,
  when a validation ran, its Dataproc batch and report URI.

Pipeline names, DAG ids, stage order and validation scopes come from
``ops.gcp.campaign_task``, the module that ran the campaign.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from data_platform.config.dataset_identity import resolve_storage_context
from ops.gcp.campaign_task import (
    CAMPAIGN_SCHEMA,
    HANDOFF_SCHEMA,
    PIPELINES,
    STAGE_NAMES,
    STAGE_ZONE,
    VALIDATION_SCOPE,
)


CAMPAIGN_FILES = ("dp1_handoff.json", "dp2_handoff.json", "dp3_completion.json", "campaign_manifest.json")
VALIDATION_STATES = frozenset({"PASS", "FAIL"})
# Zone names used by the schema inventory; DP3 writes into a Gold run root.
INVENTORY_ZONE = {"bronze": "bronze", "silver": "silver", "gold": "gold", "features": "features"}


class CampaignEvidenceError(ValueError):
    """The local campaign evidence is missing, inconsistent or tampered with."""


@dataclass(frozen=True)
class StageEvidence:
    """One transformation stage and its validation, as the campaign recorded it."""

    stage: str
    pipeline: str
    dag_id: str
    dag_run_id: str
    run_id: str
    output_root: str
    input_roots: tuple[str, ...]
    transformation_batch_id: str
    spark_application_id: str
    job_start: str
    job_end: str
    validation_scope: str
    validation_status: str | None
    validation_batch_id: str | None
    validation_report_uri: str | None
    validation_spark_application_id: str | None

    @property
    def zone(self) -> str:
        """Schema-inventory zone of this stage's outputs."""

        return INVENTORY_ZONE[self.stage]

    @property
    def validation_executed(self) -> bool:
        """Whether a validation batch produced a PASS or FAIL report."""

        return self.validation_status in VALIDATION_STATES

    def table_uri(self, table: str) -> str:
        """Return the GCS URI of one output table of this stage."""

        return f"{self.output_root}/{table}"


@dataclass(frozen=True)
class CampaignEvidence:
    """A verified campaign profile: identity, source and four stages."""

    benchmark_id: str
    spark_profile: str
    dataset_version: str
    git_sha: str
    data_bucket: str
    project_id: str
    region: str
    source_manifest_uri: str
    source_manifest_sha256: str
    source_plan_digest: str
    source_physical_bytes: int
    finalized_at: str
    stages: Mapping[str, StageEvidence]
    manifest_sha256: str

    @property
    def campaign_id(self) -> str:
        """``<benchmark>-<profile>``, the campaign identity Dataproc batches carry."""

        return f"{self.benchmark_id}-{self.spark_profile}"

    @property
    def source_root(self) -> str:
        """GCS root of the immutable source dataset."""

        return self.source_manifest_uri.rsplit("/_control/", 1)[0]

    def source_uri(self, physical_table: str) -> str:
        """Return the GCS URI of one physical Source dataset."""

        return f"{self.source_root}/{physical_table}"

    def identity(self) -> dict[str, str]:
        """Provenance fields every published entity carries."""

        return {
            "benchmark_id": self.benchmark_id,
            "spark_profile": self.spark_profile,
            "dataset_version": self.dataset_version,
            "git_sha": self.git_sha,
            "source_manifest_sha256": self.source_manifest_sha256,
            "campaign_manifest_sha256": self.manifest_sha256,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CampaignEvidenceError(f"missing campaign document: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CampaignEvidenceError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignEvidenceError(f"{path} must hold a JSON object")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CampaignEvidenceError(message)


def _text(value: Mapping[str, Any], key: str, where: str) -> str:
    item = value.get(key)
    _require(isinstance(item, str) and bool(item), f"{where}: {key} is missing")
    return item


def _stage(
    name: str,
    entry: Mapping[str, Any],
    producers: Mapping[str, tuple[str, str]],
    campaign_id: str,
    bucket: str,
) -> StageEvidence:
    where = f"stage {name}"
    pipeline = next(item for item in PIPELINES.values() if name in item.stages)
    dag_id, dag_run_id = producers[pipeline.name]
    _require(dag_id == pipeline.dag_id, f"{where}: producer DAG {dag_id!r} is not {pipeline.dag_id!r}")
    run_id = _text(entry, "run_id", where)
    output_root = _text(entry, "output_root", where).rstrip("/")
    _require(output_root.startswith(f"gs://{bucket}/lakehouse/runs/{run_id}/"),
             f"{where}: output root {output_root} is not under run {run_id} in {bucket}")
    execution = entry.get("execution_metadata")
    _require(isinstance(execution, Mapping), f"{where}: execution_metadata is missing")
    _require(execution.get("dataproc_batch_state") == "SUCCEEDED",
             f"{where}: transformation batch is {execution.get('dataproc_batch_state')!r}, not SUCCEEDED")
    _require(execution.get("campaign_id") == campaign_id, f"{where}: campaign id mismatch")
    _require(list(execution.get("output_roots") or []) == [output_root],
             f"{where}: execution output roots do not equal the stage output root")
    validation = entry.get("validation") or {}
    status = validation.get("status")
    _require(status in VALIDATION_STATES or status is None, f"{where}: unknown validation status {status!r}")
    metadata = validation.get("execution_metadata") or {}
    report_uri = validation.get("report_uri")
    if status is not None:
        _require(metadata.get("dataproc_batch_state") == "SUCCEEDED",
                 f"{where}: validation batch did not succeed")
        _require(metadata.get("validation_status") == status, f"{where}: validation status disagrees")
        _require(isinstance(report_uri, str) and report_uri.startswith(f"gs://{bucket}/"),
                 f"{where}: validation report URI is missing")
    return StageEvidence(
        stage=name,
        pipeline=pipeline.name,
        dag_id=dag_id,
        dag_run_id=dag_run_id,
        run_id=run_id,
        output_root=output_root,
        input_roots=tuple(str(item).rstrip("/") for item in execution.get("input_roots") or ()),
        transformation_batch_id=_text(execution, "dataproc_batch_id", where),
        spark_application_id=_text(execution, "spark_application_id", where),
        job_start=_text(execution, "job_start_timestamp", where),
        job_end=_text(execution, "job_end_timestamp", where),
        validation_scope=VALIDATION_SCOPE[name],
        validation_status=status,
        validation_batch_id=metadata.get("dataproc_batch_id") if status else None,
        validation_report_uri=report_uri if status else None,
        validation_spark_application_id=metadata.get("spark_application_id") if status else None,
    )


def load_campaign(
    directory: Path,
    *,
    benchmark_id: str,
    spark_profile: str,
    dataset_version: str,
    source_manifest_sha256: str | None = None,
) -> CampaignEvidence:
    """Verify one profile's campaign documents and return their evidence.

    Args:
        directory: Folder with the four campaign documents of one profile.
        benchmark_id: Benchmark the caller expects, e.g. ``ab-20261110225456``.
        spark_profile: ``baseline`` or ``optimized``.
        dataset_version: Approved dataset version the caller expects.
        source_manifest_sha256: Optional pin of the source manifest SHA-256.

    Raises:
        CampaignEvidenceError: A document is missing, a checksum or identity
            disagrees, or a stage did not complete.
    """

    documents = {name: _read(directory / name) for name in CAMPAIGN_FILES}
    manifest = documents["campaign_manifest.json"]
    completion = documents["dp3_completion.json"]
    manifest_sha = _sha256(directory / "campaign_manifest.json")

    _require(manifest.get("schema_version") == CAMPAIGN_SCHEMA,
             f"campaign manifest schema {manifest.get('schema_version')!r} is not {CAMPAIGN_SCHEMA}")
    _require(manifest.get("status") == "COMPLETE", f"campaign status is {manifest.get('status')!r}, not COMPLETE")
    _require((completion.get("campaign_manifest") or {}).get("sha256") == manifest_sha,
             "dp3_completion.json does not pin this campaign_manifest.json (checksum mismatch)")
    for pipeline in ("dp1", "dp2"):
        pinned = (manifest.get("handoffs") or {}).get(pipeline) or {}
        _require(pinned.get("sha256") == _sha256(directory / f"{pipeline}_handoff.json"),
                 f"campaign manifest does not pin {pipeline}_handoff.json (checksum mismatch)")

    expected = {
        "benchmark_id": benchmark_id,
        "spark_profile": spark_profile,
        "dataset_version": dataset_version,
    }
    for name, document in documents.items():
        for key, value in expected.items():
            _require(document.get(key) == value, f"{name}: {key} is {document.get(key)!r}, expected {value!r}")
        if name != "campaign_manifest.json":
            _require(document.get("schema_version") == HANDOFF_SCHEMA, f"{name}: unknown handoff schema")
            _require(document.get("status") == "SUCCESS", f"{name}: status is not SUCCESS")
    git_shas = {document.get("git_sha") for document in documents.values()}
    _require(len(git_shas) == 1 and None not in git_shas, f"Git SHA differs across documents: {git_shas}")
    source = manifest.get("source") or {}
    source_sha = _text(source, "manifest_sha256", "campaign source")
    shas = {document.get("source_manifest_sha256") for name, document in documents.items()
            if name != "campaign_manifest.json"}
    _require(shas == {source_sha}, "source manifest SHA-256 differs across documents")
    if source_manifest_sha256 is not None:
        _require(source_sha == source_manifest_sha256,
                 f"source manifest SHA-256 {source_sha} is not the pinned {source_manifest_sha256}")

    storage = resolve_storage_context(dataset_version, (manifest.get("storage") or {}).get("data_bucket"))
    _require(_text(source, "manifest_uri", "campaign source") == f"{storage.source_root}/_control/dataset_manifest.json",
             "source manifest URI is not the dataset's source root")

    producers: dict[str, tuple[str, str]] = {}
    for pipeline, filename in (("dp1", "dp1_handoff.json"), ("dp2", "dp2_handoff.json"), ("dp3", "dp3_completion.json")):
        producer = documents[filename].get("producer") or {}
        producers[pipeline] = (_text(producer, "dag_id", filename), _text(producer, "dag_run_id", filename))
        _require(documents[filename].get("pipeline") == pipeline, f"{filename}: pipeline is not {pipeline}")

    raw_stages = manifest.get("stages") or {}
    _require(tuple(sorted(raw_stages)) == tuple(sorted(STAGE_NAMES)),
             f"campaign stages {sorted(raw_stages)} are not {sorted(STAGE_NAMES)}")
    campaign_id = f"{benchmark_id}-{spark_profile}"
    stages = {
        name: _stage(name, raw_stages[name], producers, campaign_id, storage.data_bucket)
        for name in STAGE_NAMES
    }
    for name, stage in stages.items():
        _require(STAGE_ZONE[name] in stage.output_root.rsplit("/", 1)[-1],
                 f"stage {name} output root does not end in its zone")

    return CampaignEvidence(
        benchmark_id=benchmark_id,
        spark_profile=spark_profile,
        dataset_version=dataset_version,
        git_sha=git_shas.pop(),
        data_bucket=storage.data_bucket,
        project_id=_text(documents["dp1_handoff.json"], "project_id", "dp1_handoff.json"),
        region=_text(documents["dp1_handoff.json"], "region", "dp1_handoff.json"),
        source_manifest_uri=source["manifest_uri"],
        source_manifest_sha256=source_sha,
        source_plan_digest=_text(source, "plan_digest", "campaign source"),
        source_physical_bytes=int(source.get("physical_bytes") or 0),
        finalized_at=_text(manifest, "finalized_at", "campaign manifest"),
        stages=stages,
        manifest_sha256=manifest_sha,
    )
