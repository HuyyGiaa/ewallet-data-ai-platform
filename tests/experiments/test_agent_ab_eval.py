"""Grading and aggregation of the agent suite."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")

from ops.experiments.agent_ab_eval import compare, grade, summarise_variant  # noqa: E402

SUITE = Path("configs/experiments/fixtures/agent-ab-suite.jsonl")
BUSINESS = {"expect": {"tool": "investigate_transaction", "arguments": {"transaction_id": "t", "user_id": "u"}}}
OFF_TOPIC = {"expect": {"tool": None}}


def selection(**arguments):
    return SimpleNamespace(name="investigate_transaction", arguments=arguments)


def test_exact_arguments_are_needed_for_task_success():
    assert grade(BUSINESS, selection(transaction_id="t", user_id="u"), None)["task_success"] is True
    wrong = grade(BUSINESS, selection(transaction_id="t", user_id="x"), None)
    assert (wrong["correct_tool"], wrong["task_success"]) == (True, False)


def test_a_rejected_business_case_fails():
    assert grade(BUSINESS, None, "ungrounded_argument")["task_success"] is False


def test_off_topic_succeeds_only_when_no_tool_is_chosen():
    assert grade(OFF_TOPIC, None, "unsupported_request")["task_success"] is True
    assert grade(OFF_TOPIC, selection(), None)["task_success"] is False
    assert grade(OFF_TOPIC, None, "llm_timeout")["task_success"] is False


def row(case, variant, success, **extra):
    return {"id": case, "variant": variant, "task_success": success, "correct_tool": success, "tool_calls": 1,
            "latency_seconds": 1.0, "prompt_tokens": 10, "completion_tokens": 5, "outcome": "selected",
            "served_model": "m", "backend": "b", "failure_code": None, **extra}


def test_failed_cases_stay_in_the_denominator():
    summary = summarise_variant([row("a", "baseline", True), row("b", "baseline", False, outcome="llm_error")])
    assert summary["cases"] == 2
    assert summary["task_success_rate"] == 0.5
    assert summary["llm_errors"] == 1


def test_paired_counts():
    records = [row("a", "baseline", True), row("a", "candidate", False),
               row("b", "baseline", False), row("b", "candidate", True),
               row("c", "baseline", True), row("c", "candidate", True)]
    assert compare(records) == {"both_solved": 1, "neither_solved": 0, "only_baseline": 1, "only_candidate": 1}


def test_suite_is_fixed_and_covers_both_categories():
    rows = [json.loads(line) for line in SUITE.read_text(encoding="utf-8").splitlines()]
    assert len({row["id"] for row in rows}) == len(rows) == 24
    assert {row["category"] for row in rows} == {"business", "off_topic"}
