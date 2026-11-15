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


# Context -> actor ID pairs that Substrate reported on GKE (agi-20261115T064905Z, AGS-04-status.jsonl).
GKE_PAIRS = [["drift-specialist", "agi-20261115T064905Z-drift-specialist-c0-098c10", "asr-7ed6e5037bd81306a0bb7c53"], ["drift-specialist", "agi-20261115T064905Z-drift-specialist-c1-47b39d", "asr-115aabf794e516c1b95f17c9"], ["drift-specialist", "agi-20261115T064905Z-drift-specialist-c2-103224", "asr-16fd18422e17ab8ca4098860"]]


@pytest.mark.parametrize("agent,context,actor", GKE_PAIRS)
def test_session_actor_id_matches_kagent_on_gke(agent, context, actor):
    assert sessions.session_actor_id("ewallet-sandbox", agent, context) == actor


def test_short_session_ids_keep_the_readable_form():
    # The resume-probe actor named in the 2026-11-15 GKE error.
    assert sessions.session_actor_id("ewallet-sandbox", "drift-specialist", "resume-probe-c2b991b5") == \
        "asr-ewallet-sandbox-drift-specialist-resume-probe-c2b991b5"


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_wait_settled_waits_through_suspending_and_is_bounded():
    clock = Clock()
    states = iter(["Running", "Suspending", "Suspending", "Suspended"])
    fetch = lambda: {"data": {"actors": [{"actorId": "a", "status": next(states)}]}}
    assert sessions.wait_settled(fetch, "a", limit=10, clock=clock.now, sleep=clock.sleep) == (True, "Suspended", 1.5)
    clock = Clock()
    stuck = lambda: {"data": {"actors": [{"actorId": "a", "status": "Suspending"}]}}
    settled, status, waited = sessions.wait_settled(stuck, "a", limit=3, clock=clock.now, sleep=clock.sleep)
    assert (settled, status) == (False, "Suspending") and waited == 3


def test_follow_up_is_sent_only_after_the_previous_suspend_settled():
    events, lines = [], []

    def make_post(url, headers):
        def post(message, timeout):
            events.append(("send", message["params"]["message"]["contextId"]))
            return {"result": {"id": message["id"], "status": {"state": "completed"}}}
        return post

    def settle(actor):
        events.append(("settle", actor))
        return True, "Suspended", 0.7

    sessions.run("http://127.0.0.1:1", "ewallet-sandbox", "r1", agents=["drift-specialist"], contexts=1, messages=3,
                 timeout=5, root=ROOT, emit=lines.append, make_post=make_post, settle=settle)
    assert [kind for kind, _ in events] == ["send", "settle", "send", "settle", "send"]
    rows = [json.loads(line[9:]) for line in lines]
    assert [r["phase"] for r in rows] == ["new", "resume", "resume"]
    assert rows[1]["settled_before_send"] is True and rows[1]["actor"] == rows[0]["actor"]


def test_unsettled_suspend_stops_that_context_without_sending_into_the_race():
    sent, lines = [], []

    def make_post(url, headers):
        def post(message, timeout):
            sent.append(message["id"])
            return {"result": {"id": message["id"], "status": {"state": "completed"}}}
        return post

    sessions.run("http://127.0.0.1:1", "ewallet-sandbox", "r1", agents=["drift-specialist"], contexts=1, messages=2,
                 timeout=5, root=ROOT, emit=lines.append, make_post=make_post, settle=lambda actor: (False, "Suspending", 60.0))
    rows = [json.loads(line[9:]) for line in lines]
    assert len(sent) == 1 and rows[-1]["state"] == "not_sent" and rows[-1]["status_before_send"] == "Suspending"
