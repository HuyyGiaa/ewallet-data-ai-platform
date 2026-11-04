# Benchmark Metrics Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make telemetry snapshots append-only and make run validation independent of misleading snapshot filenames.

**Architecture:** `metrics.py` owns collision-free snapshot numbering. `compare.py` owns chronological ordering and checkpoint selection across the complete telemetry series.

**Tech Stack:** Python 3.10, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-01-benchmark-metrics-ordering-design.md`

## Global Constraints

- Do not modify existing evidence.
- Do not weaken the completed-checkpoint gate.
- Do not change infrastructure or benchmark profiles.

## Review Focus

- Empty metrics directories start at `snapshot_0000.json`.
- Existing gaps or non-snapshot files do not cause overwrite.
- Missing/malformed `taken_at` retains deterministic filename fallback.
- Completed checkpoint evidence in a non-last filename remains valid.
- A genuinely checkpointless run remains rejected.

---

### Task 1: Append and compare telemetry snapshots safely

**Files:**
- Modify: `tests/streaming/test_flink_benchmark.py`
- Modify: `data_platform/streaming/benchmark/metrics.py`
- Modify: `data_platform/streaming/benchmark/compare.py`

**Interfaces:**
- Produces: `next_snapshot_index(out: Path) -> int`.
- Produces: chronological `_snapshots(run: Path) -> list[dict]` and checkpoint-aware selection.

- [ ] **Step 1: Write regression tests**

Add tests proving a second collection appends and a completed checkpoint in a
newer `snapshot_0000` is accepted even when `snapshot_0024` has zero completed.
Keep the existing checkpointless rejection test.

- [ ] **Step 2: Verify RED**

Run the focused benchmark unit tests in the locked image. Expected: overwrite
and validation regression tests fail for the diagnosed reasons.

- [ ] **Step 3: Implement minimal fixes**

Compute the next file index from existing matching filenames. Sort loaded
snapshots by parsed `taken_at` with deterministic filename fallback. Select the
newest snapshot that reports a positive completed count for checkpoint metrics
and validation.

- [ ] **Step 4: Verify GREEN and existing evidence**

Run all streaming tests, then compare the existing baseline/optimized 100 EPS
runs without modifying them. Expected: tests pass and parity report renders.

- [ ] **Step 5: Commit**

Commit as `fix(streaming): preserve final benchmark telemetry`.
