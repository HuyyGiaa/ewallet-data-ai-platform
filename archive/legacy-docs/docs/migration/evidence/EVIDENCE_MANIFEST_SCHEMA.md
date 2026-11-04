# Evidence manifest contract

## Purpose

This contract describes a future evidence manifest. Phase B0 creates planning
records only; it does not mark any artifact as migrated or upload external data.

## Required fields

| Field | Type | Requirement |
|---|---|---|
| `schema_version` | string | Manifest schema version |
| `artifact_id` | string | Stable ID derived from the original source identity |
| `source_identity_sha256` | string | SHA-256 of the exact pre-sanitization source identifier |
| `original_source_path` | string | Portable audit source path; restricted names may be opaque |
| `proposed_destination` | string or null | Planning destination; null for blocked/non-evidence rows |
| `edai_phase` | enum | `EDAI-1`, `EDAI-2`, or an explicitly blocked unverified value |
| `component` | string | DP1, DP2, DP3, streaming, DataHub, MLOps, LLM, RAG, agents, CI/CD, observability, or justified extension |
| `rubric_row` | string | Original workbook row mapping or explicit cross-phase value |
| `git_commit_sha` | string | Full execution/source SHA when known; never infer it from file mtime |
| `run_id` | string | Airflow/Dataproc/KFP/benchmark/campaign identity when known |
| `execution_timestamp` | ISO-8601 string | Runtime timestamp with timezone; filesystem mtime must be labeled as such |
| `artifact_type` | string | Screenshot, API readback, log, report, metric, dataset, model, source/build asset, etc. |
| `artifact_size_bytes` | integer | Exact audited size |
| `sha256` | string or explicit unknown value | Integrity value and reason when unavailable |
| `evidence_status` | enum | Runtime evidence status, separate from migration status |
| `historical_current_classification` | enum | Historical/current/unverified temporal status |
| `storage_class` | enum | Git compact, external durable, active-owner hold, restricted review, or non-evidence |
| `reproduction_runbook` | path or `UNKNOWN` | Repository-relative runbook when verified |
| `migration_disposition` | enum | One of the Phase B0 values below |
| `migration_risk` | enum | Risk from the read-only audit plus owner coordination notes |
| `verification_status` | enum | `VERIFIED_SHA256` or `UNKNOWN_HASH` in Phase B0 |
| `collision_classification` | enum | Original collision analysis or artifact override |
| `path_reference_class` | enum | Documentation/code/CI/infrastructure/test/unknown reference class |
| `owner_review_reason` | string | Required decision before Phase B1 |

## Phase B0 migration dispositions

- `PLANNED`: compact artifact has a collision-safe future repository path.
- `KEEP_IN_PLACE`: artifact belongs to an active worktree; proposed path is
  planning-only until owner checkpoint.
- `EXTERNAL_REFERENCE`: artifact must remain outside Git and needs a durable URI,
  object version, checksum, and retrieval procedure.
- `BLOCKED_FOR_REVIEW`: phase, sensitivity, provenance, or ownership cannot be
  resolved safely.
- `NOT_EVIDENCE`: source/build/cache material must not be migrated into evidence.

`MIGRATED` is intentionally invalid during Phase B0.

## Evidence status

Future runtime manifests may use `CAPTURED`, `VERIFIED`, `PARTIAL`, `HISTORICAL`,
`MISSING`, or `NOT_EXECUTED`. Phase B0 uses
`PLANNING_ONLY_NOT_MIGRATED` for every mapping row.

## Identity and naming

- Preserve phase, component, run ID, stage, baseline/optimized profile, category,
  and original filename when available.
- When `run_id` is unknown, use `source-<12 hex>` derived from the captured
  `source_identity_sha256`.
- If meaningful identities still create the same destination, add the same
  stable source-derived directory before the filename.
- Never use arbitrary incrementing suffixes.
- Same-content duplicates remain separate source records and carry
  `canonical_artifact_id`; Phase B0 does not delete or coalesce them.

## External artifact extension

An external manifest entry must additionally record:

- durable URI and object generation/version;
- checksum algorithm and checksum supplied or independently verified;
- retrieval command without credentials;
- retention class and access owner;
- reproduction command and immutable source/data/image identities.

The placeholder prefix `EXTERNAL_DURABLE_STORE_REQUIRED/` is not a live URI and
must be replaced only after the owner approves the existing GCS/artifact-store
location. Phase B0 provisions and uploads nothing.

## Example planning record

```json
{
  "artifact_id": "ev-0123456789abcdef",
  "source_identity_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "original_source_path": "~/ewallet-evidence/run/summary.json",
  "proposed_destination": "docs/evidence/edai1/dp2/run-id/gold/baseline/api/summary.json",
  "edai_phase": "EDAI-1",
  "component": "dp2",
  "rubric_row": "R24-R25",
  "git_commit_sha": "UNKNOWN",
  "run_id": "run-id",
  "execution_timestamp": "UNKNOWN",
  "artifact_type": ".json",
  "artifact_size_bytes": 1024,
  "sha256": "<64-lowercase-hex-or-explicit-unknown-reason>",
  "evidence_status": "PLANNING_ONLY_NOT_MIGRATED",
  "historical_current_classification": "UNVERIFIED_TEMPORAL_STATUS",
  "storage_class": "GIT_COMPACT",
  "reproduction_runbook": "UNKNOWN",
  "migration_disposition": "PLANNED",
  "migration_risk": "HIGH",
  "verification_status": "UNKNOWN_HASH",
  "collision_classification": "DIFFERENT_CONTENT_COLLISION",
  "path_reference_class": "UNKNOWN",
  "owner_review_reason": "Phase B1 approval required"
}
```

The example is illustrative and is not a runtime evidence claim.
