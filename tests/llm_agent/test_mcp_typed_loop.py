"""mcp_typed_loop: typed read-only calls, tokens from the environment, honest counts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/llm_agent"))
import mcp_typed_loop as loop  # noqa: E402


def test_plan_uses_read_only_tools_and_their_own_tokens():
    plan = loop.calls("http://127.0.0.1:1/", {"transaction_id": "t"}, "bl")
    assert [(p[1], p[2]) for p in plan] == [("EWALLET_MCP_TOKEN_FEATURE_RAG", "feature_lookup"),
                                             ("EWALLET_MCP_TOKEN_DRIFT", "drift_detection")]
    assert plan[0][0].endswith("/feature-rag/mcp") and plan[1][0].endswith("/drift/mcp")


def test_run_counts_every_outcome_and_never_passes_a_failure(monkeypatch):
    monkeypatch.setenv("EWALLET_MCP_TOKEN_FEATURE_RAG", "a")
    monkeypatch.setenv("EWALLET_MCP_TOKEN_DRIFT", "b")
    outcomes = iter(["ok", "server_error", "ok", "ok"])
    now = [0.0]

    async def observe(endpoint, token, tool, arguments, timeout):
        assert token in ("a", "b")
        return {"outcome": next(outcomes), "http_statuses": [200]}

    async def sleep(seconds):
        now[0] += 5

    lines = []
    summary = asyncio.run(loop.run(loop.calls("http://127.0.0.1:1", {}, "bl"), observe, duration=6, interval=1,
                                   timeout=1, emit=lines.append, clock=lambda: now[0], sleep=sleep))
    assert summary["non_ok"] == 1
    assert sum(1 for line in lines if line.startswith("LOAD_ROW")) == 4
    assert "token" not in " ".join(lines)


def test_missing_token_stops_before_any_call(monkeypatch):
    monkeypatch.delenv("EWALLET_MCP_TOKEN_FEATURE_RAG", raising=False)
    with pytest.raises(SystemExit):
        asyncio.run(loop.run(loop.calls("http://127.0.0.1:1", {}, "bl"), None, duration=1, interval=1, timeout=1,
                             emit=print))
