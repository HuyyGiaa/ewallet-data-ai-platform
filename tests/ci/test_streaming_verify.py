"""Isolated Block A campaign driver: configuration isolation, step order, cleanup and run-identity evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.streaming_verify import (
    OPERATOR, REGISTRY_JOB, StreamingConfigError, campaign_namespace, configure, plan_steps, run_campaign,
    verify_evidence,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = json.loads((ROOT / "jenkins/cd/streaming-block-a.profile.json").read_text())
SHA = "d" * 40
FLINK = "asia-southeast1-docker.pkg.dev/p/ewallet-platform/flink-block-a@sha256:" + "1" * 64
SINKS = "asia-southeast1-docker.pkg.dev/p/ewallet-platform/streaming-sinks@sha256:" + "2" * 64
OWNER = {"FEAST_SNAPSHOT_ID": "snap-20261117", "FEATURE_RUN_ID": "fr-20261117"}


def campaign(tmp_path, **changes):  # type: ignore[no-untyped-def]
    arguments = dict(flink_image=FLINK, sinks_image=SINKS, git_sha=SHA, build="42", owner=OWNER,
                     evidence=tmp_path, kube_context="ctx")
    arguments.update(changes)
    return configure(PROFILE, **arguments)


def test_every_path_is_scoped_to_a_new_verify_namespace(tmp_path) -> None:
    item = campaign(tmp_path)
    assert item.namespace == f"verify-cicd-{SHA[:12]}-42"
    env = item.environment
    for name in ("FEAST_OFFLINE_DATA_ROOT", "HISTORY_ROOT", "ONLINE_REGISTRY_URI"):
        assert item.namespace in env[name], name
    assert env["FEAST_OFFLINE_DATA_ROOT"] == (f"{env['OFFLINE_FEATURE_ROOT']}/campaigns/{item.namespace}"
                                              f"/runs/{item.namespace}/offline")
    assert env["ONLINE_STORE_ID"] == "rehearsal-stream" and env["FEAST_REDIS_SECRET"] == "feast-online-redis-stream"
    assert env["FLINK_IMAGE"] == FLINK and env["SINKS_IMAGE"] == SINKS and env["FEAST_ONLINE_IMAGE"] == SINKS
    assert env["FLINK_IMAGE_SOURCE_SHA"] == env["SINKS_IMAGE_SOURCE_SHA"] == env["APPROVED_SHA"] == SHA


@pytest.mark.parametrize("changes,message", [
    (dict(flink_image="flink:latest"), "pinned by digest"),
    (dict(sinks_image=FLINK), "two distinct"),
    (dict(git_sha="short"), "full SHA"),
    (dict(owner={**OWNER, "FEAST_SNAPSHOT_ID": ""}), "missing"),
    (dict(owner={**OWNER, "FEATURE_RUN_ID": "Bad Id"}), "valid identifier"),
    (dict(kube_context=""), "kubeconfig context"),
    (dict(kube_context="in-cluster"), "P6"),
])
def test_unsafe_configurations_are_refused(tmp_path, changes, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(StreamingConfigError, match=message):
        campaign(tmp_path, **changes)


def test_the_campaign_must_use_the_verification_online_store(tmp_path) -> None:
    with pytest.raises(StreamingConfigError, match="rehearsal-stream"):
        configure({**PROFILE, "online_store_id": "canonical-online"}, flink_image=FLINK, sinks_image=SINKS,
                  git_sha=SHA, build="1", owner=OWNER, evidence=tmp_path, kube_context="ctx")


def test_the_bucket_comes_from_the_profile_not_the_caller(tmp_path) -> None:
    item = campaign(tmp_path, owner={**OWNER, "APPROVED_DATA_BUCKET": "attacker-bucket"})
    assert item.environment["HISTORY_ROOT"].startswith(f"gs://{PROFILE['bucket']}/")
    with pytest.raises(StreamingConfigError, match="bucket name"):
        configure({**PROFILE, "bucket": "gs://x/y"}, flink_image=FLINK, sinks_image=SINKS, git_sha=SHA, build="1",
                  owner=OWNER, evidence=tmp_path, kube_context="ctx")


def test_namespaces_stay_within_the_operator_guard() -> None:
    assert campaign_namespace("verify-cicd-", SHA, "jenkins-edai2-cd-streaming-123456789") .startswith("verify-cicd-")
    assert len(campaign_namespace("verify-cicd-", SHA, "x" * 80)) <= 53


def test_steps_follow_the_documented_order(tmp_path) -> None:
    steps = plan_steps(campaign(tmp_path), execute=True)
    assert [step.name for step in steps] == ["render", "preflight", "registry", "deploy", "run", "collect"]
    assert steps[1].argv[-2:] == ["--execute", "--approve-redis-claim"]
    assert steps[2].argv[0] == REGISTRY_JOB and steps[2].argv[-2:] == ["--", "apply"]
    assert all(step.argv[0] in {OPERATOR, REGISTRY_JOB} for step in steps)


def good_evidence(directory: Path, namespace: str) -> None:
    directory.mkdir(parents=True)
    (directory / "consistency-verdict.json").write_text(json.dumps({"status": "PASS"}))
    (directory / "consistency-report.json").write_text(json.dumps({
        "status": "PASS", "namespace": namespace, "features_compared": 12,
        "identity": {"run_id": namespace, "campaign_id": namespace, "git_sha": SHA},
        "offline_verification": {"status": "PASS", "run_id": namespace, "rows_retrieved": 6},
        "source_event_verification": {"status": "PASS", "comparisons": 12},
        "direct_offline_online_comparison": {"status": "PASS", "comparisons": 12, "mismatch_count": 0}}))
    (directory / "flink-jobs.json").write_text(json.dumps({"jobs": [{"jid": "j", "state": "RUNNING"}]}))


def test_evidence_of_this_run_passes(tmp_path) -> None:
    good_evidence(tmp_path / "ns", "verify-cicd-x")
    assert verify_evidence(tmp_path / "ns", "verify-cicd-x", SHA) == []


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r["identity"].update(run_id="verify-older-run"), "run id"),
    (lambda r: r["identity"].update(git_sha="e" * 40), "release SHA"),
    (lambda r: r["direct_offline_online_comparison"].update(mismatch_count=1), "parity"),
    (lambda r: r["direct_offline_online_comparison"].update(comparisons=0), "parity"),
    (lambda r: r["offline_verification"].update(rows_retrieved=0), "offline store"),
    (lambda r: r.update(namespace="verify-other"), "namespace"),
])
def test_evidence_that_does_not_prove_this_run_fails(tmp_path, mutate, message) -> None:  # type: ignore[no-untyped-def]
    good_evidence(tmp_path / "ns", "verify-cicd-x")
    path = tmp_path / "ns" / "consistency-report.json"
    report = json.loads(path.read_text())
    mutate(report)
    path.write_text(json.dumps(report))
    assert any(message in item for item in verify_evidence(tmp_path / "ns", "verify-cicd-x", SHA))


def test_a_failed_flink_job_or_missing_report_fails(tmp_path) -> None:
    good_evidence(tmp_path / "ns", "verify-cicd-x")
    (tmp_path / "ns" / "flink-jobs.json").write_text(json.dumps({"jobs": [{"state": "FAILED"}]}))
    (tmp_path / "ns" / "consistency-verdict.json").unlink()
    problems = verify_evidence(tmp_path / "ns", "verify-cicd-x", SHA)
    assert any("not healthy" in item for item in problems) and any("missing" in item for item in problems)


def test_a_failed_step_stops_the_campaign_and_still_cleans_up(tmp_path) -> None:
    item = campaign(tmp_path)
    calls: list[list[str]] = []

    def run(argv, env, cwd):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return 1 if argv[1:2] == ["run"] else 0

    report = run_campaign(item, git_sha=SHA, evidence=tmp_path, execute=True, run=run)
    assert report["verdict"] == "FAIL" and report["failure"] == "step run exited 1"
    assert [call[1] for call in calls if call[0] == OPERATOR] == ["render", "preflight", "deploy", "run", "cleanup"]
    assert report["production_changed"] is False


def test_a_full_run_passes_only_with_its_own_evidence_and_a_clean_cleanup(tmp_path) -> None:
    item = campaign(tmp_path)
    good_evidence(tmp_path / item.namespace, item.namespace)
    report = run_campaign(item, git_sha=SHA, evidence=tmp_path, execute=True, run=lambda a, e, c: 0)
    assert report["verdict"] == "PASS"
    failing_cleanup = run_campaign(item, git_sha=SHA, evidence=tmp_path, execute=True,
                                   run=lambda a, e, c: 1 if a[1:2] == ["cleanup"] else 0)
    assert failing_cleanup["verdict"] == "FAIL"


def test_a_dry_run_never_executes_cleanup(tmp_path) -> None:
    calls: list[list[str]] = []
    report = run_campaign(campaign(tmp_path), git_sha=SHA, evidence=tmp_path, execute=False,
                          run=lambda a, e, c: calls.append(list(a)) or 0)
    assert report["verdict"] == "DRY_RUN"
    assert not [call for call in calls if "--execute" in call or call[1:2] == ["cleanup"]]
