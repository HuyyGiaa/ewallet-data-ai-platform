# Phase A + Phase B0 implementation report

## Scope and Git identity

- Repository root: `/home/huyy-giaa/Develop/ewallet-data-ai-platform`.
- Canonical branch/base: `feature/phase2-fraud-platform` at
  `8ed5b94ef75a818d69d13085d68c9703a785d57c`.
- Feature worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/docs-evidence-cleanup`.
- Feature branch: `codex/docs-evidence-phase-a-b0`.
- Phase A commit: `7639a90658357edb0a77d617f0e71c8f233916ca`.
- Phase B0 commit: the commit containing this report; resolve with
  `git log -1 --format=%H -- docs/migration/PHASE_A_B0_IMPLEMENTATION_REPORT.md`.

The implementation used the frozen architecture/Living Decisions through
Addendum 15, the active EDAI-1/EDAI-2 workbook sheets, and the preserved
read-only repository audit.

## Phase A changes

- Updated `docs/README.md` as the central entrypoint.
- Updated `docs/edai1/README.md` with DP1, DP2, DP3, streaming, DataHub, schema,
  governance, and benchmark navigation.
- Added `docs/edai2/README.md` for Feature Store, KFP, MLflow, KServe, LLM, RAG,
  agents/MCP/gateway, CI/CD, observability, A/B, and load testing.
- Added `docs/runbooks/README.md` without changing existing commands or paths.
- Added `docs/evidence/README.md` with current-location and Phase B0 rules.
- Added `docs/DOCUMENTATION_REGISTRY.csv` with 99 paths:
  `CURRENT=5`, `HISTORICAL=30`, `PARTIAL=35`, `PROPOSED=1`, `UNKNOWN=28`.
- Added a reproducible documentation link validation report.

Phase A link result: 6 Markdown files, 136 repository-relative links, zero
missing or repository-escaping targets. All 99 registry paths exist in the
feature worktree.

## Phase B0 changes

- Added `EVIDENCE_MIGRATION_MAP_V2.csv` with 1,445 explicit dispositions.
- Added manifest schema, collision analysis, storage policy, migration preview,
  post-merge runbook, copy-ready Living Decision entry, deterministic builder,
  and non-destructive validator.
- Preserved the original audit inventory outside the repository unchanged and
  added a sanitized metadata-only snapshot for clean-checkout reproduction.

### Collision result

| Metric | Result |
|---|---:|
| Original collision groups | 238 |
| Original collision rows | 762 |
| Same-content groups / rows | 116 / 260 |
| Different-content groups / rows | 85 / 327 |
| Unknown-hash collision groups / rows | 37 / 175 |
| All unknown-hash artifacts | 398 |
| Misclassified source/build artifacts | 204 |
| External artifact references | 134 |
| Corrected destination collisions | 0 |
| Manual-review items | 245 |

The validator reports 1,445 unique sources, 1,445 unique artifact IDs, zero
duplicate destinations, and zero errors. All rows remain
`PLANNING_ONLY_NOT_MIGRATED`.

## Intentionally not changed

- No evidence file moved, copied, renamed, deleted, uploaded, or rehashed.
- No application source, Docker/Helm/CI configuration, runtime config, or
  existing runbook command changed.
- No canonical, G1, RAG, DataHub, Streaming, MLOps, DP2, or DP3 worktree changed.
- No cloud infrastructure or pipeline was accessed or executed.
- No merge or push was performed.
- Phase B1 is not authorized.

## Validation commands

```bash
git diff --check

python3 docs/migration/evidence/validate_evidence_migration_map.py \
  docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
  --expected-rows 1445

python3 docs/migration/evidence/build_evidence_migration_map.py \
  --inventory docs/migration/evidence/EVIDENCE_INVENTORY_SNAPSHOT.csv \
  --repo-root . \
  --map-output /tmp/EVIDENCE_MIGRATION_MAP_V2.csv \
  --validation-output /tmp/EVIDENCE_MAPPING_VALIDATION.json

cmp docs/migration/evidence/EVIDENCE_MIGRATION_MAP_V2.csv \
  /tmp/EVIDENCE_MIGRATION_MAP_V2.csv
```

The apparent documentation-count difference is reconciled in
`docs/migration/DOCUMENTATION_COUNT_RECONCILIATION.md`: 107 audit-snapshot paths
minus 11 local-only/untracked paths plus 3 new Phase A indexes equals the
99-row clean-checkout registry.

## Merge readiness and remaining gates

The final read-only recheck found canonical still at the original base. The
merge-base is exactly `8ed5b94ef75a818d69d13085d68c9703a785d57c`, and a
three-tree `git merge-tree` simulation against canonical produced zero conflict
markers.

Three non-canonical branches overlap files changed here:

| Branch | Overlapping path | Integration requirement |
|---|---|---|
| `codex/datahub-quality-assertions` | `docs/edai1/README.md` | Preserve newer DataHub assertion status while retaining approved taxonomy/navigation |
| `codex/edai1-implementation` | `docs/edai1/README.md` | Semantic merge of campaign links/status; do not choose blind ours/theirs |
| `codex/feast-training-runtime` | `docs/README.md` | Retain Feast runtime navigation and central index structure |

These are future branch-integration risks, not conflicts with the current
canonical HEAD. Re-run overlap and merge-tree checks if canonical advances.
The two commits are independently reviewable; evidence migration remains blocked.

Owner review is required for 164 blocked records and 81 active-worktree records.
Phase B1 additionally requires component checkpoints, path-specific `rg`,
checksum-before/after, a real durable location for 134 external artifacts, and
DataHub-owner coordination for the superseded evidence path.

## Living Decisions

The canonical Google Doc was read through Addendum 15. No authenticated editing
connector was available, so it was not modified. A copy-ready entry is provided
in `docs/migration/LIVING_DECISION_COPY_READY.md`; the next decision number must
be taken from the canonical document at write time.

## Exact next step

Review both commits and the 245-row owner-review queue. If accepted, authorize a
small Phase B1 pilot for one inactive component only. Do not begin broad evidence
migration or DataHub relocation until the relevant owners checkpoint their
worktrees and the durable external storage contract is approved.
