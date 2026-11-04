# Row 33 verification packet (2026-11-03)

## Source-equivalence verdict

**Equivalent for the deployed RAG runtime.**

- CD release `c9afc5b` (run #6) is an ancestor of CI release `5a1c56c` (run #7), with 8 commits between them (`source-equivalence.txt`).
- Inside the `rag-ingestion` image build context:
  - The Dockerfile, `pyproject.toml`, `uv.lock`, `data/llm_agent/rag`, `ai_platform/*`, `data_platform/*` and `scripts/ci/rag_smoke.sh` are tree-identical.
  - Only `ops/llm_agent/` differs: `prompt_registry.py` (new) and `registry_catalog.py`.
- Neither file is referenced by the RAG runtime path (`ops/llm_agent/rag_*.py`, `ai_platform/llm_agent/rag`, `scripts/ci/rag_smoke.sh`).
- Importing `ops.llm_agent.rag_ingest` loads only `ops.llm_agent` and `ops.llm_agent.rag_ingest`.
- A rebuild at `5a1c56c` would differ in those two inert `.py` files only. The image deployed by #6 was not rebuilt.

## Attribution (kept separate)

| Run | Role | SHA | Evidence |
|---|---|---|---|
| `edai2-cd-rag-pipeline` #6 | CD | `c9afc5b` | build, publish, deploy, smoke |
| `edai2-cd-rag-pipeline` #7 | CI | `5a1c56c` | full RAG test scope |

## Artifact checks (`artifact-checks.txt`, re-run 2026-11-03)

| Item | Result |
|---|---|
| Artifact Registry tag `c9afc5b…` | `sha256:1dd91888…` |
| Cloud Build `aae05981` | SUCCESS, `requestedVerifyOption=VERIFIED`, runner `ewallet-cloudbuild`, digest `1dd91888…` |
| Container Analysis | 3 BUILD occurrences, including SLSA provenance v0.1 and v1 |
| GKE CronJob `ewallet-cicd/rag-ingestion` | same digest, `suspend=true`, release-sha `c9afc5b` |
| JUnit XML in archived artifacts | #6: 3 files, manifest `git_sha` `c9afc5b`. #7: 5 files, manifest `git_sha` `5a1c56c` |
| Screenshots | stage view, #6 tests, #7 tests: present |
| Source links (Jenkinsfile.llm-agent, shared pipeline, catalog, handler, Dockerfile, CronJob manifest, CD contract, Cloud Build client) | resolvable at both SHAs |
| Evidence `SHA256SUMS.txt` | verifies (rc=0) |

## Acceptance requirements

| Requirement | Status |
|---|---|
| Pipelines that ran | met: runs #6 and #7 |
| What each step does | met: `docs/edai2/cicd_rubric_evidence_20261103.md` §1 |
| Jenkinsfile links | met (repository-relative; GitHub links after integration) |
| Test, build and deploy evidence | met: JUnit, Cloud Build, provenance, deploy, smoke, cleanup |
| GKE rollback | **NOT_VERIFIED** (only kind) |
