# Cloud Feast and bounded training snapshot plan

> Approved frozen milestone. This plan does not redesign the architecture.

## Goal

Prepare the run-scoped contract from canonical DP3 historical features to a
GCS Feast offline snapshot and then to one bounded, versioned training
snapshot. Keep all execution as future USER_RUN work.

## Constraints

- Do not run cloud, Spark, Dataproc, Airflow, Docker, or the canonical generator.
- No Feast Online Store or Redis.
- Preserve `[T-window, T)` and label isolation.
- Use immutable run-scoped GCS roots and explicit dataset/run/snapshot IDs.

## Task 1: Contract and dry-run CLI

- Add focused failing tests for path construction, ID validation, four-table
  inventory, and command rendering.
- Implement a dependency-light boundary contract and dry-run CLI.
- Ensure rendered commands bind DP3 feature Gold, canonical Silver, Feast
  snapshot, registry, bounded source, and final training snapshot explicitly.

## Task 2: Adapt existing F6/F7 entry points

- Add focused failing tests for run-scoped Gold/Silver overrides and cloud
  Feast selection.
- Make the Feast exporter accept an explicit DP3 Gold root and refuse overwrite
  for immutable cloud exports.
- Make the bounded source exporter accept an explicit canonical Silver root.
- Make the training materializer select the GCP Feast profile and explicit
  offline snapshot/registry without changing local defaults.

## Task 3: Runbook and verification

- Document the cloud input/output contract, temporal invariants, future
  USER_RUN sequence, evidence, and rollback-free retry rule.
- Run focused unit/PIT tests, Python compile, YAML parse, and `git diff --check`.
- Review the diff and commit the milestone separately without push.

## Review focus

- No implicit active/latest path.
- No labels in Feast requests or feature tables.
- No overwrite of a published snapshot.
- Existing local behavior remains the default.
