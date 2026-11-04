# R3 Dataproc Delta-on-GCS Smoke Implementation Plan

> **Historical plan/spec — not current source of truth.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove with one bounded, cost-capped batch that Managed Service for Apache Spark can write, append, and read Delta Lake data on the project GCS buckets before the platform commits to Dataproc Serverless for larger pilots.

**Architecture:** A standalone PySpark smoke job uses the preinstalled Delta runtime in Dataproc runtime `2.2`, writes a deterministic microsecond-timestamp fixture to a unique temporary GCS prefix, reads it back, and emits one machine-readable evidence JSON. A local submission CLI derives project, region, subnet, staging bucket, and Spark service account from Terraform outputs, defaults to dry-run, and requires an explicit execute flag for the cost-bearing batch.

**Tech Stack:** Python 3.10+, PySpark 3.5, Delta Lake 3.2, Dataproc Serverless runtime 2.2, GCS, gcloud CLI, unittest.

**Spec:** `docs/superpowers/specs/2026-10-21-cloud-platform-refactor-design.md`

## Global Constraints

- Do not generate the 100 GB dataset or run Bronze/Silver/Gold pipelines.
- Do not enable GKE or Jenkins and do not restart local Trino/MinIO.
- Use the existing Spark workload service account and Terraform outputs; do not create or download service-account keys.
- Write smoke output only under the lifecycle-managed temporary bucket.
- Pin Dataproc runtime `2.2`; it currently provides Spark 3.5 and Delta Lake 3.2.
- Use a unique run prefix and `errorifexists`; never overwrite another run.
- Cap the batch with a 20-minute TTL and standard CPU runtime.
- Do not commit generated plan files, runtime data, credentials, or evidence claiming PASS before cloud read-back succeeds.

## Review Focus

1. Missing or malformed Terraform outputs must fail before `gcloud` submits a batch.
2. Dry-run must be the default and must have no cloud mutation.
3. Batch IDs and run IDs must reject shell metacharacters and invalid Dataproc names.
4. The smoke must prove Delta version progression, exact rows/sum, microsecond timestamps, and read-back from GCS.
5. Evidence may be written to the repository only after the remote batch reports success and the JSON contract validates.

---

### Task 1: Deterministic Delta-on-GCS smoke job

**Files:**
- Create: `ops/gcp/dataproc_delta_smoke.py`
- Create: `tests/unit/test_dataproc_delta_smoke.py`

**Interfaces:**
- Produces: `fixture_rows() -> tuple[dict[str, object], ...]`.
- Produces: `expected_summary() -> dict[str, object]`.
- Produces: CLI accepting `--run-uri`, writing Delta under `<run-uri>/table` and JSON to `<run-uri>/evidence.json`.

- [ ] Write tests for deterministic unique IDs, exact amount sum, a non-millisecond timestamp, and URI validation.
- [ ] Run the tests and observe failure because the module does not exist.
- [ ] Implement the pure fixture/validation functions and bounded Spark job.
- [ ] Run the targeted tests and Python compile checks.

### Task 2: Cost-gated submission CLI

**Files:**
- Create: `ops/gcp/submit_dataproc_delta_smoke.py`
- Create: `tests/unit/test_submit_dataproc_delta_smoke.py`

**Interfaces:**
- Consumes: `terraform -chdir=infra/terraform/gcp/environments/dev output -json`.
- Produces: `build_submission_command(outputs, run_id) -> tuple[str, ...]`.
- Produces: dry-run CLI by default; `--execute` submits exactly one synchronous batch and validates remote evidence.

- [ ] Write tests for required Terraform outputs, exact project/region/service account/subnet/buckets, runtime `2.2`, TTL `20m`, standard tier, and no shell invocation.
- [ ] Run the tests and observe failure because the module does not exist.
- [ ] Implement output parsing, safe run ID validation, command construction, preflight, execution, and evidence validation.
- [ ] Run targeted tests and confirm dry-run does not call a cloud mutation.

### Task 3: Runbook and static validation

**Files:**
- Create: `docs/cloud/dataproc_delta_smoke.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/cloud/terraform_gcp_foundation.md`
- Create after successful cloud run: `docs/evidence/cloud/r3_dataproc_delta_smoke.json`

**Interfaces:**
- Produces: exact dry-run, execute, UI/log inspection, evidence, cost, and cleanup commands.

- [ ] Document why runtime `2.2` is used and what the smoke proves.
- [ ] Document that the smoke output is temporary and lifecycle-managed.
- [ ] Add docs links and run relative-link, secret/path, compile, unit, contract, and `git diff --check` validation.
- [ ] Review the generated command locally without submitting.

### Task 4: User-observed cloud checkpoint and commit

**Files:**
- Create from verified read-back: `docs/evidence/cloud/r3_dataproc_delta_smoke.json`

**Interfaces:**
- Consumes: one user-observed `--execute` run.
- Produces: an evidence record that selects Dataproc Serverless on PASS or records the measured fallback reason on FAIL.

- [ ] User runs the displayed execute command and observes the Dataproc Batches UI/logs.
- [ ] Validate the returned remote evidence and record actual batch ID, runtime, duration, and result.
- [ ] Run final repository validation and explicit staged review.
- [ ] Commit the verified R3 changes; do not push automatically.
