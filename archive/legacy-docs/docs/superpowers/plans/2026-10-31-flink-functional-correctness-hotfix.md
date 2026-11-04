# Flink Functional Correctness Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore canonical `device_age_seconds` parity and make functional benchmark evidence fail closed without rejecting valid performance runs that intentionally omit the oracle.

**Architecture:** Keep the canonical DP3 definition (`max(0, T - first_seen_at)`) and make cold-start fallback explicit: absent a bootstrapped device dimension, the first observed transaction establishes first-seen for later point-in-time snapshots. Make evidence validity depend on run mode, and enforce identical logical input hashes at comparison time.

**Tech Stack:** Python 3.10+, unittest/pytest, PyFlink 1.19.1, Bash benchmark driver.

**Spec:** `/home/huyy-giaa/.codex/attachments/e019873a-e757-40d6-8066-635724ed9610/pasted-text.txt`

## Global Constraints

- Preserve all existing uncommitted PC runtime fixes and their provenance.
- Do not redesign feature definitions, weaken tolerances, run stress workloads, or touch other Docker projects.
- Functional evidence is complete only when basic checks and the full oracle pass; performance evidence may omit the oracle.
- Baseline and optimized comparisons require identical non-empty logical payload hashes.

## Review Focus

- Explicit device first-seen must remain authoritative over observed transaction time.
- Missing first-seen must yield age zero on first observation and elapsed age on later snapshots.
- More than 20 mismatches must retain only 20 examples but report the full mismatch count.
- Functional evidence with missing/failed/incomplete oracle must be invalid; performance evidence without oracle remains valid.
- Empty or unequal payload hashes must make comparison fail.

---

### Task 1: Preserve and validate existing PC runtime fixes

**Files:** Existing eight modified runtime/test files shown by `git status`.

- [ ] Update only stale contract assertions needed by the already-valid runtime fixes.
- [ ] Run focused source-wiring and benchmark contract tests.
- [ ] Commit the runtime compatibility changes separately.

### Task 2: Correct cold-start device semantics

**Files:**
- Modify: `data_platform/streaming/flink/feature_state.py`
- Test: `tests/streaming/test_processing.py`

- [ ] Add a failing regression proving first observation is zero and the next snapshot uses elapsed time when dimension state is absent.
- [ ] Verify RED, then minimally learn earliest observed device event time during state updates.
- [ ] Verify explicit dimension values remain authoritative and focused tests pass.

### Task 3: Fail closed on oracle and input parity

**Files:**
- Modify: `data_platform/streaming/benchmark/outputs.py`
- Modify: `data_platform/streaming/benchmark/compare.py`
- Test: `tests/streaming/test_flink_benchmark.py`

- [ ] Add failing tests for full mismatch count, functional oracle validity, performance-mode exemption, and unequal/missing hashes.
- [ ] Verify RED; implement minimal mode-aware validation and oracle exit behavior.
- [ ] Verify GREEN and commit the correctness gate changes.

### Task 4: Runtime verification and delivery

- [ ] Run all streaming tests and report any unrelated failures by name.
- [ ] Run the bounded functional baseline only if local resources remain healthy.
- [ ] Inspect fresh evidence, oracle verdict/mismatch count, Git status, and commit SHAs.
- [ ] Do not claim VERIFIED unless the fresh oracle passes.
