# Batch D proposal: MCP/Agent CD without Jenkins reading Kubernetes Secrets

Status: **proposal only (read-only analysis, nothing applied)**. D1 migration **BLOCKED** and D-lite proposed in §9 (2026-11-03).
Original source `codex/llm-g2-llmd-routing` at `87b563d`. Rubric rows 34/35 (identical
workbook text): "CI/CD for MCP Servers & Agents: build and package the MCP
servers, then deploy the Agent with the corresponding MCP servers; the Agent
uses its system prompt from a prompt management tool".

## 1. Constraint and facts (verified 2026-11-03, read-only)

| Fact | Evidence |
|---|---|
| Owner condition: Jenkins must not read MCP auth, Redis or unrelated Secrets | owner decision 2026-11-03 |
| `llm-agent-mcp` (r6) stores its Helm history as Secrets `sh.helm.release.v1.*` in `ewallet-mcp` | `kubectl get secrets -n ewallet-mcp` |
| `ewallet-mcp` also holds 3 sensitive Secrets: `feast-online-redis`, `mcp-auth-{drift,feature-rag}-mcp-r1-transition` | same |
| RBAC cannot scope `list secrets` by label, so Helm-in-place would expose them | Kubernetes RBAC semantics |
| `llm-agent` (r32) stores its history in `default`, which holds **only** 10 Helm release Secrets | `kubectl get secrets -n default` |
| Release records hold values and the rendered manifest only. They contain Secret *names* (`secretKeyRef`, `authHeaderSecrets`) but no Secret values and no `kind: Secret` objects | `helm get values/manifest` |
| The chart never uses `.Release.Namespace`; every object's namespace comes from values | 0 matches in `templates/` |
| Helm client 3.22 supports `--take-ownership` | `helm version` |

## 2. Options

| # | Mechanism | Jenkins Secret access | New infra | Risk | Verdict |
|---|---|---|---|---|---|
| D1 | **Re-home the `llm-agent-mcp` Helm record** into a new namespace `ewallet-mcp-release` that holds only Helm records. Resources stay in `ewallet-mcp`; the release is adopted with `helm upgrade --install -n ewallet-mcp-release --take-ownership` and identical values. | Secret CRUD **only** in `ewallet-mcp-release`, which contains Helm records only | 1 namespace, RBAC | One-time ownership migration; reversible | **Recommended** |
| D2 | Switch `llm-agent-mcp` to `HELM_DRIVER=configmap` in `ewallet-mcp` (adopt with `--take-ownership`, then delete the Secret records) | none | none | Every ConfigMap reader of `ewallet-mcp` can read the release history (names only). The driver must be set on every future Helm call, or a stray Secret-driver call forks the history. | Acceptable alternative |
| D3 | In-cluster deployer Job with its own ServiceAccount (Secret rights); Jenkins only creates the Job | indirect | ValidatingAdmissionPolicy to pin image and command | Creating a Pod with a privileged SA is privilege escalation unless the admission policy is airtight | Rejected |
| D4 | GitOps controller (Argo CD/Flux) pulls a release manifest Jenkins publishes | none | cluster-scoped controller, CRDs, ClusterRoles | New platform component and RBAC surface | Rejected for this campaign |
| D5 | Cloud Deploy target for GKE | none | new GCP service plus execution SA with GKE rights | Broad IAM (`container.developer`-class) on a new identity | Rejected |
| D6 | Jenkins patches only the Deployment image (server-side apply) and leaves Helm alone | none | none | Helm drift: the next `helm upgrade` reverts the image; ownership is split | Rejected |

## 3. Recommended design (D1)

### Identity and RBAC (additions to batches A to C; no ClusterRole)

| Namespace | Role | Rights |
|---|---|---|
| `ewallet-mcp-release` (new, holds only Helm records) | `jenkins-helm-records` | `secrets`: get, list, watch, create, update, patch, delete |
| `ewallet-mcp` | `jenkins-release-llm-agent-mcp` | CRUD on configmaps, services, serviceaccounts, deployments, PDBs, NetworkPolicies, AuthorizationPolicies; read pods, logs, events, replicasets. **No Secret verbs.** |
| `ewallet-agents`, `feature-store`, `fraud-serving` | `jenkins-release-llm-agent-mcp-owned` | get, update, patch on the 6 named objects this release owns there (already drafted in `infra/kubernetes/jenkins/rbac/batch-d-production-proposal.yaml`) |
| `ewallet-agents` | `jenkins-verify-agents` | read-only health plus `pods/portforward` to the Coordinator and MCP Gateway |

GCP IAM needs no change: the `mcp-tools` image is built by `ewallet-cloudbuild` exactly as in row 33.

**Remaining exposure:** the Helm records in `ewallet-mcp-release` (values and manifests) name Secrets but contain no Secret value.

**Positive MCP smoke:** the token-authenticated positive call needs a service token. Without Secret access, Jenkins instead verifies MCP through the end-to-end business request, where the Specialist presents its own token, plus the 401 negative probes.

### Deployment flow (`edai2-cd-mcp-agents`, Jenkinsfile.llm-agent)

1. CI on `mcp-agents` and `agents` (JUnit).
2. Owner `input`, then the `release/mcp-agents` SHA gate.
3. Cloud Build of `infra/docker/llm-agent-tooling/Dockerfile --target mcp-server` produces the `mcp-tools` image at the release SHA, with provenance.
4. Handler `scripts/ci/deploy/mcp_servers.sh`:
   1. `helm get values` (record only), then `helm upgrade llm-agent-mcp -n ewallet-mcp-release --reuse-values --set mcp image digest` with `--wait`.
   2. Before the upgrade, a semantic diff from `helm template` against the live objects must show only the image digest and the checksum annotations.
   3. Rollout status of `feature-rag-mcp` and `drift-mcp`; PDB respected.
   4. Verify:
      - `/health/ready` reports the release SHA (the narrow code change)
      - unauthenticated, forged and revoked MCP calls return 401
      - one G3 business E2E through the Coordinator: Feast, KServe and RAG citations
      - cross-replica replay idempotent
      - Tempo trace has spans in `feature-rag-mcp`
   5. On any failure, `helm rollback llm-agent-mcp <previous> -n ewallet-mcp-release --wait`, then re-verify.
5. Agents (Coordinator/Specialists, release `llm-agent` in `default`):
   - The default flow **verifies** the Agents against the new MCP servers and does not redeploy them.
   - If the rubric reviewer requires "deploy Agent" literally, a later step can grant the same records-only Secret role in `default` (it already holds only Helm records).
   - That later step would also need the `llm-agent` resource RBAC (about 160 objects in `ewallet-agents`), which must be its own proposal.
6. Prompt management (row 35 clause):
   - The Drift Specialist moves from the Git-rendered ConfigMap to a ConfigMap rendered from the registry by `ops/llm_agent/prompt_registry.py configmap` at an explicit version.
   - That is the `llm-agent` r32→r33 change, which is **not approved** and is listed separately below.

### One-time migration of `llm-agent-mcp` (operator, maintenance window)

1. Back up: `helm get all llm-agent-mcp -n ewallet-mcp` and export the 6 `sh.helm.release.v1.llm-agent-mcp.*` Secrets to private evidence.
2. Create namespace `ewallet-mcp-release` (no workloads, default-deny, quota `secrets: 20`).
3. `helm template` with the reviewed r6 values, then a server-side dry-run diff against the live objects. It must show no spec change.
4. `helm upgrade --install llm-agent-mcp -n ewallet-mcp-release --take-ownership -f r6-values.yaml --wait`. Objects are not recreated; only the annotations `meta.helm.sh/release-namespace` change.
5. Verify: same pod UIDs (no restart is expected because the pod template is unchanged), G3 E2E PASS, MCP 401 probes.
6. Delete only the old record Secrets `sh.helm.release.v1.llm-agent-mcp.v1..v6` in `ewallet-mcp`. **Do not run `helm uninstall`.**

**Rollback of the migration:** restore the 6 Secrets from backup, run `helm upgrade -n ewallet-mcp --take-ownership` with the same values, and delete the record in `ewallet-mcp-release`.

## 4. Impact on G3 and current releases

| Item | Impact |
|---|---|
| G3 business path | none during migration (pod templates unchanged); a rolling update of the two MCP pods during a CD release |
| `llm-agent` r32, kagent, Istio, Gateways | unchanged |
| History | the new namespace starts `llm-agent-mcp` history at r1; r1–r6 are kept in private evidence |
| Secrets readable by Jenkins | Helm records in `ewallet-mcp-release` only |

## 5. Evidence plan

- Migration: pre/post `helm get all`, server-side diff, pod UIDs, G3 E2E and 401 probes before and after.
- CD run: Jenkins build, JUnit, Cloud Build ID and provenance, image digest, Helm revision, semantic diff, rollout, `/health/ready` release SHA, G3 E2E plus Tempo trace, rollback readiness.
- `kubectl auth can-i` matrix proving no Secret verb outside `ewallet-mcp-release`.

## 6. Approvals this would need (separately)

1. Namespace `ewallet-mcp-release` and the D1 RBAC.
2. The one-time `llm-agent-mcp` re-homing migration.
3. Each CD run.
4. `llm-agent` r32→r33 for the registry-sourced Drift Specialist prompt: exact diff, regression plan and rollback, per the 2026-11-03 decision.

## 7. Kind safety validation (2026-11-03) and verdict

Evidence: `docs/evidence/edai2/cicd_runtime_20261103/batch-d-kind/`.

Setup:
- Helm client 3.22.0; the actual chart with the live r6 values. Its render equals the live r6 manifest: 28 objects, identical content, all namespaced, no hooks, no `lookup`, no cluster-scoped objects.
- Installed on kind with a 6-revision history, plus dummy Secrets named like the 3 sensitive ones.

| # | Test | Result |
|---|---|---|
| 1 | `--take-ownership` into `ewallet-mcp-release` | 28/28 UIDs unchanged, specs unchanged, pods not recreated. Only `meta.helm.sh/release-namespace` changed. Deployment `generation` +1 with the same ReplicaSet (no rollout). |
| 2 | Two records coexisting: upgrade or rollback through the **old** record | Both succeeded **without error** and silently took ownership back (split-brain). |
| 3 | `helm uninstall` through the **stale** record | **Deleted all 28 objects**, including those owned by the new record. Helm uninstall ignores ownership annotations. |
| 4 | Correct procedure: backup, take-ownership, delete the 6 old record Secrets (never uninstall) | 28/28 UIDs and specs unchanged, pods identical; one record remains; `ewallet-mcp` holds only the 3 non-Helm Secrets. |
| 5 | CD as `system:serviceaccount:ewallet-ci:jenkins` with `infra/kubernetes/jenkins/rbac/batch-d1-proposal.yaml` | Image-digest upgrade (r2) and rollback (r3) succeeded. |
| 6 | Negative tests as the Jenkins SA | All denied: get/list Secrets in `ewallet-mcp`, `ewallet-agents`, `default`, `feature-store`; `helm list -n ewallet-mcp`; re-creating a record in `ewallet-mcp` with `--take-ownership` (split-brain attempt); creating pods; `pods/exec`. |
| 7 | Migration rollback (restore the record backup, take ownership back, delete the new records) | Back to the original baseline (UIDs and specs identical, owner `ewallet-mcp`). |
| 8 | Normal uninstall | Deletes all release objects (standard Helm). |

**Verdict:**
- **D1 is SAFE for Jenkins least privilege:** Jenkins needs no production Secret access and cannot create a competing record.
- **D1 is SAFE TO MIGRATE ONLY WITH PROCEDURAL CONTROLS**, because of hazards 2 and 3:
  1. The migration is one operator step in a declared change freeze (no other Helm operation on `llm-agent-mcp`).
  2. Delete the old record Secrets immediately after take-ownership. **Never `helm uninstall` the old record.**
  3. Keep the record backup in private evidence.
  4. Afterwards, `helm` operations on `llm-agent-mcp` must name `-n ewallet-mcp-release`. A runbook guard plus a pre-command check of `meta.helm.sh/release-namespace` is required, because an admin `helm upgrade -n ewallet-mcp` would silently fork the history again.
- **D2 (configmap driver)** has the same two-record hazard during its migration, and permanently exposes release history to every ConfigMap reader in `ewallet-mcp`. It remains the alternative, not the recommendation.
- **Agents** (`llm-agent` in `default`): `default` already holds only Helm records, so no migration is needed. Granting the records-only Secret role there plus the agent resource RBAC is a separate proposal.

**Approvals required, each separate:**
1. Namespace `ewallet-mcp-release` with the D1 RBAC (no Secret verb outside it).
2. The one-time `llm-agent-mcp` record migration in a change freeze.
3. The first Jenkins MCP CD run.

## 8. Revision 2026-11-03 (after the owner decision on D1): step 1 package and redesigned migration

### 8.1 Step 1: exact change package (approval requested; NOT applied)

**File:** `infra/kubernetes/jenkins/rbac/batch-d1-step1.yaml`, sha256 `8a6c0c4c19943fc499bf521dfca64ab04fbd053278d60cfd2a4a2b4f7129b619`.

**Objects, all new:**
- Namespace `ewallet-mcp-release` (PSS restricted)
- ResourceQuota `helm-records-only`: 0 pods, Services, PVCs, Deployments, StatefulSets, Jobs and CronJobs; 2 ConfigMaps; 20 Secrets
- NetworkPolicy `default-deny`
- Role and RoleBinding `jenkins-helm-records`: Secrets CRUD for `ewallet-ci/jenkins` in that namespace only

**GKE read-only preflight** (`batch-d1-step1/gke-preflight.txt`):
- The namespace does not exist yet.
- All 5 objects are new; no existing object is touched.
- Server dry-run accepted the Namespace.

**Kind validation** (`batch-d1-step1/`):
- 35/35 permission checks pass.
- Jenkins can manage Secrets only in `ewallet-mcp-release`.
- Jenkins cannot:
  - create pods, Deployments, Jobs, ConfigMaps, ServiceAccounts or tokens there
  - create Roles or RoleBindings, or use `bind`, `escalate` or `impersonate`
  - patch the quota or NetworkPolicy
  - read Secrets in `ewallet-mcp`, `ewallet-agents`, `feature-store`, `default` or `ewallet-ci`
  - create namespaces, ClusterRoles, ClusterRoleBindings or admission bindings
- The quota rejects a pod even for cluster-admin.
- Jenkins' `can-i --list` in every pre-existing namespace is **identical** before and after.

**Security reading:**
- Step 1 grants no application Secret access: the namespace can never run a pod, so no Secret there can be mounted.
- Step 1 is inert until step 2.

**Rollback:** `kubectl delete -f batch-d1-step1.yaml`.

### 8.2 Privilege-escalation finding for step 3 (MCP CD rights in `ewallet-mcp`)

Any role that can `create` or `patch` Deployments in `ewallet-mcp` can mount the Secrets of that namespace (`feast-online-redis`, `mcp-auth-*`) into a pod template, which amounts to indirect Secret read. Likewise, writing NetworkPolicy or AuthorizationPolicy objects could open an exfiltration path.

Step 3 therefore must not be granted with RBAC alone. It needs a ValidatingAdmissionPolicy matched on `system:serviceaccount:ewallet-ci:jenkins` that:
- for Deployments, allows UPDATE only when the pod template is unchanged except the container image and checksum annotations (`volumes`, `env`, `envFrom`, `volumeMounts`, `serviceAccountName`, `command`, `args` and `initContainers` all equal to `oldObject`), and denies CREATE and DELETE;
- for NetworkPolicy and AuthorizationPolicy, allows UPDATE only when `spec` is equal to `oldObject.spec` (Helm's no-op patch), and denies CREATE and DELETE.

This policy is a step 3 prerequisite. It is not part of step 1 and has not been validated yet.

Step 3 also needs Helm 3.22.0 in the controller image (it ships none today) and its own Trivy gate.

### 8.3 Step 2: redesigned migration (no two-owner window), kind-validated, NOT approved

**Tool:** `ops/llm_agent/helm_release_rehome.py` (unit tests: `tests/llm_agent/test_helm_release_rehome.py`). Steps, each refusing to run from the wrong state:

1. `backup`: all record Secrets to a private directory (mode 0600) with sha256, plus the release manifest.
2. `archive`: relabel the old records `owner=helm` → `owner=helm-archived`. Helm selects records by `owner=helm,name=<release>`, so the old release vanishes for every helm command. Its bytes stay in the cluster.
3. `copy`: rewrite only the `namespace` field of each record and create it in `ewallet-mcp-release`. The **full history r1…rN continues** in the new namespace.
4. `takeover`: `helm upgrade --take-ownership --wait` in the new namespace, producing rN+1.
5. Guard: ValidatingAdmissionPolicy `ewallet-helm-record-home-llm-agent-mcp` (`infra/kubernetes/jenkins/rbac/batch-d2-record-guard-proposal.yaml`). It denies, for every identity, any create or relabel of an `owner=helm` record of `llm-agent-mcp` in `ewallet-mcp`.

**Rollback:** delete the guard binding (an explicit, audited step), then `rollback`, which works from any intermediate state:
- relabels the archived records back
- backs up and then deletes the target records
- if the objects were already adopted, gives them back with `--take-ownership`

**Kind evidence** (`batch-d2-kind/`): Helm 3.22.0, actual chart, live r6/r7 values. "Unchanged" below means all 28 UIDs, specs and pods are identical to the baseline.

| Check | Result |
|---|---|
| After `archive` | `helm uninstall -n ewallet-mcp` → "release: not found" (no deletion); `upgrade` → "has no deployed releases"; `rollback` → "release: not found"; objects unchanged |
| `copy` | history r1–r6 (later r1–r7) visible in `ewallet-mcp-release` |
| `takeover` | r8 created; objects unchanged; owner `ewallet-mcp-release` |
| `helm rollback` to r7 in the new namespace | works; objects unchanged |
| Failure injection: takeover r7 **failed** (`--wait` timeout on kind), then tool `rollback` | exact baseline restored |
| Accidental admin `helm upgrade --install -n ewallet-mcp` without `--take-ownership` after migration | rejected by Helm's ownership check; no record left behind |
| Deliberate `--take-ownership -n ewallet-mcp` with the guard on | **denied at record creation**, before any object change; owner unchanged |
| Relabelling an archived record back to `owner=helm` with the guard on | denied |
| Normal CD in the new namespace with the guard on | r10 succeeded; other Secrets in `ewallet-mcp` unaffected |
| Rollback with the guard on | fails closed (no partial change) |
| Rollback after removing the guard binding | exact baseline restored |

**Hazard status:**
- A stale record regaining ownership: **eliminated**. The archived records are invisible to Helm, and the guard blocks reactivating them or creating new ones.
- Destructive uninstall of the stale release: **eliminated** ("release: not found").
- Accidental use of the old namespace: blocked by Helm's ownership check, and also by the guard for `--take-ownership`.
- History: **preserved** in both directions. On rollback, the target-side history is saved to the private backup.
- Failure between steps: covered by the step preconditions and a tested `rollback` from the failed takeover state.
- Version: tested with the production Helm client 3.22.0. Release record format `helm.sh/release.v1`.

**Remaining assumptions:**
1. The guard is cluster-scoped and requires its own approval.
2. During the migration window no other operator runs Helm on `llm-agent-mcp` (the steps are seconds apart and the tool refuses inconsistent states).
3. kind could not exercise `--wait` with Ready pods (no registry credentials), so production `--wait` timing remains untested.
4. The kind kube-apiserver is v1.37 versus GKE 1.36. ValidatingAdmissionPolicy is GA on both, and the CEL in the guard uses only the optional-field syntax (`?.`) available on both.

## 9. Revision 2026-11-03 (owner decisions 2 and 6): D1 migration BLOCKED; narrower alternative D-lite

### 9.1 What happened to step 1

On GKE, D1 step 1 (`batch-d1-step1.yaml`, sha256 `8a6c0c4c…`) was **STOPPED and rolled back**. Evidence: `batch-d1-step1-apply/`.

- Preflight passed.
- Stage 1, the Namespace, was applied.
- Before stage 2, the namespace already held 3 ConfigMaps: istiod injects `istio-ca-root-cert` and `istio-ca-crl` into 24 of 26 namespaces, next to `kube-root-ca.crt`. The reviewed quota `configmaps: "2"` would therefore start at 3/2. Kind had no Istio, so this did not show up there.
- The namespace was deleted 22 s after creation.
- Helm records (68) and Jenkins `can-i --list` in 8 namespaces are identical to before.

Revision `batch-d1-step1-r2.yaml` (sha256 `f019e772…`, `configmaps: "4"`) is prepared and **not applied**. It is only useful if the Helm path below is revived.

### 9.2 Proportionality verdict for the remaining D1 work (decision 6)

**Verdict: the D1 Helm migration is BLOCKED under the current constraints.** The kind development of the step 3 pieces was not continued, because it cannot lead to an approvable GKE change.

| Prerequisite | Why it is blocked |
|---|---|
| Image-only Deployment admission for the Jenkins identity in `ewallet-mcp` (§8.2) | A ValidatingAdmissionPolicy and its binding are **cluster-scoped objects**, even when the binding selects a single namespace. The owner decision: "Do not apply any cluster-wide admission guard to GKE." Without it, Deployment `patch` in `ewallet-mcp` is indirect Secret read (§8.2). No namespaced Kubernetes mechanism can restrict which fields a writer changes. |
| Stale-record guard `ewallet-helm-record-home-llm-agent-mcp` (§8.3) | Also a cluster-scoped VAP: same constraint. Without it, the stale-record hazard is mitigated only procedurally, which the owner rejected ("procedural warnings alone are not sufficient"). |
| Helm in the Jenkins controller image | No released Helm 3 binary meets the image's 0-HIGH bar (`batch-d-proportionality/helm-binary-scan.txt`): v3.22.0 has 5 HIGH (`x/net`, stdlib); v3.22.1, the latest v3 (2026-10-15), still has 1 HIGH, CVE-2026-111943 in stdlib 1.26.9 (fixed in 1.26.10). The alternatives are a self-built Helm (new supply-chain pipeline) or Helm 4 (server-side apply by default, untested against the existing release records). Both are separate decisions. |

The pieces already validated stay available if these constraints change:
- the rehome tool and its tests
- the kind evidence in `batch-d2-kind/`
- the step 1 r2 package

### 9.3 D-lite: Jenkins-delivered, operator-applied MCP release (proposal, not implemented)

This is the narrowest design that respects every standing constraint:
- no Jenkins right in `ewallet-mcp`, `ewallet-agents` or `default`
- no Secret access
- no cluster-scoped object
- no Helm in Jenkins
- no record migration
- no new IAM

| Stage | Who | What | New privilege |
|---|---|---|---|
| CI | Jenkins (existing job pattern) | `mcp-agents` and `agents` JUnit, as in runs #6/#7 | none |
| Release gate | Jenkins | owner `input`, then the `release/mcp-agents` branch SHA check | none |
| Build and package | Jenkins → Cloud Build (existing `ewallet-cloudbuild`, AR writer on `ewallet-platform` only) | `infra/docker/llm-agent-tooling/Dockerfile --target mcp-server` at the release SHA, `requestedVerifyOption=VERIFIED`, immutable tag | none |
| Release bundle | Jenkins | Archived artifact. Contents: the release request, image digest and provenance reference, Git SHA and chart tree SHA, the exact values override (`image digest` only), and sha256 of the bundle. | none |
| Apply | **Operator** (existing cluster-admin path), `scripts/ci/deploy/mcp_operator_release.sh --bundle <file>` (to be written) | Fail-closed checks: bundle sha256; provenance VERIFIED; the chart tree in the checkout equals the bundle SHA; a semantic diff of `helm template` against the live objects shows only the MCP image digest and checksum annotations. Then `helm upgrade llm-agent-mcp -n ewallet-mcp --reuse-values --set …digest --wait`. | none (status quo operator rights) |
| Verify | Operator script | `feature-rag-mcp` and `drift-mcp` rollout; MCP 401 (no token, forged and revoked); positive MCP call; G3 business E2E through the Coordinator; cross-replica replay; Tempo spans. On failure: `helm rollback llm-agent-mcp <prev> --wait`, then re-verify. | none |
| Agents | Operator, same script with release `llm-agent` | Verifies the Agents against the new MCP servers by default. Redeploying an Agent uses the same bundle pattern; the prompt source is the r33 decision. | none |

**Rubric reading (rows 34/35):** build and package of the MCP servers and the release gate are automated in Jenkins; the deployment is executed by the operator from the Jenkins-produced, provenance-verified bundle, i.e. continuous *delivery* with a manual apply. Whether this satisfies "deploy" is a reviewer-acceptance risk, and the evidence must state it plainly.

**Work to implement** (requires owner approval; no GKE change until a run is approved):
- Jenkins job `edai2-cd-mcp-agents` (Job DSL entry, `declareParameters`) using the existing `componentPipeline` stages up to the bundle.
- `scripts/ci/release_bundle.py` and `scripts/ci/deploy/mcp_operator_release.sh`, with unit tests.
- A kind dry run of the bundle-verify and diff path.
- One approved GKE run, followed by a G3 regression.
