# EDAI-2 CI/CD campaign closure and source-freeze candidate handoff

| Item | Value |
|---|---|
| Owner decisions | "Formal CI/CD campaign closure & source freeze preparation" (2026-11-03); Living Decision 2026-11-03.M6 |
| Status | **CI/CD campaign CLOSED.** Source freeze **candidate** prepared |
| Not declared | `SOURCE_FREEZE_OWNER_APPROVED`, `CANONICAL_INTEGRATION_AUTHORIZED` |
| Not done | No merge, push, integration branch or cloud mutation in this handoff |
| Supersedes | The previous version of this file (written at `26eaf8f`) and `integration_handoff_inventory_20261103.md` (`90ed0e5`) |
| Machine-readable inventory | `docs/evidence/edai2/source_freeze_20261103/` |

## 1. CI/CD campaign closure summary

These are the results as the owner recorded them. No missing runtime evidence has been promoted.

| Item | Recorded result |
|---|---|
| Row 33, RAG data-pipeline CI/CD | **ROW33_RUBRIC_ACCEPTED_EXECUTOR_EVIDENCE**. CD is Jenkins `edai2-cd-rag-pipeline` #6 (`c9afc5b`). CI is #7 (`5a1c56c`). The two runs are kept separate. |
| Row 34, MCP + Agent CI/CD | **CI verified**. Production MCP/Agent CD was **not executed**. |
| Row 35, prompt management | **Prompt Registry verified**. Production Agent prompt consumption was **not deployed** (r33 not deployed). |
| Row 36, streaming CI/CD | **BLOCKED** pending canonical Data Platform integration |
| G3 regression | **PASS on executor evidence**: 08:10 UTC on r32, and 14:13 UTC after M1 |
| GKE rollback (row 33) | **NOT_VERIFIED** |
| POST_MERGE_REPRODUCED | **NOT_VERIFIED** |

## 2. Git and worktree inventory

### 2.1 Source identity

All checks are read-only and were taken at 2026-11-03 15:07 UTC.

| Item | Value |
|---|---|
| Worktree | `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2` |
| Branch | `codex/llm-g2-llmd-routing` (local only, never pushed) |
| **Content checkpoint** (HEAD before this handoff commit) | `26eaf8ffaf0af0bc1a27b8a245e6bbfedc13c856`, tree `84a92bc0916989f252a7466b92b59034c29f70b2` |
| Working tree at the checkpoint | 0 staged, 0 modified tracked files, 725 untracked, 173 ignored entries |
| Freeze candidate | The commit that adds this revision of the file. Its parent is `26eaf8f`, and it changes only this file and `docs/evidence/edai2/source_freeze_20261103/`. The final SHA and tree are reported with the handoff. Check with `git diff --stat 26eaf8f <candidate>`. |
| Ancestry | `90ed0e5` ⊂ `a4634e6` ⊂ `26eaf8f` ⊂ candidate (linear). The branch is 354 commits ahead of local `main@bf153282`, and `main` has 0 commits that are not on this branch. |
| Stash stack | 2 entries. The stack is shared by all worktrees and is not owned by this campaign; it was left untouched. |

### 2.2 Reconciliation of the three reported SHAs

| SHA | What it is |
|---|---|
| `90ed0e562676…` | Commit "row 33 CI closure (#7) and rows 33–36 rubric matrix". It is the HEAD recorded by the old M6 inventory `integration_handoff_inventory_20261103.md`, and it precedes the D1 step 1 package, the security work and the closure. |
| `a4634e6ec0ca…` (tree `c9055266…`) | Commit "evidence checksums over the 250 tracked files". It is the last commit that changes source or evidence. The earlier closure handoff called it the "source snapshot". |
| `26eaf8ffaf0a…` (tree `84a92bc0…`) | Commit "CI/CD campaign closure and canonical-integration handoff". Relative to `a4634e6` it adds only the closure document and a "superseded" note: 2 files, documentation only. It is HEAD at the start of this handoff. |

### 2.3 Untracked and ignored files

Full lists: `untracked-ignored-inventory.txt`.

- **725 untracked files**, unchanged from the previous inventory. All are raw evidence from earlier campaigns that was never committed:

  | Files | Folder |
  |---|---|
  | 605 | `docs/evidence/llm_agent/agent_idempotency_runtime_20261102T114946Z/` |
  | 75 | `docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/` |
  | 36 | `docs/evidence/llm_agent/g3_20261101T170735Z/` |
  | 9 | `docs/evidence/llm_agent/rag_g3-gke-feast-redis/` |

  They are local-only, must be preserved, and are not part of any claim in this handoff.
- **Ignored entries relevant to integration:**
  - 109 private evidence files (§10).
  - 55 build/cache paths (`.venv`, `__pycache__`, tool caches).
  - `.superpowers/sdd/`: 25 local design notes; the folder ignores itself. Preserve them.
  - `infra/terraform/gcp/environments/dev/.terraform/`: provider and module cache from `terraform init`. There is **no local tfstate**.

### 2.4 Protected refs and worktrees

| Ref | SHA | State |
|---|---|---|
| `integration/final-integration-20261101` | `50e9e7333b85` | historical integration, untouched |
| `submission/edai1-ml-stable-20261102` | `c754ecf973f5` | clean EDAI-1/ML submission, untouched |
| `codex/streaming-block-a` | `3fa7c483d9e1` | **EXCLUDED**: not merged, cherry-picked, revived, modified or deleted |
| `main` | `bf1532824808` | local; ancestor of this branch |
| `origin/main` | `2033ab3efc6c` | not fetched or pushed in this campaign |
| `release/rag-pipeline` | `5a1c56c68180` | release ref used by CD #6 (`c9afc5b` ⊂ `5a1c56c`) and CI #7 |

There are 28 worktrees, all preserved; `git worktree list` was read-only. This campaign wrote only in `.worktrees/codex-llm-g2`.

## 3. Source authority and deployment-state matrix

Classes:
1. IMPLEMENTED_AND_DEPLOYED
2. IMPLEMENTED_AND_TESTED_NOT_DEPLOYED
3. PROPOSED_NOT_APPROVED
4. BLOCKED
5. SUPERSEDED_OR_EXCLUDED

"Deployed" means live on GKE `ewallet-dev`, confirmed read-only (`gke-baseline-readonly.txt`).

| Component | Source (paths) | Class | Deployed identity / note |
|---|---|---|---|
| Jenkins controller and runtime | `infra/kubernetes/jenkins/jenkins.yaml`, `infra/docker/jenkins*`, `scripts/ci/render_jenkins_runtime.py`, `jenkins/pipeline/componentPipeline.groovy`, `jenkins/catalog/components.json`, `Jenkinsfile*`, `scripts/ci/*` | 1 | Controller `9c301d1a…`; source snapshot `c1dc81c9…` = `5a1c56c` (§6). The source after `5a1c56c` is **not** in the running snapshot: `ops/llm_agent` M3 gate, probe, catalog entries, docs. |
| Cloud Build path | `scripts/ci/cloud_build.py`, `scripts/ci/release_request.py`, `release_policy.py`, `deploy_release.sh` | 1 | Used by CD #6 (build `aae05981`, provenance VERIFIED) |
| GCP IAM / buckets (Batches A, A.1, A.2) | applied through reviewed REST/gcloud steps; codified in `infra/terraform/gcp/modules/cicd_build/` and `environments/dev` (`enable_in_cluster_cd`, default `false`) | live state: 1; Terraform code: 2 | **Terraform import pending (not authorized).** With the flag `false`, Terraform manages none of it. Setting it `true` without an import would try to create resources that already exist. |
| Batch B/C Kubernetes (namespaces, quotas, RBAC, NetworkPolicy, WI, jobs) | `infra/kubernetes/cicd-runtime/namespaces.yaml`, `infra/kubernetes/jenkins/rbac/batch-b-isolated.yaml`, `jenkins.yaml` | 1 | `batch-b-isolated.yaml` already reflects M1 |
| RAG publication / ingestion CD (row 33) | `ops/llm_agent/rag_ingest.py`, `ai_platform/llm_agent/rag/*`, `infra/docker/rag-ingestion/`, `infra/kubernetes/cicd-runtime/rag/`, `scripts/ci/deploy/rag_pipeline.sh`, `jenkins/cd/rag-pipeline.contract.json` | 1 | CronJob `rag-ingestion@1dd91888…` (`c9afc5b`), **suspended**, 0 Jobs |
| RAG online retrieval (G3) | `ai_platform/llm_agent/rag/online_retriever.py`, `tools/chunk_lookup.py` | 1 | in `mcp-tools@3209d01e…` (`llm-agent-mcp` r6, gitSha `1be6c3e`) |
| G3 Coordinator / Specialists | `ai_platform/llm_agent/*`, `infra/docker/g3-byo-coordinator/` | 1 | `g3-byo-coordinator@e85a03f2…`, `g3-byo-feature-rag-specialist@bb0fae06…`, `drift-specialist` kagent golang-adk 0.10.3 (`llm-agent` r32) |
| MCP servers | `ai_platform/llm_agent/tools/*`, `infra/docker/llm-agent-tooling/` | 1 | `ewallet-mcp`: `mcp-tools@3209d01e…` (r6). `ewallet-agents/drift-mcp`: `mcp-tools@91223fcf…` (r32 values, gitSha `6952799`) |
| Helm chart `llm-agent-platform` | `infra/helm/llm-agent-platform/` | 1, plus one class-2 change | r32 was rendered from `2078a79`. The only chart change since then is `templates/agents.yaml` `promptSource` (default `git`). With r32 values it renders **identically** (semantic diff: 185 objects, 0 changes). Safe to integrate. |
| Observability (dashboards, OTel, LGTM) | `infra/helm/llm-agent-platform/dashboards/`, `templates/observability-stack.yaml`, `otel-collector.yaml`, `ai_platform/llm_agent/observability.py` | 1 | live in `ewallet-agents` (r32) |
| Prompt Registry | `infra/helm/agentregistry/values*.yaml`, `infra/kubernetes/prompt-registry/postgres.yaml` | 1 | `prompt-registry` r1; server `a2f69bd3…`; Postgres `3a426452…`; 12/12 pinned versions MATCH |
| Prompt resolver, M3 gate, catalog guard, security probe | `ops/llm_agent/prompt_registry.py`, `registry_catalog.py`, `scripts/llm_agent/registry_security_probe.py`, tests | 2 | Operator tooling, run locally against GKE read-only (gate) and against throwaway registries. Not in any image or Jenkins snapshot. |
| M1 | the Role inside `infra/kubernetes/prompt-registry/hardening-m1-m2-proposal.yaml`, mirrored in `batch-b-isolated.yaml` | 1 | applied 2026-11-03T14:12:29Z |
| M2 | the NetworkPolicies `prompt-registry-db` and `prompt-registry-server` in `hardening-m1-m2-proposal.yaml`, plus deleting `same-namespace-and-dns` | 3 | kind-reviewed. GKE still has `default-deny` and `same-namespace-and-dns`. |
| r33 (registry-sourced Drift prompt) | `templates/agents.yaml` `promptSource: registry`, applied by values override only (`--set …promptSource=registry`); `prompt_r33_proposal_20261103.md` §8 | 3 | not deployed. The live `systemMessageFrom` is `drift-specialist-prompt-v2`; the managed ConfigMap is absent. |
| Prompt canary (kind) | `infra/kubernetes/prompt-canary/canary.yaml`, `kagent-kind-values.yaml` | 2 (kind only) | never for GKE |
| MCP/Agent CD, D1 step 1 | `infra/kubernetes/jenkins/rbac/batch-d1-step1.yaml` (5 objects) | 5 | superseded by r2 after the GKE stop/rollback |
| D1 step 1 r2 | `batch-d1-step1-r2.yaml` | 3, under 4 | not authorized; only useful if D1 is revived |
| D1 migration, rehome tool, record guard | `ops/llm_agent/helm_release_rehome.py` (+ tests), `batch-d2-record-guard-proposal.yaml` (cluster-scoped VAP) | 4 | Tool: class 2 (kind-validated). Guard and migration: BLOCKED (no cluster-wide admission on GKE; helm HIGH CVE). |
| D1 overall proposal | `batch-d1-proposal.yaml` | 4 | — |
| Batch D production draft | `batch-d-production-proposal.yaml` | 5 | superseded |
| D-lite | `batch_d_mcp_agent_cd_proposal_20261103.md` §9 (design only; no code) | 3 | — |
| Jenkins CD jobs `edai2-cd-mcp-agents`, `-prompt-management`, `-streaming` | `jenkins.yaml` Job DSL | 1 (jobs exist) | **Fail closed**: their catalog components have no `deploy_handler`, and no `release/*` branch exists for them in the snapshot. Jenkins has no RBAC in `ewallet-mcp`, `ewallet-agents` or `default`. |
| Streaming (row 36) | `data_platform/processing/flink`, `configs/streaming`, `tests/streaming` on this branch | 2 (CI only) | Never deployed from this branch. The runtime-wired Flink is on `origin/main`. `codex/streaming-block-a`: 5 (EXCLUDED). |

**How integration preserves unapproved deployment changes without deploying them.** None of the class 3/4 manifests is referenced by any deploy path.

1. **Jenkins:**
   - Only `rag-pipeline`, `ml-training`, `mlflow` and `llm-serving` declare handlers in `components.json`.
   - `rag_pipeline.sh` touches only `infra/kubernetes/cicd-runtime/rag`.
   - The ML handlers lack RBAC in their namespaces, and `llm-serving` images are skipped by `release_image_specs`.
2. **Helm:**
   - `templates/agents.yaml` defaults to `git`.
   - r33 exists only as a `--set` override in a document.
3. **Raw manifests:** `infra/kubernetes/jenkins/rbac/*proposal*.yaml`, `batch-d1-step1*.yaml` and `hardening-m1-m2-proposal.yaml` are applied only by an explicit operator `kubectl apply`. No kustomization, Argo or Flux root includes them; a grep of every `.sh`, `.py`, `.groovy`, `.json` and kustomization outside `docs/` and `tests/` finds no reference.

Integration should keep them byte-identical in their current paths, together with their PROPOSAL headers. It must not add them to any aggregate apply target. A CI test asserting that no deploy handler references them is a recommended first step of the integration campaign.

## 4. Final source checkpoint proposal

- **Proposed freeze candidate:** the handoff commit described in §2.1. Its parent is `26eaf8f`, and it adds documentation and inventory only.
- **Rationale:**
  - It contains every implemented artifact.
  - Its source content equals `a4634e6`/`26eaf8f`; the handoff adds documents only.
  - The tracked working tree is clean.
  - Evidence checksums verify.
- **Scope of the freeze:**
  - Source on this branch: Claude-owned G2/G3/CI/CD, RAG CD and prompt management.
  - It does not freeze the other worktrees, `origin/main`, `50e9e733` or `c754ecf9`. Integration must reconcile them (§12).

## 5. Component dependency and uv.lock inventory

Full table: `uv-lock-inventory.txt`. The catalog (`jenkins/catalog/components.json`) maps every component to its source paths, tests, dependencies and locked project.

| Lock | sha256 (16) | Python | Packages | Catalog components |
|---|---|---|---|---|
| `infra/docker/llm-agent-tooling/uv.lock` | `715f424402911737` | ≥3.10,<3.13 | 120 | agents, ci-platform, mcp-agents, observability, prompt-management |
| `infra/docker/g3-byo-coordinator/uv.lock` | `2cf466c3c1fc8b0c` | 3.12 | 77 | agents |
| `infra/docker/rag-ingestion/uv.lock` | `20c00145ff1f8cb3` | 3.10 | 164 | rag-pipeline |
| `ai_platform/features/feast/uv.lock` | `949f5629819cd8b1` | 3.10 | 104 | feast |
| `ai_platform/inference/uv.lock` | `0fe677cd76312e01` | 3.10 | 105 | fraud-api |
| `ai_platform/serving/uv.lock` | `3ab4eee69506a8ba` | 3.10 | 119 | fraud-serving |
| `ai_platform/ml/uv.lock`, `ml/pipelines/uv.lock` | `c044ce4a…`, `3c55b936…` | 3.10 | 103 / 38 | ml-training |
| `infra/kubernetes/mlflow/image/uv.lock` | `1662242883c458c6` | 3.10 | 103 | mlflow |
| `data_platform/metadata/datahub/uv.lock` | `6f44b661ab3ff683` | 3.10 | 80 | data-governance |
| `ai_platform/ml/dvc-operator/uv.lock`, `infra/docker/flink-benchmark/uv.lock`, `infra/docker/vllm-cpu-model/uv.lock` | `dfae2e41…`, `125e8f44…`, `106f47f5…` | — | 112 / 54 / 3 | not referenced by a catalog command |

Lock overlap with protected refs (§12):
- `llm-agent-tooling` and `rag-ingestion` differ from `integration/final-integration-20261101`.
- `feast`, `inference`, `serving`, `dvc-operator` and `datahub` overlap with `submission/edai1-ml-stable-20261102`.
- `flink-benchmark` overlaps with all three.

## 6. Jenkins / Cloud Build source snapshot mapping

`jenkins-source-snapshot` is the init container: an immutable Git bundle of the source SHA.

| Batch | Snapshot digest | Source SHA | Used by |
|---|---|---|---|
| (previous campaign) | `45f2a58cf113…` | `8d34a1a` | ML CI #2 / scenario runs |
| C | `bbd9791603c0…` | `5eb326f` | RAG CD #1, #2 (fail-closed: logs-bucket permission; overwrite needed delete) |
| C.1 | `b0c8698276eb…` | `8625002` | RAG CD #3 (fail-closed: Cloud Build logging) |
| C.2 | `18d443cf6bc1…` | `19582d6` | RAG CD #4 (build `d63b43bb` pushed; provenance verify needed the Container Analysis API) |
| C.3 | `3132cb679553…` | `48bf89c` | RAG CD #5 (fail-closed at Deploy, 0.5 s) |
| C.4 | `199eb593e646…` | `c9afc5b` | **RAG CD #6 PASS** (Cloud Build `aae05981` → `rag-ingestion@1dd91888…`) |
| C.5 (**live**) | `c1dc81c93e5c…` | `5a1c56c` | RAG CI #7 (308 tests) |

Evidence: `cicd_runtime_20261103/batch-c*/source-snapshot-digest.txt` and `kubectl-diff.txt`; run revisions from `jenkins/jobs/edai2-cd-rag-pipeline-N/api.json` (`lastBuiltRevision`).

## 7. Container image digests and Helm revisions (live, read-only)

| Release / workload | Revision / digest |
|---|---|
| Jenkins controller / init | `jenkins-controller@sha256:9c301d1a7fa4e8a6a2de8eafbc85c664eda292c6170f1168f71982c1fee5565d` / `jenkins-source-snapshot@sha256:c1dc81c93e5cf7fff962627367d5a57741195d6a59459f75b876c5aae948a45f` |
| `llm-agent` (default) | **r32** (2026-11-03T11:38Z) |
| `llm-agent-mcp` (ewallet-mcp) | **r6** (2026-11-02T21:53Z): `mcp-tools@sha256:3209d01e514669af01a967ce37a3e909db5cd682bc066b267aaf82399761411f` |
| `kagent` / `kagent-crds` | r5 / r1 (0.10.3) |
| Istio base, cni, istiod, ztunnel | r13 (1.30.7) |
| `prompt-registry` (ewallet-prompts) | r1: `agentregistry/server@sha256:a2f69bd36fcdb8000d9a72fd6eef88f0c4aebc17ff3c55ea3ed174ae0bb12e3c`; Postgres `postgres@sha256:3a4264520e121176619f54ff867d12774bc81e95e7b84db58d63bccdb6144b81` |
| RAG CronJob (ewallet-cicd) | `rag-ingestion@sha256:1dd91888548968e304d735d962c4eb3fc50560d092ca050efb6240e64e2dcba3` (suspended) |
| cert-manager, kserve(-crd), substrate(-crds) | r1 each |

The complete per-workload image list for `ewallet-agents`, `ewallet-mcp`, `ewallet-prompts`, `ewallet-ci` and `ewallet-cicd` is in `gke-baseline-readonly.txt`.

## 8. Applied versus proposed infrastructure changes

| Change | Applied (retained) | Proposed / blocked (not applied) |
|---|---|---|
| GCP | Batch A, A.1, A.2 (Cloud Build identities, buckets, custom role, WI binding); Container Analysis API enabled | Terraform import of those resources |
| Kubernetes | Batches B, C–C.5; `ewallet-cicd` and `ewallet-prompts` namespaces; registry Postgres; **M1** | M2; D1 step 1 r2; D1 migration; record-guard VAP; Batch D production draft (superseded) |
| Helm | `prompt-registry` r1; `llm-agent` r32 (dashboard fix) | `llm-agent` r33; any `llm-agent-mcp` change |
| Transient, fully reverted | D1 step 1 Namespace (created and deleted after 22 s); Authorized Networks temporary /32 (restored to `113.188.119.38/32`); registry F9 write (restored from Git, 12/12) | — |
| Workloads | RAG CronJob from CD #6 | D-lite; GKE rollback failure injection |

**Confirmed read-only at 15:07 UTC:**
- No `ewallet-mcp-release` namespace.
- No `ewallet-*` ValidatingAdmissionPolicy or binding.
- `ewallet-prompts` has `default-deny` and `same-namespace-and-dns` only.
- The Jenkins port-forward check returns `no`.
- The Drift Specialist reads `drift-specialist-prompt-v2`.
- 0 executor port-forwards.

## 9. Sanitized evidence manifest and SHA256 verification

| Set | Files | Verification |
|---|---|---|
| `docs/evidence/edai2/cicd_runtime_20261103/` | 250 + `SHA256SUMS.txt` | `sha256sum -c` **all OK** (re-run in this handoff) |
| `docs/evidence/edai2/source_freeze_20261103/` (this handoff) | listed in its `SHA256SUMS.txt` | written in this handoff |
| Other tracked EDAI-2 evidence (`g3_*`, `unified_cicd_observability_20261102T160000Z`) | 399 tracked | Git object integrity; no change in this campaign's last phase |

## 10. Private evidence exclusions

These files are git-ignored, exist only on the executor laptop, and must not be committed or published:

- `cicd_runtime_20261103/batch-a/*iam*.json`, `bucket-iam.json`: name principals.
- `*/apply*.log`, `batch-api/enable.log`.
- `jenkins/scans/*.json`: full Trivy reports.
- `*-tool-calls.log`: MCP tool-call logs with request payloads.
- `prompt-canary-kind/echo-*.log`: full prompt echoes.
- Helm record backups and the pre-M1 Role export: session scratchpad only.

**Redaction in committed evidence:** the operator account in `registry-security/incident-audit-gke.txt`. No credential, token or Secret value is in Git.

## 11. Remaining rubric gaps and blockers

| Row / gate | Gap | What would close it |
|---|---|---|
| 33 | GKE rollback NOT_VERIFIED; POST_MERGE_REPRODUCED NOT_VERIFIED; GitHub links | Approved failure-injection run; re-run after canonical integration |
| 34 | Production MCP/Agent CD not executed | Owner decision on D-lite (Batch D §9), or acceptance of cluster-scoped admission plus a patched Helm for D1 |
| 35 | Production Agent prompt consumption not deployed; registry writes unauthenticated (0.4.0); Langfuse substitution | r33 approval (§8 of its proposal); M2 (recommended); registry upgrade or auth proxy decision; reviewer acceptance |
| 36 | CD BLOCKED | Canonical Data Platform integration (M6) reconciling the Flink on `origin/main`; Streaming Block A stays excluded |
| Jenkins snapshot | The live snapshot is `5a1c56c`; later tooling is not in Jenkins | New snapshot only after integration (no redeploy authorized now) |

## 12. Integration risks and known cross-platform conflicts

Details: `cross-ref-overlap.txt`, `overlap-*.txt` and `overlap-*.differing.txt`.

| Against | Merge base | Commits ahead (theirs) | Overlapping files | **Content differs** |
|---|---|---|---|---|
| `integration/final-integration-20261101@50e9e73` | `e7dbbc2` | 80 | 71 | **35** |
| `submission/edai1-ml-stable-20261102@c754ecf` | `03f6b3c` | 16 | 136 | **24** |
| `origin/main@2033ab3` | `03f6b3c` | 10 | 43 | **22** |

Conflict areas:

1. **Parallel CI implementation.** Both this branch and `50e9e733` changed `jenkins/catalog/components.json`, `jenkins/pipeline/componentPipeline.groovy`, `Jenkinsfile*` and `scripts/ci/{catalog,change_detection,run_ci,deploy_release.sh}`, and the files differ. This branch's version is the one proven by Jenkins runs #1–#7. Reconcile semantically; do not take either side wholesale.
2. **Lock files.** `llm-agent-tooling` and `rag-ingestion` `uv.lock` differ from `50e9e733`. The ML/Feast/serving/DataHub locks overlap with `c754ecf9`. Re-lock and re-run each component's catalog tests; never hand-merge `uv.lock`.
3. **Helm values.** `infra/helm/llm-agent-platform/values.yaml` differs from `50e9e733`. The live r32 values must stay reproducible: verify with the semantic-diff method of `prompt-r33/helm-semantic-diff.txt` (expected 0 changes).
4. **RAG and Agent runtime.** `ai_platform/llm_agent/rag/*`, `tools/service.py`, `observability.py` and `ops/llm_agent/rag_*.py` differ from `50e9e733`. These files are deployed (`mcp-tools` r6 and r32), so integration must keep the RAG CD contract (`jenkins/cd/rag-pipeline.contract.json`) and the G3 regression green.
5. **Streaming.**
   - `data_platform/streaming` (30 files), `configs/streaming` and `infra/docker/flink-benchmark` overlap with `origin/main` and `c754ecf9`.
   - Row 36 is owned by Data Platform.
   - `codex/streaming-block-a` overlaps 56 files and stays **EXCLUDED**.
6. **Proposed manifests.** These must arrive non-deployed (§3).
7. **Operational state outside Git.** Several things live only outside Git:
   - GCP IAM created outside Terraform (import pending)
   - Jenkins JCasC on its PVC
   - the registry content (rebuildable from Git through `registry_catalog publish --all-prompt-versions` behind the M3 gate)

   The integration E2E must not assume Terraform owns them.
8. **System E2E prerequisites** for the next campaign:
   - G3 business E2E, replay idempotency and MCP authentication against the live baseline.
   - The row 33 contract.
   - The registry gate.
   - Catalog CI of every component on the integrated tree.

   These are the minimum, and they are what the success criterion needs: reconciliation, build verification, integration CI and working System E2E.
