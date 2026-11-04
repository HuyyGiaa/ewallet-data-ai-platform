# Benchmark Metrics Ordering Design

## Intent

Prevent a final one-shot telemetry collection from overwriting an earlier
in-flight snapshot and prevent the comparator from rejecting a valid run when
a completed checkpoint exists outside the lexicographically last snapshot.

## Approaches

- **Selected: append plus timestamp-aware comparison.** The collector starts
  after the greatest existing snapshot index. The comparator orders snapshots
  by `taken_at` (falling back to filename) and accepts a run when any captured
  snapshot proves a completed checkpoint. This fixes both the writer and reader
  boundaries without modifying historical evidence.
- **Comparator-only workaround.** Rejected because future final collections
  would continue overwriting telemetry.
- **Rename or edit existing evidence.** Rejected because benchmark evidence is
  immutable.

## Constraints

- Do not modify Docker, Compose, Flink profiles, checkpoint configuration, or
  existing evidence directories.
- Preserve the strict checkpoint gate: at least one real completed checkpoint
  must still be present.
- Metrics reported for checkpoint counts and sizes must come from the newest
  captured snapshot that contains a completed checkpoint.

## Acceptance

- A second collector invocation appends after existing snapshot files.
- A later-written `snapshot_0000` with a newer `taken_at` sorts after older
  higher-numbered snapshots.
- Validation passes if any snapshot contains a completed checkpoint and fails
  if none does.
- The existing baseline/optimized 100 EPS evidence compares without rewriting
  either run directory.
