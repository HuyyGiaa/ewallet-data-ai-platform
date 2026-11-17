# Platform Infrastructure and Security

This report covers:

- how the cloud infrastructure is provisioned with Terraform;
- how access to the cluster and its services is controlled;
- how secrets are handled;
- how traffic inside the cluster is restricted.

It closes with the parts of the external-access and release-experimentation design that are not yet built.

## 1. Infrastructure as code (Terraform)

All GCP resources are declared with Terraform under [`infra/terraform/gcp/`](../../../infra/terraform/gcp). The code is split by service, so each concern can be reviewed and planned on its own:

| Directory | Purpose |
|---|---|
| `bootstrap/` | Remote state bucket (versioning, uniform bucket-level access, public access prevention) |
| `environments/dev/` | The development environment root module that composes the service modules |
| `modules/project_services` | Required Google APIs |
| `modules/network` | VPC, subnetwork, firewall |
| `modules/gke` | GKE cluster and CPU node pool |
| `modules/iam`, `modules/workload_identity` | Service accounts, project IAM, Workload Identity bindings |
| `modules/storage` | Data and CI build buckets |
| `modules/artifact_registry` | Container image repository |
| `modules/dataproc` | Dataproc Serverless prerequisites for the Spark jobs |
| `modules/cicd_build` | Cloud Build service account and permissions |
| `modules/jenkins_vm` | Optional VM for Jenkins (Jenkins currently runs on GKE instead) |

**GKE module safeguards.** The module ([`modules/gke/main.tf`](../../../infra/terraform/gcp/modules/gke/main.tf)) enables Workload Identity and a master authorized-networks list. It contains validation rules that reject an empty list and reject `0.0.0.0/0`, so the control plane can never be exposed publicly by mistake.

**Execution record.** The foundation provisioning record ([`r2_terraform_plan.json`](../../evidence/cloud/r2_terraform_plan.json), source `02fffb2`):

| Step | Result |
|---|---|
| Bootstrap | Applied: the state bucket |
| First environment apply | 42 resources created. 3 Workload Identity bindings failed, because the GKE identity pool did not exist before the cluster |
| Recovery plan | 1 create, 42 unchanged; the next plan showed no changes |
| Foundation state | 43 managed resources: 13 enabled services, 6 service accounts, 17 IAM bindings (project, bucket and service-account), the network, subnetwork and firewall, the Artifact Registry repository, 2 buckets, and the Dataproc prerequisites |
| GKE | The cluster module is guarded by `enable_gke` (off by default, for cost). After it was enabled, the environment outputs report `gke_cluster_name = ewallet-dev` (read-only output capture, 2026-10-29). The output is null while GKE is disabled ([`outputs.tf`](../../../infra/terraform/gcp/environments/dev/outputs.tf)), so this confirms the cluster is managed by the `gke` module |

Neither the plan binary nor any credentials are stored. The runbook is [`docs/cloud/terraform_gcp_foundation.md`](../../cloud/terraform_gcp_foundation.md).

## 2. Access control

| Layer | Mechanism |
|---|---|
| Cluster control plane | Master authorized networks: two `/32` operator addresses |
| Operator access to UIs (Grafana, kagent, Agent Registry, Jenkins, Kubeflow, MLflow) | `kubectl port-forward` from an authorized network; nothing is exposed through a public load balancer. See the [UI runbook](../../runbooks/edai2/UI_REPLAY.md) |
| Workload identity to GCP | Workload Identity: pods use Kubernetes service accounts mapped to GCP service accounts, so no key files exist |
| CI/CD | The Jenkins service account may write only to its target namespace ([CI/CD §4.4](../cicd/CICD.md#44-least-privilege)) |
| LLM, MCP and A2A traffic | agentgateway with client keys, per-agent tool authorization and rate limits ([LLM Platform §3.2](../llm/LLM_PLATFORM.md#4-agentgateway), [Agents §7](../agents/AGENTS_REGISTRY_GATEWAYS.md#7-mcp-gateway-and-a2a-gateway-security)) |

## 3. In-cluster network security

**Mesh.** The agent namespaces run in Istio ambient mode, which provides mTLS and workload identities.

**Policies.** The Helm chart ([`templates/security.yaml`](../../../infra/helm/llm-agent-platform/templates/security.yaml), [`templates/cross-namespace.yaml`](../../../infra/helm/llm-agent-platform/templates/cross-namespace.yaml)) adds two layers:

- **NetworkPolicies** (Calico): default-deny plus explicit allows for each flow, for example agentgateway → vLLM, specialist → mcp-gateway and Alloy → Loki;
- **Istio AuthorizationPolicies**: a server accepts a request only from the expected service-account principal.

Egress to the Kubernetes API server is pinned to the exact API server addresses.

**Chart tests.** The chart tests verify these properties on every change:

- RBAC verbs;
- NetworkPolicy peers;
- the absence of `nodes/proxy`;
- digest-pinned images;
- non-root containers with no host access.

See [Testing §3.4](../testing/TESTING.md#34-chart-and-security-contracts).

## 4. Secret management

**Storage and injection.**

- Credentials (API keys, MCP service tokens, the Redis password, the Grafana admin password) are Kubernetes Secrets scoped to the namespace that uses them.
- They are injected as environment variables or files and never placed in images, Helm values or logs.

**Rotation.** Rotation follows a documented lifecycle ([`docs/llm_agent/g3_credential_lifecycle.md`](../../llm_agent/g3_credential_lifecycle.md)): a new key is issued, consumers roll, and the old key is revoked. The MCP negative tests confirm that a revoked token is rejected.

**Leak checks.**

- Log shipping redacts known secret patterns.
- A scan of the shipped logs found no credential values ([Observability §4](../observability/OBSERVABILITY.md#4-logs)).

## 5. Limitations and future work

- **External gateway.** No NGINX (or other) ingress publishes the UIs or the Fraud API. There is therefore no domain, HTTPS certificate, basic authentication or edge rate limiting. All UI access goes through authorized port-forwards. An ingress with per-route authentication, rate limits and cert-manager certificates is future work.
- **Centralised secrets.** Vault is implemented and validated locally but not deployed. See [Vault](VAULT.md).
- **Configuration management.** No Ansible playbooks exist. The only VM-oriented module (`jenkins_vm`) is unused, because Jenkins runs on GKE.
- **A/B experimentation.** Model, agent and LLM A/B are implemented and tested locally. None has run on GKE. See [ML](../mlops/ML_AB_DESIGN.md), [Agent](../agents/AGENT_AB.md) and [LLM](LLM_AB_DESIGN.md).
- **Evidence.** The Terraform run is documented by its recorded plan and apply results. No terminal or console screenshot of it was kept.
