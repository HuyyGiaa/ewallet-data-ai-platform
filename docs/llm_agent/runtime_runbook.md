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

Status: **source-ready, not run.** R1 needs DP2 terminal and owner approval
for a `helm upgrade` on the local kind cluster. Nothing cluster-scoped is
installed.

### What the 2026-10-31 HTTP 504 was, and what changed

The offline mock showed two causes; the real agentgateway v1.7.0 binary runs
in front of a mock vLLM (`scripts/llm_agent/r1_mock/run.sh`, evidence
`docs/evidence/llm_agent/source_review/r1_prep/gateway_mock_scenarios.json`).

1. **Replay.** The entry route (`:4000` → LLM listener `:3000`) had its own
   `backendRequestTimeout`, equal to the listener's, and retried
   500/502/503/504 twice. When the hop timed out, the whole CPU generation
   was replayed: the backend saw two requests, then the entry budget gave a
   504.
   - Now the entry route has only the overall budget.
   - Retry is limited to 502/503, once.
   - Result: a slow backend gives a single 504 at the listener budget, with
     one backend request.
2. **Budgets.** The budgets were 28 s and 10 s against a 40–80 s CPU
   generation, and the smoke client gave up at 120 s.
   - Budgets now nest, enforced by a chart guard: ModelConfig 200 s > entry
     180 s > listener 170 s > backend 160 s ≥ idle 160 s.
   - The acceptance client waits 220 s.

Other offline mock results:
- 500 is not replayed.
- 503 is retried once, then propagated (fail closed).
- After two failures the primary is evicted, and the next request is served
  by the fallback (`x-ewallet-backend: fallback`).
- Stream and non-stream `tool_calls` pass through intact. The gateway
  rewrites `model` to `ewallet-fraud-llm-v1` and replaces the client key with
  the vLLM key.

### Steps (after DP2 is terminal)

```bash
cd ~/Develop/ewallet-data-ai-platform/.worktrees/claude-integration-20261031
K=kind-llm-agent-local; NS=ewallet-agents; E=docs/evidence/llm_agent/r1; mkdir -p $E
CHART=infra/helm/llm-agent-platform

# 0. Resource guard in a second terminal (exits 3 on a breach: stop and run step 8).
scripts/llm_agent/resource_sample.sh 10 0 | tee $E/resource_samples.txt

# 1. Read-only preflight: must print "verdict": "GO".
python3 -m ops.llm_agent.r1_preflight --kube-context $K --json-output $E/preflight.json

# 2. Owner-approved upgrade of the existing release (namespaced resources only).
#    values-r1-local.yaml disables the stale MCP release and keeps one vLLM pod.
helm --kube-context $K -n default upgrade llm-agent $CHART \
  -f $CHART/values-local.yaml -f $CHART/values-r1-local.yaml --wait --timeout 25m \
  | tee $E/helm_upgrade.txt
kubectl --context $K -n $NS rollout status deploy/vllm-primary --timeout=25m   # warm-up probe = one real tool call
kubectl --context $K -n $NS get deploy,pods -o wide > $E/workloads.txt

# 3. Local demo token (never printed or committed).
export EWALLET_VLLM_API_KEY="$(cat ~/Develop/ewallet-llm-agent-local/.vllm_local_demo_token)"

# 4. Direct vLLM.
kubectl --context $K -n $NS port-forward svc/vllm-primary 18000:8000 >/dev/null & PF1=$!
python3 scripts/llm_agent/smoke_tool_call.py --endpoint http://127.0.0.1:18000 --model ewallet-fraud-llm-v1 --evidence $E/direct_non_stream.json
python3 scripts/llm_agent/smoke_tool_call.py --endpoint http://127.0.0.1:18000 --model ewallet-fraud-llm-v1 --stream --evidence $E/direct_stream.json

# 5. Through agentgateway, with the virtual model the ModelConfig uses.
kubectl --context $K -n $NS port-forward svc/agentgateway 13000:3000 >/dev/null & PF2=$!
python3 scripts/llm_agent/smoke_tool_call.py --endpoint http://127.0.0.1:13000 --model ewallet-fraud-llm --evidence $E/gateway_non_stream.json
python3 scripts/llm_agent/smoke_tool_call.py --endpoint http://127.0.0.1:13000 --model ewallet-fraud-llm --stream --evidence $E/gateway_stream.json

# 6. ModelConfig readback (CRD only; agents consume it in R3) and gateway LLM metrics.
kubectl --context $K -n $NS get modelconfig ewallet-global-llm -o yaml > $E/modelconfig.yaml
kubectl --context $K -n $NS port-forward deploy/agentgateway 15020:15020 >/dev/null & PF3=$!
curl -s http://127.0.0.1:15020/metrics | grep -E '^# TYPE agentgateway_(gen_ai|requests|request_duration)' > $E/gateway_metric_types.txt
docker stats llm-agent-local-control-plane --no-stream > $E/node_resources.txt
kill $PF1 $PF2 $PF3

# 7. Optional, separately approved: rate-limit 429 and failover (needs the fallback pod).

# 8. Stop / rollback.
kubectl --context $K -n $NS scale deploy/vllm-primary --replicas=0        # free CPU/RAM
helm --kube-context $K -n default rollback llm-agent 5                     # previous release, if needed
```

### Acceptance (runtime evidence only; offline mock results do not count)

| ID | Criterion | Evidence |
|---|---|---|
| A1 | Preflight verdict GO | `preflight.json` |
| A2 | `vllm-primary` Ready: the warm-up startup probe completed a real tool-shaped completion | `workloads.txt` |
| A3 | Direct, non-stream: HTTP 200, `verdict: PASS`, `tool_call_errors: []`, second turn 200 | `direct_non_stream.json` |
| A4 | Direct, stream: same, `finish_reason: tool_calls`, `first_byte_s` recorded | `direct_stream.json` |
| A5 | Gateway, non-stream: as A3, plus `backend: primary` | `gateway_non_stream.json` |
| A6 | Gateway, stream: as A4, plus `backend: primary` | `gateway_stream.json` |
| A7 | No 504 and no client timeout in A3–A6. Latency per turn recorded (`latency_s`, `first_byte_s`, token `usage`) | the four JSON files |
| A8 | No guard breach; Airflow healthy throughout; node CPU/RAM recorded | `resource_samples.txt`, `node_resources.txt` |
| A9 | ModelConfig read back: model `ewallet-fraud-llm`, baseUrl agentgateway `:3000/v1` | `modelconfig.yaml` |
| A10 | Token and TTFT metric names present (confirms the UNVERIFIED dashboard panels) | `gateway_metric_types.txt` |

R1 is LOCAL_RUNTIME_VERIFIED only when A1–A9 pass. A10 updates the dashboard
status. `--tool-choice auto` may legitimately produce no tool call; acceptance
uses `required`.

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
