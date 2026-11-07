"""Train the fraud-detector v2 candidates locally on the v1 training snapshot.

Reuses the production training code: one snapshot, train on train, choose on validation
only, then score the control and the chosen candidate once on the held-out test split.
The result is an offline comparison. It does not register a model and says nothing
about serving; registration stays with the MLflow pipeline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import numpy as np
import yaml

from ai_platform.ml.modeling.config import ModelConfig
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.evaluation import evaluate_scores
from ai_platform.ml.modeling.experiment import CandidateResult, select_validation_winner, train_candidate


@dataclass(frozen=True)
class Candidate:
    """One configured model and whether it may win selection."""

    model: ModelConfig
    selection_eligible: bool


def load_candidates(path: Path) -> tuple[Candidate, ...]:
    """Read the candidate list of an experiment file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(
        Candidate(
            ModelConfig(item["name"], item["estimator"], item["strategy"], dict(item.get("params", {}))),
            bool(item["selection_eligible"]),
        )
        for item in raw["candidates"]
    )


def test_report(result: CandidateResult, test: Any) -> dict[str, Any]:
    """Score one fitted candidate on test, at its own validation threshold."""
    started = time.perf_counter()
    scores = result.pipeline.predict_proba(test.X)[:, 1]
    seconds = time.perf_counter() - started
    metrics = evaluate_scores(test.y.to_numpy(), scores, result.threshold).as_dict()
    return {
        "model": result.name,
        "threshold": result.threshold,
        "metrics": metrics,
        "predict_seconds": round(seconds, 4),
        "scores_sha256": hashlib.sha256(np.round(scores, 10).tobytes()).hexdigest(),
    }


def export_for_serving(results: dict[str, CandidateResult], test: Any, root: Path) -> None:
    """Write each model as the predictor image would hold it, plus the test rows.

    Needs the serving environment, which owns MLflow. The identity files use the
    planned registry versions; they are local only and register nothing.
    """
    import mlflow.pyfunc
    from mlflow.models import infer_signature

    from ai_platform.ml.tracking.mlflow_tracker import python_package_snapshot
    from ai_platform.ml.tracking.model import FraudPyFuncModel

    example = test.X.iloc[:20]
    for version, result in results.items():
        target = root / f"v{version}"
        model = FraudPyFuncModel(result.pipeline, result.threshold)
        mlflow.pyfunc.save_model(
            str(target / "model"),
            python_model=model,
            signature=infer_signature(example, model.predict(None, example)),
            code_paths=[str(python_package_snapshot(target / "code"))],
        )
        (target / "model_identity.json").write_text(
            json.dumps({"registered_model_name": "fraud-detector", "registered_model_version": version})
        )
    frame = test.X.astype(object).where(test.X.notna(), None)
    with (root / "test_rows.jsonl").open("w", encoding="utf-8") as handle:
        for index, (features, label) in enumerate(zip(frame.to_dict("records"), test.y.tolist())):
            handle.write(json.dumps({"id": index, "label": int(label), "features": features}, default=_plain) + "\n")


def _plain(value: Any) -> Any:
    """Convert NumPy scalars for JSON."""
    return value.item() if hasattr(value, "item") else str(value)


def main() -> int:
    """Train every candidate, select on validation and write one report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("ai_platform/ml/config/c7_candidate_v2.yaml"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, help="write local serving artifacts here")
    args = parser.parse_args()
    splits = prepare_modeling_splits(load_dataset(args.snapshot))
    candidates = load_candidates(args.config)
    results = tuple(
        train_candidate(candidate, splits["train"], splits["validation"], seed=args.seed)
        for candidate in candidates
    )
    winner = select_validation_winner(results)
    control = next(result for result in results if not result.selection_eligible)
    report = {
        "scope": "LOCAL_OFFLINE_ONLY: not registered, not served, not a production claim",
        "snapshot_manifest_sha256": hashlib.sha256((args.snapshot / "manifest.json").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "seed": args.seed,
        "rows": {name: int(len(split.y)) for name, split in splits.items()},
        "validation": [result.validation_report for result in results],
        "validation_winner": winner.name,
        "test": {"control": test_report(control, splits["test"]), "candidate": test_report(winner, splits["test"])},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=float) + "\n", encoding="utf-8")
    if args.export_dir:
        args.export_dir.mkdir(parents=True, exist_ok=True)
        export_for_serving({"1": control, "2": winner}, splits["test"], args.export_dir)
    print(json.dumps({"winner": winner.name, "test": report["test"]}, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
