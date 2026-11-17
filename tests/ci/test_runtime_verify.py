"""Runtime verification adapter over agent_replay.sh: only verdicts the command wrote count."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.runtime_verify import CHECKS, VerificationError, checks_for, run_checks

ROOT = Path(__file__).resolve().parents[2]


def fake_helper(results: dict[str, tuple[int, str | None]]):  # type: ignore[no-untyped-def]
    """Stands in for agent_replay.sh: writes the verdict file the real command writes."""

    calls: list[list[str]] = []

    def run(argv, env):  # type: ignore[no-untyped-def]
        args = list(argv[1:])
        calls.append(args)
        key = " ".join(args)
        code, verdict = results.get(key, (0, "PASS"))
        replay = Path(env["REPLAY_DIR"])
        if verdict is not None:
            if args[0] == "e2e":
                path = replay / "e2e" / f"replay-{args[1]}-{len(calls)}-verdict.json"
            else:
                path = replay / args[0] / "verdict.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"verdict": "COMPLETE" if (args[0] == "e2e" and verdict == "PASS") else verdict}))
        return code

    return run, calls


def coordinator_units() -> list[dict]:
    units = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())["units"]
    names = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())["groups"]["release/coordinator"]
    return [units[name] for name in names["production"]]


def test_the_coordinator_release_runs_status_registry_three_workflows_and_negatives(tmp_path) -> None:
    run, calls = fake_helper({})
    report = run_checks(checks_for(coordinator_units()), tmp_path, run=run, environment={})
    assert report["verdict"] == "PASS"
    # Full stack (candidate, MCP, fleet, Coordinator): each distinct command once, prerequisites first.
    # Registry readback is not a rollback gate (its digest labels go stale until the approved publication).
    assert calls == [["status"], ["e2e", "fraud"], ["e2e", "drift"], ["e2e", "combined"], ["negative"]]


def test_any_failing_workflow_fails_the_release(tmp_path) -> None:
    run, _ = fake_helper({"e2e drift": (1, "PARTIAL")})
    report = run_checks(["e2e-fraud", "e2e-drift", "e2e-combined"], tmp_path, run=run, environment={})
    assert report["verdict"] == "FAIL"
    assert [check["status"] for check in report["checks"]] == ["PASS", "FAIL", "PASS"]


def test_an_exit_zero_without_a_verdict_file_is_a_failure(tmp_path) -> None:
    run, _ = fake_helper({"status": (0, None)})
    assert run_checks(["platform-status"], tmp_path, run=run, environment={})["verdict"] == "FAIL"


def test_an_earlier_verdict_never_counts_for_a_later_command(tmp_path) -> None:
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "verdict.json").write_text(json.dumps({"verdict": "PASS"}))
    run, _ = fake_helper({"status": (0, None)})
    assert run_checks(["platform-status"], tmp_path, run=run, environment={})["verdict"] == "FAIL"


def test_unknown_checks_are_refused_and_every_declared_check_is_known() -> None:
    with pytest.raises(VerificationError):
        run_checks(["kubectl-get-pods"], Path("/nonexistent"), run=lambda a, e: 0, environment={})
    units = json.loads((ROOT / "jenkins/catalog/release-units.json").read_text())["units"]
    from scripts.ci.sandbox_candidate import CHECKS as CANDIDATE_CHECKS

    for unit in units.values():
        declared = set(unit["verify"]) | set(unit["prerequisites"])
        if unit["kind"] == "sandbox-candidate":
            # Enforced by the candidate executor itself, before any promotion.
            assert declared <= set(CANDIDATE_CHECKS), unit
        elif unit["stage"] == "production":
            assert declared <= set(CHECKS), unit
