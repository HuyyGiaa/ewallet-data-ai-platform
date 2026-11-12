"""Campaign verifier: the Docker A/B workload, checked against the committed 5GB evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ops.edai1_verifier.__main__ import main
from ops.edai1_verifier.report import canonical_json, fingerprint, verify
from ops.edai1_verifier.spec import SpecError, load_spec

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/edai1/spark/rehearsal-5gb-ab-20261110225456"
SPEC = ROOT / "images/edai1-campaign-verifier/workload-5gb.json"


def test_committed_campaign_passes_every_check():
    report = verify(EVIDENCE, load_spec(SPEC))
    assert report["verdict"] == "PASS", report["failed_checks"]
    assert report["check_count"] == 45
    assert report["rules_per_profile_and_stage"]["optimized"] == {"bronze": 54, "features": 53, "gold": 93,
                                                                  "silver": 60}
    assert report["tables_per_stage"] == {"dp1-bronze": 9, "dp2-gold": 12, "dp2-silver": 9, "dp3-features": 4}
    assert len(report["input_sha256"]) == 18


def test_report_is_deterministic_and_path_independent(tmp_path):
    copy = tmp_path / "evidence"
    shutil.copytree(EVIDENCE, copy)
    first = verify(EVIDENCE, load_spec(SPEC))
    second = verify(copy, load_spec(SPEC))
    assert canonical_json(first) == canonical_json(second)
    assert fingerprint(first) == fingerprint(second)
    assert str(tmp_path) not in canonical_json(second)


def test_an_edited_handoff_fails_the_checksum_chain(tmp_path):
    copy = tmp_path / "evidence"
    shutil.copytree(EVIDENCE, copy)
    path = copy / "manifests/optimized/dp1_handoff.json"
    value = json.loads(path.read_text())
    value["completed_at"] = "2026-01-01T00:00:00+00:00"
    path.write_text(json.dumps(value))
    report = verify(copy, load_spec(SPEC))
    assert report["verdict"] == "FAIL"
    assert "optimized:manifest_pins_dp1_handoff" in report["failed_checks"]


def test_a_missing_report_is_a_failed_check_not_a_crash(tmp_path):
    copy = tmp_path / "evidence"
    shutil.copytree(EVIDENCE, copy)
    (copy / "validation_reports/baseline-gold.json").unlink()
    report = verify(copy, load_spec(SPEC))
    assert "readable:validation_reports/baseline-gold.json" in report["failed_checks"]
    assert "baseline:rules_total" in report["failed_checks"]


def test_a_spec_for_another_campaign_fails(tmp_path):
    spec = json.loads(SPEC.read_text())
    spec["rules_per_profile"] = 261
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    report = verify(EVIDENCE, load_spec(path))
    assert report["failed_checks"] == ["baseline:rules_total", "optimized:rules_total"]


@pytest.mark.parametrize("field,value", [("source_git_sha", "abc"), ("benchmark_id", "ab-1"),
                                         ("compared_tables", 35)])
def test_invalid_specs_are_rejected(tmp_path, field, value):
    spec = json.loads(SPEC.read_text())
    spec[field] = value
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(SpecError):
        load_spec(path)


def test_cli_exit_codes_and_summary(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert main(["--spec", str(SPEC), "--evidence-dir", str(EVIDENCE), "--output", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verdict"] == "PASS"
    assert summary["report_sha256"] == fingerprint(json.loads(output.read_text()))
    assert main(["--spec", str(SPEC), "--evidence-dir", str(tmp_path / "missing")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert main(["--spec", str(bad), "--evidence-dir", str(EVIDENCE)]) == 2
