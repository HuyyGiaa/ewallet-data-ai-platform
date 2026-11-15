"""sandbox_sessions: the SandboxAgent route, the new/resume phases and the bounds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.experiments import sandbox_sessions as sessions

ROOT = Path(__file__).resolve().parents[2]


def test_sandbox_agents_use_the_a2a_sandboxes_route():
    assert sessions.sandbox_url("http://127.0.0.1:1/", "ewallet-sandbox", "drift-specialist") == \
        "http://127.0.0.1:1/api/a2a-sandboxes/ewallet-sandbox/drift-specialist/"


def test_each_context_creates_then_resumes_and_both_agents_run():
    calls, lines = [], []

    def make_post(url, headers):
        def post(message, timeout):
            calls.append((url, message["params"]["message"]["contextId"]))
            return {"result": {"id": message["id"], "status": {"state": "failed"}}}
        return post

    sessions.run("http://127.0.0.1:1", "ewallet-sandbox", "r1", agents=["feature-rag-specialist", "drift-specialist"],
                 contexts=2, messages=2, timeout=5, root=ROOT, emit=lines.append, make_post=make_post)
    rows = [json.loads(line[9:]) for line in lines]
    assert len(rows) == 8
    assert {r["agent"] for r in rows} == {"feature-rag-specialist", "drift-specialist"}
    for agent in ("feature-rag-specialist", "drift-specialist"):
        mine = [r for r in rows if r["agent"] == agent]
        assert [r["phase"] for r in mine] == ["new", "resume", "new", "resume"]
        assert len({r["context"] for r in mine}) == 2
    assert all("/api/a2a-sandboxes/ewallet-sandbox/" in url for url, _ in calls)


@pytest.mark.parametrize("args", [["--url", "http://example.com"], ["--url", "http://127.0.0.1:1", "--contexts", "11"],
                                  ["--url", "http://127.0.0.1:1", "--agents", "payments"]])
def test_cli_bounds(args):
    with pytest.raises(SystemExit):
        sessions.main([*args, "--run-id", "x"])
