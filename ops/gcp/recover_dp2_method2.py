"""Audited recovery operator for one existing DP2 Method-2 Airflow DagRun.

The operator separates the immutable execution Git SHA from the committed
collector/controller SHA. Its default ``status`` mode is read-only. Recovery
finishes missing local evidence and Silver winner artifacts without launching
Spark. Airflow reconciliation uses only the public API and never edits its DB.
Paid Gold work remains behind a distinct authorization and budget gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from data_platform.config.spark_benchmark import load_pair_manifest
from ops.gcp.dp2_method2 import _action, _winner

RECOVERY_SCHEMA = "dp2-method2-recovery-v1"
SILVER_RECOVERED_TASKS = (
    "silver_optimized_evidence",
    "validate_and_select_silver_winner",
)
GOLD_TASKS = (
    "prepare_gold_pair_from_silver_winner",
    "gold_baseline_quota", "gold_baseline_submit", "gold_baseline_verify",
    "gold_optimized_quota", "gold_optimized_submit", "gold_optimized_verify",
    "gold_baseline_semantic_scan", "gold_optimized_semantic_scan",
    "gold_baseline_evidence", "gold_optimized_evidence",
    "validate_and_select_gold_winner",
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _write_once(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(dict(value), stream, indent=2, sort_keys=True)
        stream.write("\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name not in {
            "recovery-audit.json", "airflow-reconciliation.json",
            "airflow-safety-hold.json",
        }
        and path.relative_to(root).parts[0] != "gold"
        and not path.relative_to(root).name.startswith("dp2-gold-")
    }


def _git_identity(repo: Path, expected: str, *, clean: bool) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if head != expected:
        raise ValueError(f"Git HEAD mismatch for {repo}")
    if clean:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout
        if status:
            raise ValueError(f"Git worktree is not clean: {repo}")


def _config(path: Path) -> dict[str, Any]:
    config = _load(path)
    required = {
        "dag_id", "dag_run_id", "benchmark_id", "execution_git_sha",
        "controller_git_sha", "canonical_repo", "evidence_root",
        "bronze_winner", "dataset_version", "data_bucket", "project", "region",
        "baseline_silver_run_id", "optimized_silver_run_id",
        "baseline_gold_run_id", "optimized_gold_run_id", "airflow_url",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError(f"Recovery config is missing fields: {missing}")
    if config["dataset_version"] != "canonical-100gb-v1":
        raise ValueError("Recovery dataset_version is not canonical-100gb-v1")
    if not str(config["dag_run_id"]).startswith("dp2-method2-"):
        raise ValueError("Recovery must target the existing DP2 Method-2 DagRun")
    return config


def _dp2_args(config: Mapping[str, Any], *, action: str,
              stage: str | None = None, profile: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        action=action, root=Path(str(config["evidence_root"])),
        bronze_winner=Path(str(config["bronze_winner"])),
        benchmark_id=str(config["benchmark_id"]),
        dataset_version=str(config["dataset_version"]),
        git_sha=str(config["execution_git_sha"]),
        data_bucket=str(config["data_bucket"]), project=str(config["project"]),
        region=str(config["region"]), ttl="8h", approval="",
        require_clean=False, stage=stage, profile=profile,
        baseline_silver_run_id=str(config["baseline_silver_run_id"]),
        optimized_silver_run_id=str(config["optimized_silver_run_id"]),
        baseline_gold_run_id=str(config["baseline_gold_run_id"]),
        optimized_gold_run_id=str(config["optimized_gold_run_id"]),
        baseline_gold_batch_id=config.get("baseline_gold_batch_id"),
        optimized_gold_batch_id=config.get("optimized_gold_batch_id"),
        recovery_git_sha=config.get("controller_git_sha"),
        compute_repository=Path(str(config["canonical_repo"])),
    )


def _request(config: Mapping[str, Any], method: str, path: str,
             token: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
    url = str(config["airflow_url"]).rstrip("/") + path
    data = None if body is None else json.dumps(dict(body)).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Airflow API {method} {path} failed: {exc.code} {detail}") from exc
    if not isinstance(value, dict):
        raise ValueError("Airflow API returned a non-object response")
    return value


def _run_path(config: Mapping[str, Any]) -> str:
    dag = urllib.parse.quote(str(config["dag_id"]), safe="")
    run = urllib.parse.quote(str(config["dag_run_id"]), safe="")
    return f"/api/v2/dags/{dag}/dagRuns/{run}"


def _set_dag_paused(config: Mapping[str, Any], token: str, paused: bool) -> dict[str, Any]:
    dag = urllib.parse.quote(str(config["dag_id"]), safe="")
    return _request(config, "PATCH", f"/api/v2/dags/{dag}", token,
                    {"is_paused": paused})


def airflow_status(config: Mapping[str, Any], token: str) -> dict[str, Any]:
    run_path = _run_path(config)
    dag_run = _request(config, "GET", run_path, token)
    tasks = _request(config, "GET", run_path + "/taskInstances?limit=100", token)
    items = tasks.get("task_instances")
    if not isinstance(items, list):
        raise ValueError("Airflow task-instance response is malformed")
    task_states = {str(item["task_id"]): item.get("state") for item in items}
    conf = dag_run.get("conf")
    if not isinstance(conf, Mapping) or conf.get("git_sha") != config["execution_git_sha"]:
        raise ValueError("Airflow DagRun is not bound to the original execution Git SHA")
    return {"dag_run_state": dag_run.get("state"), "task_states": task_states}


def _artifacts_complete(config: Mapping[str, Any]) -> None:
    root = Path(str(config["evidence_root"]))
    optimized = root / "silver" / "optimized"
    required = (
        optimized / "summary.json", optimized / "original-batch.json",
        optimized / "original-performance.json", optimized / "validation-evidence.json",
        optimized / "batch-evidence.json", root / "dp2-silver-equivalence.json",
        root / "dp2-silver-comparison.json", root / "dp2-silver-comparison.csv",
        root / "dp2-silver-winner.json", root / "recovery-audit.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"Recovered Silver artifacts are incomplete: {missing}")
    _winner(root / "dp2-silver-winner.json", stage="dp2-silver",
            benchmark=str(config["benchmark_id"]), bucket=str(config["data_bucket"]),
            expected_git_sha=str(config["execution_git_sha"]))


def recover_silver(config: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(str(config["evidence_root"]))
    audit_path = root / "recovery-audit.json"
    if audit_path.exists():
        audit = _load(audit_path)
        expected_identity = {
            "schema_version": RECOVERY_SCHEMA,
            "status": "SILVER_RECOVERED",
            "dag_id": config["dag_id"], "dag_run_id": config["dag_run_id"],
            "benchmark_id": config["benchmark_id"],
            "execution_git_sha": config["execution_git_sha"],
            "controller_git_sha": config["controller_git_sha"],
        }
        if any(audit.get(key) != value for key, value in expected_identity.items()):
            raise ValueError("Existing recovery audit identity mismatch")
        if audit.get("completed_artifact_sha256") != _inventory(root):
            raise ValueError("Recovered artifact inventory changed after audit")
        _artifacts_complete(config)
        return audit
    before = _inventory(root)
    _action(_dp2_args(config, action="evidence", stage="silver", profile="optimized"))
    final_paths = (
        root / "dp2-silver-equivalence.json", root / "dp2-silver-comparison.json",
        root / "dp2-silver-comparison.csv", root / "dp2-silver-winner.json",
    )
    existing = [path.exists() for path in final_paths]
    if any(existing) and not all(existing):
        raise ValueError("Interrupted Silver finalization artifacts require manual review")
    if not all(existing):
        _action(_dp2_args(config, action="finalize", stage="silver"))
    _winner(final_paths[-1], stage="dp2-silver", benchmark=str(config["benchmark_id"]),
            bucket=str(config["data_bucket"]),
            expected_git_sha=str(config["execution_git_sha"]))
    after = _inventory(root)
    mutated = sorted(name for name, digest in before.items() if after.get(name) != digest)
    if mutated:
        raise ValueError(f"Recovery changed pre-existing artifacts: {mutated}")
    performance = _load(root / "silver" / "optimized" / "original-performance.json")
    audit = {
        "schema_version": RECOVERY_SCHEMA, "status": "SILVER_RECOVERED",
        "dag_id": config["dag_id"], "dag_run_id": config["dag_run_id"],
        "benchmark_id": config["benchmark_id"],
        "execution_git_sha": config["execution_git_sha"],
        "controller_git_sha": config["controller_git_sha"],
        "preexisting_artifact_sha256": before,
        "completed_artifact_sha256": after,
        "selected_original_application": {
            key: performance[key] for key in (
                "application_id", "application_name", "dataproc_cohort_id",
                "application_start_epoch_ms", "application_end_epoch_ms",
                "event_log_source_uri", "original_performance_digest",
            )
        },
    }
    _write_once(audit_path, audit)
    _artifacts_complete(config)
    return audit


def reconcile_silver(config: Mapping[str, Any], token: str) -> dict[str, Any]:
    _artifacts_complete(config)
    path = Path(str(config["evidence_root"])) / "airflow-reconciliation.json"
    if path.exists():
        evidence = _load(path)
        expected = {
            "schema_version": RECOVERY_SCHEMA,
            "status": "SILVER_AIRFLOW_RECONCILED_GOLD_BLOCKED",
            "dag_id": config["dag_id"], "dag_run_id": config["dag_run_id"],
            "execution_git_sha": config["execution_git_sha"],
            "controller_git_sha": config["controller_git_sha"],
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing Airflow reconciliation identity mismatch")
        current = airflow_status(config, token)
        if any(current["task_states"].get(task) != "success"
               for task in SILVER_RECOVERED_TASKS):
            raise ValueError("Airflow Silver state regressed after reconciliation")
        if any(current["task_states"].get(task) == "success" for task in GOLD_TASKS):
            raise ValueError("Gold is no longer blocked at the Silver recovery milestone")
        return evidence
    # Airflow 3 may automatically queue a failed DagRun and clear all downstream
    # upstream_failed states when a recovered task is patched to success. Pause
    # the DAG first so that cascade can never launch the paid Gold chain.
    _set_dag_paused(config, token, True)
    before = airflow_status(config, token)
    states = before["task_states"]
    allowed = {
        "silver_optimized_evidence": {"failed", "success"},
        "validate_and_select_silver_winner": {"upstream_failed", "success"},
    }
    for task_id, valid_states in allowed.items():
        if states.get(task_id) not in valid_states:
            raise ValueError(f"Unexpected Airflow state for {task_id}: {states.get(task_id)!r}")
    run_path = _run_path(config)
    note = ("Externally recovered from immutable artifacts; execution SHA "
            f"{config['execution_git_sha']}; controller SHA {config['controller_git_sha']}")
    for task_id in SILVER_RECOVERED_TASKS:
        if states.get(task_id) != "success":
            task = urllib.parse.quote(task_id, safe="")
            _request(config, "PATCH", run_path + f"/taskInstances/{task}", token,
                     {"new_state": "success", "note": note})
    after = airflow_status(config, token)
    if any(after["task_states"].get(task) != "success" for task in SILVER_RECOVERED_TASKS):
        raise ValueError("Airflow did not persist recovered Silver task states")
    unsafe_gold = {"running", "queued", "scheduled", "success"}
    if any(after["task_states"].get(task) in unsafe_gold for task in GOLD_TASKS):
        raise ValueError("Unexpected Gold activity during Silver-only reconciliation")
    if after["dag_run_state"] != "failed":
        _request(config, "PATCH", _run_path(config), token,
                 {"state": "failed", "note": "Paid Gold safety hold after Silver recovery"})
        after = airflow_status(config, token)
    if after["dag_run_state"] != "failed":
        raise ValueError("Airflow DagRun did not enter the paid Gold safety hold")
    evidence = {
        "schema_version": RECOVERY_SCHEMA,
        "status": "SILVER_AIRFLOW_RECONCILED_GOLD_BLOCKED",
        "dag_id": config["dag_id"], "dag_run_id": config["dag_run_id"],
        "execution_git_sha": config["execution_git_sha"],
        "controller_git_sha": config["controller_git_sha"],
        "states_before": before, "states_after": after,
    }
    _write_once(path, evidence)
    return evidence


def _gold_outputs_absent(config: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "cancelled_baseline_gold_batch_id", "baseline_gold_batch_id",
        "optimized_gold_batch_id",
    )
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ValueError(f"Gold recovery config is missing: {missing}")
    cancelled = str(config["cancelled_baseline_gold_batch_id"])
    if cancelled != str(config["baseline_gold_run_id"]):
        raise ValueError("Cancelled baseline identity must equal original output run ID")
    cancelled_result = subprocess.run(
        ["gcloud", "dataproc", "batches", "describe", cancelled,
         f"--project={config['project']}", f"--region={config['region']}",
         "--format=json"], capture_output=True, text=True,
    )
    if cancelled_result.returncode != 0:
        raise RuntimeError("Cannot verify the cancelled original Gold batch")
    cancelled_description = json.loads(cancelled_result.stdout)
    if cancelled_description.get("state") != "CANCELLED":
        raise ValueError("Original Gold batch is not the trusted CANCELLED record")
    for profile in ("baseline", "optimized"):
        run_id = str(config[f"{profile}_gold_run_id"])
        batch_id = str(config[f"{profile}_gold_batch_id"])
        if batch_id == run_id or batch_id == cancelled:
            raise ValueError("Recovery Dataproc batch ID is not distinct")
        result = subprocess.run(
            ["gcloud", "dataproc", "batches", "describe", batch_id,
             f"--project={config['project']}", f"--region={config['region']}", "--format=json"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            raise ValueError(f"Gold recovery attempt already exists: {batch_id}")
        if "NOT_FOUND" not in (result.stderr + result.stdout).upper():
            raise RuntimeError(f"Cannot verify Gold batch absence: {batch_id}")
        uri = f"gs://{config['data_bucket']}/lakehouse/runs/{run_id}/gold/**"
        listing = subprocess.run(
            ["gcloud", "storage", "ls", uri], capture_output=True, text=True,
        )
        if listing.returncode == 0 and listing.stdout.strip():
            raise ValueError(f"Immutable Gold output already exists: {run_id}")
        no_match = "matched no objects" in (listing.stderr + listing.stdout).lower()
        if listing.returncode != 0 and not no_match:
            raise RuntimeError(f"Cannot verify Gold output absence: {run_id}")
    return cancelled_description


def plan_gold(config: Mapping[str, Any], token: str) -> dict[str, Any]:
    status = airflow_status(config, token)
    if status["dag_run_state"] != "failed":
        raise ValueError("Gold recovery planning requires the failed safety hold")
    dag = urllib.parse.quote(str(config["dag_id"]), safe="")
    dag_state = _request(config, "GET", f"/api/v2/dags/{dag}", token)
    if dag_state.get("is_paused") is not True:
        raise ValueError("Gold recovery planning requires the DAG to remain paused")
    cancelled_description = _gold_outputs_absent(config)
    pair_path = Path(str(config["evidence_root"])) / "dp2-gold-pair.json"
    pair = load_pair_manifest(pair_path)
    if pair["git_sha"] != config["execution_git_sha"]:
        raise ValueError("Gold pair execution Git SHA changed")
    before_digest = str(pair["pair_manifest_digest"])
    for profile in ("baseline", "optimized"):
        _action(_dp2_args(config, action="plan-submit", stage="gold", profile=profile))
    after = load_pair_manifest(pair_path)
    if after["pair_manifest_digest"] != before_digest:
        raise ValueError("Gold pair manifest changed during planning")
    result = {
        "schema_version": RECOVERY_SCHEMA,
        "status": "GOLD_RECOVERY_PLAN_VERIFIED_NOT_SUBMITTED",
        "dag_id": config["dag_id"], "dag_run_id": config["dag_run_id"],
        "execution_git_sha": config["execution_git_sha"],
        "controller_git_sha": config["controller_git_sha"],
        "pair_manifest_digest": before_digest,
        "cancelled_original_batch_id": config["cancelled_baseline_gold_batch_id"],
        "cancelled_original_batch": cancelled_description,
        "cancelled_original_batch_sha256": hashlib.sha256(
            json.dumps(
                cancelled_description, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "attempts": {
            profile: {
                "dataproc_batch_id": config[f"{profile}_gold_batch_id"],
                "output_run_id": config[f"{profile}_gold_run_id"],
            }
            for profile in ("baseline", "optimized")
        },
    }
    path = Path(str(config["evidence_root"])) / "gold-recovery-plan.json"
    if path.exists():
        if _load(path) != result:
            raise ValueError("Existing Gold recovery plan identity mismatch")
    else:
        _write_once(path, result)
    return result


GOLD_RECOVERY_STEPS = (
    "baseline-quota", "baseline-submit", "baseline-verify",
    "baseline-summary", "baseline-evidence", "optimized-quota",
    "optimized-submit", "optimized-verify", "optimized-summary",
    "optimized-evidence", "finalize",
)


def _require_budget_evidence(path: Path) -> dict[str, Any]:
    budget = _load(path)
    required_numbers = (
        "observed_gross_spend_vnd", "billing_lag_reserve_vnd",
        "estimated_remaining_gold_vnd",
    )
    if (budget.get("status") != "PASS" or budget.get("currency") != "VND" or
            budget.get("gross_cap_vnd") != 5_000_000 or
            budget.get("reserve_vnd") < 700_000 or
            budget.get("quota_available") is not True):
        raise ValueError("Trusted budget/quota evidence does not satisfy the Gold gate")
    if any(not isinstance(budget.get(name), int) or budget[name] < 0
           for name in required_numbers):
        raise ValueError("Budget evidence lacks numeric spend, lag or Gold estimates")
    committed = sum(budget[name] for name in required_numbers)
    if committed > budget["gross_cap_vnd"] - budget["reserve_vnd"]:
        raise ValueError("Projected Gold spend would violate the 700,000 VND reserve")
    return budget


def _describe_attempt(config: Mapping[str, Any], batch_id: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["gcloud", "dataproc", "batches", "describe", batch_id,
         f"--project={config['project']}", f"--region={config['region']}",
         "--format=json"], capture_output=True, text=True,
    )
    if result.returncode == 0:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("Dataproc describe returned a non-object")
        return value
    if "NOT_FOUND" in (result.stderr + result.stdout).upper():
        return None
    raise RuntimeError(f"Cannot inspect Dataproc batch: {batch_id}")


def _hold_after_task_patch(config: Mapping[str, Any], token: str, task_id: str) -> None:
    task = urllib.parse.quote(task_id, safe="")
    note = (
        "Externally completed distinct Gold recovery attempt; execution SHA "
        f"{config['execution_git_sha']}; controller SHA {config['controller_git_sha']}"
    )
    _request(config, "PATCH", _run_path(config) + f"/taskInstances/{task}", token,
             {"new_state": "success", "note": note})
    current = airflow_status(config, token)
    if current["task_states"].get(task_id) != "success":
        raise ValueError(f"Airflow did not reconcile completed task: {task_id}")
    if current["dag_run_state"] != "failed":
        _request(config, "PATCH", _run_path(config), token,
                 {"state": "failed", "note": "Paid Gold recovery remains paused"})


def _record_gold_step(config: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    path = (
        Path(str(config["evidence_root"])) / "gold-recovery-steps" /
        f"{result['step']}.json"
    )
    value = dict(result)
    if path.exists():
        if _load(path) != value:
            raise ValueError(f"Existing Gold step evidence differs: {path}")
    else:
        _write_once(path, value)
    return value


def run_gold_step(
    config: Mapping[str, Any], token: str, *, step: str,
    budget_evidence: Path, authorization: str,
) -> dict[str, Any]:
    if authorization != "I_APPROVE_DP2_GOLD_RECOVERY":
        raise ValueError("Explicit DP2 Gold recovery authorization is missing")
    if step not in GOLD_RECOVERY_STEPS:
        raise ValueError(f"Unsupported Gold recovery step: {step}")
    budget = _require_budget_evidence(budget_evidence)
    plan = _load(Path(str(config["evidence_root"])) / "gold-recovery-plan.json")
    if (plan.get("status") != "GOLD_RECOVERY_PLAN_VERIFIED_NOT_SUBMITTED" or
            plan.get("execution_git_sha") != config["execution_git_sha"] or
            plan.get("controller_git_sha") != config["controller_git_sha"]):
        raise ValueError("Trusted Gold recovery plan is missing or stale")
    status = airflow_status(config, token)
    dag = urllib.parse.quote(str(config["dag_id"]), safe="")
    if status["dag_run_state"] != "failed" or _request(
        config, "GET", f"/api/v2/dags/{dag}", token
    ).get("is_paused") is not True:
        raise ValueError("Gold recovery requires the paused failed DagRun")

    task_states = status["task_states"]
    predecessor_tasks = {
        "baseline-quota": "prepare_gold_pair_from_silver_winner",
        "baseline-submit": "gold_baseline_quota",
        "baseline-verify": "gold_baseline_submit",
        "baseline-summary": "gold_baseline_verify",
        "baseline-evidence": "gold_baseline_semantic_scan",
        "optimized-quota": "gold_baseline_evidence",
        "optimized-submit": "gold_optimized_quota",
        "optimized-verify": "gold_optimized_submit",
        "optimized-summary": "gold_optimized_verify",
        "optimized-evidence": "gold_optimized_semantic_scan",
        "finalize": "gold_optimized_evidence",
    }
    predecessor = predecessor_tasks.get(step)
    if predecessor is not None and task_states.get(predecessor) != "success":
        raise ValueError(f"Gold recovery predecessor is incomplete: {predecessor}")

    profile = "baseline" if step.startswith("baseline-") else "optimized"
    action = step.split("-", 1)[-1]
    task_by_action = {
        "submit": f"gold_{profile}_submit",
        "verify": f"gold_{profile}_verify",
        "summary": f"gold_{profile}_semantic_scan",
        "evidence": f"gold_{profile}_evidence",
    }
    if action == "submit":
        quota_path = (
            Path(str(config["evidence_root"])) / "gold" / profile /
            f"quota-{config[f'{profile}_gold_batch_id']}.json"
        )
        if not quota_path.is_file() or _load(quota_path).get("status") != "READY":
            raise ValueError("Fresh recovery quota evidence is required before submit")
    result: dict[str, Any] = {
        "schema_version": RECOVERY_SCHEMA, "step": step,
        "execution_git_sha": config["execution_git_sha"],
        "controller_git_sha": config["controller_git_sha"],
        "budget_evidence_sha256": _sha256(budget_evidence),
        "budget_gate": budget,
    }
    if step == "finalize":
        _action(_dp2_args(config, action="finalize", stage="gold"))
        _hold_after_task_patch(config, token, "validate_and_select_gold_winner")
        _request(config, "PATCH", _run_path(config), token,
                 {"state": "success", "note": "All recovered Gold artifacts verified"})
        result["status"] = "GOLD_FINALIZED_DAGRUN_SUCCESS"
        return _record_gold_step(config, result)
    if action == "quota":
        _action(_dp2_args(config, action="quota", stage="gold", profile=profile))
        task_id = f"gold_{profile}_quota"
        if status["task_states"].get(task_id) != "success":
            _hold_after_task_patch(config, token, task_id)
        result["status"] = "QUOTA_VERIFIED"
        return _record_gold_step(config, result)
    batch_id = str(config[f"{profile}_gold_batch_id"])
    if action == "submit":
        description = _describe_attempt(config, batch_id)
        if description is None:
            _action(_dp2_args(config, action="submit", stage="gold", profile=profile))
            description = _describe_attempt(config, batch_id)
        if description is None:
            raise ValueError("Gold submission returned without a Dataproc batch")
        state = description.get("state")
        if state in {"PENDING", "RUNNING", "CANCELLING"}:
            result.update(status="ATTEMPT_IN_PROGRESS_NO_RESUBMIT", batch_state=state)
            return result
        if state != "SUCCEEDED":
            raise ValueError(f"Gold recovery attempt did not succeed: {state}")
    elif action == "summary":
        from ops.gcp.dp2_method2 import _validation_batch_id
        output_run_id = str(config[f"{profile}_gold_run_id"])
        validation_id = _validation_batch_id(output_run_id, "gold", profile)
        description = _describe_attempt(config, validation_id)
        if description is None:
            _action(_dp2_args(config, action="summary", stage="gold", profile=profile))
            description = _describe_attempt(config, validation_id)
        if description is None or description.get("state") != "SUCCEEDED":
            state = None if description is None else description.get("state")
            if state in {"PENDING", "RUNNING", "CANCELLING"}:
                result.update(status="SUMMARY_IN_PROGRESS_NO_RESUBMIT", batch_state=state)
                return result
            raise ValueError(f"Gold summary batch did not succeed: {state}")
    else:
        description = _describe_attempt(config, batch_id)
        if description is None or description.get("state") != "SUCCEEDED":
            raise ValueError("Gold evidence/verification requires a succeeded attempt")
        _action(_dp2_args(config, action=action, stage="gold", profile=profile))
    _hold_after_task_patch(config, token, task_by_action[action])
    result.update(status="STEP_COMPLETED_AND_RECONCILED", dataproc_batch_id=batch_id)
    return _record_gold_step(config, result)


def resume_gold(config: Mapping[str, Any], token: str,
                budget_evidence: Path, authorization: str) -> dict[str, Any]:
    if authorization != "I_APPROVE_DP2_GOLD_RECOVERY":
        raise ValueError("Explicit DP2 Gold recovery authorization is missing")
    _require_budget_evidence(budget_evidence)
    raise ValueError(
        "Scheduler resume is disabled for distinct recovery attempts; use the "
        "source-controlled external Gold step workflow while the DAG stays paused"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=(
        "status", "recover-silver", "reconcile-silver", "plan-gold",
        "run-gold-step", "resume-gold"),
        default="status")
    parser.add_argument("--authorization", default="")
    parser.add_argument("--budget-evidence", type=Path)
    parser.add_argument("--gold-step", choices=GOLD_RECOVERY_STEPS)
    parser.add_argument("--airflow-token-env", default="AIRFLOW_API_TOKEN")
    args = parser.parse_args(argv)
    config = _config(args.config)
    controller_repo = Path(__file__).resolve().parents[2]
    _git_identity(Path(str(config["canonical_repo"])),
                  str(config["execution_git_sha"]), clean=True)
    _git_identity(controller_repo, str(config["controller_git_sha"]), clean=True)
    token = os.environ.get(args.airflow_token_env, "")
    if args.mode == "status":
        if not token:
            raise ValueError(f"Airflow token environment variable is missing: {args.airflow_token_env}")
        result = airflow_status(config, token)
    elif args.mode == "recover-silver":
        if args.authorization != "I_APPROVE_LOCAL_SILVER_EVIDENCE_RECOVERY":
            raise ValueError("Explicit local Silver evidence recovery authorization is missing")
        result = recover_silver(config)
    elif args.mode == "reconcile-silver":
        if args.authorization != "I_APPROVE_AIRFLOW_SILVER_RECONCILIATION":
            raise ValueError("Explicit Airflow Silver reconciliation authorization is missing")
        if not token:
            raise ValueError(f"Airflow token environment variable is missing: {args.airflow_token_env}")
        result = reconcile_silver(config, token)
    elif args.mode == "plan-gold":
        if not token:
            raise ValueError(f"Airflow token environment variable is missing: {args.airflow_token_env}")
        result = plan_gold(config, token)
    elif args.mode == "run-gold-step":
        if not token:
            raise ValueError(f"Airflow token environment variable is missing: {args.airflow_token_env}")
        if args.budget_evidence is None or args.gold_step is None:
            raise ValueError("Gold execution requires --budget-evidence and --gold-step")
        result = run_gold_step(
            config, token, step=args.gold_step,
            budget_evidence=args.budget_evidence,
            authorization=args.authorization,
        )
    else:
        if not token:
            raise ValueError(f"Airflow token environment variable is missing: {args.airflow_token_env}")
        if args.budget_evidence is None:
            raise ValueError("--budget-evidence is required for Gold recovery")
        result = resume_gold(config, token, args.budget_evidence, args.authorization)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
