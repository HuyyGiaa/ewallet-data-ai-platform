# B1 testing runs at commit 281db72 (2026-11-06)

Raw results of the owner's B1 evidence session, copied byte-for-byte from `~/Develop/ewallet-b1-evidence/results/`. The `.coverage` database and the generated HTML report are left out: both can be regenerated, and the HTML is shown in `28_02`.

| Folder | Row | Command (repository scripts) | Result |
|---|---|---|---|
| `coverage/` | 28 | `scripts/testing/run_coverage.sh` | 199 passed; 99.03% (1,077 statements, 260 branches) |
| `equivalence-boundary/` | 29 | `uv run --locked --project ai_platform/inference --group test python -m pytest -v` on the four boundary modules | 70 passed, 77 subtests |
| `mutation/` | 30 | `scripts/testing/run_mutation.sh` | 1,040 mutants; actionable 94.69% |
| `property/ci`, `property/thorough` | 31 | `scripts/testing/run_property_tests.sh` with `HYPOTHESIS_PROFILE=b1-ci` / `b1-thorough` | 551 passed, 13 xfailed (both) |

Provenance:
- each folder's `provenance.txt` records `git_commit=281db72…`, the branch `feature/b1-testing-quality`, the UTC time and the `uv.lock` SHA-256;
- `source-sha.txt` is the export SHA.

Screenshots: [`../../screenshots/testing/`](../../screenshots/testing/EVIDENCE_MANIFEST.md).
