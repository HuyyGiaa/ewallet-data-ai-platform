"""Warehouse benchmark command line.

Subcommands (run from the repository root)::

    extract  --campaign-evidence-dir D --workload-spec F --days N --output-dir SNAPSHOT_DIR
    load     --snapshot-dir SNAPSHOT_DIR
    before   --run-dir RUN_DIR            # no secondary index; writes before.json + dbeaver_session.sql
    index    --run-dir RUN_DIR [--already-created]
    after    --run-dir RUN_DIR            # writes report.json + summary.txt
    all      --run-dir RUN_DIR            # before + index + after in one go

``extract`` reads GCS (read-only) and needs ``deltalake``/``pyarrow`` (the
``ai_platform/features/feast`` uv project); the other subcommands only talk to the
isolated container through ``docker exec``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from . import benchmark, dbeaver
from .postgres import Psql


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _read(path: Path) -> Any:
    if not path.is_file():
        raise SystemExit(f"{path} is missing; run the previous phase first")
    return json.loads(path.read_text(encoding="utf-8"))


def summary(report: dict[str, Any]) -> str:
    """Compact text result for the terminal and the evidence folder."""

    read, write = report["wh_a_read"], report["wh_c_write"]
    index = report["wh_b_index"]
    return "\n".join([
        f"warehouse benchmark  postgres={report['postgres_version']}  rows={report['profile']['rows']:,}",
        f"query: {report['query']}",
        f"index: {index['definition']}  size={index['size_bytes']:,} B  created_by={index['created_by']}",
        f"WH-A without index: median {read['without_index']['execution_ms']['median']} ms  "
        f"({read['without_index']['runs'][0]['access_paths']})",
        f"WH-A with index:    median {read['with_index']['execution_ms']['median']} ms  "
        f"({read['with_index']['runs'][0]['access_paths']})",
        f"WH-A before/after verdict: {read['before_after']['verdict']} ({read['before_after']['runtime_change_percent']} %)",
        f"WH-A interleaved verdict:  {read['interleaved']['comparison']['verdict']} "
        f"({read['interleaved']['comparison']['runtime_change_percent']} %)",
        f"WH-C insert median: {write['execution_ms']['without_index']['median']} -> "
        f"{write['execution_ms']['with_index']['median']} ms  verdict {write['comparison']['verdict']}  "
        f"WAL {write['wal_bytes']['without_index']:,} -> {write['wal_bytes']['with_index']:,} B "
        f"(+{write['wal_bytes']['overhead_percent']} %)",
        f"WH-D semantic equivalence: {'PASS' if report['wh_d_semantics']['passed'] else 'FAIL'}",
    ]) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data_platform.storage.warehouse", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--campaign-evidence-dir", type=Path, required=True)
    extract.add_argument("--workload-spec", type=Path, required=True)
    extract.add_argument("--days", type=int, required=True)
    extract.add_argument("--output-dir", type=Path, required=True)
    load = commands.add_parser("load")
    load.add_argument("--snapshot-dir", type=Path, required=True)
    for name in ("before", "index", "after", "all"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", type=Path, required=True)
        sub.add_argument("--repetitions", type=int, default=11)
        sub.add_argument("--warmups", type=int, default=2)
        if name in ("index", "all"):
            sub.add_argument("--already-created", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "extract":
        from ops.gcp.inspect_storage_layout import load_verified_campaign, table_uri

        from .source import extract as run_extract

        spec = json.loads(args.workload_spec.read_text(encoding="utf-8"))
        binding = load_verified_campaign(args.campaign_evidence_dir, args.workload_spec,
                                         dataset_version=spec["dataset_version"],
                                         source_spark_git_sha=spec["source_git_sha"])
        provenance = run_extract(binding, table_uri(binding, "dp2-gold", "fact_transactions"),
                                 days=args.days, output_dir=args.output_dir)
        print(json.dumps({"snapshot": provenance["snapshot"], "profile": provenance["profile"]}, indent=2))
        return 0
    psql = Psql()
    if args.command == "load":
        from .load import load as run_load

        print(json.dumps(run_load(args.snapshot_dir, psql), indent=2))
        return 0
    run_dir: Path = args.run_dir
    if args.command in ("before", "all"):
        if (run_dir / "before.json").exists():
            raise SystemExit(f"{run_dir}/before.json exists; use a new run directory")
        run_dir.mkdir(parents=True, exist_ok=True)
        before = benchmark.phase_before(psql, warmups=args.warmups, repetitions=args.repetitions)
        _write(run_dir / "before.json", before)
        (run_dir / "dbeaver_session.sql").write_text(dbeaver.render(before), encoding="utf-8")
        print(f"before: median {before['without_index']['execution_ms']['median']} ms; "
              f"DBeaver script: {run_dir / 'dbeaver_session.sql'}")
    if args.command in ("index", "all"):
        before = _read(run_dir / "before.json")
        index = benchmark.create_index(psql, before["design"], execute=not args.already_created)
        _write(run_dir / "index.json", index)
        print(f"index: {index['definition']} ({index['size_bytes']:,} bytes)")
    if args.command in ("after", "all"):
        report = benchmark.phase_after(psql, _read(run_dir / "before.json"), _read(run_dir / "index.json"),
                                       warmups=args.warmups, repetitions=args.repetitions)
        _write(run_dir / "report.json", report)
        text = summary(report)
        (run_dir / "summary.txt").write_text(text, encoding="utf-8")
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
