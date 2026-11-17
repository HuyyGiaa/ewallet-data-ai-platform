"""Isolated Block A verification campaign for the ``streaming-verification`` release unit (row 36).

The streaming runtime is frozen and already has a reviewed, fail-closed operator
(``scripts/streaming/block_a_cloud.sh``: verify-* namespace guard, storage-binding validation,
claimed non-production Redis database, create-only registry, campaign-labelled resources). This
driver does not re-implement it. It runs that operator for one build, in the documented order
(``docs/runbooks/edai2/FEATURE_STORE_RUNBOOK.md`` section 4):

    render -> preflight (claims the rehearsal-stream Redis DB) -> registry apply (create-only)
    -> deploy (Redpanda, Flink JobManager/TaskManager) -> run (topics, Flink job, sink Jobs)
    -> collect (Flink REST state, validator Job, consistency report) -> cleanup (always)

and then checks the evidence itself, so a PASS proves *this* run wrote both stores:

* the consistency report and the operator's own ``check-report`` verdict pass;
* the report's namespace, run id and campaign id are this build's namespace, and its Git SHA is
  the release SHA (pre-existing GCS objects or Redis keys of another run cannot satisfy this:
  every path and key prefix is derived from the namespace, which is new per build);
* Offline verification, source-event parity and the direct Offline/Online comparison pass with a
  non-zero number of comparisons and no mismatch;
* the Flink JobManager reports at least one job and none failed.

Images are the release digests of ``flink-block-a`` and ``streaming-sinks``, both built from the
release SHA. Owner-supplied, non-secret campaign values come from the environment (see the
profile's ``owner_parameters``). Nothing runs without ``--execute``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = "scripts/streaming/block_a_cloud.sh"
REGISTRY_JOB = "scripts/mlops/feast_online_job.sh"
NAMESPACE = re.compile(r"^verify-[a-z0-9][a-z0-9-]{0,46}$")
DIGEST = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
OWNER_VALUE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
Runner = Callable[[Sequence[str], Mapping[str, str], Path], int]


class StreamingConfigError(ValueError):
    """The campaign cannot be configured safely."""


@dataclass
class Step:
    name: str
    argv: list[str]
    exit_code: int | None = None


@dataclass
class Campaign:
    namespace: str
    environment: dict[str, str]
    steps: list[Step] = field(default_factory=list)


def campaign_namespace(prefix: str, git_sha: str, build: str) -> str:
    build_id = re.sub(r"[^a-z0-9-]", "-", build.lower()).strip("-") or "local"
    namespace = f"{prefix}{git_sha[:12]}-{build_id}"[:53].rstrip("-")
    if not NAMESPACE.fullmatch(namespace):
        raise StreamingConfigError(f"derived namespace {namespace} is not a verify-* namespace")
    return namespace


def configure(profile: Mapping, *, flink_image: str, sinks_image: str, git_sha: str, build: str,
              owner: Mapping[str, str], evidence: Path, kube_context: str) -> Campaign:
    """Every value the operator needs, derived from the profile, the release and the namespace."""

    if not SHA.fullmatch(git_sha):
        raise StreamingConfigError("release SHA must be a full SHA")
    for label, image in (("flink-block-a", flink_image), ("streaming-sinks", sinks_image)):
        if not DIGEST.fullmatch(image):
            raise StreamingConfigError(f"{label} image must be pinned by digest")
    if flink_image == sinks_image:
        raise StreamingConfigError("the Flink and sink images must be the two distinct release artifacts")
    missing = [name for name in profile["owner_parameters"] if not owner.get(name, "").strip()]
    if missing:
        raise StreamingConfigError(f"owner campaign parameters are missing: {missing}")
    bucket = str(profile.get("bucket", ""))
    if not BUCKET.fullmatch(bucket):
        raise StreamingConfigError("the profile bucket must be a bucket name, not a path")
    for name in ("FEAST_SNAPSHOT_ID", "FEATURE_RUN_ID"):
        if not OWNER_VALUE.fullmatch(owner[name].strip()):
            raise StreamingConfigError(f"{name} is not a valid identifier")
    if profile["online_store_id"] != "rehearsal-stream" or profile["dataset_version"] != "rehearsal-5gb-v1":
        raise StreamingConfigError("the CD campaign must use the rehearsal-stream verification Online Store")
    if not kube_context or kube_context == "in-cluster":
        # The frozen operator runs kubectl --context; the Jenkins pod has no kubeconfig and its
        # in-cluster identity has no ml-platform/feature-store rights (runbook prerequisite P6).
        raise StreamingConfigError("an explicit kubeconfig context of the approved streaming identity is required "
                                   "(not in-cluster; runbook prerequisite P6)")
    namespace = campaign_namespace(profile["namespace_prefix"], git_sha, build)
    values = {"bucket": bucket, "namespace": namespace, "dataset_version": profile["dataset_version"]}
    paths = {name: template.format(**values) for name, template in profile["paths"].items()}
    for name, path in paths.items():
        if name != "OFFLINE_FEATURE_ROOT" and namespace not in path:
            raise StreamingConfigError(f"{name} is not scoped to this campaign")
    environment = {
        **paths,
        "FLINK_IMAGE": flink_image, "SINKS_IMAGE": sinks_image,
        "FLINK_IMAGE_SOURCE_SHA": git_sha, "SINKS_IMAGE_SOURCE_SHA": git_sha, "APPROVED_SHA": git_sha,
        "ONLINE_STORE_ID": profile["online_store_id"], "FEAST_REDIS_SECRET": profile["feast_redis_secret"],
        "DATASET_VERSION": profile["dataset_version"], "FEAST_SNAPSHOT_ID": owner["FEAST_SNAPSHOT_ID"].strip(),
        "FEATURE_RUN_ID": owner["FEATURE_RUN_ID"].strip(),
        "BLOCK_A_ESTIMATED_REDIS_BYTES": str(int(profile["estimated_redis_bytes"])),
        "BLOCK_A_EVIDENCE_ROOT": str(evidence), "BLOCK_A_PREFLIGHT_ROOT": str(evidence / "preflight"),
        "KUBE_CONTEXT": kube_context,
        # feast_online_job.sh (registry apply) reads these names.
        "FEAST_ONLINE_IMAGE": sinks_image, "EXECUTION_POLICY": "verification", "REDIS_CAMPAIGN_ID": namespace,
        "SNAPSHOT_ID": owner["FEAST_SNAPSHOT_ID"].strip(), "ESTIMATED_REDIS_BYTES": str(int(profile["estimated_redis_bytes"])),
        "FEAST_ONLINE_GIT_SHA": git_sha,
    }
    return Campaign(namespace, environment)


def plan_steps(campaign: Campaign, execute: bool) -> list[Step]:
    flag = ["--execute"] if execute else []
    ns = campaign.namespace
    registry_run = f"fs-apply-{ns[len('verify-'):]}"[:50].rstrip("-")
    steps = [
        Step("render", [OPERATOR, "render", ns]),
        Step("preflight", [OPERATOR, "preflight", ns, *flag, *(["--approve-redis-claim"] if execute else [])]),
        Step("registry", [REGISTRY_JOB, registry_run, *flag, "--", "apply"]),
        Step("deploy", [OPERATOR, "deploy", ns, *flag]),
        Step("run", [OPERATOR, "run", ns, *flag]),
        Step("collect", [OPERATOR, "collect", ns, *flag]),
    ]
    return steps


def verify_evidence(run_dir: Path, namespace: str, git_sha: str) -> list[str]:
    """Every reason the collected evidence does not prove this run wrote both stores."""

    problems: list[str] = []

    def load(name: str) -> dict | list | None:
        path = run_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{name} is missing or not JSON")
            return None

    verdict = load("consistency-verdict.json")
    if isinstance(verdict, dict) and verdict.get("status") != "PASS":
        problems.append("the operator's check-report verdict is not PASS")
    report = load("consistency-report.json")
    if isinstance(report, dict):
        identity = report.get("identity") or {}
        offline = report.get("offline_verification") or {}
        direct = report.get("direct_offline_online_comparison") or {}
        source = report.get("source_event_verification") or {}
        checks = {
            "report status PASS": report.get("status") == "PASS",
            "report namespace is this run": report.get("namespace") == namespace,
            "report run id is this run": identity.get("run_id") == namespace,
            "report campaign id is this run": identity.get("campaign_id") == namespace,
            "report written by the release SHA": identity.get("git_sha") == git_sha,
            "offline store verified for this run": offline.get("status") == "PASS" and offline.get("run_id") == namespace
            and int(offline.get("rows_retrieved") or 0) > 0,
            "source events reached the online store": source.get("status") == "PASS"
            and int(source.get("comparisons") or 0) > 0,
            "offline/online parity": direct.get("status") == "PASS" and int(direct.get("comparisons") or 0) > 0
            and int(direct.get("mismatch_count", -1)) == 0,
            "features compared": int(report.get("features_compared") or 0) > 0,
        }
        problems += [f"failed: {name}" for name, ok in checks.items() if not ok]
    jobs = load("flink-jobs.json")
    if isinstance(jobs, dict):
        states = [str(job.get("state")) for job in jobs.get("jobs", [])]
        if not states:
            problems.append("the Flink JobManager reports no job")
        elif any(state in {"FAILED", "FAILING", "CANCELED"} for state in states):
            problems.append(f"a Flink job is not healthy: {states}")
    return problems


def _run(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    return subprocess.run(list(argv), env={**os.environ, **env}, cwd=cwd, check=False).returncode


def run_campaign(campaign: Campaign, *, git_sha: str, evidence: Path, execute: bool, run: Runner = _run,
                 root: Path = ROOT) -> dict:
    steps = plan_steps(campaign, execute)
    failure = ""
    for step in steps:
        step.exit_code = run(step.argv, campaign.environment, root)
        if step.exit_code != 0:
            failure = f"step {step.name} exited {step.exit_code}"
            break
    problems: list[str] = []
    if execute and not failure:
        problems = verify_evidence(evidence / campaign.namespace, campaign.namespace, git_sha)
    cleanup = Step("cleanup", [OPERATOR, "cleanup", campaign.namespace, *(["--execute"] if execute else [])])
    if execute and any(step.exit_code is not None for step in steps[1:]):
        cleanup.exit_code = run(cleanup.argv, campaign.environment, root)
    steps.append(cleanup)
    if not execute:
        verdict = "DRY_RUN" if not failure else "FAIL"
    else:
        verdict = "PASS" if not failure and not problems and cleanup.exit_code == 0 else "FAIL"
    return {
        "unit": "streaming-block-a", "verdict": verdict, "environment": "rehearsal",
        "namespace": campaign.namespace, "release_sha": git_sha, "production_changed": False,
        "steps": [{"name": step.name, "argv": step.argv, "exit_code": step.exit_code} for step in steps],
        "failure": failure or None, "evidence_problems": problems,
        "isolation": {key: campaign.environment[key] for key in
                      ("ONLINE_STORE_ID", "FEAST_REDIS_SECRET", "OFFLINE_FEATURE_ROOT", "FEAST_OFFLINE_DATA_ROOT",
                       "HISTORY_ROOT", "ONLINE_REGISTRY_URI")},
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=ROOT / "jenkins/cd/streaming-block-a.profile.json")
    parser.add_argument("--image", action="append", required=True, help="flink-block-a=REF and streaming-sinks=REF")
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--build", default=os.environ.get("BUILD_NUMBER", "local"))
    parser.add_argument("--kube-context", default=os.environ.get("KUBE_CONTEXT", ""))
    parser.add_argument("--unit", default="streaming-block-a")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    images = dict(item.split("=", 1) for item in args.image if "=" in item)
    try:
        campaign = configure(
            json.loads(args.profile.read_text(encoding="utf-8")),
            flink_image=images.get("flink-block-a", ""), sinks_image=images.get("streaming-sinks", ""),
            git_sha=args.git_sha, build=args.build, owner=os.environ, evidence=args.evidence_dir.resolve(),
            kube_context=args.kube_context)
    except (StreamingConfigError, KeyError) as error:
        parser.error(str(error))
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    report = run_campaign(campaign, git_sha=args.git_sha, evidence=args.evidence_dir.resolve(), execute=args.execute)
    (args.evidence_dir / "verdict.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("unit", "verdict", "namespace", "failure")}, sort_keys=True))
    return 0 if report["verdict"] in {"PASS", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
