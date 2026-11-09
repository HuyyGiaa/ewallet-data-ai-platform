"""The EXP-V1 -> EXP-V2 gate refuses an incomplete or invalid session."""

from __future__ import annotations

import json
from pathlib import Path

from ops.experiments.chain_gate import expected_order, gate, main


def build(root: Path, levels=("1", "2", "4"), runs: int = 3, verdict: str = "NO_SIGNIFICANT_CHANGE") -> Path:
    s = root / "exp-v1"
    lines = ["2026-11-09T09:31:47Z start exp-v1 session profile=formal"]
    for name in expected_order(runs):
        arm, run = name.split("-")
        lines.append(f"2026-11-09T10:00:00Z run {run} {arm} (label, route: Service -> vLLM)")
        raw, met = s / arm / "raw" / f"run-{run}", s / arm / "metrics" / f"run-{run}"
        raw.mkdir(parents=True)
        met.mkdir(parents=True)
        (raw / "summary.json").write_text(json.dumps({"levels": {
            lv: {"status": "MEASURED", "requests": 12, "succeeded": 11 if arm == "baseline" else 12} for lv in levels}}))
        (raw / "raw_requests.jsonl").write_text("".join(json.dumps({"level": int(lv)}) + "\n"
                                                        for lv in levels for _ in range(12)))
        (raw / "controls.json").write_text("{}")
        (met / "window-start.txt").write_text("t0")
        (met / "window-end.txt").write_text("t1")
        (met / "prometheus-readback.json").write_text(json.dumps({"targets": [{"job": "vllm", "health": "up"}]}))
    lines.append("2026-11-09T12:10:00Z session end status=0")
    (s / "session.log").write_text("\n".join(lines) + "\n")
    (s / "manifest.json").write_text(json.dumps({"finished_at": "2026-11-09T12:08:00Z", "deviations": []}))
    (s / "comparison").mkdir()
    (s / "comparison/metrics.json").write_text(json.dumps({"runs": {"baseline": runs, "optimized": runs},
                                                           "verdict": verdict}))
    (s / "comparison/comparison.md").write_text("x")
    (s / "comparison/comparison.csv").write_text("x")
    (s / "safety").mkdir()
    (s / "safety/cleanup.txt").write_text("pool and namespace confirmed absent; no bench VMs or disks\n")
    (s / "production-before.txt").write_text("same")
    (s / "production-after.txt").write_text("same")
    return s


def test_complete_session_passes_whatever_the_direction(tmp_path) -> None:
    for verdict in ("IMPROVED", "REGRESSED", "NO_SIGNIFICANT_CHANGE"):
        report = gate(build(tmp_path / verdict, verdict=verdict), ["1", "2", "4"], 3)
        assert report["passed"], report["problems"]


def test_inconclusive_or_invalid_comparison_blocks(tmp_path) -> None:
    for verdict in ("INCONCLUSIVE", "INVALID_COMPARISON", "NOT_EXECUTED"):
        assert not gate(build(tmp_path / verdict, verdict=verdict), ["1", "2", "4"], 3)["passed"]


def test_each_missing_piece_blocks(tmp_path) -> None:
    cases = {
        "status": lambda s: (s / "session.log").write_text("x run 01 baseline (\nx session end status=1\n"),
        "summary": lambda s: (s / "optimized/raw/run-03/summary.json").unlink(),
        "level": lambda s: (s / "baseline/raw/run-02/summary.json").write_text(json.dumps(
            {"levels": {"1": {"status": "MEASURED", "requests": 12, "succeeded": 12}}})),
        "raw": lambda s: (s / "baseline/raw/run-01/raw_requests.jsonl").write_text('{"level": 1}\n'),
        "target": lambda s: (s / "optimized/metrics/run-01/prometheus-readback.json").write_text(
            json.dumps({"targets": [{"job": "vllm", "health": "down"}]})),
        "cleanup": lambda s: (s / "safety/cleanup.txt").write_text("CLEANUP FAILED\n"),
        "production": lambda s: (s / "production-after.txt").write_text("changed"),
        "runs": lambda s: (s / "comparison/metrics.json").write_text(json.dumps(
            {"runs": {"baseline": 2, "optimized": 3}, "verdict": "IMPROVED"})),
    }
    for name, damage in cases.items():
        session = build(tmp_path / name)
        damage(session)
        assert not gate(session, ["1", "2", "4"], 3)["passed"], name


def test_cli_writes_the_report(tmp_path, capsys) -> None:
    session = build(tmp_path)
    out = tmp_path / "gate.json"
    assert main(["--session", str(session), "--levels", "1,2,4", "--runs", "3", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["passed"] is True
    assert "PASS" in capsys.readouterr().out
