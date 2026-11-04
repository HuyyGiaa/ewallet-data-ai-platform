# Evidence collision resolution

## Input and scope

- Input: the versioned, sanitized read-only audit metadata snapshot
  `EVIDENCE_INVENTORY_SNAPSHOT.csv`.
- Rows processed: 1,445.
- Original colliding destination keys: 238.
- Rows involved in original collisions: 762.
- Evidence artifacts moved/copied/renamed/deleted: 0.

The original `proposed_destination` column is retained in the V2 map for audit
comparison but must never drive an automated move.

## Original collision classification

| Classification | Groups | Rows | Resolution |
|---|---:|---:|---|
| `SAME_CONTENT_DUPLICATE` | 116 | 260 | Preserve every source record and checksum; assign a canonical artifact ID but do not delete/coalesce in Phase B0 |
| `DIFFERENT_CONTENT_COLLISION` | 85 | 327 | Preserve run/stage/profile and add deterministic source identity where required |
| `UNKNOWN_HASH` | 37 | 175 | Keep separate destinations; never deduplicate without a verified hash |
| **Total** | **238** | **762** | All original groups classified |

## Corrected mapping result

- Corrected destination collision count: **0**.
- Corrected rows sharing a destination: **0**.
- Unique source records: **1,445**.
- Unique artifact IDs: **1,445**.
- Validator status: **PASS**.

The algorithm first constructs a meaningful path from phase, component, run ID,
stage, profile, artifact category, and original filename. If two rows still map
to the same path, it inserts `source-<12 hex>` derived from the captured
pre-sanitization source identity. The result is stable for the same inventory
and does not depend on a machine-specific absolute path, row order, or arbitrary
numbering.

## Artifact classification overrides

| Result | Rows | Treatment |
|---|---:|---|
| `MISCLASSIFIED_ARTIFACT` | 204 | Python/build-context/cache material becomes `NOT_EVIDENCE` |
| `EXTERNAL_LARGE_ARTIFACT` | 134 | Parquet/model/database/compressed or >10 MiB artifact becomes `EXTERNAL_REFERENCE` |
| Verified SHA-256 | 1,047 | Integrity is available for later Phase B1 verification |
| Unknown/unavailable hash | 398 | Distinct destination retained; no deduplication |

The effective row-level collision classifications in the map differ from the
group table because source/build assets and large external artifacts override a
filename collision category.

## Migration dispositions

| Disposition | Rows | Meaning |
|---|---:|---|
| `PLANNED` | 862 | Compact future Git destination, not yet migrated |
| `KEEP_IN_PLACE` | 81 | Active-worktree-only artifact; owner checkpoint required |
| `EXTERNAL_REFERENCE` | 134 | Durable external artifact identity required |
| `BLOCKED_FOR_REVIEW` | 164 | Phase/sensitivity/provenance decision required |
| `NOT_EVIDENCE` | 204 | Keep with source/build context or exclude from submission |

Manual owner review covers 245 rows: 164 blocked records plus 81 active-worktree
records. A future review may reduce that number, but Phase B0 does not invent a
phase or take ownership from another workstream.

## DataHub coordination

The DataHub assertions worktree uses the superseded
`docs/evidence/images/datahub/` location. V2 proposes destinations under:

- `docs/evidence/edai1/datahub/lineage/{screenshots,api}/`
- `docs/evidence/edai1/datahub/assertions/{screenshots,api}/`
- `docs/evidence/edai1/datahub/datasets_schema/{screenshots,api}/`

The V2 map does not modify that worktree. Its Markdown links and source branch
must be updated by the DataHub owner in a later dependency-safe Phase B1 commit.

## Safety properties

1. No proposed repository destination escapes `docs/evidence/`.
2. No external placeholder is presented as a real uploaded URI.
3. Unknown hashes never establish duplicate content.
4. Baseline and optimized tokens are retained when present in the source path.
5. Known run IDs are retained; unknown IDs receive a stable source identity.
6. Original source path and original collision key remain in every row.
7. Python/build-context files do not enter the evidence taxonomy.
8. The validator performs no artifact filesystem operation.
