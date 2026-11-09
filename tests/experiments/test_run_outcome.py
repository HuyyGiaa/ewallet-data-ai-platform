"""The rule that separates a measured run with failed requests from a failed run."""

from __future__ import annotations

import json

from ops.experiments.run_outcome import main, outcome


def level(requests: int = 12, succeeded: int = 12, status: str = "MEASURED") -> dict:
    return {"status": status, "requests": requests, "succeeded": succeeded}


def test_clean_run_is_measured() -> None:
    assert outcome({"levels": {"1": level(), "2": level()}}, ["1", "2"], 0) == (True, "measured")


def test_failed_requests_are_kept() -> None:
    keep, reason = outcome({"levels": {"1": level(succeeded=11), "2": level()}}, ["1", "2"], 1)
    assert keep and "1 of 24 requests failed" in reason


def test_crash_rate_limit_guard_missing_level_or_no_success_stop() -> None:
    good = {"levels": {"1": level()}}
    assert outcome(good, ["1"], 7)[0] is False
    assert outcome(None, ["1"], 1)[0] is False
    assert outcome({"levels": {"1": {"status": "BLOCKED_GATEWAY_RATE_LIMIT"}}}, ["1"], 1)[0] is False
    assert outcome({"levels": {"1": {"status": "BLOCKED_RESOURCE_GUARD"}}}, ["1"], 1)[0] is False
    assert outcome(good, ["1", "2"], 1)[0] is False
    assert outcome({"levels": {"1": level(succeeded=0)}}, ["1"], 1)[0] is False
    assert outcome({"status": "INVALID", "levels": {"1": level()}}, ["1"], 1)[0] is False


def test_cli(tmp_path, capsys) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"levels": {"1": level(succeeded=11)}}))
    assert main(["--summary", str(path), "--levels", "1", "--exit-code", "1"]) == 0
    assert main(["--summary", str(tmp_path / "missing.json"), "--levels", "1", "--exit-code", "1"]) == 1
    assert "without summary.json" in capsys.readouterr().out
