# G2 deployment readiness

Source candidate: branch `integration/g0-20261107`, the commit that adds this file. It is local and unpushed. **Nothing in it has run on GKE.** The cluster is asleep: cpu-pool at 0 nodes.

This page says what each component needs before it can be deployed for the CPU verification campaign.

**GPU quota and a public domain block only the GPU LLM A/B (P10) and the public edge phase.** They do not block the CPU campaign.

## Verdict by component

| Component | Packages | Deployable after | Blocked by |
|---|---|---|---|
| Fraud API (new image), Feature API | P-A1, P-A2 | Image build; R2 steps 1–4 and 6 (Terraform IAM, KSAs, identity check) | Terraform apply approval |
| Serving KEDA | P-A3 | P-A1; KEDA install; R1 handover | Nothing beyond its own approval |
| Agent images (Coordinator, Specialist) | P-A4, P9 | Image build | **Security gate below:** synthetic, PII-free prompts only |
| Agent KEDA | P-A4 | Agent images with the in-flight gauge | Security gate |
| Edge, internal phase | P-A5 phase 1 | P-A1; ClusterIP gateway | Nothing beyond its own approval |
| Edge, public phase | P-A5 phase 2 | Domain, issuer, load balancer decision | Owner decisions |
| Vault for the Fraud API | P7 | R2 step 5 in the same window as the Fraud API switch | Vault unseal on wake |
| ML A/B | P8 | Candidate v2 registered in MLflow | v2 not registered |
| LLM A/B on GPU | P10 | GPU quota | `GPUS_ALL_REGIONS=0` |
| Streaming release | P1 (streaming) | Images built from this commit, with the watermark fix | Jenkins CD approval |

## Security release gate (R8)

| Surface | State | Gate |
|---|---|---|
| LLM transport | Fixed in source (B1-F6, B1-F7, evasion forms) | Ship only images built from this commit; r41 still leaks |
| Conversation persistence | **Open (R8-P1):** kagent stores the raw prompt | **No real or PII-bearing prompts.** The CPU campaign uses synthetic prompts without PII |
| Logs and traces | Clean in source | During the agent campaign, check agentgateway and kagent controller access logs once with a synthetic PII prompt |

## Images to build

All images must be pinned by digest. Build them with Cloud Build rather than on the laptop: only 18 GB of disk is free.

| Image | Source | Why it changes |
|---|---|---|
| Fraud API (also serves the Feature API) | `ai_platform/inference/Dockerfile` | Feature API, Vault loader, ML A/B router |
| BYO agent (Coordinator, Feature-RAG Specialist) | `infra/docker/g3-byo-coordinator/Dockerfile` | R8 masking, Agent A/B, in-flight gauge, MCP client failover |
| MCP and tooling | `infra/docker/llm-agent-tooling/Dockerfile` | Shares `ai_platform/llm_agent`; rebuild for one source revision |
| Flink and streaming sinks | `infra/docker/flink-benchmark`, `infra/docker/streaming-sinks` | C-A watermark fix and sink jobs |
| Candidate predictor | P8 tooling | Only for ML A/B |

## Namespaces and new resources

| Namespace | New in this candidate |
|---|---|
| `ml-platform` | KSAs `fraud-api`, `feature-api`; Deployment and Service `feature-api`; headless metrics Services; ScaledObjects `feature-api`, `fraud-api`; network policies for the edge |
| `feature-store` | Additive policy `redis-clients-feature-api` |
| `ewallet-vault` | Roles and policies `fraud-api`, `feature-api`; network policy admitting both |
| `keda` | KEDA 2.21 controller and CRDs (P-A3) |
| `ewallet-agents` | ScaledObjects replacing three HPAs via `transferHpa` (P-A4); edge allow policy for Grafana |
| `ewallet-edge` | `istio/gateway` Deployment and ClusterIP Service, Gateway, VirtualServices, EnvoyFilter (P-A5) |
| GCP | GSAs `ewallet-fraud-api`, `ewallet-feature-api`; conditional `objectViewer` on `features/registries/`; Workload Identity bindings |

The production render of `llm-agent` r41 changes only the Grafana and Prometheus config checksums. Both pods restart at the next upgrade.

## Ownership handovers

- **R1, replicas.** Hand the Fraud API's replica count to KEDA with `scripts/serving/fraud_api_keda_handover.sh`. Never apply the `keda` manifest directly. See [R1_FRAUD_API_KEDA_HANDOVER.md](R1_FRAUD_API_KEDA_HANDOVER.md).
- **R2, identities.** Follow [R2_SERVING_IDENTITY_MIGRATION.md](R2_SERVING_IDENTITY_MIGRATION.md). Terraform first, then the KSAs and an identity check, then the rollout. Do not run `terraform apply` while the cluster is asleep, because it reconciles the node pool. Apply only the `iam` and `workload_identity` changes, on the reviewed plan.
- **Agents.** The `transferHpa` path from C-B. Delete ScaledObjects before removing KEDA.

## Capacity

Node counts come from the Session A packages. Each node is an `e2-standard-4` on cpu-pool.

| Campaign | Nodes | List price |
|---|---|---|
| P-A1, P-A2 serving | 1 | $0.285/hour |
| P-A3 serving KEDA | 2 | $0.46/hour |
| P-A5 edge | 2 | $0.46/hour, plus a load balancer only in phase 2 |
| P-A4 agent KEDA | 3 | $0.64/hour |

Per-replica requests:

| Workload | Request | Limit | Replicas |
|---|---|---|---|
| Feature API | 250m CPU, 512 MiB | 1 CPU, 1 GiB | 1–3 |
| Fraud API | 250m CPU, 512 MiB | 1 CPU, 1 GiB | 1–3 |

Three replicas of each fit in one extra node.

**The KServe predictor stays at one replica (R3).** Measure it as the expected bottleneck in A-02, and do not add a scaler.

## Recommended wake sequence

1. **Owner.** Billing and credit check. Approve the packages that will run.
2. **Read-only.** P-A0 preflight; `cloud_lab.py status`.
3. **Build.** Cloud Build images from this commit; record the digests.
4. **Terraform.** Targeted R2 IAM apply on the reviewed plan.
5. **Wake.** `cloud_lab.py wake-minimal --profile serving-load`. Unseal Vault only if P7 runs, and check Valkey re-forms.
6. **Serving.** R2 steps 2–4, then P-A1 (Feature API with its own account) and P-A2.
7. **Serving KEDA.** P-A3 fixed arms, the R1 handover, the KEDA arms, then `verify`.
8. **Agents.** P-A4 with the new agent images, synthetic prompts only.
9. **Edge.** P-A5 phase 1.
10. **Evidence.** Collect under `docs/evidence/edai2/{load,keda,edge}`.
11. **Sleep.** New recovery manifest, then `cloud_lab.py sleep --scale-pdb-guarded`.

## Rollback

| Change | Rollback |
|---|---|
| Fraud API identity | Patch `serviceAccountName` back to `training`, which keeps its binding; restore `bootstrap.json` if P7 is live |
| Feature API | `helm rollback` or uninstall `ewallet-serving` |
| Serving KEDA | `autoscaling.enabled=false`, then the R1 `rollback` action |
| Agent KEDA | C-B `transferHpa` rollback: ScaledObjects first |
| Edge | Uninstall `ewallet-edge` and the gateway release |
| Agent images | `helm rollback llm-agent` to r41, which reintroduces the B1-F6 and F7 masking gaps |
