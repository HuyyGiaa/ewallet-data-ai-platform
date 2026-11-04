# Evidence migration preview

## Important status

This is a preview, not an executed migration. No evidence was copied, moved,
renamed, deleted, uploaded, or marked `MIGRATED`.

## Inventory disposition

| Future treatment | Rows | Phase B0 result |
|---|---:|---|
| Small Git-tracked candidate | 862 | `PLANNED`; collision-safe proposed path |
| Active-owner artifact | 81 | `KEEP_IN_PLACE`; do not touch before checkpoint |
| Durable external artifact | 134 | `EXTERNAL_REFERENCE`; placeholder identity only |
| Manual classification/security review | 164 | `BLOCKED_FOR_REVIEW` |
| Source/build/cache material | 204 | `NOT_EVIDENCE` |
| **Total** | **1,445** | All rows have an explicit disposition |

## Example naming outcomes

The mapping preserves meaningful run/stage/profile tokens. Examples are taken
from the generated V2 map; they remain planning paths.

```text
docs/evidence/edai1/dp2/<run-id>/silver/baseline/api/summary.json
docs/evidence/edai1/dp2/<run-id>/gold/optimized/logs/attempt-1.log
docs/evidence/edai1/datahub/assertions/api/<run-id>/assertion-results.json
docs/evidence/edai2/llm/<run-id>/general/common/screenshots/rollout.png
```

When the inventory has no run ID, `<run-id>` becomes a deterministic
`source-<12 hex>` identity. When otherwise meaningful paths collide, an
additional source-derived directory is inserted before the filename.

## External classification

The map routes Parquet, serialized models, databases, compressed artifacts, and
files over 10 MiB to `EXTERNAL_DURABLE_STORE_REQUIRED/...`. This prefix is a
planning marker. A later owner-approved phase must replace it with an existing
durable URI and immutable object metadata. It is not valid retrieval evidence.

## Reference impact snapshot

| Reference class | Rows |
|---|---:|
| Documentation-only current evidence path | 356 |
| Explicitly referenced by documentation | 21 |
| Referenced by code | 1 |
| Referenced by infrastructure | 1 |
| Referenced by tests | 2 |
| Unknown/no exact repository reference | 1,064 |

`UNKNOWN` does not mean safe to move. It means the exact path was not found by
the bounded source scan or the source is external. Phase B1 must repeat `rg` on
the then-current integration base before each move.

## Owner-review queue

- 148 shared/unverified-phase records remain blocked after excluding four source/build files.
- Potentially sensitive-name records remain blocked without content inspection.
- 81 active-worktree-only records remain in place.
- DataHub branch-only evidence requires DataHub owner coordination and link updates.
- Unknown hashes remain separate; they are not deduplication candidates.
- External durable store name, retention, and access owner are not selected here.

## Reproduction

From any clean checkout containing this commit:

```bash
python3 docs/migration/evidence/build_evidence_migration_map.py \
  --inventory docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv \
  --repo-root . \
  --map-output /tmp/EVIDENCE_MIGRATION_MAP_V2.csv \
  --validation-output /tmp/EVIDENCE_MAPPING_VALIDATION.json

cmp docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
  /tmp/EVIDENCE_MIGRATION_MAP_V2.csv

cmp docs/migration/evidence/EVIDENCE_MAPPING_VALIDATION.json \
  /tmp/EVIDENCE_MAPPING_VALIDATION.json

python3 docs/migration/evidence/validate_evidence_migration_map.py \
  docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
  --expected-rows 1445
```

The build reads the versioned sanitized inventory metadata only. It does not
open evidence artifacts or perform migration operations. Verify the snapshot
checksum first as documented in `EVIDENCE_INVENTORY_SNAPSHOT.md`.
