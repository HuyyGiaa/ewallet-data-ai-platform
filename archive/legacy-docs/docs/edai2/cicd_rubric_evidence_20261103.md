# EDAI-2 CI/CD evidence index: rows 33–36, G3 regression and observability

| Item | Value |
|---|---|
| Date | 2026-11-03 |
| Branch | `codex/llm-g2-llmd-routing` (local, not pushed; GitHub links follow integration) |
| Evidence root | `docs/evidence/edai2/cicd_runtime_20261103/` |

Verdict vocabulary:

- **EXECUTOR_REPORTED_PASS**: runtime PASS observed by the executor. Independent owner review is pending.
- **LOCAL_KIND_VERIFIED**: proven on the local kind cluster only.
- **PARTIAL**, **BLOCKED**, **NOT_STARTED**.

## 1. Rubric row 33: CI/CD for the RAG data pipeline

Status: **ROW33_RUBRIC_ACCEPTED_EXECUTOR_EVIDENCE** (owner decision 1, 2026-11-03).

- **CI** is run #7 (`5a1c56c`). **CD** is run #6 (`c9afc5b`). They are separate evidence and not combined.
- GKE rollback is **NOT_VERIFIED**: there was no previous image, and failure injection was not approved.
- POST_MERGE_REPRODUCED is **NOT_VERIFIED** (not merged).
- No further RAG deployments are made in this campaign.

### Pipeline that ran

| Item | Value |
|---|---|
| Jenkins job / build | `edai2-cd-rag-pipeline` #6, SUCCESS, 5 min 20 s, controller `jenkins-controller@sha256:9c301d1a…` on GKE `ewallet-dev`, namespace `ewallet-ci` |
| Jenkinsfile | `Jenkinsfile.llm-agent` → shared `jenkins/pipeline/componentPipeline.groovy` (`run(profile: 'llm-agent')`) |
| Job definition | Job DSL in `infra/kubernetes/jenkins/jenkins.yaml` (`createPipeline('edai2-cd-rag-pipeline', '*/release/rag-pipeline', …)`) |
| Catalog entry | `jenkins/catalog/components.json` → `rag-pipeline` (handler `scripts/ci/deploy/rag_pipeline.sh`, target `infra/kubernetes/cicd-runtime/rag`, artifact `images/rag-ingestion`) |
| Release SHA / base | `c9afc5b145b2c0ac1f412f29249f12b5ba86024a` (branch `release/rag-pipeline`) / `aba2fcd960dc78ae4287eeba6bf30a3f5289450d` |
| Source snapshot | `jenkins-source-snapshot@sha256:199eb593…`, bundle sha256 `ed16d708…` (`batch-c4/`) |

### What each step does

| # | Stage | What it does | Result |
|---|---|---|---|
| 1 | Checkout and provenance | Checks out `release/rag-pipeline` from the immutable bundle; fails unless HEAD equals `EXPECTED_GIT_SHA` | SUCCESS |
| 2 | Validate catalog and detect scope | `scripts.ci.catalog validate`; `scripts.ci.change_detection` diff base…release, which selected `rag-pipeline`, `mcp-agents` and `agents` | SUCCESS |
| 3 | Component CI | `uv lock --check` and `uv sync --locked` per project, then the catalog test commands through the JUnit runner (`scripts/ci/junit_unittest.py`) | SUCCESS |
| 4 | Controlled release gate | Jenkins `input` approval (owner) | SUCCESS |
| 5 | Verify release branch | `origin/release/rag-pipeline` must resolve to the SHA | SUCCESS |
| 6 | Build and publish (Cloud Build) | `scripts/ci/cloud_build.py`: `git archive <sha>` → create-only upload → build `infra/docker/rag-ingestion/Dockerfile` as `ewallet-cloudbuild`, VERIFIED provenance | SUCCESS, build `aae05981-dc07-4f3f-ac3a-b2e4fbfe6056` |
| 7 | Deploy and verify | `scripts.ci.release_request` → `release_policy build` (digest-only plan) → `deploy_release.sh` → `rag_pipeline.sh` | SUCCESS |

### Artifacts and runtime

- **Image:** `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/rag-ingestion@sha256:1dd91888548968e304d735d962c4eb3fc50560d092ca050efb6240e64e2dcba3`.
  - Source object `gs://…-cicd-builds/source/c9afc5b…/rag-ingestion.tar.gz#1793692741046271`, sha256 `b4ee81c8…`.
  - SLSA v0.1 provenance recorded in Container Analysis.
- **Deployment:** CronJob `ewallet-cicd/rag-ingestion`, `suspend=true`, pinned to that digest. Annotation `ewallet.io/release-sha=c9afc5b…`, field manager `ewallet-jenkins-cd`. Deploying never starts ingestion.
- **Smoke Job** `rag-cd-c9afc5b145b2-6`, Complete. It ran all 6 DAG stages against an ephemeral in-namespace Redis.
  - `publication_identity` is `b85ae796…`, identical to the production publication, so chunking and embeddings are unchanged.
  - 4 chunks, 384 dimensions, readback MATCH, recall@2 1.0, MRR 1.0, 0 citation errors, `release_sha` matches.
  - Verdict: `scripts/ci/verify_rag_smoke.py` against `jenkins/cd/rag-pipeline.contract.json`.
- **Cleanup:** the smoke Job, the Redis Deployment and its Service were deleted; only the suspended CronJob remains (`rag-cd-run6/postdeploy-state.txt`).
- **Production untouched:** Redis DB0/DB1, Airflow, `llm-agent` r32, `llm-agent-mcp` r6.

### JUnit: 186 passed, 0 failed, 61 skipped

| Suite | Tests | Skipped | Skip reason |
|---|---|---|---|
| `component:agents:1` (Coordinator BYO, idempotency) | 36 | 0 | — |
| `component:agents:2` (G3 orchestration and runtime config) | 73 | 22 | `helm binary unavailable`: chart-render tests. The controller deliberately ships no `helm`. |
| `component:mcp-agents:1` | 138 | 39 | `helm binary unavailable` (same) |

All 61 skips are Helm chart-rendering tests. They run where `helm` exists; they are not executed in Jenkins.

**CI coverage closure: `edai2-cd-rag-pipeline` #7 (CI-only, `DEPLOY_APPROVED=false`, 2026-11-03).**

#6 had run only `test_phase_c` (11 tests) for `rag-pipeline`, through `validate_rag_pipeline.sh` and outside JUnit. Run #7 closes that gap:

| Item | Value |
|---|---|
| Release SHA / base | `5a1c56c6818088a2f9ea6cdfc57eb4e1fadf34f7` / `a7fc52e266e3103bcf96d977c71644a6dbe40d3e`. Both contain `b23c10e`, which adds the RAG suite, and `a7fc52e`, which puts `test_phase_c` into JUnit. |
| Snapshot | `jenkins-source-snapshot@sha256:c1dc81c9…`, bundle sha256 recorded in `batch-c5/source-metadata.txt`. Pre-apply `kubectl diff`: snapshot digest and SHA only. Jobs, build history, PVC UID and credentials unchanged (`batch-c5/pre-post-state.txt`). |
| Result | SUCCESS in 90 s. Stages: checkout/provenance, scope, Component CI. No release stage ran: no build, publish or deploy, and no image tag or digest touched. |
| JUnit | 308 tests: **246 passed, 0 failed, 0 errors, 62 skipped, 0 deselected** (unittest has no deselection) |
| RAG suites (all through JUnit) | `test_phase_c` 11/0/0; `test_rag_airflow_ingestion` + `test_rag_online_retriever` + `test_rag_chunk_lookup` 50 tests, 0 failed, 1 skipped |
| Release-change tests collected and passed | `test_cd_contract_matches_the_corpus_manifest`, `test_publication_records_the_release_that_produced_it`, `test_image_release_sha_identifies_runs_without_git`, `test_full_pipeline_publishes_reads_back_and_cites_sources` |
| Skips | 61 Helm chart-render tests in `agents`/`mcp-agents` (`helm binary unavailable`; the controller ships no helm). 1 opt-in real-ONNX RAG test (`EWALLET_RAG_MODEL_TESTS=1`). The real model was exercised at runtime by the #6 smoke Job inside the image. Neither skip covers a row 33 acceptance criterion. |
| Evidence | `jenkins/jobs/edai2-cd-rag-pipeline-7/` (`artifacts.zip` holds the JUnit XML and per-stage logs; `junit-analysis.txt`); `screenshots/jenkins-cd-rag-pipeline-7-tests.png` |

**Not combined across runs:**
- #6 is the CD evidence: build, publish, deploy and smoke for `c9afc5b`.
- #7 is the CI-scope evidence for `5a1c56c`.
- Between the two SHAs, the RAG runtime code differs only by the CD-contract guard test and the CI catalog. The `rag-ingestion` image deployed by #6 was not rebuilt.

### Rollback

- **LOCAL_KIND_VERIFIED:** on kind, as the Jenkins ServiceAccount, a failed verdict restored the previous CronJob release and removed temporary resources.
- **Not GKE_RUNTIME_VERIFIED:** run #6 was the first deployment, so it had no previous image. GKE failure injection was not approved.

### Fail-closed history (runs #1–#5, nothing deployed)

| Run | Stopped at | Cause | Fix |
|---|---|---|---|
| #1 | Cloud Build request | runner lacked `storage.buckets.get` on the logs bucket | Batch A.1 |
| #2 | Source upload | re-upload of the same object needed delete | create-only upload, `b6d5761` |
| #3 | Cloud Build logging | log chunks need delete in the source bucket | dedicated logs bucket, Batch A.2 and `468414c` |
| #4 | Provenance verify | Container Analysis API disabled | API enabled (owner decision) |
| #5 | Deploy (0.5 s) | never-executed heredoc passed `str` to `load_catalog` | `scripts/ci/release_request.py`, `aba2fcd`; full Deploy dry run on kind |

## 2. Rows 34 and 35: CI/CD for MCP servers and Agents, prompt management

The workbook gives rows 34 and 35 identical text: "Build and package MCP servers ⇒ deploy Agent with the corresponding MCP servers; Agent uses the system prompt from a prompt management tool such as Langfuse". The repository matrix splits them as 34 = data Agent + MCP servers and 35 = Coordinator Agent.

| Part | Status | Evidence |
|---|---|---|
| CI for MCP servers and Agents | EXECUTOR_REPORTED_PASS (CI only) | `mcp-agents` 138 and `agents` 109 JUnit tests in run #6 |
| CD (build, deploy MCP + Agent) | **BLOCKED**. The D1 Helm migration needs cluster-scoped admission guards, which are not allowed on GKE, and a helm binary with 0 HIGH CVEs, which does not exist yet (v3.22.1 has 1 HIGH). D1 step 1 was STOPPED and rolled back (Istio CA ConfigMaps exceed the reviewed quota). A narrower alternative, D-lite, is proposed. | `docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md` §9 |
| Prompt management tool | **PARTIAL**: runtime registry verified; agent not yet sourced from it. Security: M1 applied, M2 reviewed (not applied), M3 gate implemented, incident F9 closed. | `prompt-registry/`, `prompt_registry_security_assessment_20261103.md`, `prompt_registry_incident_f9_20261103.md` |

### Prompt management detail

- **agentregistry v0.4.0** runs in `ewallet-prompts` (release `prompt-registry`).
  - Its Postgres is external, digest-pinned and PSS-restricted, because the chart's bundled Postgres violates `restricted` and hardcodes its password.
- **Publication:** 17/17 entries published and read back, all 12 pinned prompt versions plus 3 Agents and 2 MCPServers, on kind and GKE.
- **Resolver** `ops/llm_agent/prompt_registry.py`:
  - resolves explicit versions (v1 and v2 differ);
  - refuses unknown, unpublished and tampered versions;
  - falls back to Git only when the registry is down and fallback is allowed, and records `pinned-git-fallback`;
  - registry data persists across restarts.
- **Production link, read-only:** the Drift Specialist's `systemMessageFrom` ConfigMap `drift-specialist-prompt-v2` has sha256 `0c0798e1…`, identical to registry `drift-specialist-system/v2`. The running agent answers `"prompt_revision":"drift-specialist/v2"`.
- **Not done:** the agent still reads the Git/Helm-rendered ConfigMap. Sourcing it from the registry is `llm-agent` r32→r33, **not approved**.

## 3. Row 36: CI/CD for streaming feature jobs

- **CI:** the `streaming` component runs in the catalog.
- **CD: BLOCKED by ownership.**
  - The runtime-wired Flink, generator and benchmark code is on `origin/main` (46bc732/46fe7b4).
  - This branch has the Feast online writer and history sink, which have never been run.
  - Reconciling the two belongs to the Data Platform workstream.
  - No change was made.

## 4. G3 regression on the current baseline (2026-11-03 08:10 UTC)

Verdict: **PASS**. Evidence: `g3-regression/`.

| Check | Result |
|---|---|
| Business E2E (Coordinator → A2A → Feature-RAG Specialist → MCP Gateway → feature-rag-mcp) | `status=ok`, `unavailable=[]`; 4 Feast features; KServe `fraud-detector/1`, p=0.06399, schema `03c0ea04…`; 3 RAG citations `fraud-policy-v1-20261030` |
| Cross-replica idempotency | replays to both Coordinator pods return the identical artifact. The MCP log shows exactly 3 tool calls (1 execution). Tempo: the original trace has 109 spans across 3 services; each replay trace has 36 Coordinator-only spans. |
| MCP authentication | no token, forged token and revoked token each return 401; the valid service token returns `is_error=false` |
| MCP authorization | the drift token calling `fraud_scoring` gets `Unknown tool` |
| HPA / PDB | 3 HPAs (min 2, max 3) at 2/2; PDBs `minAvailable 1` |
| Sandbox | `kagent-pilot` worker and its egress policy present |
| Releases | unchanged (`llm-agent` r32, `llm-agent-mcp` r6, `kagent` r5, `istiod` r13) |

## 5. Observability coverage (live readback, same run)

| Area | Covered (data present) | Gap |
|---|---|---|
| ML | KFP and MLflow health, Feast client requests and freshness, KServe scrape and client inference, Fraud API scrape | KFP workflow run status (no exporter) |
| LLM | vLLM primary up, 42 successful requests; agentgateway (LLM) 129 requests | A2A/MCP gateways not scraped; vLLM fallback scaled to 0 (target down by design) |
| Agents | Coordinator and Specialist server-span metrics; MCP tool calls per tool incl. `knowledge_search` | Redis exporter; HPA metrics (no kube-state-metrics) |
| Traces | Server spans: `fraud-coordinator`, `feature-rag-specialist`, `feature-rag-mcp`. Client-side only: `feast-online.get_features`, `kserve.infer`, kagent `/api/tasks` | gateways emit no spans |
| Logs | Loki has 3 `mcp.tool.completed` lines correlated by `trace_id` | single log source (`feature-rag-mcp`) |
| Grafana | 30 of 57 Prometheus panel targets return data via `/api/ds/query` | 13 need cAdvisor or kube-state-metrics; 10 absent at source; 4 legitimately empty (error-only) |
| Infra | — | container CPU/RAM/disk/network not scraped (needs a reviewed ClusterRole) |

## 6. Rubric matrix: rows 33–36 (status vocabulary per the 2026-11-03 owner decision)

| Row | Exact requirement (workbook) | SOURCE_READY | TEST_PASS | CI_RUNTIME_PASS | CD_RUNTIME_PASS | BUSINESS_E2E_VERIFIED | RUBRIC_VERIFIED | POST_MERGE_REPRODUCED |
|---|---|---|---|---|---|---|---|---|
| 33 | CI/CD for the data pipelines (demo the RAG data pipeline); proof: documentation of the pipelines that ran, what each step does, and links to the Jenkinsfiles | yes | yes | yes (#7, full RAG scope) | yes (#6: Cloud Build `aae05981`, digest `1dd91888…`, isolated GKE deploy, smoke PASS); rollback LOCAL_KIND_VERIFIED only | n/a (data pipeline; the smoke Job checks the RAG publication contract) | **ROW33_RUBRIC_ACCEPTED_EXECUTOR_EVIDENCE** (owner decision 1); packet `row33-packet/README.md`; GKE rollback NOT_VERIFIED | NOT_VERIFIED |
| 34 | CI/CD for MCP servers and Agents: build and package MCP servers ⇒ deploy Agent with the corresponding MCP servers; Agent uses its system prompt from a prompt management tool (e.g. Langfuse) | partial (D1 migration **BLOCKED**: cluster-scoped VAPs not allowed on GKE, helm binary has a HIGH CVE; D-lite proposed) | yes (`mcp-agents` 138, `agents` 109, chart 35, prompt 7 tests) | yes (CI scope in #6/#7) | **no**: BLOCKED (see §2) | G3 regression PASS on r32 (unchanged) | no | no |
| 35 | Same workbook text as row 34 (repository matrix: Coordinator Agent) | registry and resolver applied; r33 prepared; security: M1 applied, M2 kind-reviewed, M3 gate implemented (writes unauthenticated in 0.4.0, integrity anchored in Git) | yes | yes | no; prompt registry runtime verified in `ewallet-prompts`; agent consumption proven only on the kind canary | canary contract 3/3; production G3 PASS on r32 | no | no |
| 36 | CI/CD for stream feature jobs (to OFFLINE & ONLINE STORE) | CI only | streaming unit tests in the catalog | not run in this campaign | **BLOCKED**: Data Platform ownership (M6: Streaming Block A excluded; reconcile with the integrated Flink V2) | no | no | no |

Missing acceptance gates and owner decisions:

1. **Row 33:** accepted on executor evidence.
   - GKE rollback stays NOT_VERIFIED (it needs approval for a failure-injection run).
   - GitHub links and post-merge reproduction come after integration.
2. **Row 34:**
   - The D1 migration is BLOCKED.
   - Owner decision on D-lite (Batch D §9). D-lite needs no Jenkins privilege in `ewallet-mcp` and no cluster-scoped admission.
   - Do not apply D1 step 1 r2 unless the Helm path is revived.
3. **Row 35:**
   - Optional: M2 approval.
   - Approve `llm-agent` r32→r33 (`docs/edai2/prompt_r33_proposal_20261103.md` §8).
   - Registry write-auth follow-up.
   - Reviewer acceptance of agentregistry in place of Langfuse.
4. **Row 36:**
   - Data Platform integration decision under M6.
