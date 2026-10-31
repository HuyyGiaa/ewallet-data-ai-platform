# LLM + Agent runtime runbook (local kind, small scale)

Nothing here has run unless an evidence file says so. Each milestone needs
the resource gate, and needs owner approval where marked.

## Resource gate (before every step)

- DP2 is not running: no active `dp2_*` DagRun and no `gcloud dataproc
  batches submit` process. Otherwise, explicit owner clearance is needed.
- Airflow `http://localhost:8084/api/v2/monitor/health`: scheduler and
  dag_processor `healthy`. Never restart Airflow.
- `MemAvailable` ≥ 8 GiB, normalized load ≤ 0.8, `/proc/pressure/memory`
  avg10 near 0, no swap thrashing (`vmstat 1 5`: si/so 0).
- Sample every 10 s during a step (`scripts/llm_agent/resource_sample.sh`).
  Stop and scale back to zero on any breach.
- Run at most one vLLM pod at a time. The kind node is capped at 4 CPU / 9 GiB.
- Always pass `--context kind-llm-agent-local`. The default context is GKE.

## Stage the model

The pinned files sit under `~/Develop/ewallet-llm-agent-local/models` and are
mounted read-only by `infra/kubernetes/llm-agent-local/model-pv.yaml`.
`values-local.yaml` holds their SHA-256 list. The vLLM init container checks
every file with `sha256sum --check --strict` before vLLM starts offline.

## R1 — ModelConfig → agentgateway → vLLM (direct)

The first runtime milestone. The 2026-10-31 attempt returned HTTP 504 through
agentgateway. Causes fixed in source:

- the listener request timeout was hard-coded to 28 s, and `responseIdleTimeout`
  was 10 s, against a 40–80 s CPU generation;
- Qwen3 thinking was enabled.

```bash
helm --kube-context kind-llm-agent-local upgrade llm-agent infra/helm/llm-agent-platform \
  -f infra/helm/llm-agent-platform/values-local.yaml -f ~/Develop/ewallet-llm-agent-local/mcp-values.yaml
kubectl --context kind-llm-agent-local -n ewallet-agents rollout status deploy/vllm-primary --timeout=20m
python3 scripts/llm_agent/smoke_tool_call.py --endpoint http://127.0.0.1:<port-forward of svc/agentgateway 3000> \
  --model ewallet-fraud-llm --tool-choice required
```

Pass criteria (evidence in `docs/evidence/llm_agent/agentgateway/`):
- warm-up startup probe Ready;
- tool call 200 with `tool_calls` for `auto` and `required`;
- `x-ewallet-backend: primary`;
- 429 once past the request rate;
- ModelConfig `ewallet-global-llm` read back. Only the CRD is needed; agents
  consuming it come in R3.

Rollback: `helm rollback` to the previous revision, and scale vLLM to 0.

## R2 — llm-d routing (owner approval: controller and CRDs are cluster-scoped)

1. `source infra/helm/llm-d/versions.env`. Install the GAIE manifest after the
   sha256 check, `agentgateway-crds` and `agentgateway`
   (`-f infra/helm/llm-d/agentgateway-controller-values.yaml`) into
   `agentgateway-system`, and the router chart
   (`-f infra/helm/llm-d/router-values.yaml`) into `ewallet-agents`, every
   chart with `--version` pinned.
2. `helm upgrade llm-agent ... --set llmd.enabled=true`.
3. Read back the provisioned `llm-inference-gateway` Deployment labels and
   ServiceAccount. Declare them as a workload with the four unresolved flows in
   `llmd.unresolvedFlows`, then upgrade again.
4. Checks:
   - `kubectl get inferencepool ewallet-fraud-llm -o yaml` shows Accepted and
     ResolvedRefs status;
   - EPP logs show a pick for each request;
   - the response carries `x-ewallet-backend: llm-d`.
5. Failure test: scale the EPP to 0. Requests fail over (`x-ewallet-backend:
   primary`) within `circuitBreakDuration`. Scale it back.
6. Routing decision: only with owner approval for a second primary replica
   (about 3 CPU / 6 GiB more). Without it, record "single endpoint, no scoring
   decision observable".

## R3 — Substrate, kagent controller and agents (owner approval: cluster-scoped CRDs)

1. Install Substrate 0.0.9 and kagent 0.10.3 by digest
   (`infra/kubernetes/kagent-pilot/versions.env`), the kagent chart with
   `-f infra/kubernetes/kagent-platform/kagent-values.yaml`.
2. `helm upgrade llm-agent ... --set agents.enabled=true --set
   agents.snapshotsLocation=<approved bucket prefix>`.
3. Read back:
   - `kubectl get workerpool,sandboxagent,remotemcpserver -n ewallet-agents`
     (Ready/Accepted);
   - **the ServiceAccount of each worker pod** (identity blocker,
     docs/llm_agent/agents.md);
   - A2A agent cards at `a2a-gateway:8083/api/a2a/ewallet-agents/<agent>`.
4. Delegation trace: one Coordinator task, then the trace shows Coordinator →
   a2a-gateway → specialist → MCP Gateway. With the MCP servers still on
   DenyAllAuthenticator, the expected tool result is
   `authentication_not_configured`. Record it as such; never as business
   success.

Resource estimate: Substrate system pods ≈ 1 CPU / 1.5 GiB, 3 WorkerPools ×
(0.5 CPU / 1 GiB requests), kagent controller and UI ≈ 0.5 CPU / 0.7 GiB.
That is too much beside one vLLM pod on the laptop kind node (4 CPU / 9 GiB):
run R3 with vLLM scaled to 0 (agent calls then fail over to an unavailable
LLM, which is recorded), or on GKE with approval.

## R4 — MCP Gateway and A2A gateway

Locally, without a cluster (already run, evidence committed):
`scripts/llm_agent/validate_gateway_configs.sh` and
`scripts/llm_agent/gateway_smoke/run.sh`.

In-cluster (needs the namespace enrolled in ambient, and ztunnel healthy: the
kind ztunnel release is `failed` because of the host inotify limit, which
needs an owner-approved sysctl):
1. `--set mcpGateway.enabled=true --set a2aGateway.enabled=true`, with the
   MCP servers at a reviewed image digest.
2. Identity proof (docs/llm_agent/mcp_gateway.md): log `source.identity` at the
   gateway for a call from each specialist worker. Without it, stop: the
   result is DEPLOY_NO_GO.
3. Negative tests:
   - a pod outside the gateway calls an MCP server directly: denied (L4);
   - `drift-specialist` calls `fraud_scoring`: filtered and denied;
   - a spoofed `x-ewallet-caller` is overwritten.

## R5 — Registry and KEDA

Registry locally (already run): `scripts/llm_agent/registry_local_smoke.sh`.

In-cluster:
1. Install agentregistry 0.4.0 by digest with
   `-f infra/helm/agentregistry/values.yaml`.
2. Port-forward `svc/agentregistry 12121`.
3. `python -m ops.llm_agent.registry_catalog publish --evidence ...`.
4. Take a UI screenshot of the three agents (rows 8, 21, 26, 41).

KEDA:
1. Install KEDA 2.21.0 (`-f infra/helm/keda/values.yaml`).
2. `--set agents.autoscaling.enabled=true`.
3. `kubectl get scaledobject,hpa -n ewallet-agents` shows Ready.
4. Scale test: in the cron window `kubectl get workerpool` shows 2. Outside it,
   1.
5. Measure first-request latency with one warm worker against a cold start
   (scale to 0 by hand, then send one request).

## R6 — End-to-end (row 22)

R6 needs, all at once:
- R1 (LLM);
- R3 (agents);
- R4 with proven identity;
- reviewed MCP server images;
- Feast online Redis with materialized features;
- the KServe fraud model with a reviewed wire contract;
- the published RAG index (Gate F).

Any missing dependency is reported as BLOCKED with its name. A stub or
fixture output is never accepted as E2E evidence.
