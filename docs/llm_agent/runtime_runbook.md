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

## R3 and later

R3 kagent controller and Substrate, R4 MCP Gateway, R5 Registry and KEDA,
R6 E2E: see the sections added by each gate below.
