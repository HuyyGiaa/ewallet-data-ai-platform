# EDAI-2 CI/CD runtime evidence (2026-11-03)

Index and verdicts: [`docs/edai2/cicd_rubric_evidence_20261103.md`](../../../edai2/cicd_rubric_evidence_20261103.md).
Infrastructure review: [`docs/edai2/cicd_infra_change_review_20261103.md`](../../../edai2/cicd_infra_change_review_20261103.md).
Batch D proposal: [`docs/edai2/batch_d_mcp_agent_cd_proposal_20261103.md`](../../../edai2/batch_d_mcp_agent_cd_proposal_20261103.md).

| Directory | Content |
|---|---|
| `batch-a/` | Cloud Build identities: additive IAM diff, SA scan, A.1/A.2 bucket bindings |
| `batch-b/` | Isolated namespaces/RBAC: `kubectl auth can-i`, Pod Security/quota rejections, NetworkPolicy runtime probes |
| `batch-c*/` | Jenkins identity/config and each source snapshot (rendered manifest, `kubectl diff`, digests) |
| `batch-api/` | Container Analysis API enablement |
| `jenkins/jobs/edai2-cd-rag-pipeline-{1..6}/` | Build metadata, stages, console, JUnit totals, archived evidence (`artifacts.zip`) |
| `rag-cd-run6/` | Post-deploy cluster, Cloud Build and provenance state |
| `g3-regression/` | Business E2E, replays, MCP auth, runtime state, Tempo/Loki/Prometheus/Grafana readback |
| `prompt-registry/` | agentregistry install, publish/resolve on GKE and kind, production prompt comparison |
| `screenshots/` | Jenkins stage view and test report |

Private (not committed): full IAM policies, Trivy JSON (`jenkins/scans/`), see `.gitignore`.
