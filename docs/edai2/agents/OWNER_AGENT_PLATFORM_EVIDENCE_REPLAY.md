# Owner evidence replay: Agent Platform on GKE

This runbook is for the owner. You run every demonstration yourself on the real
`ewallet-dev` cluster and capture your own screenshots. Nothing here produces images or
imitates UI output. Each command was run on GKE on 2026-11-15 by campaign
`agi-20261115T145608Z`. Those results are in the
[completion report](AGENT_PLATFORM_COMPLETION.md#3-results-on-gke).

- **Helper:** `scripts/llm_agent/agent_replay.sh`. It is a thin wrapper around the
  campaign's validated tools. It changes no Helm release, Secret, policy or production
  Deployment.
- **Output:** each helper call writes raw output under
  `~/ewallet-evidence/agents/replay/<UTC timestamp>/`. You can choose the folder with
  `REPLAY_DIR`.
- **Screenshots:** save them in `~/Pictures/Screenshots/` with the names given below.
- **Status labels:** the report marks every screenshot `OWNER_CAPTURE_PENDING` until you
  add it.

| Label | What it does |
|---|---|
| `[READ-ONLY]` | Reads state. |
| `[LOAD]` | Sends bounded real traffic through production agents. |
| `[SESSIONS]` | Creates Sandbox session actors. |
| `[EXPERIMENT]` | Creates and deletes the isolated `ewallet-mcp-exp` namespace. |

## 0. Before you start (once)

```bash
cd ~/Develop/ewallet-data-ai-platform && git checkout feature/edai2-agent-platform-integration && git pull --ff-only
source ~/.cache/ewallet-agi/current.env && source "$AGI_ENV"      # CAMPAIGN, EV, CTX, BASELINE, image digests
export PATH=~/miniconda3/envs/fintech/bin:$PATH                    # uv
curl -s https://api.ipify.org; echo                                # your public IP
gcloud container clusters describe ewallet-dev --zone asia-southeast1-b \
  --format="value(masterAuthorizedNetworksConfig.cidrBlocks.cidrBlock.list())"
```

- If your IP is not in the list, `kubectl` times out.
- The campaign removed its temporary `/32` at sleep.
- If you add your IP, keep the existing CIDRs in the list:

```bash
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --enable-master-authorized-networks \
  --master-authorized-networks <existing CIDRs>,<your IP>/32
```

This is Terraform drift; remove it again when you finish.

**Do not modify** during the replay:

- Helm releases `llm-agent` (namespace `default`) and `llm-agent-mcp` (`ewallet-mcp`);
- the `ewallet-mcp` MCP Deployments and their PDBs;
- the `ewallet-sandbox` SandboxAgents, WorkerPool `business-agents` and ActorTemplates;
- the AuthorizationPolicies and NetworkPolicies;
- the Agent Registry data.

Never delete or restart a production MCP pod to make a screenshot. REPLAY-04 uses the
isolated namespace for that.

## 1. Wake the cluster and verify (REPLAY-01) `[READ-ONLY]` after the wake

The recovery manifest records the replicas to restore. The campaign woke from it and
slept back to it.

```bash
M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest "$M"          # read-only: nodes, replicas
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --execute --confirm-cluster ewallet-dev
# The manifest dates from 2026-11-06 and records the MCP servers at 1 replica. Restore the 2 replicas
# that Helm release llm-agent-mcp declares (PKG-P1). This matches the release; it does not change it:
kubectl --context "$CTX" -n ewallet-mcp scale deploy/feature-rag-mcp deploy/drift-mcp --replicas=2
kubectl --context "$CTX" -n ewallet-mcp rollout status deploy/feature-rag-mcp && kubectl --context "$CTX" -n ewallet-mcp rollout status deploy/drift-mcp
```

- This wakes 4 nodes. That is enough for every scenario except REPLAY-04, which needs 5
  (PKG-C, section 4).
- The CPU vLLM needs about 10 minutes before natural-language requests work.
- KEDA brings the agents back to their minimum of 2 by itself.

```bash
scripts/llm_agent/agent_replay.sh pf        # port-forwards: 18083 A2A GW, 18082 MCP GW, 18383 kagent API,
                                            #   18300 kagent UI, 13000 Grafana, 18121 Agent Registry
scripts/llm_agent/agent_replay.sh status    # nodes, Helm, Agents, MCP, Sandbox fleet, KEDA, restarts
```

**PASS when all of these hold.** This is Kubernetes readiness only. It is not business
E2E, which REPLAY-06 to 08 prove.

- 3 Agents `BYO` Ready.
- `feature-rag-mcp` and `drift-mcp` at 2/2 with `RollingUpdate`, `maxUnavailable 0`.
- SandboxAgents `drift-specialist` and `feature-rag-specialist` are Ready and Accepted.
- WorkerPool `business-agents` is 2/2.
- 3 ScaledObjects are Ready, each with HPA min 2 / max 3.
- No `RESTARTED` lines.

**Screenshot `AGENT_01_PLATFORM_STATE.png`:** the `status` output, sections Agents, MCP
and Sandbox.

## 2. Agent Registry (REPLAY-02) `[READ-ONLY]`

- **UI:** open http://127.0.0.1:18121 (Agent Registry, `ewallet-prompts/prompt-registry-agentregistry`).
  Open Agents, Prompts and MCP Servers.
- **Comparison with what is deployed (GET only):**

```bash
scripts/llm_agent/agent_replay.sh registry
kubectl --context "$CTX" -n ewallet-agents get agents.kagent.dev -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,VERSION:.spec.version
```

**Expected on 2026-11-15 (true, not yet fixed):**

- Prompts `fraud-coordinator-system v5`, `feature-rag-specialist-system v5` and
  `drift-specialist-system v2` match.
- MCP servers `feature-rag-mcp v1` and `drift-mcp v1` match. Both point at the MCP
  Gateway routes.
- The registry is **stale for agents**. It holds all three only at `1.0.0` with runtime
  `kagent-sandboxagent`, published on 2026-11-03.
- The deployed agents are BYO `fraud-coordinator 1.1.0`, `feature-rag-specialist 1.0.0`
  and `drift-specialist 1.1.0`. So `readback` prints `FAIL`.
- The corrective publish passed a server-side dry-run. It is a registry write, so it needs
  your approval.

If you approve it, run:

```bash
uv run --locked --project infra/docker/llm-agent-tooling python -m ops.llm_agent.registry_catalog publish --registry http://127.0.0.1:18121 \
  --confirm-live-write http://127.0.0.1:18121 \
  --values-overlay infra/helm/llm-agent-platform/g3/g3.yaml --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml
```

Then run `agent_replay.sh registry` again; it should print `PASS`.

**Screenshots:**

- `AGENT_02_REGISTRY_AGENTS.png`: the UI Agents list.
- `AGENT_02b_REGISTRY_MCP_PROMPTS.png`: MCP servers and prompts.
- `AGENT_02c_REGISTRY_READBACK.png`: the terminal readback.

**What this shows:** it separates *registered* (registry), *deployed* (`kubectl`) and
*working* (REPLAY-06 to 08).

## 3. MCP multi-replica and rolling update (REPLAY-03) `[READ-ONLY]`

```bash
kubectl --context "$CTX" -n ewallet-mcp get deploy,pdb,pods -o wide
kubectl --context "$CTX" -n ewallet-mcp get endpointslices -l kubernetes.io/service-name=feature-rag-mcp
kubectl --context "$CTX" -n ewallet-mcp get endpointslices -l kubernetes.io/service-name=drift-mcp
scripts/llm_agent/agent_replay.sh e2e drift      # a real MCP tool call through the MCP Gateway
```

**PASS when:**

- each server has 2 Ready pods on different nodes;
- PDB `ALLOWED DISRUPTIONS 1`;
- 2 endpoints per server;
- the e2e run shows `"tool_name": "drift_detection" "status": "ok"`.

**Screenshot:** `AGENT_03_MCP_REPLICAS.png`.

**Rolling update:**

- It was proven on GKE as AGR-03: 170/170 typed calls succeeded during a rolling update
  (evidence in `$EV/agr`).
- Repeating it means a `kubectl rollout restart` of a production Deployment. Do that only
  if you decide to: `kubectl -n ewallet-mcp rollout restart deploy/drift-mcp` while
  `agent_replay.sh e2e drift` runs in a loop.
- The safe alternative is REPLAY-04, which kills a pod of the isolated copy.

## 4. MCP failover (REPLAY-04) `[EXPERIMENT]`, needs 5 nodes

The optimized arm needs two nodes without the `ewallet.io/g3-llm` label. Resize the pool
to 5 nodes, using the same step `cloud_lab` uses. Do not use `wake-full` on an awake
cluster: it re-applies the manifest replica counts.

```bash
gcloud container clusters resize ewallet-dev --project ewallet-data-ai-platform-hg26 --location asia-southeast1-b \
  --node-pool cpu-pool --num-nodes=5 --quiet
```

Run two terminals:

- **Terminal 2:** `watch -n1 kubectl --context "$CTX" -n ewallet-mcp-exp get pods -o wide`
- **Terminal 1:** `scripts/llm_agent/agent_replay.sh failover`. It runs baseline, then
  optimized. Each arm takes about 3 minutes.

What happens in each arm:

1. The helper creates `ewallet-mcp-exp`, with its own copy of drift-mcp and its own
   token.
2. A probe sends 10 calls/s for 120 s.
3. At 40 s one pod is force-deleted (`--grace-period=0`), so live connections drop.
4. The namespace is then removed.

The helper compares production `ewallet-mcp` before and after the run.

**Capture these screenshots:**

- `AGENT_04a_FAILOVER_POD_KILLED.png`: Terminal 2 while a pod is Terminating and a new
  one starts.
- `AGENT_04b_FAILOVER_RESULTS.png`: the two summaries.

**Measured 2026-11-15 (run-04):**

- Baseline, 1 replica, no retry: 1129/1189 (94.95 %), outage 12.0 s.
- Optimized, 2 replicas, client retry: 1188/1188 (100 %), with 1 call recovered by the
  client's own retry (`client_retries: 1`).
- **PASS:** the optimized arm has 0 failed calls and retries > 0, the baseline shows
  failures, and the helper prints `production ewallet-mcp unchanged`.
- Retries depend on timing. A run with `client_retries: 0` and 0 failures is still valid
  Service-level failover. Run it again if you want a visible retry.

Return to 4 nodes afterwards with `--num-nodes=4`, or let the final sleep do it.

## 5. Sandbox security (REPLAY-05) `[READ-ONLY]`

```bash
kubectl --context "$CTX" -n ewallet-sandbox get sandboxagent,workerpool,actortemplate -o wide
kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
kubectl --context "$CTX" -n ewallet-sandbox get sa default -o jsonpath='automount={.automountServiceAccountToken}{"\n"}'
kubectl --context "$CTX" auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox
kubectl --context "$CTX" -n ewallet-agents get authorizationpolicy allow-sandbox-workers-to-mcp-gateway \
  allow-sandbox-workers-to-otel-collector -o yaml | grep -A3 principals
scripts/llm_agent/agent_replay.sh negative     # wrong credential 401, ungranted tool hidden, undeclared route 404
```

**PASS when:**

- the ActorTemplates show class `gvisor`;
- `automount=false`;
- RBAC is discovery only;
- the policies admit only `cluster.local/ns/ewallet-sandbox/sa/default` on 8080
  (MCP Gateway) and 4317 (OTLP);
- the three negatives print `PASS`.

For the allowed request, REPLAY-06 shows the MCP server logging
`"actor_id": "sandbox-feature-rag"` for a Sandbox call.

**Screenshots:** `AGENT_05a_SANDBOX_GVISOR_RBAC.png` and `AGENT_05b_SANDBOX_NEGATIVES.png`.

**Limitation:** the egress proxy refuses non-cluster hosts. That is proven by unit tests
(`infra/kubernetes/kagent-pilot/patches/substrate-0.0.9-sandbox-egress-proxy.patch`). The
worker image is distroless, so there is no shell to prove it on GKE.

## 6. Feature-RAG Specialist through the Coordinator (REPLAY-06) `[READ-ONLY]` data

1. **kagent UI:** open http://127.0.0.1:18300 and choose **Skip wizard**.
2. Go to **Agents** → `ewallet-agents/fraud-coordinator` → **Chat**.
3. Paste this prompt. It is the reviewed test fixture: payment `dc58ff4e-…`.

```
Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan.
```

**Expected answer** (CPU LLM, about 60–75 s). These values were verified on 2026-11-15:

- `fraud-detector` v1, `fraud_probability = 0.0639895560144887`, prediction 0.
- Features: `account_tx_count_1h = 0`, `account_tx_count_24h = 0`,
  `merchant_tx_count_24h = 6`, `user_avg_amount_30d = 474960.33`, all at
  `2026-05-31T23:59:56Z`.
- 3 policy citations (`policy-payment-anomaly:0000`, `policy-account-takeover:0000/0001`).

**Independent proof of the path.** Run the same request through the API; the helper then
prints the hops:

```bash
scripts/llm_agent/agent_replay.sh e2e nl-fraud     # or: e2e fraud (typed, no LLM, ~15-30 s)
```

The hops must show:

- `POST …/api/a2a-sandboxes/ewallet-sandbox/feature-rag-specialist/ 200`;
- the A2A Gateway route `default/sandbox-feature-rag-specialist`;
- MCP `"actor_id": "sandbox-feature-rag"`, with `feature_lookup`, `fraud_scoring` and
  `knowledge_search` all `ok`, on one `trace_id`;
- Deployment Specialists `0`, meaning no fallback was used.

**Screenshots:**

- `AGENT_06a_COORDINATOR_UI_FRAUD.png`: the UI answer.
- `AGENT_06b_FRAUD_HOPS.png`: the helper hops.

**Backend comparison:** the direct MCP results from the campaign are in
`$EV/stage2/AGI-04-feature-lookup.json` and `AGI-05-fraud-scoring.json`. They hold the
same values.

## 7. Drift Specialist (REPLAY-07) `[READ-ONLY]` data

Same chat. Prompt:

```
Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.
```

**Expected** (about 35–50 s):

- section "Từ drift-specialist";
- `PSI = 0.0004795543627041439`, `no_drift`, 19338 observations;
- reference and current `feast-r5gb-20261111-214544`;
- thresholds 0.1 / 0.25.

Run `scripts/llm_agent/agent_replay.sh e2e nl-drift`. The hops show the route
`sandbox-drift-specialist` and `"actor_id": "sandbox-drift"`. The independent value is in
`$EV/stage2/AGI-07-drift.json`.

**Screenshots:** `AGENT_07a_COORDINATOR_UI_DRIFT.png` and `AGENT_07b_DRIFT_HOPS.png`.

## 8. Coordinator multi-agent workflow (REPLAY-08)

```bash
scripts/llm_agent/agent_replay.sh e2e combined     # typed fraud + drift, both Sandboxes in parallel
```

**PASS when:**

- `status complete`, `workflow combined`;
- `feature-rag-specialist: status=ok fraud_probability=0.0639…`;
- `drift-specialist: status=ok psi=0.000479…`;
- routes for both Sandbox agents and actor IDs `sandbox-feature-rag` and `sandbox-drift`.

**Screenshot:** `AGENT_08_COMBINED_WORKFLOW.png`.

**Known limitation:** a combined natural-language prompt is rejected with
`ungrounded_argument`. The 0.6B CPU model returns drift arguments that differ from the
text. Strict fail-closed grounding is kept by owner decision. Show it if you want, as
correct rejection behaviour.

## 9. KEDA Agent autoscaling 2 → 3 → 2 (REPLAY-09) `[LOAD]`

This is time-sensitive. Use three terminals and Grafana.

- **Terminal 2**, split in two panes and leave both running:
  - `watch -n2 kubectl --context "$CTX" -n ewallet-agents get hpa`
  - `watch -n2 kubectl --context "$CTX" -n ewallet-agents get pods -l 'ewallet.io/agent in (fraud-coordinator,feature-rag-specialist)'`
- **Terminal 3:** `tail -f <REPLAY_DIR>/keda/timeline.txt`. The helper prints the path.
- **Grafana:** open http://127.0.0.1:13000 and log in. Read the password without printing
  it to the terminal history:
  `kubectl -n ewallet-agents get secret ewallet-grafana-admin -o jsonpath='{.data.password}' | base64 -d | xclip -sel clip`.
  Then go to Explore → datasource `prometheus` → time range *Last 15 minutes*, refresh 5 s.
  There is no dedicated agent-KEDA dashboard. Use these queries:
  - in-flight tasks per agent, the KEDA trigger:
    `sum by (agent) (ewallet_agent_inflight_tasks{agent=~"fraud-coordinator|feature-rag-specialist"})`
  - replicas reporting:
    `count by (agent) (ewallet_agent_inflight_tasks{agent=~"fraud-coordinator|feature-rag-specialist"})`
  - outcomes: `sum by (agent, outcome) (increase(ewallet_agent_invocations_total[1m]))`
- **Terminal 1:** `scripts/llm_agent/agent_replay.sh keda`. It takes about 12 minutes.

| Phase (helper banner) | What you see | Capture |
|---|---|---|
| `BASELINE` | HPA `0/2 (avg)`, `REPLICAS 2`; 2 pods per agent | `AGENT_09a_KEDA_BASELINE.png` |
| `LOAD 1` (100 tasks, concurrency 8, ~45 s) | after ~30–40 s HPA target goes above 2 (e.g. `4500m/2`), desired 3, a third pod `ContainerCreating` → `Running 1/1` | **`AGENT_09b_KEDA_SCALEOUT.png` while the third pod appears**; Grafana in-flight graph `AGENT_09c_GRAFANA_INFLIGHT.png` |
| `LOAD 2 on 3 replicas` | the 3 pods serve; at +120 s the helper prints tasks per pod, the youngest pod > 0 | `AGENT_09d_KEDA_NEW_POD_WORK.png` |
| `SCALE_IN` (~5 min after load) | HPA target `0/2`, after the 300 s window `REPLICAS 2`, the extra pod Terminating; events `New size: 2; reason: … below target` | `AGENT_10_KEDA_SCALEIN.png` |

**Expected with the current deployment (measured in the final regression, 2026-11-15 21:33Z):**

- KEDA scales **the Coordinator** 2 → 3 → 2, and the scale-out pod completes tasks.
- `feature-rag-specialist` stays at 2. Since P4 the Feature-RAG work runs on the Sandbox actors, so the Deployment
  copy is idle.
- About 40 % of the tasks complete. The fixed 2-worker Sandbox pool refuses the overflow:
  `substrate worker pool has no free workers`.
- The fix, `f6ea89b5` (the Coordinator waits for a free worker), is committed but not deployed.

**PASS for the autoscaling claim when:**

- desired and Ready reach 3;
- the new pod completes tasks;
- replicas return to 2.

Report the task success rate as measured. Do not present it as 100 % unless `f6ea89b5` is deployed and the run shows it.
The 100/100 result (`$EV/agr/AGR-06/AGR-06-RESULT.txt`) was measured before P4, with the Deployment Specialists.

The events print `New size: 3 … above target` and `New size: 2 … below target`.

**Not shown here:**

- WorkerPool autoscaling (B′) is out of scope.
- Throughput is capped by the MCP Gateway rate limit (about 2.2–2.5 tasks/s). Extra
  replicas add concurrency headroom, not throughput.

## 10. Agent Fleet: shared Substrate workers (REPLAY-10) `[SESSIONS]`

```bash
watch -n2 'kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide'   # terminal 2
scripts/llm_agent/agent_replay.sh workers                                                                         # terminal 1, ~6 min
```

The helper runs sessions one agent type at a time: 4 Feature-RAG, then 4 Drift, then 4
Feature-RAG. It prints actor → worker assignments with the golden snapshot actors
excluded.

**PASS when:**

- the output ends `verdict: SHARED_WORKERS_OBSERVED`;
- each of the 2 worker pods lists both `feature-rag-specialist` and `drift-specialist`.

On 2026-11-15: feature-rag 4 + drift 2 actors on each worker. Placement varies: a run where
both agents started in parallel ended `NOT_OBSERVED`. Run it again if you see that.

**Screenshot:** `AGENT_11_SHARED_WORKERS.png`, showing the sharing table.

This is a **fixed** 2-worker pool shared by many session actors. It is not WorkerPool
autoscaling.

## 11. Observability (REPLAY-11) `[READ-ONLY]`

First run fresh requests: `agent_replay.sh e2e nl-fraud` and `agent_replay.sh e2e drift`.
In Grafana (time range *Last 30 minutes*):

- **Dashboard "E-Wallet Agent & MCP"** (uid `ewallet-agent-mcp`): agent calls by outcome,
  MCP tool calls per tool and MCP latency p95.
  - The MCP series are named `ewallet_ewallet_mcp_tool_calls_total`. The collector's
    `namespace: ewallet` prefixes a name that already contains `ewallet_`, and the
    dashboard queries that real name.
  - Screenshot: `AGENT_12a_GRAFANA_AGENT_MCP.png`.
- **Dashboard "E-Wallet LLM Runtime"**: tokens, time to first token, end-to-end latency.
  Screenshot: `AGENT_12b_GRAFANA_LLM.png`.
- **Loki** (Explore → `loki`): `{namespace="ewallet-agents"} |= "coordinator.task_completed"`.
  Screenshot: `AGENT_12c_LOKI_AUDIT.png`.
- **Tempo** (Explore → `tempo` → TraceQL):
  `{ resource.service.name = "fraud-coordinator" && name = "g3.coordinator.execute" }`.
  Open the newest trace and expand the waterfall. Screenshot:
  `AGENT_13_TEMPO_TRACE.png`. One trace holds:
  - the A2A Gateway entry and `g3.coordinator.nl_intake.llm_tool_selection`, with
    `gen_ai.usage.*` and `ewallet.intake.offered_tools`;
  - the LLM Gateway call to vLLM;
  - `g3.coordinator.delegate`;
  - the A2A Gateway `POST /api/a2a-sandboxes/…`;
  - the MCP Gateway `tools/call feature-rag_*`;
  - feature-rag-mcp `mcp.tool *`, `feast-online.get_features` and `kserve.infer`.

**Disclose in the report:** the Sandbox Specialist's own spans are missing.

- The actors buffer spans and are suspended before export.
- The fix is in Git (`bb7a2a09`) but is **not deployed**, because it needs an approved
  image roll.
- The trace context still propagates through the Sandbox: the MCP spans sit in the
  Coordinator's trace.

## 12. Final check, cleanup and sleep (REPLAY-12)

```bash
scripts/llm_agent/agent_replay.sh status        # no RESTARTED, HPAs back at 2
kubectl --context "$CTX" get ns ewallet-mcp-exp  # NotFound
kubectl --context "$CTX" -n ewallet-agents get job agent-load   # NotFound (the load Job removes itself)
scripts/llm_agent/agent_replay.sh stop-pf
M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
python3 ops/gcp/cloud_lab.py plan-sleep --manifest "$M"         # read-only: active_work must be empty
```

The MCP and agent PDBs block a plain sleep, so sleep takes the guarded path. It is a
separate owner approval. Write a new campaign id; the approval is per campaign.

```bash
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<your-campaign-id> \
  python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev \
  --scale-pdb-guarded --manifest "$M" --campaign <your-campaign-id> --also-approve ewallet-mcp/feature-rag-mcp
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$M"   # no failures, cpu-pool at 0
```

- `--also-approve ewallet-mcp/feature-rag-mcp` is the PDB that PKG-P1 added after the
  manifest was recorded. `plan-sleep` lists it under `not_covered`.
- Remove any `/32` you added in section 0.
- Metrics, logs and traces live in in-cluster Prometheus, Loki and Tempo. They do not
  survive a sleep, so generate fresh data after each wake (section 11).

## Screenshot checklist

| File | Scenario | Status |
|---|---|---|
| `AGENT_01_PLATFORM_STATE.png` | 1 | OWNER_CAPTURE_PENDING |
| `AGENT_02_REGISTRY_AGENTS.png`, `AGENT_02b_REGISTRY_MCP_PROMPTS.png`, `AGENT_02c_REGISTRY_READBACK.png` | 2 | OWNER_CAPTURE_PENDING |
| `AGENT_03_MCP_REPLICAS.png` | 3 | OWNER_CAPTURE_PENDING |
| `AGENT_04a_FAILOVER_POD_KILLED.png`, `AGENT_04b_FAILOVER_RESULTS.png` | 4 | OWNER_CAPTURE_PENDING |
| `AGENT_05a_SANDBOX_GVISOR_RBAC.png`, `AGENT_05b_SANDBOX_NEGATIVES.png` | 5 | OWNER_CAPTURE_PENDING |
| `AGENT_06a_COORDINATOR_UI_FRAUD.png`, `AGENT_06b_FRAUD_HOPS.png` | 6 | OWNER_CAPTURE_PENDING |
| `AGENT_07a_COORDINATOR_UI_DRIFT.png`, `AGENT_07b_DRIFT_HOPS.png` | 7 | OWNER_CAPTURE_PENDING |
| `AGENT_08_COMBINED_WORKFLOW.png` | 8 | OWNER_CAPTURE_PENDING |
| `AGENT_09a…09d`, `AGENT_10_KEDA_SCALEIN.png` | 9 | OWNER_CAPTURE_PENDING |
| `AGENT_11_SHARED_WORKERS.png` | 10 | OWNER_CAPTURE_PENDING |
| `AGENT_12a…12c`, `AGENT_13_TEMPO_TRACE.png` | 11 | OWNER_CAPTURE_PENDING |
