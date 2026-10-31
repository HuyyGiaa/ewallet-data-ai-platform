"""Atomic runtime artifact publication for the active F8 model."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import joblib


ARTIFACT_NAMES = (
    "model.joblib",
    "validation_report.json",
    "test_report.json",
    "feature_summary.json",
    "run_manifest.json",
)


@dataclass(frozen=True)
class ArtifactPayload:
    model_pipeline: object
    validation_report: dict[str, object]
    test_report: dict[str, object]
    feature_summary: dict[str, object]
    run_manifest: dict[str, object]


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _verify_staged(directory: Path) -> None:
    actual = {path.name for path in directory.iterdir()}
    if actual != set(ARTIFACT_NAMES):
        raise ValueError(f"Incomplete artifact inventory: {sorted(actual)}")
    for name in (
        "validation_report.json",
        "test_report.json",
        "feature_summary.json",
        "run_manifest.json",
    ):
        json.loads((directory / name).read_text(encoding="utf-8"))
    joblib.load(directory / "model.joblib")


def publish_artifacts(payload: ArtifactPayload, output_dir: Path) -> Path:
    """Stage, verify, then atomically promote one complete artifact set."""
    output = Path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    backup: Path | None = None
    try:
        joblib.dump(payload.model_pipeline, staged / "model.joblib")
        _write_json(staged / "validation_report.json", payload.validation_report)
        _write_json(staged / "test_report.json", payload.test_report)
        _write_json(staged / "feature_summary.json", payload.feature_summary)
        manifest = dict(payload.run_manifest)
        manifest["dependencies"] = {
            package: version(package)
            for package in ("pandas", "pyarrow", "scikit-learn", "PyYAML")
        }
        manifest["artifact_inventory"] = list(ARTIFACT_NAMES)
        _write_json(staged / "run_manifest.json", manifest)
        _verify_staged(staged)

        if output.exists():
            backup = output.with_name(f".{output.name}.backup-{uuid.uuid4().hex}")
            os.replace(output, backup)
        os.replace(staged, output)
        if backup is not None:
            shutil.rmtree(backup)
        return output
    except Exception:
        if backup is not None and backup.exists() and not output.exists():
            os.replace(backup, output)
        shutil.rmtree(staged, ignore_errors=True)
        raise
