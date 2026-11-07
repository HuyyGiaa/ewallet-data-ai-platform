"""Score one fixed labelled set on both KServe arms and compare quality.

Input rows are JSON lines ``{"id": ..., "label": 0|1, "features": {...}}`` holding the
41 contract columns. Every row goes to both arms through the production client, so
wire, version and signature checks all apply. A failed row is kept and counted, not
dropped, and any failure blocks the comparison.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import InferenceError
from ai_platform.inference.kserve import KServeV2Client
from ai_platform.inference.wire import FraudV2WireContract
from ops.experiments.ab_stats import classification_report


# Offline rows have no online feature read; the wire format never sends this field.
_NO_ONLINE_TIMESTAMP = datetime(1970, 1, 1, tzinfo=timezone.utc)


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Read and validate the fixed evaluation set."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("evaluation set is empty")
    for row in rows:
        if row.get("label") not in (0, 1) or set(row["features"]) != set(MODEL_INPUT_COLUMNS):
            raise ValueError(f"row {row.get('id')} violates the 41-column contract")
    return rows


def vector_for(row: dict[str, Any]) -> ModelInputVector:
    """Build the production input vector in contract column order."""
    return ModelInputVector(
        columns=MODEL_INPUT_COLUMNS,
        values=tuple(row["features"][column] for column in MODEL_INPUT_COLUMNS),
        feature_timestamp=_NO_ONLINE_TIMESTAMP,
    )


async def score_arm(client: Any, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score every row sequentially and record each failure with its code."""
    results = []
    for row in rows:
        try:
            prediction = await client.predict(vector_for(row), correlation_id=f"eval-{row['id']}")
            results.append({
                "id": row["id"], "label": row["label"], "score": prediction.fraud_probability,
                "prediction": prediction.fraud_prediction, "model_version": prediction.model_version,
            })
        except InferenceError as error:
            results.append({"id": row["id"], "label": row["label"], "error": error.code})
    return results


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return quality metrics, or a blocked result when any row failed."""
    failed = [row for row in results if "error" in row]
    if failed:
        return {"status": "BLOCKED", "failed_rows": len(failed), "rows": len(results)}
    report = classification_report(
        [row["label"] for row in results],
        [row["score"] for row in results],
        [row["prediction"] for row in results],
    )
    return {"status": "COMPLETE", "model_versions": sorted({row["model_version"] for row in results}), **report}


def main() -> int:
    """Run both arms and write raw rows plus one summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-name", default="fraud-detector")
    parser.add_argument("--arm", action="append", required=True, help="name=base_url=version")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.dataset)
    args.out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"dataset": str(args.dataset), "rows": len(rows), "arms": {}}
    for spec in args.arm:
        name, base_url, version = spec.split("=", 2)
        client = KServeV2Client(
            base_url=base_url, model_name=args.model_name, model_version=version,
            timeout_seconds=10.0, wire_contract=FraudV2WireContract(),
        )
        results = asyncio.run(score_arm(client, rows))
        (args.out / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in results), encoding="utf-8"
        )
        summary["arms"][name] = summarise(results)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0 if all(arm["status"] == "COMPLETE" for arm in summary["arms"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
