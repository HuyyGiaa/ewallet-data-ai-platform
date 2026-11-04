# EDAI-2 CD infrastructure: batched change review

Source: `codex/llm-g2-llmd-routing` at `a86772e`. Nothing in this document is
applied until its batch is approved.

## Revisions after the owner review (2026-11-03)

- Jenkins gets **no** Secret verb in any namespace. The `ewallet-mcp` Helm
  release rights moved to batch D, which is a proposal only.
- `roles/cloudbuild.builds.editor` was narrowed to a custom role with only
  `cloudbuild.builds.create`, `get` and `update`.
- `kubectl` was dropped from the controller image: every v1.36/1.37 release
  carries two HIGH Go CVEs. CD uses the stdlib client `scripts/ci/k8s_api.py`,
  and the controller keeps its 0-HIGH digest `9c301d1a…`.
- The isolated RAG CD path was proven locally before any cloud write:
  - On kind, as `system:serviceaccount:ewallet-ci:jenkins` with batch B RBAC
    only: PASS. Rollback: verified.
  - In the controller image with the new JCasC: CI 186/0/61, input approval,
    and the `release/<name>` SHA gate.

## Batch A: Cloud Build identities (`infra/terraform/gcp/modules/cicd_build`)

| Change | Exact identity / scope | Permissions |
|---|---|---|
| GSA | `ewallet-jenkins@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com` | none by itself |
| GSA | `ewallet-cloudbuild@…` | none by itself |
| custom role | `projects/ewallet-data-ai-platform-hg26/roles/ewalletCicdBuildSubmitter` | `cloudbuild.builds.create`, `get`, `update` |
| project binding | role above → `ewallet-jenkins` | Cloud Build has no narrower resource |
| SA binding | `roles/iam.serviceAccountUser` on `ewallet-cloudbuild` only → `ewallet-jenkins` | actAs this runner only |
| WI binding | `roles/iam.workloadIdentityUser` on `ewallet-jenkins` → `…svc.id.goog[ewallet-ci/jenkins]` | — |
| bucket | `ewallet-data-ai-platform-hg26-cicd-builds` (asia-southeast1, uniform access, public access prevention, 30-day delete) | — |
| bucket bindings | `objectCreator` and `objectViewer` → both GSAs | this bucket only |
| repo binding | `roles/artifactregistry.writer` on repository `ewallet-platform` → `ewallet-cloudbuild` | this repository only |

Escalation check: the project default build identity is the Compute default SA
(`roles/editor`). It requires `actAs`, which `ewallet-jenkins` lacks, so builds
can only run as `ewallet-cloudbuild`.

- **Resource impact:** none until a build runs. A build costs about 5–20
  build-minutes (free tier). The bucket holds less than 1 GB.
- **Rollback:** remove the 6 bindings, delete the custom role, both GSAs and the
  empty bucket. This affects nothing else.

## Batch B: isolated namespaces and RBAC

Files: `infra/kubernetes/cicd-runtime/namespaces.yaml` and
`infra/kubernetes/jenkins/rbac/batch-b-isolated.yaml`.

| Object | Content |
|---|---|
| Namespace `ewallet-cicd` | PSS restricted. Quota: 2 / 4 CPU, 6 / 10 Gi, 0 PVC, 6 Jobs, 2 CronJobs. LimitRange 100m/128Mi → 500m/512Mi. Default-deny, plus same-namespace and DNS egress only. |
| Namespace `ewallet-prompts` | PSS restricted. Quota: 0.5 / 2 CPU, 1 / 2 Gi, 1 PVC ≤ 2 Gi. LimitRange as above. Default-deny, plus same-namespace and DNS egress only. |
| Role `jenkins-bounded-workloads` (ewallet-cicd) | CRUD on jobs, cronjobs, deployments, statefulsets, configmaps, services; read and delete pods; read pods/log and events |
| Role `jenkins-prompt-registry` (ewallet-prompts) | read pods and services; create `pods/portforward` |
| 2 RoleBindings | subject `ewallet-ci/jenkins` |

There is no Secret verb, no ClusterRole and no production namespace.

- **Resource impact:** zero until a Job runs. Quotas cap any run.
- **Rollback:** `kubectl delete -f` both files.
- **G3 impact:** none.

## Batch C: Jenkins controller identity and configuration

The semantic diff against the live `ewallet-ci` objects contains only:

1. ServiceAccount `jenkins`: annotation `iam.gke.io/gcp-service-account: ewallet-jenkins@…`.
2. ConfigMap `jenkins-casc`: Job DSL adds parameter `RELEASE_BRANCH` and the
   jobs `edai2-cd-{mcp-agents,rag-pipeline,prompt-management,streaming}`. All
   default to no SHA and `DEPLOY_APPROVED=false`.
3. Deployment `jenkins`: a projected ServiceAccount token (3600 s, plus CA and
   namespace) mounted at the standard path. `automountServiceAccountToken`
   stays false.
4. NetworkPolicy `jenkins-runtime-allow`: egress `169.254.169.254/32:80` and
   `169.254.169.252/32:988` (Workload Identity metadata only).
5. A new source snapshot image (bundle = `a86772e`+ base plus `release/*`
   refs), built and scanned locally with Trivy, then pushed to
   `ewallet-platform`.

Unchanged:

- controller image `9c301d1a…`
- PVC, Service, quota and Recreate strategy

- **Resource impact:** none. The Jenkins pod restarts (Recreate, about 2
  minutes). Job history on the PVC is kept.
- **Rollback:** re-apply the previous rendered manifest
  (`docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/jenkins/jenkins-runtime-rendered.yaml`
  with snapshot `45f2a58c…`).
- **G3 impact:** none.

## Batch D (not requested now): production MCP/Agent deployment

See `infra/kubernetes/jenkins/rbac/batch-d-production-proposal.yaml`. It is
blocked by the no-Secret condition: Helm stores `llm-agent-mcp` in Secrets of
`ewallet-mcp`. It will be redesigned and submitted separately after the
isolated CD proofs.
