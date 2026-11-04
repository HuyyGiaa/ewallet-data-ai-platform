"""Submit the compiled F9 package to standalone KFP 2.17.3 and wait for it.

The KFP API is reached through ``kubectl port-forward svc/ml-pipeline-ui``.
The package checksum, every parameter and the final task states are written
to the evidence file; nothing is inferred from a previous run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import kfp


PACKAGE = Path("ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://127.0.0.1:8888")
    parser.add_argument("--expected-package-sha256", required=True)
    parser.add_argument("--experiment", default="f9-fraud-canonical")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--param", action="append", default=[], help="name=value")
    args = parser.parse_args(argv)

    package_sha = hashlib.sha256(PACKAGE.read_bytes()).hexdigest()
    if package_sha != args.expected_package_sha256:
        raise SystemExit(f"package checksum {package_sha} != expected")
    parameters = dict(item.split("=", 1) for item in args.param)

    client = kfp.Client(host=args.host)
    experiment = client.create_experiment(args.experiment)
    run = client.create_run_from_pipeline_package(
        str(PACKAGE),
        arguments=parameters,
        run_name=args.run_name,
        experiment_name=args.experiment,
        enable_caching=False,
    )
    evidence = {
        "schema_version": "f9-kfp-run-evidence-v1",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run.run_id,
        "run_name": args.run_name,
        "experiment_id": experiment.experiment_id,
        "package_sha256": package_sha,
        "parameters": parameters,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"run_id": run.run_id}), flush=True)

    deadline = time.monotonic() + args.timeout_seconds
    detail = client.get_run(run.run_id)
    while detail.state not in {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELED"}:
        if time.monotonic() > deadline:
            evidence["state"] = "TIMED_OUT"
            break
        time.sleep(20)
        detail = client.get_run(run.run_id)
        print(json.dumps({"state": detail.state}), flush=True)
    else:
        evidence["state"] = detail.state
    tasks = (detail.run_details.task_details or []) if detail.run_details else []
    evidence.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        tasks={
            task.display_name: {"state": task.state, "pod": getattr(task, "pod_name", None)}
            for task in tasks
        },
    )
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"state": evidence["state"]}))
    return 0 if evidence["state"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
