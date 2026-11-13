"""Assemble the canonical verification report and its functional fingerprint."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import REPORT_SCHEMA
from .checks import Evidence, airflow_checks, campaign_checks, comparison_checks, summarize_rules, validation_checks
from .spec import WorkloadSpec


def verify(evidence_root: Path, spec: WorkloadSpec) -> dict[str, Any]:
    """Run every check and return the report.

    The report holds no timestamp, host name or absolute path, so two runs over the
    same inputs produce byte-identical canonical JSON.
    """

    evidence = Evidence(evidence_root)
    checks = campaign_checks(evidence, spec)
    validation, rule_counts = validation_checks(evidence, spec)
    comparison, tables = comparison_checks(evidence, spec)
    checks += validation + comparison + airflow_checks(evidence, spec)
    checks += evidence.errors
    failed = [check.name for check in checks if not check.passed]
    return {
        "schema_version": REPORT_SCHEMA,
        "spec": spec.to_dict(),
        "verdict": "PASS" if checks and not failed else "FAIL",
        "check_count": len(checks),
        "failed_checks": failed,
        "rules_per_profile_and_stage": summarize_rules(rule_counts),
        "tables_per_stage": dict(sorted(tables.items())),
        "input_sha256": dict(sorted(evidence.digests.items())),
        "checks": [asdict(check) for check in sorted(checks, key=lambda item: item.name)],
    }


def canonical_json(report: dict[str, Any]) -> str:
    """Canonical serialization: sorted keys, no insignificant whitespace, trailing newline."""

    return json.dumps(report, sort_keys=True, separators=(",", ":"), default=str) + "\n"


def fingerprint(report: dict[str, Any]) -> str:
    """SHA-256 of the canonical report: the value compared across image variants."""

    return hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
