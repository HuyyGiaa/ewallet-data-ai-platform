# Canonical standalone KFP preparation plan

**Scope:** Prepare and locally compile a standalone Kubeflow Pipelines workflow
that orchestrates the already-proven Feast snapshot, F9 selection, and MLflow
tracking code. Do not deploy KFP, run canonical training, or mutate cloud
resources.

## Frozen invariants

- Input is one immutable, bounded training snapshot from the cloud Feast
  boundary, with explicit dataset/Silver/feature/snapshot lineage and manifest
  checksum.
- Candidate matrix, hyperparameters, seed, feature set, target, and temporal
  partitions remain the canonical F9 contract.
- Every candidate sees train and validation. Only the validation winner sees
  test. Registration logs only the winner and never assigns a production alias.
- KFP standalone orchestrates existing Python modules; it does not replace
  Feast, MLflow, the registry, or F9 business logic.

## Implementation

1. Refactor the existing `ExperimentRunner` into reusable train-candidate,
   validation-selection, and winner-test functions without changing behavior.
2. Add a KFP runtime CLI that downloads the bounded GCS snapshot, validates its
   manifest/provenance/schema, enforces the chronological split contract, calls
   the reusable F9 functions, and finally calls `MlflowTracker`.
3. Define a pinned KFP 2.17.3 standalone pipeline with validate, split, four
   parallel train, select, winner-only test, and MLflow registration tasks.
4. Pin the component image dependencies and configure small CPU/memory
   requests and limits with no GPU.
5. Compile the pipeline locally, add a USER_RUN/evidence/rollback runbook, and
   validate only focused tests, YAML/JSON, Python compile, and diff checks.
