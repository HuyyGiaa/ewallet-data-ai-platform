"""The read-only live view prints each session line and finished request once."""

from __future__ import annotations

import json

from ops.experiments.progress import Follower, main


def test_follower_prints_new_lines_once_and_waits_for_partial_lines(tmp_path) -> None:
    session = tmp_path / "exp-v1"
    raw = session / "baseline/raw/run-01/raw_requests.jsonl"
    raw.parent.mkdir(parents=True)
    (session / "session.log").write_text("2026-11-09T01:00:00Z run 01 baseline measuring\nkubectl noise\n")
    row = {"id": "tool-1", "category": "tool_call", "level": 2, "status": 200, "ttft_ms": 812.4, "e2e_ms": 4100.0,
           "output_tokens": 31, "backend": "primary", "failed": False, "started_at": 1794186000.0}
    raw.write_text(json.dumps(row) + "\n" + '{"id": "half')
    follower = Follower(session)
    first = follower.poll()
    assert first[0] == "[session] 2026-11-09T01:00:00Z run 01 baseline measuring"
    assert len(first) == 2 and "baseline  run 01 c=2" in first[1] and "http 200" in first[1] and "ok" in first[1]
    assert follower.poll() == []
    with raw.open("a") as handle:
        handle.write('-1", "level": 2, "status": 503, "failed": true}\n')
    second = follower.poll()
    assert len(second) == 1 and "http 503" in second[0] and second[0].endswith("FAILED")


def test_once_exits_and_missing_session_is_refused(tmp_path, capsys) -> None:
    (tmp_path / "s").mkdir()
    assert main([str(tmp_path / "s"), "--once"]) == 0
    try:
        main([str(tmp_path / "absent"), "--once"])
    except SystemExit as exc:
        assert exc.code == 2
    assert "does not exist yet" in capsys.readouterr().err
