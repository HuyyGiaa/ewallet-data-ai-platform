# Documentation count reconciliation: 107 versus 99

## Scope difference

The prior audit counted 107 paths in its `canonical_worktree` filesystem
snapshot. The Phase A registry contains 99 paths reproducible from this branch's
clean Git checkout. The counts reconcile exactly:

```text
107 audit-snapshot paths - 11 local-only paths + 3 Phase A indexes = 99 registry paths
```

The 96 paths common to both scopes were retained. No tracked clean-checkout
document was omitted.

## Audit-snapshot paths excluded from the registry

| Path | Git state in audited canonical worktree | Clean checkout | Reason excluded |
|---|---|---|---|
| `.superpowers/sdd/2026-10-21-cloud-refactor-foundation/progress.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-21-cloud-refactor-foundation/task-1-brief.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-21-r2-terraform-foundation/progress.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-21-r2-terraform-foundation/task-1-brief.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-22-r4-sharded-generator-gcs-bronze/progress.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-22-r4-sharded-generator-gcs-bronze/task-1-brief.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/2026-10-22-r4-sharded-generator-gcs-bronze/task-2-brief.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `.superpowers/sdd/autonomous-bounded-cloud-data/progress.md` | untracked, ignored | absent | Local SDD working record; not versioned repository documentation |
| `E_WALLET_PROJECT_CONTEXT.md` | untracked, ignored | absent | Local handoff file; not versioned repository documentation |
| `docs/evidence/dp2_method2_ab-20261031035745/README.md` | untracked | absent | Local evidence companion; Phase B1 movement/import is not authorized |
| `docs/evidence/dp3_historical_features_ab-20261025134342/README.md` | untracked | absent | Local evidence companion; Phase B1 movement/import is not authorized |

Exclusion criteria are therefore explicit: a path observed only in the audited
working-directory filesystem, but not tracked and not present in a clean
checkout, is outside the Phase A versioned-document registry. This does not
classify or delete the local artifact and does not make it safe to discard.

## Phase A paths added after the audit snapshot

| Path | Registry status | Reason added |
|---|---|---|
| `docs/edai2/README.md` | `CURRENT` | New EDAI-2 navigation index |
| `docs/evidence/README.md` | `CURRENT` | New evidence navigation and Phase B0 rules |
| `docs/runbooks/README.md` | `CURRENT` | New runbook navigation index |

Registry result remains 99 rows with `CURRENT=5`, `HISTORICAL=30`,
`PARTIAL=35`, `PROPOSED=1`, and `UNKNOWN=28`.
