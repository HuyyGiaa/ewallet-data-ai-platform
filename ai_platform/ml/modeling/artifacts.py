"""Atomic runtime artifact publication for local F8 runs."""

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
import pandas as pd


ARTIFACT_NAMES = (
    "comparison.json",
    "comparison.csv",
    "champion.joblib",
    "selected_features.json",
    "test_report.json",
    "run_manifest.json",
)


@dataclass(frozen=True)
class ArtifactPayload:
    comparison: list[dict[str, object]]
    champion_pipeline: object
    selected_features: dict[str, object]
    test_report: dict[str, object]
    run_manifest: dict[str, object]


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _comparison_rows(comparison: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for candidate in comparison:
        metrics = candidate["validation_metrics"]
        features = candidate["feature_summary"]
        rows.append(
            {
                "name": candidate["name"],
                "threshold": candidate["threshold"],
                "input_features": 41,
                "output_features": features["output_feature_count"],
                "pr_auc": metrics["pr_auc"],
                "roc_auc": metrics.get("roc_auc"),
                "precision": metrics.get("precision"),
                "recall": metrics.get("recall"),
                "f1": metrics["f1"],
                "fit_seconds": candidate["fit_seconds"],
                "predict_seconds": candidate["predict_seconds"],
            }
        )
    return rows


def _verify_staged(directory: Path) -> None:
    actual = {path.name for path in directory.iterdir()}
    if actual != set(ARTIFACT_NAMES):
        raise ValueError(f"Incomplete artifact inventory: {sorted(actual)}")
    for name in (
        "comparison.json",
        "selected_features.json",
        "test_report.json",
        "run_manifest.json",
    ):
        json.loads((directory / name).read_text(encoding="utf-8"))
    pd.read_csv(directory / "comparison.csv")
    joblib.load(directory / "champion.joblib")


def publish_artifacts(payload: ArtifactPayload, output_dir: Path) -> Path:
    """Stage, verify, then atomically promote one complete artifact set."""
    output = Path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    backup: Path | None = None
    try:
        _write_json(staged / "comparison.json", payload.comparison)
        pd.DataFrame(_comparison_rows(payload.comparison)).to_csv(
            staged / "comparison.csv", index=False
        )
        joblib.dump(payload.champion_pipeline, staged / "champion.joblib")
        _write_json(staged / "selected_features.json", payload.selected_features)
        _write_json(staged / "test_report.json", payload.test_report)
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
