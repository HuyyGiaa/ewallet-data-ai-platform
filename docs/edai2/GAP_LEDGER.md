# EDAI-2 gap ledger

**Companion to:** [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md). This file lists every row that is not COMPLETE.

**Gap classes:**

| Class | Meaning |
|---|---|
| `DOCUMENTATION_MISSING` | The documentation is missing |
| `SCREENSHOT_MISSING` | Runtime is proven; no image was retained |
| `RUNTIME_EVIDENCE_MISSING` | Source exists; it was not executed or observed |
| `IMPLEMENTATION_MISSING` | The implementation does not exist |

**Priority** combines points at stake with the cost and risk of closing the gap:
- **P1:** no cloud mutation needed.
- **P2:** needs an owner-approved bounded runtime action.
- **P3:** needs implementation work and approval.

| Row | Pts | Class | Existing evidence | Missing proof | Recommended action | Priority |
|---:|---:|---|---|---|---|---|
| 5 | 1 | CLOSED 2026-11-10 | Owner policy screenshots POL-1 to POL-7 and raw probes | — | See [BENCHMARK_RESULTS §5](llm/BENCHMARK_RESULTS.md#5-agentgateway-security-and-policy-evidence) | — |
| 9 | 2 | SCREENSHOT_MISSING | DagRun `rag_fraud_policy_ingestion/manual__2026-11-02T04:47:47…` 6/6 success; publication `b85ae796…` | Airflow Grid/Graph image of that run | Start the existing local Airflow (`rag_runbook.md` §3) and capture the preserved run, if its metadata DB still holds it. Otherwise one bounded re-run (owner approval) | P1 |
| 10 | 2 | SCREENSHOT_MISSING | DataHub GMS read-back: 25 records, 5 assertion runs | DataHub UI lineage/assertion image | Start the persisted DataHub Quickstart alone (RAM budget: check `free -g`, one stack at a time) and capture the RAG dataset lineage and assertions | P1 |
| 29 | 2 | CLOSED 2026-11-08 | Owner screenshots 29_01–05 and raw run at `281db72`; rerun green at HEAD | — | See [Row 29](testing/ROW_29_EQUIVALENCE_BOUNDARY.md) | — |
| 44 | 1 | SCREENSHOT_MISSING | `r2_terraform_plan.json`: 43 resources applied, plan `NO_CHANGES` | An image of Terraform having run | Read-only `terraform state list` / `terraform plan` (expect no changes), captured by the owner | P1 |
| 55 | 2 | DOCUMENTATION_MISSING | README has the business domain, architecture image + mermaid, TOC, repo structure, docs table; module docstrings 339/372 (91 %) | (a) diagram arrows are colour-coded but **not numbered** per flow (only the RAG steps are numbered); (b) the diagram band "Agent Platform Extensions" shows KEDA, Sandbox and A/B, which are not deployed; (c) public function/class docstrings 447/1933 (23 %) | Owner decision: number the arrows and mark the not-deployed capabilities in the diagram; add docstrings to the public API of the submitted modules | P1 |
| 12 | 2 | RUNTIME_EVIDENCE_MISSING | Flink dual-store source (`data_platform/streaming/...`), tests | The jobs running, data in both stores | Bounded Flink run per `MLOPS_FEATURE_STORE.md` "Row 12" capture steps (owner approval; Data Platform ownership) | P2 |
| 27 | 2 | RUNTIME_EVIDENCE_MISSING | Substrate CRDs, WorkerPool `kagent-pilot` 1/1, SandboxAgent Ready (runtime/03) | Image of many agents served by few workers | Bounded demonstration: N SandboxAgents on 1 WorkerPool, capture `kubectl get` + UI (approval: creates objects) | P2 |
| 28 | 1 | CLOSED 2026-11-08 | 99.03 % at `281db72` (screenshots), 91.90 % at HEAD | Raise `feature_app.py` and `vault.py` above 90 % (follow-up, not blocking) | See [Row 28](testing/ROW_28_UNIT_TEST_COVERAGE.md) | P3 |
| 34 | 2 | RUNTIME_EVIDENCE_MISSING | N9 CI agent-ci #2, agent-negative #2; prompt registry live | A CD run building MCP and deploying Agent; agent sourcing its prompt from the registry | Approve the D-lite proposal (`batch_d_mcp_agent_cd_proposal_20261103.md` §9), then one CD rehearsal | P2 |
| 35 | 2 | RUNTIME_EVIDENCE_MISSING | N9 CI | Coordinator CD run | Same as row 34 | P2 |
| 36 | 2 | RUNTIME_EVIDENCE_MISSING | `streaming` component in the CI catalog | `edai2-cd-streaming` run | After row 12 runtime exists, one CD rehearsal | P2 |
| 11 | 2 | IMPLEMENTATION_MISSING | K8s operator jobs, incremental window, parity 155/155 | Materialization orchestrated by Airflow | Thin Airflow DAG wrapping `scripts/mlops/feast_online_job.sh` (same operator); one bounded run | P3 |
| 14 | 2 | IMPLEMENTATION_MISSING | MLflow model v1 with full data lineage; alias `candidate` | Automatic `latest`/`production` alias | Set alias `latest` automatically on registration (keep `production` behind the guarded promotion); re-capture the registry page | P3 |
| 16 | 1 | IMPLEMENTATION_MISSING | FastAPI/Pydantic/health complete (runtime/08); registry, 2 replicas, UI chat | Business agent running in a Sandbox | Run the business agents as SandboxAgents (gVisor `gvisor-default` already exists), or document an accepted equivalent | P3 |
| 18 | 2 | IMPLEMENTATION_MISSING | Helm `llm-agent-mcp` r6, RollingUpdate maxUnavailable 0 | MCP auto fallback | Install KEDA and deploy the ScaledObject with `fallback` (source exists in `autoscaling.yaml`) | P3 |
| 19 | 2 | IMPLEMENTATION_MISSING | 2/2 replicas + HPA live | KEDA autoscaling and an observed scale event | Install KEDA 2.21 (`infra/helm/keda/`), enable the ScaledObject, bounded load and capture | P3 |
| 20 | 1 | IMPLEMENTATION_MISSING | Pilot SandboxAgent only | Business agent sandboxed | Same as row 16 | P3 |
| 22 | 8 | IMPLEMENTATION_MISSING | feature-rag-specialist: registry, 2/2, UI chat with real Feast/KServe/RAG tools, YAML | Sandbox; KEDA autoscale | Rows 16/19 actions applied to the specialist. **Highest-value single gap (8 pts)** | P3 |
| 25 | 2 | IMPLEMENTATION_MISSING + RUNTIME_EVIDENCE_MISSING | 2/2 + HPA; kagent chat | KEDA; image of a scale test | Same as row 19, for the coordinator | P3 |
| 30 | 2 | PARTIAL | mutmut 94.69 % actionable at `281db72`, 91.44 % on integrated code | Literal "only changed code on each push": select mutants from `git diff` and run as a CI stage | Extend `scripts/testing/run_mutation.sh` with a diff-based `only_mutate`; add a Jenkins stage | P2 |
| 31 | 2 | CLOSED 2026-11-08 | Hypothesis properties for prediction, tools, store and Coordinator; owner screenshots 31_01–03 | Optional: CrossHair backend | See [Row 31](testing/ROW_31_PROPERTY_BASED_IDEMPOTENCY.md) | P3 |
| 32 | 2 | CLOSED 2026-11-07 | G5-A in-cluster Locust on GKE, 8 HTML reports | — | See [Row 32](testing/ROW_32_LOCUST_GKE.md) | — |
| 37–43 | 13 | IMPLEMENTATION_MISSING | Internal agentgateway auth/rate limits; UIs reached by port-forward | NGINX routes, basic auth, rate limit, domain + HTTPS | NGINX ingress with one route per UI, `auth_basic` + `limit_req`, cert-manager (approval: public exposure, cluster-scoped objects) | P3 |
| 45 | 1 | IMPLEMENTATION_MISSING | — | Ansible roles | Ansible role(s) for a VM (e.g. the `jenkins_vm` module target) | P3 |
| 52 | 1 | RUNTIME_EVIDENCE_MISSING | Coordinator variants implemented and tested locally | GKE run with real investigations + dashboard | Package P9 | P3 |
| 53 | 1 | RUNTIME_EVIDENCE_MISSING | Weighted routing and GPU stages implemented, tested locally | Both models live on GKE with per-model metrics | Package P10; needs GPU quota | P3 |
| 54 | 1 | RUNTIME_EVIDENCE_MISSING | Vault integration validated on kind | Vault on GKE with a real consumer | Package P7 | P3 |

## Notes that are not gaps but must stay visible

- **Rows 8, 21, 26 (registry metadata).** The registry entries say "SandboxAgent" while the runtime agents are BYO/Declarative. The rows remain COMPLETE because the agents are published; the wording should be corrected at the next publication.
- **Row 6 (historical).** The O1 and G2 results are from 2026-11-01 and were not re-run, by directive.
- **Rows 56–57.** The candidates have runtime proof. The owner must choose which two ideas are submitted.
- **N13 inventory.** The external N13 inventory was located after the first pass and reconciled. See the end of [RUBRIC_EVIDENCE_INDEX.md](RUBRIC_EVIDENCE_INDEX.md).
- **Multi-replica.** Native UI proof was added in the quality gate: runtime/10, Grafana Explore, two pods per agent at r41.
- **vLLM fallback.** `vllm-fallback` is scaled to 0 at r41. Failover was proven only in G1 run 6. This is not a rubric gap, but the documentation no longer claims a live fallback.

## Corrections made during the documentation rewrite

| Item | Earlier text | Corrected from evidence |
|---|---|---|
| `edai2-agent-ci` #2 JUnit result | 538 passed, 1 skipped | 537 passed, 1 skipped (538 total), from the Jenkins test report |
| `edai2-docs-change-detection` #3 JUnit result | 855 passed, 2 skipped; "15 components" | 853 passed, 2 skipped (855 total); 14 component suites |
| vLLM fallback | "primary and fallback live" | `vllm-fallback` is scaled to 0 at the current revision; failover was demonstrated only at the initial deployment |
| Terraform | GKE implied in the recorded apply | The recorded foundation apply had GKE disabled; the later outputs show the cluster managed by the `gke` module |
| RAG assertions | Generic description | The five actual assertion IDs and observed values from the DataHub read-back |
| Curated screenshots | `cicd/03_jenkins_agent_ci.png` | Removed from the repository copy because its change list shows commit trailers; the original is unchanged in the evidence root |
