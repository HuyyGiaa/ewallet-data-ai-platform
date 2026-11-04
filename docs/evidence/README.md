# Evidence index and migration status

Evidence currently exists in three scopes:

- tracked, untracked, and ignored artifacts under `docs/evidence/`;
- local external artifacts under `~/ewallet-evidence/`;
- files present only in active worktrees.

Phase A/B0 does not move, copy, rename, delete, or upload any artifact. The
existing locations remain authoritative until a later owner-approved Phase B1.

## Navigation

| Area | Current index/report | Status |
|---|---|---|
| EDAI-1 | [EDAI-1 documentation and evidence links](../edai1/README.md) | Mixed historical/partial/current artifacts |
| EDAI-2 | [EDAI-2 documentation and evidence links](../edai2/README.md) | Mixed historical/partial/current artifacts |
| Rubric | [Rubric audit](../rubric/rubric_audit.md) | Point-in-time crosswalk |
| Migration planning | `docs/migration/evidence/` | Phase B0 mapping only; not migrated |

## Approved future taxonomy

- EDAI-1: `docs/evidence/edai1/{dp1,dp2,dp3,streaming,datahub}/`.
- DataHub: `docs/evidence/edai1/datahub/{lineage,assertions,datasets_schema}/{screenshots,api}/`.
- EDAI-2: `docs/evidence/edai2/{mlops,llm,rag,agents,cicd,observability}/`.

Additional subdirectories are allowed only when the rubric or actual component
responsibility requires them. The older DataHub location
`docs/evidence/images/datahub/` is superseded but remains untouched while its
owning worktree is active.

## Evidence rules

- A file's existence is not proof that a rubric criterion passed.
- Runtime evidence must identify the source Git SHA, run ID, timestamp, source
  system, artifact type, checksum, and reproduction runbook when available.
- Screenshots supplement API/log readback; they do not replace machine-readable
  runtime identity.
- Large datasets, Parquet output, model weights, and full traces stay outside Git
  in an approved durable store. A local absolute path alone is not portable.
- Historical failed runs and exact error logs must not be rewritten as success.
- No artifact is `MIGRATED` during Phase B0. Allowed planning dispositions are
  `PLANNED`, `KEEP_IN_PLACE`, `EXTERNAL_REFERENCE`, `BLOCKED_FOR_REVIEW`, and
  `NOT_EVIDENCE`.

The collision-safe mapping, validation summary, and storage policy are produced
under `docs/migration/evidence/`. They preserve the original audit inventory and
must be reviewed before any Phase B1 migration.
