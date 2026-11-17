# EDAI-2 UI Access Runbook (read-only)

**Purpose.** Reach the UIs of the GKE deployment and reproduce the screenshots used in the EDAI-2 reports.

**Scope.** Every step is read-only, except the optional chat in §3, which sends one bounded prompt (one vLLM request).

**Do not:**

- press "Let's Get Started" in kagent;
- start KFP runs;
- trigger DAGs or Jenkins builds;
- change Helm releases.

## 0. Preconditions

```bash
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26
helm -n default history llm-agent --max 1        # the documentation was captured at revision 41
```

Notes:

- The cluster control plane accepts only the configured authorized networks (two `/32` operator addresses). Run from one of them.
- No credential is printed by any step below. Never paste Secret values into evidence.

## 1. Port-forwards

Open only the ones you need. **The MLflow local port must be 5000**, because of its Host allowlist.

| UI | Command | URL |
|---|---|---|
| kagent UI | `kubectl -n kagent port-forward svc/kagent-ui 18300:8080` | http://127.0.0.1:18300 (click **Skip wizard**) |
| Agent registry | `kubectl -n ewallet-prompts port-forward svc/prompt-registry-agentregistry 18121:12121` | http://127.0.0.1:18121 |
| Grafana (Prometheus, Loki, Tempo) | `kubectl -n ewallet-agents port-forward svc/grafana 13000:3000` | http://127.0.0.1:13000 |
| vLLM counters | `kubectl -n ewallet-agents port-forward svc/vllm-primary 18000:8000` | `/metrics` |
| Kubeflow Pipelines UI | `kubectl -n kubeflow port-forward svc/ml-pipeline-ui 18800:80` | http://127.0.0.1:18800 |
| MLflow | `kubectl -n ml-platform port-forward svc/mlflow 5000:5000` | http://127.0.0.1:5000 |
| Jenkins | `kubectl -n ewallet-ci port-forward svc/jenkins 18080:8080` | http://127.0.0.1:18080 |
| KServe predictor | `kubectl -n fraud-serving port-forward svc/fraud-detector-predictor 18181:80` | `/v2/models/fraud-detector/ready` |
| Fraud API | `kubectl -n ml-platform port-forward svc/fraud-api 18100:80` | `/health/ready`, `POST /v1/fraud/predict` |

## 2. Historical runtime state evidence

The N13 command transcripts are retained under
[`runtime/raw/`](../../evidence/edai2/screenshots/runtime/raw). They describe
the r41 state recorded on 2026-11-04 and are not current cluster queries or
screenshots. The former programmatic PNG renderer is deprecated for submission
evidence.

Any future evidence collection must capture the actual terminal or application
window and requires a separately approved runtime campaign. Do not rerun the
historical capture script merely to recreate the old evidence.

## 3. kagent chat (optional, one LLM request)

Open Agents → `ewallet-agents/fraud-coordinator` → Chat, and send an analyst question that contains a transaction ID, account, amount and timestamp (see [Agents §6](../../edai2/agents/AGENTS_REGISTRY_GATEWAYS.md#6-agent-conversations-in-the-kagent-ui)).

Expected result:

- a grounded answer with the KServe probability;
- Feast features;
- 3 citations.

Search for the trace ID in Grafana → Explore → Tempo.

## 4. Observability panels

In Grafana, open these dashboards:

- `ml-platform`
- `platform-overview`
- `logs-operations`
- `distributed-tracing`
- `llm-runtime`
- `agent-mcp`

The PromQL, LogQL and TraceQL used for every number are listed in [OBSERVABILITY.md](../../edai2/observability/OBSERVABILITY.md).

## 5. Close port-forwards by PID

Never use `pkill -f`: it can match and kill the invoking shell.

```bash
for p in $(pgrep -x kubectl); do tr '\0' ' ' </proc/$p/cmdline | grep -q port-forward && kill "$p"; done
pgrep -a kubectl || echo "no kubectl left"
```
