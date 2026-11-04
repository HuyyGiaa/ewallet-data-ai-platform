# One-pass rubric audit and implementation plan

**Spec:** `/home/huyy-giaa/Downloads/codex_one_pass_rubric_audit_implementation.md`

**Authoritative inputs:** the two active sheets in `EDAI K11.xlsx`, the live
Google Docs architecture (sections 3–6, 9, 14–15, 17–20), Living Decisions
21.12, 21.32, 21.63, 21.65–66, 21.102–103, 21.115, 21.117, 21.120 and
21.135–144, and the actual Git/source/evidence inventory.

**Global constraints:** no Spark implementation or execution; no cloud write,
deployment, merge, push, commit, dependency change, expensive workload, or
worktree cleanup. Preserve the dirty Block A worktree read-only. Source/YAML
alone never earns VERIFIED points. Keep all changes uncommitted in the existing
`codex-feast-training-runtime` worktree.

## Task 1: Machine-enforced rubric evidence contract

**Files:**

- Create `ops/rubric/__init__.py`
- Create `ops/rubric/evidence_matrix.py`
- Create `ops/rubric/validate_evidence_matrix.py`
- Create `tests/unit/test_rubric_evidence_matrix.py`

Implement a standard-library-only CSV validator and JSON renderer. Tests are
written and observed failing first. Enforce exact row coverage and sheet totals,
unique `(sheet,row)`, legal status/score combinations, real-runtime evidence for
VERIFIED, baseline/optimized measurements for optimization rows, explicit
equivalence decisions for frozen-architecture conflicts, and zero awarded
points for OPEN/PARTIAL/source-only claims.

Verification: `python -m unittest -v tests.unit.test_rubric_evidence_matrix`.

## Task 2: Complete 93-row evidence matrix and audit report

**Files:**

- Create `docs/rubric/rubric_evidence_matrix.csv`
- Generate `docs/rubric/rubric_evidence_matrix.json`
- Create `docs/rubric/rubric_audit.md`

Record every scored row from both active sheets with exact points, current
status, source worktree/SHA, tests, environment/deployment evidence, runtime
artifacts, missing proof, next safe command, and verified/inferred boundary.
Cross-check all source and evidence paths. The report contains weighted totals,
architecture conflicts, implemented gaps, Block A read-only findings, and all
remaining runtime gates. It must not fabricate credit.

Verification: validate CSV and generated JSON; check every referenced local
path and every recorded worktree SHA where applicable.

## Task 3: Reproducible local evidence-pack automation

**Files:**

- Extend `ops/rubric/evidence_matrix.py`
- Extend `ops/rubric/validate_evidence_matrix.py`
- Extend `tests/unit/test_rubric_evidence_matrix.py`
- Create `docs/rubric/runtime_evidence_template.json`

Add a safe read-only default that validates a matrix and emits summary JSON.
Add an explicit local-only initializer for a run-scoped evidence template. It
must refuse overwrite, never contact cloud services, bind Git SHA/worktree,
and require baseline/optimized identities for benchmark rows. Test overwrite,
invalid Git identity, missing benchmark fields, and deterministic output.

Verification: focused unit tests plus CLI smoke tests in a temporary directory.

## Task 4: CI-ready gate, consolidated handoff, and bounded verification

**Files:**

- Create `scripts/validation/validate_rubric_evidence.sh`
- Extend `docs/rubric/rubric_audit.md`

Add a thin repository entrypoint that runs the validator without dependencies
or external side effects. Run compileall, focused tests, relevant adjacent
Feast/MLOps tests, shell syntax checks, `git diff --check`, and final status.
Perform a fresh whole-change review against this plan and the source spec.
Report exact passes/failures/skips and a strict user-run queue for runtime proof.

Verification: `bash scripts/validation/validate_rubric_evidence.sh` and the
bounded test commands recorded in the final report.

## Review focus

- A row cannot receive credit merely because source/config/docs exist.
- The validator cannot silently omit a scored row or accept incorrect sheet
  totals.
- Optimization rows cannot be VERIFIED without matched baseline/optimized
  measurements and correctness evidence.
- Frozen-architecture conflicts cannot be silently treated as compliant.
- Generated evidence must be deterministic, run-scoped, fail closed, and local
  unless a later user-run command explicitly performs an authorized mutation.
