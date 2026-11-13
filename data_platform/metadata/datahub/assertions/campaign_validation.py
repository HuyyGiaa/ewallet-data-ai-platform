"""Map the executed rules of a verified campaign to DataHub validation outcomes.

Input: one verified campaign profile (``campaign_evidence.load_campaign``) and the
byte copies of its four ``campaign-validation-report-v1`` reports, one per stage.
Output: a ``datahub-quality-validation-v1`` artifact that the existing
``result_bound_publisher`` dry-runs or publishes.

Every outcome is one executed rule on the dataset that rule actually checks:

* ``<table>.<check>`` names the table directly. The Gold validator also re-checks
  Silver ``merchant_history`` (the SCD2 input), so in the ``core-gold`` scope a
  Silver table name resolves to the Silver run, not the Gold run.
* ``silver.<x>_count_equals_<table>_count`` and ``<a>_count_equals_<table>_count``
  compare a population with the Gold ``<table>`` built from it; the outcome is on
  ``<table>``.
* ``silver.<x>_population_equals_<feature table>`` is on that feature table.
* ``dp3_features.<check>`` is one rule over the four DP3 tables together; it
  becomes one outcome per table, each saying it is a cross-table rule.

An unknown rule name raises: a new validator rule needs an explicit mapping here,
never a guess. A rule whose report carries no boolean ``passed`` becomes
``UNKNOWN``, and a stage whose validation never ran becomes ``NOT_EXECUTED``; the
publisher writes neither as a run result.

The observed values and expectations are copied from the report unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from data_platform.contracts.schema_inventory import ZoneInventory, zone_inventory
from data_platform.metadata.datahub.assertions.validation_artifact import (
    ValidationArtifact,
    ValidationOutcome,
)
from data_platform.metadata.datahub.campaign_evidence import (
    CampaignEvidence,
    CampaignEvidenceError,
    StageEvidence,
    load_campaign,
)
from data_platform.metadata.datahub.urns import dataset_urn


REPORT_SCHEMA = "campaign-validation-report-v1"
CONTRACT_DIRECTORY = Path(__file__).resolve().parents[3] / "contracts"
# Zones whose table names a rule prefix may resolve to, per validation scope.
SCOPE_ZONES: Mapping[str, tuple[str, ...]] = {
    "bronze": ("bronze",),
    "silver": ("silver",),
    "core-gold": ("gold", "silver"),
    "features": ("features",),
}
# Run roots a report must name, per scope: report field -> stage owning the root.
SCOPE_ROOTS: Mapping[str, Mapping[str, str]] = {
    "bronze": {"bronze_root": "bronze"},
    "silver": {"bronze_root": "bronze", "silver_root": "silver"},
    "core-gold": {"silver_root": "silver", "gold_root": "gold", "feature_gold_root": "gold"},
    "features": {"silver_root": "silver", "gold_root": "gold", "feature_gold_root": "features"},
}
_GOLD_POPULATION = re.compile(r"^(?:silver\.)?\w+?_count_equals_(?P<table>\w+)_count$")
_FEATURE_POPULATION = re.compile(r"^silver\.\w+_population_equals_(?P<table>feat_\w+)$")
_CROSS_FEATURE_PREFIX = "dp3_features."


class RuleMappingError(ValueError):
    """A validator rule cannot be mapped to the dataset it checks."""


@dataclass(frozen=True)
class RuleTarget:
    """The dataset one executed rule is published on."""

    zone: str
    table: str
    cross_table: bool = False


def report_path(directory: Path, spark_profile: str, stage: str) -> Path:
    """Local name of one stage report: ``<profile>-<stage>.json``."""

    return directory / f"{spark_profile}-{stage}.json"


def rule_targets(rule_name: str, scope: str, inventory: ZoneInventory) -> tuple[RuleTarget, ...]:
    """Return the dataset(s) one rule checks.

    Raises:
        RuleMappingError: The rule matches no table of the scope's zones.
    """

    if scope not in SCOPE_ZONES:
        raise RuleMappingError(f"unknown validation scope {scope!r}")
    if rule_name.startswith(_CROSS_FEATURE_PREFIX):
        if scope != "features":
            raise RuleMappingError(f"{rule_name} is a DP3 rule outside the features scope")
        return tuple(RuleTarget("features", table, cross_table=True) for table in inventory.features)
    population = _FEATURE_POPULATION.match(rule_name) if scope == "features" else None
    if population is None and scope == "core-gold":
        population = _GOLD_POPULATION.match(rule_name)
    if population is not None:
        zone = "features" if scope == "features" else "gold"
        table = population.group("table")
        if table not in inventory.tables(zone):
            raise RuleMappingError(f"{rule_name} compares with unknown {zone} table {table!r}")
        return (RuleTarget(zone, table),)
    prefix, separator, check = rule_name.partition(".")
    if not separator or not check:
        raise RuleMappingError(f"{rule_name} does not name its table")
    for zone in SCOPE_ZONES[scope]:
        if prefix in inventory.tables(zone):
            return (RuleTarget(zone, prefix),)
    raise RuleMappingError(f"{rule_name} names no table of scope {scope}")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        raise RuleMappingError(f"cannot derive an identifier from {value!r}")
    return slug


def assertion_id(campaign: CampaignEvidence, scope: str, rule_name: str, target: RuleTarget) -> str:
    """Stable assertion identifier: benchmark, profile, scope, rule and, for cross-table rules, table."""

    parts = ["edai1", campaign.benchmark_id, campaign.spark_profile, scope, _slug(rule_name)]
    if target.cross_table:
        parts.append(target.table)
    return ".".join(parts)


def load_contract_expectations(directory: Path = CONTRACT_DIRECTORY) -> dict[str, tuple[str, str]]:
    """Return validator rule -> (contract file, expectation) from the data contracts."""

    expectations: dict[str, tuple[str, str]] = {}
    for path in sorted(directory.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            continue
        relative = f"data_platform/contracts/{path.name}"
        for item in document.get("quality_rules") or ():
            if isinstance(item, Mapping) and item.get("validator_rule"):
                expectations[str(item["validator_rule"])] = (relative, str(item.get("expectation", "")))
        for item in document.get("foreign_keys") or ():
            if isinstance(item, Mapping) and item.get("validator_rule"):
                text = f"{', '.join(item.get('columns') or [])} references {item.get('references')}"
                expectations[str(item["validator_rule"])] = (relative, text)
    return expectations


def _zone_stage(campaign: CampaignEvidence) -> dict[str, StageEvidence]:
    return {stage.zone: stage for stage in campaign.stages.values()}


def _read_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CampaignEvidenceError(f"validation report is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CampaignEvidenceError(f"{path} must hold a JSON object")
    return value


def verify_report(report: Mapping[str, Any], campaign: CampaignEvidence, stage: StageEvidence) -> None:
    """Refuse a report that does not belong to this campaign stage.

    Raises:
        CampaignEvidenceError: Schema, dataset, run, scope, status, roots or the
            rule counts disagree with the verified campaign.
    """

    where = f"{stage.stage} validation report"
    checks = {
        "schema_version": REPORT_SCHEMA,
        "dataset_version": campaign.dataset_version,
        "run_id": stage.run_id,
        "scope": stage.validation_scope,
        "status": stage.validation_status,
    }
    for key, expected in checks.items():
        if report.get(key) != expected:
            raise CampaignEvidenceError(f"{where}: {key} is {report.get(key)!r}, expected {expected!r}")
    roots = {name: stage_name for name, stage_name in SCOPE_ROOTS[stage.validation_scope].items()}
    for field, owner in roots.items():
        expected_root = campaign.stages[owner].output_root
        if str(report.get(field, "")).rstrip("/") != expected_root:
            raise CampaignEvidenceError(f"{where}: {field} is not the campaign's {owner} root")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise CampaignEvidenceError(f"{where}: results are missing")
    passed = sum(1 for item in results if item.get("passed") is True)
    failed = sorted(str(item.get("rule_name")) for item in results if item.get("passed") is False)
    if report.get("passed_rules") != passed or sorted(report.get("failed_rules") or []) != failed:
        raise CampaignEvidenceError(f"{where}: passed_rules/failed_rules disagree with the results")
    if (stage.validation_status == "PASS") != (not failed):
        raise CampaignEvidenceError(f"{where}: status {stage.validation_status} disagrees with failed rules")


def _description(rule_name: str, expected: str, target: RuleTarget,
                 contracts: Mapping[str, tuple[str, str]]) -> str:
    text = f"{rule_name}: {expected}."
    if target.cross_table:
        text += " Cross-table rule over the four DP3 feature tables."
    contract = contracts.get(rule_name)
    if contract is not None:
        text += f" Data contract {contract[0]}: {contract[1]}"
    return text


def stage_outcomes(
    campaign: CampaignEvidence,
    stage: StageEvidence,
    report_file: Path | None,
    inventory: ZoneInventory,
    contracts: Mapping[str, tuple[str, str]],
) -> tuple[ValidationOutcome, ...]:
    """Return every outcome of one stage, or one NOT_EXECUTED outcome per output table."""

    zone_stage = _zone_stage(campaign)
    if not stage.validation_executed:
        return tuple(
            ValidationOutcome(
                assertion_id=".".join(("edai1", campaign.benchmark_id, campaign.spark_profile,
                                       stage.validation_scope, "stage_validation", table)),
                pipeline=stage.dag_id,
                dataset_urn=dataset_urn(stage.table_uri(table)),
                dataset_uri=stage.table_uri(table),
                rule_name=f"{stage.validation_scope}.stage_validation",
                description=f"{stage.stage} semantic validation of {table} has no executed result.",
                status="NOT_EXECUTED",
                expected="an executed campaign validation report",
                observed={"reason": "the campaign manifest records no validation for this stage"},
                executed_at="", source_run_id="", source_batch_id="", source_git_sha="",
                source_artifact="", source_artifact_sha256="",
            )
            for table in inventory.tables(stage.zone)
        )
    if report_file is None:
        raise CampaignEvidenceError(f"{stage.stage}: validation ran but no report file was given")
    report = _read_report(report_file)
    verify_report(report, campaign, stage)
    digest = "sha256:" + hashlib.sha256(report_file.read_bytes()).hexdigest()
    outcomes: list[ValidationOutcome] = []
    for item in report["results"]:
        rule_name = str(item.get("rule_name", ""))
        passed = item.get("passed")
        status = "PASS" if passed is True else "FAIL" if passed is False else "UNKNOWN"
        for target in rule_targets(rule_name, stage.validation_scope, inventory):
            uri = zone_stage[target.zone].table_uri(target.table)
            outcomes.append(ValidationOutcome(
                assertion_id=assertion_id(campaign, stage.validation_scope, rule_name, target),
                pipeline=stage.dag_id,
                dataset_urn=dataset_urn(uri),
                dataset_uri=uri,
                rule_name=rule_name,
                description=_description(rule_name, str(item.get("expected", "")), target, contracts),
                status=status,
                expected=str(item.get("expected", "")),
                observed={
                    "actual_value": item.get("actual_value"),
                    "validation_scope": stage.validation_scope,
                    "report_status": report["status"],
                    "validated_run_id": stage.run_id,
                    "dag_run_id": stage.dag_run_id,
                    "spark_profile": campaign.spark_profile,
                    "benchmark_id": campaign.benchmark_id,
                    "validation_spark_application_id": stage.validation_spark_application_id,
                },
                executed_at=str(report["generated_at"]) if status != "UNKNOWN" else "",
                source_run_id=stage.run_id if status != "UNKNOWN" else "",
                source_batch_id=str(stage.validation_batch_id or ""),
                source_git_sha=campaign.git_sha,
                source_artifact=str(stage.validation_report_uri) if status != "UNKNOWN" else "",
                source_artifact_sha256=digest if status != "UNKNOWN" else "",
            ))
    return tuple(outcomes)


def outcomes_by_stage(
    campaign: CampaignEvidence,
    reports_directory: Path,
    *,
    stages: Sequence[str] | None = None,
) -> dict[str, tuple[ValidationOutcome, ...]]:
    """Return ``stage -> outcomes`` for the selected stages (all four by default).

    A stage whose manifest records a validation but whose report file is absent
    raises: a missing report is never turned into a silent NOT_EXECUTED.

    Raises:
        CampaignEvidenceError: A report is missing or does not match the campaign.
        RuleMappingError: A rule cannot be mapped to a dataset.
    """

    inventory = zone_inventory(campaign.dataset_version)
    contracts = load_contract_expectations()
    selected = tuple(stages or campaign.stages)
    unknown = set(selected) - set(campaign.stages)
    if unknown:
        raise CampaignEvidenceError(f"unknown stages {sorted(unknown)}")
    result: dict[str, tuple[ValidationOutcome, ...]] = {}
    for name in selected:
        stage = campaign.stages[name]
        path = report_path(reports_directory, campaign.spark_profile, name)
        result[name] = stage_outcomes(campaign, stage, path if path.is_file() else None, inventory, contracts)
    return result


def campaign_outcomes(
    campaign: CampaignEvidence,
    reports_directory: Path,
    *,
    stages: Sequence[str] | None = None,
) -> tuple[ValidationOutcome, ...]:
    """Return the outcomes of the selected stages as one tuple with unique identifiers.

    Raises:
        CampaignEvidenceError: A report is missing or does not match the campaign.
        RuleMappingError: A rule cannot be mapped, or two outcomes share an identifier.
    """

    grouped = outcomes_by_stage(campaign, reports_directory, stages=stages)
    outcomes = [item for values in grouped.values() for item in values]
    identities = [item.assertion_id for item in outcomes]
    duplicates = sorted({value for value in identities if identities.count(value) > 1})
    if duplicates:
        raise RuleMappingError(f"duplicate assertion identifiers: {duplicates[:5]}")
    return tuple(outcomes)


def summary(outcomes: Sequence[ValidationOutcome]) -> dict[str, Any]:
    """Count outcomes per pipeline, status and dataset."""

    per_status: dict[str, int] = {}
    per_pipeline: dict[str, dict[str, int]] = {}
    per_dataset: dict[str, int] = {}
    for item in outcomes:
        per_status[item.status] = per_status.get(item.status, 0) + 1
        bucket = per_pipeline.setdefault(item.pipeline, {})
        bucket[item.status] = bucket.get(item.status, 0) + 1
        per_dataset[item.dataset_uri] = per_dataset.get(item.dataset_uri, 0) + 1
    return {
        "outcome_count": len(outcomes),
        "per_status": dict(sorted(per_status.items())),
        "per_pipeline": dict(sorted(per_pipeline.items())),
        "dataset_count": len(per_dataset),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Write the validation artifact of one verified campaign profile."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True,
                        help="folder with the four campaign documents of one profile")
    parser.add_argument("--reports-dir", type=Path, required=True,
                        help="folder with <profile>-<stage>.json validation report copies")
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", required=True, choices=("baseline", "optimized"))
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--stage", action="append", choices=("bronze", "silver", "gold", "features"),
                        help="limit to one stage; repeatable (default: all four)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    campaign = load_campaign(
        args.manifest_dir,
        benchmark_id=args.benchmark_id,
        spark_profile=args.spark_profile,
        dataset_version=args.dataset_version,
        source_manifest_sha256=args.source_manifest_sha256,
    )
    outcomes = campaign_outcomes(campaign, args.reports_dir, stages=args.stage)
    # The generation time is the campaign's own finalisation time, so the same
    # inputs always give byte-identical artifacts.
    ValidationArtifact(outcomes=outcomes, generated_at=campaign.finalized_at).write(args.output)
    print(json.dumps({"output": str(args.output), **summary(outcomes)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
