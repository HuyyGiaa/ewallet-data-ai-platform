"""Recover immutable Airflow task evidence without rerunning a DAG.

The collector intentionally reads only existing log files.  It is usable after
Airflow metadata has been removed and records hashes, terminal state markers,
return codes and referenced Dataproc batch identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


LOG_PATH = re.compile(
    r"dag_id=(?P<dag>[^/]+)/run_id=(?P<run>[^/]+)/"
    r"task_id=(?P<task>[^/]+)/attempt=(?P<attempt>\d+)\.log$"
)
TIMESTAMP = re.compile(
    r'(?:\[|"timestamp":")(?P<value>\d{4}-\d{2}-\d{2}T[^\]" ]+Z)(?:\]|"| )'
)
RETURN_CODE = re.compile(r"Command exited with return code (?P<code>-?\d+)")
BATCH_ID = re.compile(r"\bab-\d{14}-(?:baseline|optimized)-[a-z0-9-]+-r\d{12}\b")


@dataclass(frozen=True)
class TaskEvidence:
    dag_id: str
    run_id: str
    task_id: str
    attempt: int
    path: str
    sha256: str
    size_bytes: int
    first_timestamp: str | None
    last_timestamp: str | None
    return_codes: tuple[int, ...]
    final_return_code: int | None
    success_marker: bool
    failure_marker: bool
    dataproc_batch_ids: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_task_log(path: Path) -> TaskEvidence | None:
    match = LOG_PATH.search(path.as_posix())
    if match is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    timestamps = [item.group("value") for item in TIMESTAMP.finditer(text)]
    return_codes = tuple(int(item.group("code")) for item in RETURN_CODE.finditer(text))
    return TaskEvidence(
        dag_id=match.group("dag"),
        run_id=match.group("run"),
        task_id=match.group("task"),
        attempt=int(match.group("attempt")),
        path=str(path.resolve()),
        sha256=_sha256(path),
        size_bytes=path.stat().st_size,
        first_timestamp=timestamps[0] if timestamps else None,
        last_timestamp=timestamps[-1] if timestamps else None,
        return_codes=return_codes,
        final_return_code=return_codes[-1] if return_codes else None,
        success_marker=(
            "Marking task as SUCCESS" in text
            or "Task instance in success state" in text
        ),
        failure_marker=(
            "Marking task as FAILED" in text
            or "Task instance in failed state" in text
        ),
        dataproc_batch_ids=tuple(sorted(set(BATCH_ID.findall(text)))),
    )


def collect(
    log_root: Path,
    dag_prefix: str = "dp",
    run_prefix: str | None = None,
) -> dict[str, object]:
    records = []
    for path in sorted(log_root.rglob("attempt=*.log")):
        record = parse_task_log(path)
        if (
            record is not None
            and record.dag_id.startswith(dag_prefix)
            and (run_prefix is None or record.run_id.startswith(run_prefix))
        ):
            records.append(record)

    states = Counter(
        "success" if item.success_marker and item.final_return_code == 0
        else "failed" if item.failure_marker or item.final_return_code not in (None, 0)
        else "unknown"
        for item in records
    )
    runs: dict[str, dict[str, object]] = {}
    for item in records:
        key = f"{item.dag_id}/{item.run_id}"
        run = runs.setdefault(key, {"dag_id": item.dag_id, "run_id": item.run_id, "tasks": []})
        run["tasks"].append(item.task_id)

    return {
        "scope": "existing task logs only; no DAG or workload was rerun",
        "log_root": str(log_root.resolve()),
        "task_log_count": len(records),
        "state_counts": dict(sorted(states.items())),
        "run_count": len(runs),
        "runs": list(runs.values()),
        "task_logs": [asdict(item) for item in records],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dag-prefix", default="dp")
    parser.add_argument("--run-prefix")
    args = parser.parse_args(argv)
    payload = collect(args.log_root, args.dag_prefix, args.run_prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("task_log_count", "state_counts", "run_count")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
