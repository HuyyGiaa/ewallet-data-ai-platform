# Canonical integration handoff inventory (Living Decision 2026-11-03.M6)

> **Superseded** by `cicd_campaign_closure_handoff_20261103.md` (HEAD `a4634e6`). Kept for history.

Inventory for a future owner-approved source freeze. **Not SOURCE_FROZEN and not INTEGRATION_READY.** No canonical merge, push or integration branch was created.

## 1. Source identity (recorded before this document's own commit)

| Item | Value |
|---|---|
| Worktree / branch | `.worktrees/codex-llm-g2` / `codex/llm-g2-llmd-routing` (owner: Claude Code G2/G3/CI/CD) |
| HEAD / tree | `90ed0e562676a076d4571389e662a5be229ed98a` / `eb96d758ec327e014699655c09f7e7e6275d38ea` |
| Files | 2748 tracked; 725 untracked (earlier-campaign raw evidence, below); ignored = virtualenvs, caches, private scans |
| Boundary | 57 commits since the historical Agent Platform checkpoint `66f3915`; 38 since `f9267d7` (start of this CI/CD campaign) |
| Release refs (local) | `release/rag-pipeline` → `5a1c56c6818088a2f9ea6cdfc57eb4e1fadf34f7` |
| Protected, untouched | `integration/final-integration-20261101@50e9e733`, `submission/edai1-ml-stable-20261102@c754ecf9`, `codex/streaming-block-a@3fa7c483` (excluded from integration), `main@bf153282` |

## 2. CI/CD source of record

| Concern | Path |
|---|---|
| Jenkinsfiles | `Jenkinsfile`, `Jenkinsfile.ml`, `Jenkinsfile.llm-agent` (thin wrappers), `Jenkinsfile.rag` (legacy local Airflow) |
| Shared pipeline | `jenkins/pipeline/componentPipeline.groovy` |
| Catalog / CD contract | `jenkins/catalog/components.json`, `jenkins/cd/rag-pipeline.contract.json` |
| CI/CD modules | `scripts/ci/{catalog,change_detection,run_ci,junit_unittest,cloud_build,k8s_api,release_request,release_policy,verify_rag_smoke}.py`, `scripts/ci/deploy_release.sh`, `scripts/ci/deploy/rag_pipeline.sh`, `scripts/ci/rag_smoke.sh` |
| Jenkins runtime | `infra/kubernetes/jenkins/jenkins.yaml` (template), `scripts/ci/render_jenkins_runtime.py`, `infra/docker/jenkins-{controller,source-snapshot}/` |

**Live Jenkins** (`ewallet-ci`):
- controller `jenkins-controller@sha256:9c301d1a7fa4e8a6a2de8eafbc85c664eda292c6170f1168f71982c1fee5565d`
- source snapshot `jenkins-source-snapshot@sha256:c1dc81c93e5cf7fff962627367d5a57741195d6a59459f75b876c5aae948a45f` (source SHA `5a1c56c`; bundle sha256 in `docs/evidence/edai2/cicd_runtime_20261103/batch-c5/source-metadata.txt`)

## 3. Applied versus proposed infrastructure

| Change | State | Source |
|---|---|---|
| Batch A (+A.1, A.2): `ewallet-jenkins`, `ewallet-cloudbuild`, custom role, WI, build and log buckets, repo writer | **APPLIED** (gcloud; Terraform source matches, import pending) | `infra/terraform/gcp/modules/cicd_build` |
| Container Analysis API | **APPLIED** | owner decision |
| Batch B: `ewallet-cicd`, `ewallet-prompts`, quotas, LimitRanges, NetworkPolicies, Jenkins Roles | **APPLIED** | `infra/kubernetes/cicd-runtime/namespaces.yaml`, `infra/kubernetes/jenkins/rbac/batch-b-isolated.yaml` |
| Batch C (+C.1–C.5): Jenkins WI, projected token, metadata egress, CD jobs, snapshots | **APPLIED** | `infra/kubernetes/jenkins/jenkins.yaml` |
| Prompt registry (`prompt-registry` r1, external Postgres) | **APPLIED** in `ewallet-prompts` | `infra/helm/agentregistry/values-ewallet-prompts.yaml`, `infra/kubernetes/prompt-registry/postgres.yaml` |
| RAG CronJob (suspended) | **APPLIED by Jenkins** in `ewallet-cicd` | `infra/kubernetes/cicd-runtime/rag/` |
| Batch D1 (`ewallet-mcp-release`, RBAC, record migration) | **PROPOSED** (kind-validated) | `infra/kubernetes/jenkins/rbac/batch-d1-proposal.yaml`, `docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md` |
| `llm-agent` r33 (registry prompt for drift-specialist) | **PROPOSED** (kind canary) | `docs/edai2/prompt_r33_proposal_20261103.md` |
| Batch D production proposal (old) | superseded by D1 | `infra/kubernetes/jenkins/rbac/batch-d-production-proposal.yaml` |

## 4. Production runtime (2026-11-03)

**Helm releases:**
- `llm-agent` default r32
- `llm-agent-mcp` ewallet-mcp r6
- `kagent` r5, `kagent-crds` r1
- `istio-base`, `istio-cni`, `istiod`, `ztunnel` r13
- `kserve` r1, `kserve-crd` r1
- `substrate` r1, `substrate-crds` r1
- `cert-manager` r1
- `prompt-registry` ewallet-prompts r1 (new)

**Key image digests:**

| Component | Image |
|---|---|
| G3 coordinator | `g3-byo-coordinator@sha256:e85a03f2…` |
| Feature-RAG Specialist | `g3-byo-feature-rag-specialist@sha256:bb0fae06…` |
| MCP (`llm-agent-mcp`) | `mcp-tools@sha256:3209d01e…` |
| MCP (`llm-agent` drift-mcp) | `mcp-tools@sha256:91223fcf…` |
| vLLM | `vllm-cpu-model@sha256:f695b23e…` |
| agentgateway | `agentgateway:v1.7.0@sha256:60216aaf…` |
| kagent go agent | `golang-adk:0.10.3@sha256:5ee9592c…` |
| KServe predictor | `fraud-detector-predictor@sha256:cba90c22…` |
| Fraud API | `fraud-api@sha256:20e9c2ad…` |
| MLflow | `mlflow:v3.17.1-gcs-postgres-1` (tag, **not digest-pinned**; pre-existing) |
| Prompt registry | server `@sha256:a2f69bd3…`, postgres `@sha256:3a426452…` |
| RAG CronJob | `rag-ingestion@sha256:1dd91888…` |

## 5. Jenkins builds and outcomes

| Build | SHA | Outcome |
|---|---|---|
| `edai2-ml-ci` #1 / #2 | `cbb9b5f` / `8074651` | CPS compile failure / SUCCESS |
| `edai2-agent-ci` #1, `edai2-docs-change-detection` #1 | `8fad87f` / `7570de8` | SUCCESS / SUCCESS |
| `edai2-agent-negative` #1 | `8fad87f` | expected FAILURE (gate exit 42) |
| `edai2-cd-rag-pipeline` #1–#5 | `5eb326f`, `8625002`, `19582d6`, `48bf89c` | fail-closed, nothing deployed (causes fixed) |
| `edai2-cd-rag-pipeline` #6 | `c9afc5b` | **CD SUCCESS**: Cloud Build `aae05981`, digest `1dd91888…`, smoke PASS |
| `edai2-cd-rag-pipeline` #7 | `5a1c56c` | **CI SUCCESS** (CI-only): 246 passed, 0 failed, 62 skipped |

## 6. Prompt registry content

- 12 prompt versions from `infra/helm/llm-agent-platform/prompts/manifest.json`:
  - drift-specialist v1–v2
  - feature-rag-specialist v1–v5
  - fraud-coordinator v1–v5
- Plus 3 Agents and 2 MCPServers; published read-back 17/17.
- Production drift-specialist uses v2 (sha256 `0c0798e185cd5f86734069b7688badd0ab0aea2a416cab29f62767d8e4375fef`), byte-identical to registry v2.

## 7. Evidence

- **Sanitized (committed):**
  - `docs/evidence/edai2/cicd_runtime_20261103/` with `SHA256SUMS.txt`
  - `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/` (earlier campaign)
- **G3 regression:** `docs/evidence/edai2/cicd_runtime_20261103/g3-regression/`
- **Private (must not be committed or pushed):**
  - full project and repository IAM policies (`batch-a/project-iam-*.json`, `ar-iam-*.json`, `bucket-iam.json`)
  - Trivy JSON (`jenkins/scans/`)
  - Helm values and manifests and the record backup in the operator scratchpad
  - the 725 untracked raw files of earlier campaigns (`docs/evidence/llm_agent/agent_idempotency_runtime_20261102T114946Z/`, `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/` raw parts, `docs/evidence/llm_agent/g3_20261101T170735Z/`, `docs/evidence/llm_agent/rag_g3-gke-feast-redis/`). These need review before any commit.

## 8. Outstanding changes and rubric gaps

- **Row 33:** owner review; GKE rollback not verified.
- **Row 34:** Batch D1 approvals (namespace and RBAC, migration, first MCP CD run).
- **Row 35:** r33 approval; registry write authentication; reviewer acceptance of agentregistry in place of Langfuse.
- **Row 36:** Data Platform decision.
- **Observability gaps:** KFP run state, A2A/MCP gateway scrape, Redis exporter, HPA and container metrics, single log source.
- **Terraform:** `terraform import` of the Batch A resources is still to do.

## 9. Ownership and active processes

- **Other worktrees with uncommitted changes (not modified here):**
  - primary checkout `feature/phase2-fraud-platform` (44 entries; Data/ML workstream)
  - `codex/streaming-block-a` (14; excluded, preserved)
  - `llm-agent-rubric-20261030` (2)
- **Active local processes:** none besides a Jenkins port-forward (closed at the end of this campaign) and the local kind clusters (`llm-agent-local`, `edai-cluster`) and registry.
- No background Jenkins build was running.
