# LLM + Agent campaign, CPU on GKE (2026-10-31)

Owner decisions this campaign: CPU vLLM is the runtime target (instructor
accepts CPU inference); it runs on GKE `ewallet-dev`, not kind or Docker; no
GPU. Google Docs (`1uShjF2P…`) was **not readable** from this session (no
authenticated Google connector); this report relies on the reviewed source,
the original workbook `EDAI K11.xlsx` sheet `edai-2 (50%)`, and runtime reads.

## A. Source identity

| Item | Value |
|---|---|
| Worktree / branch | `.worktrees/claude-integration-20261031`, `claude/cpu-gke-runtime-20261031` |
| Start | `5bb6a3c` (post-R1 source) |
| Commits | `e0a8aa6` GKE CPU profile, Istio ambient for GKE, stage preflight; this report |
| Canonical | `feature/phase2-fraud-platform` fast-forwarded locally `e58a500 -> 5bb6a3c` (no push) |

Gate 0 (DP2 closure, read-only): DagRun `dp2-method2-ab-20261031035745` is
terminal, 24/25 tasks `success`, `validate_and_select_gold_winner` `failed`
(bit-exact fingerprint on `double` aggregates; evidence in
`docs/evidence/dp2_method2_ab-20261031035745/`). No RUNNING/PENDING Dataproc
batch. Airflow runs from the canonical checkout, so before the fast-forward
both trees' DAG folders were parsed in an isolated `AIRFLOW_HOME`: 0 import
errors; the only new DAG (`rag_fraud_policy`) has `schedule=None` and Airflow
pauses new DAGs. Airflow health stayed `healthy` after the fast-forward.

Block B (`139ba7e`): content-identical to HEAD (0-line diff, integrated as
`acad36b`); `tests/inference` 55 passed in the uv-locked tooling environment.
MCP `fraud_scoring` uses its `FraudPredictionRequest/Response` contract.

Worktrees removed (merged, clean, caches only, no process): `claude-dp2-gold-fix`,
`codex-consolidation-ready-20261030`, `codex-ml1-training-snapshot`; branch refs kept.
Retained: everything with unique commits or dirty files (`codex-streaming-block-a`
14 dirty, `llm-agent-rubric-20261030` 2 dirty, `codex-feast-training-runtime` in use),
`codex-dp2-method2-recovery` (DP2 recovery, Dataproc build artifacts), and this
integration worktree.

## B. CPU readiness on GKE

See `docs/llm_agent/gke_cpu_runtime.md` and
`docs/evidence/llm_agent/gke_cpu/cost_and_cluster_20261031.json`.

- `cpu-pool` e2-standard-4 has **0 nodes**; every runtime stage needs a paid resize.
- e2-standard-4 node-hour ≈ 0.1729 USD (catalog); plan ≈ 4.15 USD (2 nodes × 3 h, 3 nodes × 6 h), proposed ceiling 10 USD.
- Image `vllm-openai-cpu:v0.32.0` by digest, Qwen3-0.6B @ `c1899de2…`, primary + fallback, CPU budgets 200/180/170/160/160 s.
- Deployment status: **not deployed** (awaiting approval). No runtime evidence yet.

## C. Agent readiness

kagent 0.10.3, Substrate 0.0.9, Coordinator + two specialists, RemoteMCPServer,
WorkerPool/KEDA, Registry: source and offline tests only; no CRD on GKE.

## D. Business E2E

Feast Online (Redis), KServe, RAG publication and drift baseline are not running
on GKE. R22 is not verified.

## E. Security

- NetworkPolicy is **not enforced** on GKE (legacy dataplane, policy disabled): the chart's default-deny is a no-op there until the owner enables it (or waives it explicitly).
- Control-plane authorized network is a stale /32; access used the IAM DNS endpoint with a separate kubeconfig; nothing was changed.
- MCP identity: unchanged, **MCP_DEPLOY_NO_GO** (DenyAllAuthenticator stays).
- Correction to the previous audit: the `llm-agent` Helm release lives in `default` by design (`deploy_atomic.sh --namespace default`; the chart creates `ewallet-agents`). The kind `ztunnel` release is `failed` because of the host inotify limit (runbook), while its pod runs.

## F. Rubric matrix (edai-2, LLM/Agent rows; raw points from the workbook)

Classification: SOURCE_TESTED = source + offline tests; nothing below is RUNTIME_VERIFIED.

| Row | Requirement (abridged) | Pts | Status | Remaining |
|---|---|---|---|---|
| 3 | Deploy LLM inference platform (llm-d quickstart) | 2 | SOURCE_TESTED | G1/G2 on GKE |
| 4 | Custom LLM model | 2 | SOURCE_TESTED | served model readback |
| 5 | agentgateway + security/policy | 1 | INTEGRATION_TESTED (real binary, mock backend) | real backend, 401 test |
| 6 | Benchmark + optimize | 2 | SOURCE_TESTED | CPU benchmark on GKE |
| 7 | Global ModelConfig | 2 | SOURCE_TESTED | CRD + readback |
| 8 | Agent registry + UI screenshot | 2 | INTEGRATION_TESTED (local smoke) | deploy, UI |
| 15 | KServe serving | 2 | SOURCE_TESTED (Block B) | KServe on GKE |
| 16-18 | FastAPI/pydantic, async, MCP helm + rolling + fallback | 1+1+2 | SOURCE_TESTED | deploy MCP image |
| 19-21 | Agent multi-replica/autoscale, sandbox, registry | 2+1+1 | SOURCE_TESTED | G3 |
| 22 | Second agent (Online FS + 2 tools) | 8 | SOURCE_TESTED | Feast Online + KServe + G3 |
| 23 | MCP Gateway | 2 | BLOCKED (identity) | identity gate |
| 24 | A2A Gateway | 2 | BLOCKED (identity) | identity gate |
| 25-26 | Coordinator multi-replica/autoscale, registry | 2+2 | SOURCE_TESTED | G3 |
| 27 | Warm-up (Substrate) | 2 | SOURCE_TESTED | G3 |
| 33-35 | CI/CD RAG, MCP/agent, coordinator | 2+2+2 | SOURCE_TESTED (fake-tool tests) | real Jenkins run |
| 37-43 | Gateway/ingress, UIs, auth, HTTPS | 13 | SOURCE_TESTED (Istio instead of NGINX: literal-equivalence risk) | G0 + Gateway API |
| 46-51 | Observability incl. LLM/agent telemetry | 8 | INTEGRATION_TESTED (OTel redaction smoke) | traces/metrics on GKE |
| 52-53 | A/B agents, LLM versions | 1+1 | NOT_IMPLEMENTED at runtime | after G3 |
| 54 | Vault | 1 | NOT_IMPLEMENTED | deferred |

## G. Deferred work and approvals

1. Resize `cpu-pool` 0 -> 2 (then 3), ceiling 10 USD, resize to 0 after each session.
2. NetworkPolicy enforcement (or explicit waiver).
3. Cluster-scoped installs: Istio ambient (G0), kagent CRDs (G1), GAIE + llm-d (G2), Substrate + kagent (G3).
4. Secret `vllm-api-key` from a generated token; `agents.snapshotsLocation` bucket.
5. MCP identity gate (Task 2), Feast Online/Redis and KServe on GKE for R22.

Independent work still available without approval: MCP identity design against
pinned agentgateway (JWT per specialist), Hypothesis/mutmut on bounded modules,
DP2 Gold equivalence tolerance fix (separate Data Platform lane).

## Copy-ready Living Decision (not written to Google Docs)

> **21.300 — CPU vLLM on GKE is the LLM runtime target.** The instructor accepts
> CPU inference. vLLM CPU (`v0.32.0` by digest, Qwen3-0.6B @ `c1899de2`) runs on
> GKE `ewallet-dev` `cpu-pool` (e2-standard-4, AVX2) through
> ModelConfig -> agentgateway -> llm-d, profile `values-gke-cpu.yaml` with CPU
> timeout budgets (200/180/170/160/160 s; A2A 660 s < 700 s). No GPU, CUDA or
> GPU quota. Runtime stages G0-G3 each need owner approval of the node count
> and cost ceiling (plan ≈ 4.15 USD, ceiling 10 USD) and a `gke_preflight` GO.
> NetworkPolicy enforcement must be enabled or explicitly waived before G1.
