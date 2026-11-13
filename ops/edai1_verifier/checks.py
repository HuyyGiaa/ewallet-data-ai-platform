"""Evidence checks. Each returns ``Check`` records; none raises on a failed check.

A missing or unreadable input file is itself a failed check, so the report always
says what was verified and what was not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .spec import PROFILES, STAGES, WorkloadSpec

CAMPAIGN_FILES = ("dp1_handoff.json", "dp2_handoff.json", "dp3_completion.json", "campaign_manifest.json")
CAMPAIGN_SCHEMA = "edai1-pipeline-campaign-v2"
REPORT_SCHEMA = "campaign-validation-report-v1"
COMPARISON_SCHEMA = "campaign-comparison-v2"
VALIDATION_SCOPE = {"bronze": "bronze", "silver": "silver", "gold": "core-gold", "features": "features"}


@dataclass(frozen=True)
class Check:
    """One verified statement and the observed value behind it."""

    name: str
    passed: bool
    observed: Any = None
    expected: Any = None


@dataclass
class Evidence:
    """Loaded JSON documents and the SHA-256 of every file read."""

    root: Path
    documents: dict[str, Any] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    errors: list[Check] = field(default_factory=list)

    def load(self, relative: str) -> Any:
        """Return one JSON document, recording its digest, or None after a failed check."""

        if relative in self.documents:
            return self.documents[relative]
        path = self.root / relative
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(Check(f"readable:{relative}", False, type(exc).__name__))
            self.documents[relative] = None
            return None
        self.digests[relative] = hashlib.sha256(raw).hexdigest()
        self.documents[relative] = value
        return value


def _manifest_path(profile: str, name: str) -> str:
    return f"manifests/{profile}/{name}"


def campaign_checks(evidence: Evidence, spec: WorkloadSpec) -> list[Check]:
    """Checksum chain, identity and stage status of both profiles."""

    checks: list[Check] = []
    for profile in PROFILES:
        docs = {name: evidence.load(_manifest_path(profile, name)) for name in CAMPAIGN_FILES}
        if any(value is None for value in docs.values()):
            continue
        manifest = docs["campaign_manifest.json"]
        manifest_sha = evidence.digests[_manifest_path(profile, "campaign_manifest.json")]
        pinned = (docs["dp3_completion.json"].get("campaign_manifest") or {}).get("sha256")
        checks.append(Check(f"{profile}:dp3_completion_pins_campaign_manifest", pinned == manifest_sha,
                            pinned, manifest_sha))
        for pipeline in ("dp1", "dp2"):
            expected = (manifest.get("handoffs") or {}).get(pipeline, {}).get("sha256")
            actual = evidence.digests[_manifest_path(profile, f"{pipeline}_handoff.json")]
            checks.append(Check(f"{profile}:manifest_pins_{pipeline}_handoff", expected == actual, actual, expected))
        identity = {
            "benchmark_id": spec.benchmark_id,
            "dataset_version": spec.dataset_version,
            "spark_profile": profile,
            "git_sha": spec.source_git_sha,
        }
        for name, document in docs.items():
            observed = {key: document.get(key) for key in identity}
            checks.append(Check(f"{profile}:{name}:identity", observed == identity, observed, identity))
        source_sha = (manifest.get("source") or {}).get("manifest_sha256")
        checks.append(Check(f"{profile}:source_manifest_sha256", source_sha == spec.source_manifest_sha256,
                            source_sha, spec.source_manifest_sha256))
        checks.append(Check(f"{profile}:campaign_status",
                            manifest.get("schema_version") == CAMPAIGN_SCHEMA and manifest.get("status") == "COMPLETE",
                            [manifest.get("schema_version"), manifest.get("status")], [CAMPAIGN_SCHEMA, "COMPLETE"]))
        stages = manifest.get("stages") or {}
        for stage in STAGES:
            entry = stages.get(stage) or {}
            batch = (entry.get("execution_metadata") or {}).get("dataproc_batch_state")
            validation = (entry.get("validation") or {}).get("status")
            checks.append(Check(f"{profile}:{stage}:transformation_and_validation",
                                batch == "SUCCEEDED" and validation == "PASS",
                                [batch, validation], ["SUCCEEDED", "PASS"]))
    return checks


def validation_checks(evidence: Evidence, spec: WorkloadSpec) -> tuple[list[Check], dict[str, dict[str, int]]]:
    """Validation reports: bound to their stage run, PASS, rule counts consistent."""

    checks: list[Check] = []
    counts: dict[str, dict[str, int]] = {}
    for profile in PROFILES:
        manifest = evidence.load(_manifest_path(profile, "campaign_manifest.json")) or {}
        counts[profile] = {}
        for stage in STAGES:
            report = evidence.load(f"validation_reports/{profile}-{stage}.json")
            if report is None:
                continue
            run_id = ((manifest.get("stages") or {}).get(stage) or {}).get("run_id")
            results = report.get("results") or []
            passed = sum(1 for item in results if item.get("passed") is True)
            counts[profile][stage] = len(results)
            consistent = (
                report.get("schema_version") == REPORT_SCHEMA
                and report.get("dataset_version") == spec.dataset_version
                and report.get("run_id") == run_id
                and report.get("scope") == VALIDATION_SCOPE[stage]
                and report.get("status") == "PASS"
                and report.get("passed_rules") == passed == len(results)
                and not report.get("failed_rules")
            )
            checks.append(Check(f"{profile}:{stage}:validation_report", consistent,
                                {"run_id": report.get("run_id"), "status": report.get("status"), "rules": len(results)},
                                {"run_id": run_id, "status": "PASS"}))
        total = sum(counts[profile].values())
        checks.append(Check(f"{profile}:rules_total", total == spec.rules_per_profile, total, spec.rules_per_profile))
    return checks, counts


def comparison_checks(evidence: Evidence, spec: WorkloadSpec) -> tuple[list[Check], dict[str, int]]:
    """Semantic comparison: verdict, table inventory per stage, bound to both campaigns."""

    comparison = evidence.load("comparison.json")
    if comparison is None:
        return [], {}
    per_stage = {stage["stage"]: len(stage.get("tables") or []) for stage in comparison.get("stages") or []}
    checks = [
        Check("comparison:verdict",
              comparison.get("schema_version") == COMPARISON_SCHEMA
              and comparison.get("verdict") == "SEMANTIC_EQUIVALENT" and not comparison.get("problems"),
              comparison.get("verdict"), "SEMANTIC_EQUIVALENT"),
        Check("comparison:identity",
              comparison.get("benchmark_id") == spec.benchmark_id
              and comparison.get("dataset_version") == spec.dataset_version
              and comparison.get("source_manifest_sha256") == spec.source_manifest_sha256,
              [comparison.get("benchmark_id"), comparison.get("dataset_version")],
              [spec.benchmark_id, spec.dataset_version]),
        Check("comparison:tables_per_stage", per_stage == dict(spec.tables_per_stage),
              dict(sorted(per_stage.items())), dict(sorted(spec.tables_per_stage.items()))),
        Check("comparison:all_tables_passed",
              all(table.get("passed") is True for stage in comparison.get("stages") or []
                  for table in stage.get("tables") or []),
              sum(len(stage.get("tables") or []) for stage in comparison.get("stages") or []),
              spec.compared_tables),
    ]
    for profile, key in (("baseline", "baseline_run_ids"), ("optimized", "optimized_run_ids")):
        manifest = evidence.load(_manifest_path(profile, "campaign_manifest.json")) or {}
        expected = sorted(stage.get("run_id") for stage in (manifest.get("stages") or {}).values())
        observed = sorted((comparison.get(key) or {}).values())
        checks.append(Check(f"comparison:{profile}_runs_match_campaign", observed == expected, observed, expected))
    return checks, per_stage


def airflow_checks(evidence: Evidence, spec: WorkloadSpec) -> list[Check]:
    """Airflow record: every DagRun and task instance of the benchmark succeeded."""

    record = evidence.load("airflow_dagruns.json")
    if record is None:
        return []
    runs = record.get("dag_runs") or []
    tasks = record.get("task_instances") or []
    return [
        Check("airflow:benchmark", record.get("benchmark_id") == spec.benchmark_id,
              record.get("benchmark_id"), spec.benchmark_id),
        Check("airflow:dag_runs_success", len(runs) == spec.dag_runs and all(r.get("state") == "success" for r in runs),
              len(runs), spec.dag_runs),
        Check("airflow:task_instances_success",
              len(tasks) == spec.task_instances and all(t.get("state") == "success" for t in tasks),
              len(tasks), spec.task_instances),
    ]


def summarize_rules(counts: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Rule counts per profile and stage, sorted for a stable report."""

    return {profile: dict(sorted(values.items())) for profile, values in sorted(counts.items())}
