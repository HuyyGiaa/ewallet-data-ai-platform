"""Dispatch a verified release plan: one typed executor per release-unit kind.

``scripts/ci/deploy_release.sh`` is a thin wrapper around this module. Before anything runs the
plan is re-verified against the catalog, the release units, the checked-out SHA and the image
manifest bytes (``release_policy.verify_release_plan``). The executor comes from the unit's kind,
never from a path in the plan, so a stale or edited plan cannot reach an arbitrary script.

Rehearsal units run in order under their resource locks; the first failure stops the release
(each rehearsal executor restores or parks its own workload). Production plans go to
``scripts/ci/production_release.py`` (candidate SandboxAgents, Helm image promotions, the
SandboxAgent fleet patch), which rolls every changed unit back in reverse order on failure and
reconciles the runtime verification with the transaction. The release verdict states the stage
and whether production changed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence

from scripts.ci.catalog import CatalogError, load_catalog
from scripts.ci.release_policy import ReleasePolicyError, verify_release_plan
from scripts.ci.release_units import load_release_units

ROOT = Path(__file__).resolve().parents[2]
TOOLING = ["uv", "run", "--locked", "--project", "infra/docker/llm-agent-tooling", "python", "-m"]
STREAMING = ["uv", "run", "--locked", "--project", "infra/docker/streaming-sinks", "python", "-m"]
Runner = Callable[[Sequence[str], Mapping[str, str]], int]


class DispatchError(RuntimeError):
    """The plan cannot be dispatched."""


@dataclass
class UnitOutcome:
    name: str
    kind: str
    commands: list[list[str]]
    exit_codes: list[int]
    status: str


def _image_args(unit: Mapping) -> list[str]:
    args: list[str] = []
    for name, reference in sorted(unit["images"].items()):
        args += ["--image", f"{name}={reference}"]
    return args


def commands_for(unit: Mapping, plan: Mapping, evidence: Path, *, prompt_registry: str = "",
                 execute_streaming: bool = True, streaming_context: str = "<streaming-identity-context>") -> list[list[str]]:
    """The commands a rehearsal unit runs, by kind (a closed set)."""

    common = ["--unit", unit["name"], "--git-sha", plan["git_sha"], "--evidence-dir", str(evidence)]
    kind = unit["kind"]
    if kind == "rag-rehearsal":
        return [["scripts/ci/deploy/rag_pipeline.sh", *common, *_image_args(unit)]]
    if kind == "bounded-rehearsal":
        return [[*TOOLING, "scripts.ci.bounded_cd", "--contract", unit["contract"], *common, *_image_args(unit)]]
    if kind == "registry-gate":
        return [[*TOOLING, "scripts.ci.registry_gate", *common, "--registry", prompt_registry]]
    if kind == "streaming-verification":
        smoke_image = {"flink-block-a": unit["images"]["flink-block-a"]}
        smoke = [*TOOLING, "scripts.ci.bounded_cd", "--contract", unit["contract"], "--unit", unit["name"],
                 "--git-sha", plan["git_sha"], "--evidence-dir", str(evidence / "image-smoke"),
                 *_image_args({"images": smoke_image})]
        campaign = [*STREAMING, "scripts.ci.streaming_verify", *common, *_image_args(unit),
                    # The campaign runs as the dedicated streaming identity (streaming_identity.py), never as
                    # the plan's in-cluster Jenkins context.
                    "--kube-context", streaming_context, *(["--execute"] if execute_streaming else [])]
        return [smoke, campaign]
    raise DispatchError(f"release unit {unit['name']} of kind {kind} is not a rehearsal executor")


def _run(argv: Sequence[str], env: Mapping[str, str]) -> int:
    return subprocess.run(list(argv), cwd=ROOT, env={**os.environ, **env}, check=False).returncode


def _streaming_identity(workdir: Path):  # type: ignore[no-untyped-def]
    from scripts.ci.streaming_identity import resolve

    return resolve(workdir)


def dispatch_rehearsal(plan: Mapping, evidence_root: Path, *, run: Runner = _run, locks=None,
                       prompt_registry: str = "", execute_streaming: bool = True,
                       streaming_identity: Callable[[Path], object] = _streaming_identity) -> dict:
    outcomes: list[UnitOutcome] = []
    failure = ""
    lock_names = sorted({unit["lock"] for unit in plan["units"]})

    def body() -> None:
        nonlocal failure
        for unit in plan["units"]:
            evidence = evidence_root / unit["name"]
            evidence.mkdir(parents=True, exist_ok=True)
            env = {"RELEASE_UNIT": unit["name"]}
            private = None
            context = "<streaming-identity-context>"
            if unit["kind"] == "streaming-verification":
                # The token-bearing kubeconfig lives outside the archived evidence and is removed after the unit.
                private = Path(tempfile.mkdtemp(prefix="streaming-identity."))
                try:
                    identity = streaming_identity(private)
                except Exception as error:  # StreamingIdentityError and API failures: the unit is blocked
                    shutil.rmtree(private, ignore_errors=True)
                    (evidence / "blocked.txt").write_text(f"{error}\n", encoding="utf-8")
                    outcomes.append(UnitOutcome(unit["name"], unit["kind"], [], [], "BLOCKED"))
                    failure = f"release unit {unit['name']} is blocked: {error}"
                    return
                env.update(identity.environment())
                context = identity.context
            commands = commands_for(unit, plan, evidence, prompt_registry=prompt_registry,
                                    execute_streaming=execute_streaming, streaming_context=context)
            codes = []
            try:
                for command in commands:
                    codes.append(run(command, env))
                    if codes[-1] != 0:
                        break
            finally:
                if private is not None:
                    shutil.rmtree(private, ignore_errors=True)
            status = "PASS" if len(codes) == len(commands) and all(code == 0 for code in codes) else "FAIL"
            outcomes.append(UnitOutcome(unit["name"], unit["kind"], commands, codes, status))
            if status != "PASS":
                failure = f"release unit {unit['name']} failed; later units were not deployed"
                return

    if locks is None:
        body()
    else:
        with locks.hold(lock_names):
            body()
    not_run = [unit["name"] for unit in plan["units"][len(outcomes):]]
    return {
        "verdict": "PASS" if not failure and not not_run else "FAIL",
        "stage": "rehearsal", "production_changed": False, "git_sha": plan["git_sha"],
        "release_branch": plan["release_branch"], "locks": lock_names, "failure": failure or None,
        "units": [outcome.__dict__ for outcome in outcomes], "not_run": not_run,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def _locks(namespace: str, holder: str, api: str | None):
    from scripts.ci.k8s_api import Client
    from scripts.ci.release_lock import KubeLeaseApi, ReleaseLocks

    return ReleaseLocks(KubeLeaseApi(Client.from_environment(api), namespace), namespace, holder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="without it, only verify and print the commands")
    parser.add_argument("--catalog", type=Path, default=ROOT / "jenkins/catalog/components.json")
    parser.add_argument("--units", type=Path, default=ROOT / "jenkins/catalog/release-units.json")
    parser.add_argument("--api", default=os.environ.get("K8S_API"), help="local API proxy for locks (dry runs)")
    parser.add_argument("--production-context", default="", help="production promotion only")
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        catalog.validate(ROOT)
        units = load_release_units(args.units)
        units.validate(ROOT, catalog)
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        manifest = Path(plan.get("image_manifest_path", ""))
        verify_release_plan(catalog, units, plan, head_sha=_git_head(),
                            manifest_path=manifest if manifest.is_absolute() else ROOT / manifest)
    except (CatalogError, ReleasePolicyError, OSError, ValueError) as error:
        print(f"release_dispatch: refused: {error}", flush=True)
        return 2
    evidence_root = Path(plan["evidence_dir"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    holder = os.environ.get("BUILD_TAG") or f"local-{os.getpid()}"
    if plan["stage"] == "production":
        from scripts.ci import production_release, runtime_verify

        if not args.execute:
            print("DRY RUN: the production transaction runs with --execute only (scripts/ci/production_release.py)")
            return 0
        context = args.production_context
        try:
            # Refuse before any mutation when the post-promotion verification could not reach the same
            # cluster: discovering that after a promotion would force a rollback.
            runtime_verify.guard_target(os.environ, context)
            if not context or context == "in-cluster":
                raise production_release.ProductionReleaseError("an explicit production kube context is required")
        except (runtime_verify.VerificationError, production_release.ProductionReleaseError) as error:
            print(f"release_dispatch: refused before any deployment: {error}", flush=True)
            return 2
        manifest_data = json.loads((manifest if manifest.is_absolute() else ROOT / manifest).read_text(encoding="utf-8"))

        def verify(expected) -> bool:
            result = runtime_verify.run_checks(runtime_verify.checks_for(plan["units"]), evidence_root / "runtime",
                                               expected=expected, production_context=context)
            (evidence_root / "runtime-verdict.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                                                 encoding="utf-8")
            return result["verdict"] == "PASS"

        from scripts.ci.k8s_api import Client, KubeError
        from scripts.ci.release_lock import LockError
        from scripts.ci.sandbox_candidate import ClientKube

        try:
            # One identity for the whole transaction: the locks, the SandboxAgent units and Helm all go
            # through the explicit production context (an operator workstation has no in-cluster token).
            with production_release.kubectl_proxy(context) as api:
                locks = _locks(units.lock_namespace, holder, args.api or api)
                with locks.hold(sorted({unit["lock"] for unit in plan["units"]})):
                    report = production_release.run(plan, context, manifest_data, execute=True,
                                                    kube=ClientKube(Client(api)), verify=verify,
                                                    evidence=evidence_root / "units")
        except (KubeError, LockError, production_release.ProductionReleaseError) as error:
            print(f"release_dispatch: refused before any deployment: {error}", flush=True)
            return 2
    else:
        if not args.execute:
            for unit in plan["units"]:
                for command in commands_for(unit, plan, evidence_root / unit["name"]):
                    print("DRY RUN:", unit["name"], " ".join(command))
            return 0
        from scripts.ci.k8s_api import KubeError
        from scripts.ci.release_lock import LockError

        try:
            report = dispatch_rehearsal(plan, evidence_root, locks=_locks(units.lock_namespace, holder, args.api),
                                        prompt_registry=os.environ.get("PROMPT_REGISTRY_URL", ""))
        except (KubeError, LockError) as error:
            # No cluster access or a held lock: nothing was deployed; say so plainly.
            print(f"release_dispatch: refused before any deployment: release locks unavailable: {error}", flush=True)
            return 2
    (evidence_root / "release-verdict.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                                        encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("verdict", "stage", "production_changed", "failure")},
                     sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
