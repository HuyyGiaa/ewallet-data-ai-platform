"""Prompt Management gate: Git pins and registry readback; never presented as runtime consumption."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts.ci.registry_gate import PROMPTS, check_git_manifest, gate


def test_the_repository_prompt_manifest_matches_its_files() -> None:
    assert check_git_manifest() == []


def test_a_tampered_prompt_file_fails(tmp_path) -> None:
    copy = tmp_path / "prompts"
    shutil.copytree(PROMPTS, copy)
    manifest = json.loads((copy / "manifest.json").read_text())
    entry = next(iter(manifest["prompts"].values()))
    (copy / entry["path"]).write_text("tampered\n")
    assert any("do not match" in item for item in check_git_manifest(copy))


def test_a_missing_registry_url_is_a_failure_not_a_skip() -> None:
    report = gate("", run=lambda argv: 0)
    assert report["verdict"] == "FAIL" and report["checks"]["registry"] == "FAIL"


def test_registry_verification_uses_the_existing_tool_and_its_exit_code() -> None:
    calls: list[list[str]] = []
    report = gate("http://registry.invalid", run=lambda argv: calls.append(list(argv)) or 0)
    assert report["verdict"] == "PASS"
    assert calls[0][-4:] == ["ops.llm_agent.prompt_registry", "verify-all", "--registry", "http://registry.invalid"]
    assert gate("http://registry.invalid", run=lambda argv: 1)["verdict"] == "FAIL"


def test_the_verdict_never_claims_runtime_prompt_consumption() -> None:
    report = gate("http://registry.invalid", run=lambda argv: 0)
    assert report["runtime_consumption"]["status"] == "NOT_PROVEN"
    assert "nl_intake.py" in report["runtime_consumption"]["reason"]
    assert report["production_changed"] is False
