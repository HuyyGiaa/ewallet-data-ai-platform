# F9 canonical MLflow implementation plan

**Scope:** Prepare the existing four-model F9 experiment to consume the
immutable bounded training snapshot produced by the frozen cloud Feast
boundary. Prepare an internal Kubernetes MLflow server backed by PostgreSQL
and GCS. Do not deploy or train during this milestone.

## Invariants

- Candidate models, parameters, feature set, target, and chronological splits
  remain unchanged.
- Every candidate is fit on train and evaluated on validation. Only an
  eligible validation winner is evaluated on test.
- Only the winner model artifact is registered. Registration never assigns a
  stage or alias; production promotion remains a separate manual action.
- The local staged snapshot must carry the canonical dataset, Silver, feature,
  and snapshot lineage from its immutable GCS source.
- Canonical configuration contains no localhost or MinIO endpoint.

## Implementation

1. Add a snapshot manifest reader that validates immutable lineage, derives
   row/class/split counts, and hashes the manifest and model feature schema.
2. Make F9 paths, lineage IDs, and tracking URI environment-driven and require
   the exact canonical four-candidate matrix.
3. Add a canonical F9 entrypoint that validates the snapshot, records Git SHA,
   runs the existing experiment, and publishes complete MLflow provenance.
4. Extend MLflow logging with required candidate lineage and winner-only test
   metrics/registration while leaving production promotion manual.
5. Add namespace-scoped Kubernetes manifests for a ClusterIP MLflow server,
   existing training Workload Identity, PostgreSQL Secret reference, GCS
   artifact ConfigMap reference, probes, and bounded resources.
6. Add a runbook and evidence contract with future USER_RUN and rollback
   commands. Validate with focused unit/static tests, compile, YAML/JSON parse,
   and `git diff --check`.
