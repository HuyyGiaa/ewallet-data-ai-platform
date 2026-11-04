# I7 documentation count reconciliation

I7 preserves the accepted Phase A 99-row registry as
`phase_a/DOCUMENTATION_REGISTRY_PHASE_A_SNAPSHOT.csv` and deterministically
reconciles it with the source integrated through I6.

| Scope | Count | Explanation |
|---|---:|---|
| Accepted Phase A snapshot | 99 | 96 clean-checkout documents plus the three Phase A navigation indexes |
| I6 tracked Markdown documents outside `docs/migration/` | 134 | Actual `git ls-files '*.md'` result at I6 |
| Phase A indexes absent at I6 and added by I7 | 3 | `docs/edai2/README.md`, `docs/evidence/README.md`, `docs/runbooks/README.md` |
| Documents added after the Phase A snapshot | 38 | I2-I6 DataHub, RAG, CI/CD, Observability, Streaming and evidence companions |
| I7 registry | 137 | 99 preserved classifications plus 38 new `UNKNOWN` classifications |

The historical 107-versus-99 reconciliation remains unchanged: 107 filesystem
snapshot paths minus 11 local-only/untracked paths plus three Phase A indexes
equals 99. I7 does not retroactively rewrite that audit.

All 137 I7 paths exist. The 38 later documents are deliberately `UNKNOWN`;
their existence does not establish runtime verification or rubric completion.
Migration-control documents under `docs/migration/` are excluded from the
product-document registry to avoid a self-referential registry. They remain
versioned and link-validated as integration records.

Rebuild:

```bash
python3 docs/migration/phase_a/build_documentation_registry.py \
  --repo-root . \
  --snapshot docs/migration/phase_a/DOCUMENTATION_REGISTRY_PHASE_A_SNAPSHOT.csv \
  --paths docs/migration/phase_a/DOCUMENTATION_PATHS_I7.txt \
  --output /tmp/DOCUMENTATION_REGISTRY.csv
cmp docs/DOCUMENTATION_REGISTRY.csv /tmp/DOCUMENTATION_REGISTRY.csv
```
