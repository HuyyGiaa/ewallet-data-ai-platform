# Canonical Size Remediation Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Add a fail-closed `plan`/`apply`/`rollback` tool that rewrites only the approved 35 `transactions_v2` Parquet objects and updates manifests with GCS generation preconditions.

**Architecture:** Keep Parquet transformation, manifest aggregation, and state-machine rules in pure functions. Put GCS reads/writes behind an injected object-store boundary so focused tests use an in-memory versioned store. The CLI binds the approved canonical constants and lazily creates the real Google Cloud Storage adapter.

**Tech stack:** Python 3.10, PyArrow, google-cloud-storage, unittest.

---

### Task 1: Define plan contract and Parquet rewrite invariants

**Files:**
- Create: `tests/unit/test_canonical_size_remediation.py`
- Create: `ops/gcp/remediate_canonical_size.py`

1. Write failing tests for checksum validation, schema/value equality rejection, insufficient gain, and immutable plan output.
2. Run the focused test file and confirm failures are caused by the missing module/behavior.
3. Implement the minimal pure helpers and read-only plan path.
4. Run the focused tests until green.

### Task 2: Implement conditional apply and manifest rebuild

**Files:**
- Modify: `tests/unit/test_canonical_size_remediation.py`
- Modify: `ops/gcp/remediate_canonical_size.py`

1. Add failing tests for generation conflicts, exact mutation scope, dataset manifest last, invariant preservation, and partial failure ledger state.
2. Implement preflight generation checks, exact staged uploads, affected manifest updates, aggregate rebuild from every shard manifest, and commit-marker upload.
3. Run the focused tests until green.

### Task 3: Implement rollback state machine

**Files:**
- Modify: `tests/unit/test_canonical_size_remediation.py`
- Modify: `ops/gcp/remediate_canonical_size.py`

1. Add failing tests for ordered restoration, partial-apply rollback, idempotent already-restored objects, and ambiguous-state refusal.
2. Implement generation-based restore and verification with the dataset manifest restored last.
3. Run the focused tests until green.

### Task 4: CLI, runbook, and final verification

**Files:**
- Modify: `tests/unit/test_canonical_size_remediation.py`
- Create: `docs/cloud/canonical_size_remediation.md`

1. Add CLI parsing/`--execute` guard tests.
2. Document exact future `USER_RUN` and disk/runtime expectations.
3. Run focused tests, Python compile, and `git diff --check`.
4. Review the complete branch diff and commit once with a coherent message.

## Global constraints

- Never call real GCS, the canonical generator, Dataproc, Airflow, Docker, or the final gate during implementation/testing.
- Preserve the approved table, shard range, encoding, dataset version, current total, and minimum projected total.
- Never mutate an object without an exact generation match.
- The dataset manifest is always the final apply/rollback write.

## Review focus

- A partial failure must leave enough durable local ledger state for deterministic rollback.
- Re-running apply or rollback must fail closed unless the current state is provably complete or already restored.
- No unaffected Parquet object may be opened, downloaded, or uploaded.
