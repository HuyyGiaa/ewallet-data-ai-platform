# Owner evidence replay: Agent Platform on GKE

This is the single guide for the owner's evidence replay on the real `ewallet-dev` cluster. You run every
demonstration yourself and capture your own screenshots. Nothing in this guide produces images or imitates
terminal output.

- **Runtime it describes:** the state verified by campaign `lc-20261117T002220Z` (2026-11-17): `llm-agent`
  rev 59, `llm-agent-mcp` rev 19, registry 8/8, 5/5 business E2E, 3/3 security negatives, Coordinator and
  WorkerPool KEDA 2→3→2, guarded sleep. Results are in
  [AGENT_PLATFORM_COMPLETION.md §12](AGENT_PLATFORM_COMPLETION.md#12-legacy-mcp-infrastructure-cleanup-lc-20261117t002220z).
- **Guide status:** `OWNER_REPLAY_RUNBOOK_READY` means the guide and the helper are correct offline, against
  the source and mocked tests. It does not mean the replay has been executed. Anything marked
  `LIVE_VERIFICATION_PENDING` has not been observed on the cluster yet.
- **Helper:** `scripts/llm_agent/agent_replay.sh` collects observations. `ops/llm_agent/replay_verdict.py` decides
  PASS/FAIL and sets the exit code. A non-zero exit always means a failed check.
- **Output:** one `REPLAY_DIR` for the whole owner campaign (set in Phase 0). Every helper command writes a
  subdirectory of it, and refuses to run without it.
- **Scope:** this guide follows the approved Master Plan v2.0. The primary autoscaling evidence is a **new** B′ replay
  (Phase 5). Historical runs are listed only as context and never replace a new owner screenshot.
- **Screenshots:** save them in `~/Pictures/Screenshots/` with the names in the
  [checklist](#screenshot-checklist).

## Current architecture

```text
Owner / test client -> A2A Gateway -> fraud-coordinator (BYO Agent, KEDA 2-3)
  -> A2A Gateway /api/a2a-sandboxes/ewallet-sandbox/<agent>/ -> kagent controller
  -> feature-rag-specialist | drift-specialist (SandboxAgents, gVisor) on the shared WorkerPool
     ewallet-sandbox/business-agents (Substrate, KEDA 2-3)
  -> MCP Gateway -> ewallet-mcp/feature-rag-mcp | ewallet-mcp/drift-mcp (2 replicas each, PDB)
  -> Feast online store / KServe fraud-detector / RAG index / drift artifacts
```

| Expected | Value |
|---|---|
| BYO Agent | 1: `ewallet-agents/fraud-coordinator`, 2 replicas at rest |
| SandboxAgents | 2: `feature-rag-specialist` and `drift-specialist` in `ewallet-sandbox` |
| Specialist Deployments | 0. Retired by `sp-cleanup` (rev 55). Their absence is expected, not an error |
| WorkerPool | 1 shared pool: `ewallet-sandbox/business-agents`, 2 workers at rest |
| MCP servers | 2 in `ewallet-mcp`, 2 replicas each, `RollingUpdate maxUnavailable 0`, PDB. The in-release `ewallet-agents/drift-mcp` was removed in rev 59 |
| KEDA ScaledObjects | 2: `ewallet-agents/fraud-coordinator` and `ewallet-sandbox/business-agents-workerpool` |

## Roles and the pending cleanup

The next wake combines five separate responsibilities. Each has its own approval.

| Step | Who | Approval | Where |
|---|---|---|---|
| 1. `lf-agents` cleanup (9 orphan objects of the disabled in-release `feature-rag-mcp`) | Claude | `LF-AGENTS:<campaign>` | runbook [§2.6g](../../runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md#26g-final-orphan-and-auth-table-cleanup-lf-agents-proposed-not-executed) |
| 2. Delete the 2 unused `ewallet-agents` final auth tables | Claude | `AUTH-TABLES:<campaign>` | runbook §2.6g |
| 3. Final GKE regression | Claude | part of the campaign | completion report §13.4 |
| 4. Owner Evidence Replay (this guide) | owner | the cluster wake; `REPLAY_AUTOSCALE_APPROVED` (B′ load); `REPLAY_SESSIONS_APPROVED` (sessions) | Phases 1–6 |
| 5. Guarded sleep | owner or Claude | guarded-sleep and KEDA-pause approvals | Phase 7 |

- **The helper never runs the cleanup.** `agent_replay.sh` changes no Helm release, Secret, policy or Registry
  entry.
- **Replay after the cleanup.** Run Phases 2–6 after steps 1–3 pass. `status` then shows `llm-agent` rev 60
  (`LIVE_VERIFICATION_PENDING`: the expected revision, not yet observed).
- **If the cleanup is blocked**, `llm-agent` stays at rev 59. `status` accepts 59 as "before the lf-agents
  cleanup", and every replay check in this guide still applies: the 9 orphan objects select no pod, and the
  2 unused tables are read by nothing. Only the post-cleanup evidence waits for the next approved attempt:
  rev 60 and the absence of the 9 objects.
- **If `helm --atomic` rolled back**, Helm shows a higher revision (for example 61) whose manifest equals rev 59.
  `status` reports that revision as unexpected. Stop and review it with Claude before replaying.

## Helper commands

| Command | Class | Approval flag | In the default replay | PASS means | Fails when |
|---|---|---|---|---|---|
| `status` | read-only | — | yes | every readiness check PASS; retired workloads absent | wrong Helm revision; Coordinator, gateway, SandboxAgent, WorkerPool or MCP server not ready; MCP not 2/2 with PDB and `maxUnavailable 0`; a ScaledObject not Ready, paused or in fallback; a retired workload present |
| `pf` | local | — | yes | the 6 port-forwards listen and belong to this script | a port held by another process (nothing is killed); a missing Service; a forward that does not start |
| `stop-pf` | local | — | yes | it stops only the recorded forwards whose command line still matches | — (it never kills other processes) |
| `registry` | read-only (GET) | — | yes | strict readback exits 0, verdict PASS, exactly 3 Agents, 2 MCPServers and 3 Prompts, all `matches` | any mismatch, missing entry, non-zero readback, missing or malformed evidence |
| `e2e <case>` | business traffic (1 request) | — | yes | `verdict: COMPLETE` for the case's contract | failed or canceled task, partial result, missing specialist, backend unavailable, invalid or incomplete response, another request's result |
| `negative` | read-only probes | — | yes | 3 denials, each with a working control | any probe or evidence check fails; a retired credential would be used |
| `keda` | read-only | — | yes | both ScaledObjects present, Ready, not paused, min 2; their HPAs and the replica counts read back | a query fails or returns malformed data; a ScaledObject, its HPA or a replica count is missing. It never claims a scale-out |
| `autoscale` | load (100 typed tasks, concurrency 8, ≤ 900 s) | `REPLAY_AUTOSCALE_APPROVED=yes` | **yes: the primary autoscaling replay** | Coordinator 2→3→2 and WorkerPool 2→3→2 observed; the added worker ran Sandbox actors; 100/100 tasks; harness exit 0; latency, throughput and peak resource usage recorded; KEDA healthy after | KEDA unhealthy before (no load sent) or after; an autoscaler not at 2 before the load; scaling or the added worker's actors not observed (`NOT_OBSERVED`); a failed task; missing samples |
| `workers` | Sandbox sessions | `REPLAY_SESSIONS_APPROVED=yes` (separate approval) | yes, in the capture scope | every session turn ok (new and resume); latency per phase, placement and suspend/resume printed | a session turn fails or is not sent; no actor placed |
| `keda demo` | load (6 requests) | `REPLAY_LOAD_APPROVED=yes` | **no**: helper capability only, never a substitute for `autoscale` | 6/6 COMPLETE and scaling observed | any request fails, or scaling is not observed (6 requests may not reach the trigger) |
| `failover` | experiment (`ewallet-mcp-exp`, pod force-deleted there) | `REPLAY_EXPERIMENT_APPROVED=yes` | **no** (separate owner approval) | optimized arm 0 failed calls; production `ewallet-mcp` unchanged | production differs; experiment error |

Credentials are read inside the helper and passed to probes through the environment only. They never appear in
argv, output or evidence. The probes use the active Sandbox credentials and the kagent controller's list-only
credential, never a retired one: the guard refuses the run otherwise.

## Phase 0: preparation (read-only, local)

```bash
cd ~/Develop/ewallet-data-ai-platform
git switch feature/edai2-agent-platform-integration && git pull --ff-only && git log -1 --oneline
export PATH=~/miniconda3/envs/fintech/bin:$PATH                          # uv (the helper's Python environments)
gcloud auth print-access-token > /dev/null && echo "gcloud authenticated"
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26
RC=replay-$(date -u +%Y%m%dT%H%M%SZ); echo "$RC"                        # your campaign id for approvals and the manifest
export REPLAY_DIR=~/ewallet-evidence/agents/replay/$RC                   # ONE evidence directory for the whole campaign
mkdir -p "$REPLAY_DIR" && echo "$REPLAY_DIR"
M=~/ewallet-evidence/agents/legacy-cleanup/lc-20261117T002220Z/final-sleep/recovery-manifest   # wake and verify
P=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z                    # approved PDB record for the guarded sleep only
```

**Replay env file.** The helper needs `CTX`, `EV` (with `inputs/transaction.json`, the reviewed fixture) and
`BASELINE`. It holds no secret:

```bash
mkdir -p ~/.cache/ewallet-agi
cat > ~/.cache/ewallet-agi/replay.env <<EOF
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
EV=$HOME/ewallet-evidence/agents/legacy-cleanup/lc-20261117T002220Z/agi
BASELINE=bl-r5gb-202604.account_tx_count_1h
EOF
export AGI_ENV=~/.cache/ewallet-agi/replay.env && source "$AGI_ENV"   # CTX for the kubectl commands below
```

**Network access.**
- `cloud_lab.py` reaches the cluster through its own kubeconfig on the IAM DNS endpoint, so it needs no IP
  allow-list.
- `kubectl`, `helm` and the helper use the default kubeconfig, which points at the public endpoint. That endpoint
  answers only the authorized CIDRs.

```bash
MYIP=$(curl -s https://api.ipify.org)       # keep it in this shell; do not paste it into notes or docs
ORIG=$(gcloud container clusters describe ewallet-dev --zone asia-southeast1-b \
  --format='value(masterAuthorizedNetworksConfig.cidrBlocks[].cidrBlock)' | tr ';' ',')
umask 077; echo "$ORIG" > ~/.cache/ewallet-agi/$RC-original-cidrs.txt
echo "$ORIG" | tr ',' '\n' | wc -l          # expect 3
echo ",$ORIG," | grep -q ",$MYIP/32," && echo "already authorized" || echo "not authorized: add a temporary /32 after the wake"
```

**Do not modify during the replay:**
- the Helm releases `llm-agent` (namespace `default`) and `llm-agent-mcp` (`ewallet-mcp`);
- the MCP Deployments and their PDBs;
- the SandboxAgents, the WorkerPool and the ActorTemplates;
- the AuthorizationPolicies and NetworkPolicies, the Secrets and the Agent Registry;
- the KEDA ScaledObjects, their thresholds and the Helm values.

The only objects the replay creates are the `autoscale` load Job and its additive access objects in
`ewallet-agents` (label `ewallet.io/experiment-load=true`). The harness deletes them on every exit, including
failure and Ctrl-C.

Never restart or delete a production MCP pod for a screenshot.

## Phase 1: wake and platform readiness

**Read-only plan first:**

```bash
python3 ops/gcp/cloud_lab.py status                                      # pool size (expect 0 VMs)
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest "$M"       # nodes and replicas to restore
python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --nodes 5         # dry run: prints PLAN lines only
```

**Wake (cost starts here; about $0.8/h for 5 nodes at list price):**

```bash
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --nodes 5 --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect awake --manifest "$M"
```

- `wake-full` resizes `cpu-pool` to 5 nodes, labels the LLM nodes, restores the manifest's 75 workloads, and
  unpauses the ScaledObjects the last guarded sleep paused (`keda_resumed`). The manifest already records both
  MCP servers at 2 replicas; no manual scaling is needed.
- `verify --expect awake` fails while a ScaledObject is still paused or a workload is below its manifest replicas.
- **Temporary /32** (cloud mutation; only if Phase 0 said "not authorized"):
  ```bash
  gcloud container clusters update ewallet-dev --zone asia-southeast1-b --enable-master-authorized-networks \
    --master-authorized-networks "$ORIG,$MYIP/32"
  ```
- **Wait for readiness.** The Coordinator and the gateways need about 2–5 minutes. The CPU vLLM needs about
  10 minutes before natural-language requests work.
  ```bash
  kubectl --context "$CTX" -n ewallet-agents rollout status deploy/fraud-coordinator --timeout=10m
  kubectl --context "$CTX" -n ewallet-mcp rollout status deploy/feature-rag-mcp && kubectl --context "$CTX" -n ewallet-mcp rollout status deploy/drift-mcp
  ```

```bash
scripts/llm_agent/agent_replay.sh pf                # 18083 A2A GW, 18082 MCP GW, 18383 kagent API, 18300 kagent UI,
                                                    # 13000 Grafana, 18121 Agent Registry; exits 1 on any FAIL line
scripts/llm_agent/agent_replay.sh status            # tables + checks; exit 0 only when every check passes
```

**PASS:**
- `verdict: PASS`;
- `Helm llm-agent` shows rev 59 before the cleanup, or rev 60 after it;
- `kagent Agents: only the BYO Coordinator`;
- both SandboxAgents and the WorkerPool PASS;
- both MCP servers `ready 2/2, RollingUpdate maxUnavailable=0, PDB=yes`;
- both ScaledObjects `Ready=True Paused=False`;
- the 6 `retired … absent (expected)` lines.

`container restarts` is reported as `INFO`/`WARN`. A restart during the wake is normal; one during the replay
needs a look.

**Screenshots:**
- `AGENT_A01_GKE_WORKLOADS.png`: Google Cloud Console → Kubernetes Engine → **Workloads**, cluster `ewallet-dev`,
  namespaces `ewallet-agents`, `ewallet-sandbox` and `ewallet-mcp`. Show `fraud-coordinator` 2/2, the three gateways,
  the WorkerPool's worker Deployment in `ewallet-sandbox`, and `feature-rag-mcp` and `drift-mcp` 2/2, all OK.
- `AGENT_A02_PLATFORM_STATUS.png`: the `status` checks block (CRDs and assertions the Console cannot show).
- Conditional `AGENT_A03_KAGENT_AGENTS.png`: the kagent UI agent list (http://127.0.0.1:18300). Take it only if the UI
  lists the SandboxAgents next to `fraud-coordinator` (`LIVE_VERIFICATION_PENDING`); otherwise `AGENT_A02` covers them.

## Phase 2: Agent Registry

1. **UI:** open http://127.0.0.1:18121 (`ewallet-prompts/prompt-registry-agentregistry`), with the Agents, MCP
   Servers and Prompts pages.
2. **Strict readback** (GET only; it renders the catalog from the live Helm values and the live SandboxAgent image,
   then compares it entry by entry):

```bash
scripts/llm_agent/agent_replay.sh registry
```

**PASS:** `verdict: PASS`, with 11 PASS lines:
- Agents `fraud-coordinator 1.1.0`, `feature-rag-specialist 1.0.0`, `drift-specialist 1.1.0`;
- MCPServers `feature-rag-mcp v1`, `drift-mcp v1`;
- Prompts `fraud-coordinator-system v5`, `feature-rag-specialist-system v5`, `drift-specialist-system v2`.

In the registry the Specialists are described as SandboxAgent runtimes (published 2026-11-16, §11.1). Older
`1.0.0` entries remain as history.

**If it fails, do not publish anything from this guide.** A mismatch means the registry and the runtime differ.
Record the output and raise it with Claude; a registry write is a separate approval.

**Screenshots:** `AGENT_A04_REGISTRY_AGENTS.png` (UI Agents, one entry opened to show version and runtime labels),
`AGENT_A05_REGISTRY_MCP_PROMPTS.png` (UI MCP servers and prompts), `AGENT_A06_REGISTRY_READBACK.png` (terminal verdict).

## Phase 3: business workflows (Coordinator → SandboxAgent → MCP)

Each `e2e` call sends one request with a unique id (`replay-<case>-<UTC>-<pid>-<random>`), saves the request,
the raw response, the timing and the verdict, then prints the hops from the logs.

```bash
scripts/llm_agent/agent_replay.sh e2e fraud        # typed, no LLM, ~15-50 s
scripts/llm_agent/agent_replay.sh e2e drift        # typed, ~40 s
scripts/llm_agent/agent_replay.sh e2e combined     # typed fraud + drift, both SandboxAgents in parallel
scripts/llm_agent/agent_replay.sh e2e nl-fraud     # natural language through the LLM Gateway, ~60-100 s
scripts/llm_agent/agent_replay.sh e2e nl-drift     # natural language, ~50 s
```

| Case | Verdict line must show | Hops must show |
|---|---|---|
| `fraud` / `nl-fraud` | `COMPLETE`, `g3-orchestration/v1`, `status ok`, `unavailable []`, `fraud_probability 0.0639895560144887` | `POST …/api/a2a-sandboxes/ewallet-sandbox/feature-rag-specialist/ 200`; route `default/sandbox-feature-rag-specialist`; MCP `"actor_id": "sandbox-feature-rag"` with `feature_lookup`, `fraud_scoring`, `knowledge_search` all `ok` on one `trace_id` |
| `drift` / `nl-drift` | `COMPLETE`, `g3-orchestration/v2`, `status complete`, `workflow drift`, `drift-specialist ok`, `psi 0.0004795543627041439` | route `default/sandbox-drift-specialist`; MCP `"actor_id": "sandbox-drift"`, `drift_detection ok` |
| `combined` | `COMPLETE`, `status complete`, `workflow combined`, `unavailable []`, both specialists `ok`, `fraud_probability` and `psi` as above | routes and actor ids for both SandboxAgents |

These values match the 2026-11-17 regression. They come from pinned feature, model and drift revisions, so a
different value means the data path changed. Do not treat that as a pass.

Every hop block ends with `retired Specialist Deployments (expected 0…): 0`.

**kagent UI (natural language).** Open http://127.0.0.1:18300, choose **Skip wizard**, then **Agents** →
`ewallet-agents/fraud-coordinator` → **Chat**, and paste:

```
Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan.
```

**Expected:**
- `fraud-detector` v1 with `fraud_probability = 0.0639895560144887`;
- features `account_tx_count_1h = 0`, `account_tx_count_24h = 0`, `merchant_tx_count_24h = 6`,
  `user_avg_amount_30d = 474960.33`;
- policy citations.

The drift prompt is:

```
Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.
```

Its expected answer is the section "Từ drift-specialist", `PSI = 0.0004795…`, `no_drift`, 19338 observations.

**Known limitation:** a combined natural-language prompt is rejected with `ungrounded_argument`. This is the
strict fail-closed grounding kept by owner decision. Only the typed `combined` case is required.

**Screenshots:**
- `AGENT_B01_KAGENT_CHAT_FRAUD.png` (UI answer);
- `AGENT_B02_FRAUD_HOPS.png` (`e2e fraud` or `nl-fraud`: verdict and hops);
- `AGENT_B03_DRIFT_HOPS.png` (`e2e drift`);
- `AGENT_B04_COMBINED_WORKFLOW.png` (`e2e combined`);
- conditional `AGENT_B05_KAGENT_CHAT_DRIFT.png`.

## Phase 4: security

```bash
kubectl --context "$CTX" -n ewallet-sandbox get sandboxagents.kagent.dev,workerpools.ate.dev,actortemplates.ate.dev -o wide
kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
kubectl --context "$CTX" -n ewallet-sandbox get sa default -o jsonpath='automount={.automountServiceAccountToken}{"\n"}'
kubectl --context "$CTX" auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox
kubectl --context "$CTX" -n ewallet-agents get authorizationpolicy allow-sandbox-workers-to-mcp-gateway \
  allow-sandbox-workers-to-otel-collector -o yaml | grep -A3 principals
kubectl --context "$CTX" -n ewallet-mcp get networkpolicy,authorizationpolicy
scripts/llm_agent/agent_replay.sh negative
```

**PASS:**
- the ActorTemplates show the `gvisor` class;
- `automount=false`;
- RBAC is discovery only;
- the sandbox policies admit only `cluster.local/ns/ewallet-sandbox/sa/default` on 8080 (MCP Gateway) and 4317
  (OTLP);
- `negative` ends `verdict: PASS` with 3 PASS lines:
  - `wrong-server-credential.json: expected unauthenticated`: the `sandbox-feature-rag` credential gets 401 from
    drift-mcp; the control `sandbox-drift` is listed;
  - `ungranted-tool.json: expected call-denied`: the kagent controller's list-only credential cannot call
    `drift_detection`; the control is listed;
  - `a2a-unrouted.json: expected no-route`: an undeclared A2A host returns 404; the control route returns 200.

**Limitations:**
- Laptop port-forwards bypass the Ambient L4 caller check. The negatives therefore prove gateway routes and server
  credentials and grants. The SPIFFE caller policies are the policy objects above.
- The Sandbox egress proxy's refusal of non-cluster hosts is proven by unit tests, not on GKE. The worker image is
  distroless.

**Screenshots:** `AGENT_C01_SANDBOX_GVISOR_RBAC.png` (the first four commands), `AGENT_C02_GATEWAY_POLICIES.png` (the
principals and the `ewallet-mcp` policies), `AGENT_C03_SECURITY_NEGATIVES.png` (`negative`).

## Phase 5: reliability and autoscaling

**MCP replicas and rollout safety (read-only):**

```bash
kubectl --context "$CTX" -n ewallet-mcp get deploy,pdb,pods -o wide
kubectl --context "$CTX" -n ewallet-mcp get endpointslices -l kubernetes.io/service-name=feature-rag-mcp
kubectl --context "$CTX" -n ewallet-mcp get endpointslices -l kubernetes.io/service-name=drift-mcp
```

**PASS:** each server has 2 Ready pods on different nodes, PDB `ALLOWED DISRUPTIONS 1`, and 2 endpoints. The
Phase 3 hops already show real tool calls on these pods.

**Screenshot:** `AGENT_G01_MCP_REPLICAS_PDB.png`.

**KEDA, read-only (required):**

```bash
scripts/llm_agent/agent_replay.sh keda
```

**PASS:**
- both ScaledObjects are `READY True`, `PAUSED` empty, with min 2 / max 3;
- the HPAs are at 2;
- `coord=2 pool=2` at rest.

This proves configuration and current state, not a scale-out.

`keda` exits 1 when a required observation fails: a missing ScaledObject, a failed or malformed query, a missing HPA
or replica count. Fix or report that before any load; `autoscale` (and the `keda demo` helper) refuse to send load when it fails.

**Screenshot:** `AGENT_D01_KEDA_BASELINE.png`.

**Primary autoscaling replay (`[LOAD]`, B′ profile, required):** 100 typed business tasks, concurrency 8, at most
900 s, sent to `fraud-coordinator` and delegated to the SandboxAgents on the shared WorkerPool. Approval:
`REPLAY_AUTOSCALE_APPROVED=yes`. It takes about 15 minutes of load plus 5–15 minutes of scale-in.

Before you start, check that `keda` passed and that `pf` is running (the actor sampler uses the kagent API on 18383).
Open three windows so you can capture the screenshots while the load runs.

```bash
REPLAY_AUTOSCALE_APPROVED=yes scripts/llm_agent/agent_replay.sh autoscale
# second terminal (D03/D04):
watch -n2 'kubectl --context "$CTX" -n ewallet-agents get hpa,deploy/fraud-coordinator; kubectl --context "$CTX" -n ewallet-sandbox get hpa,workerpools.ate.dev; kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide'
# third terminal (D07), during the load:
kubectl --context "$CTX" -n ewallet-sandbox top pods -l ate.dev/worker-pool=business-agents; kubectl --context "$CTX" -n ewallet-agents top pods -l ewallet.io/agent=fraud-coordinator
```

What `autoscale` does, in order:
1. It runs `keda` into `$REPLAY_DIR/autoscale/before`. If any KEDA observation fails, it stops and sends nothing.
2. It requires both autoscalers at 2 (`coord=2 pool=2`), otherwise it stops and sends nothing.
3. It starts the samplers:
   - the replica timeline of both autoscalers and the worker pods with their created → Ready times, every 5 s;
   - Substrate actors per worker, from the kagent status API, every 10 s;
   - `kubectl top` every 30 s.
4. It runs the corrected B′ load harness `scripts/experiments/agent_experiment.sh exp-c5-keda optimized run`, with
   concurrency 8, 100 tasks and 900 s.
   - The harness refuses to run unless the Coordinator has its ScaledObject.
   - It reads the Specialist image from the SandboxAgent, never from the retired `feature-rag-specialist`
     Deployment.
   - It deletes its load objects on every exit.
5. It waits at least 300 s, and at most 900 s, for both autoscalers to return to 2.
6. It runs `keda` again into `autoscale/after`.
7. It writes the verdict to `autoscale/verdict.json`.

You run this once per `REPLAY_DIR`: a second call refuses, because `autoscale/` already exists.

**PASS (`verdict: PASS`):**
- `Coordinator … 2 -> 3 -> 2` PASS and `WorkerPool … 2 -> 3 -> 2` PASS, each from observed replica counts;
- `the added worker executed Sandbox actors` PASS, naming the third worker pod and its Running actors;
- `workload results` `100/100 tasks completed`, and `load harness exit code 0`;
- the INFO lines for task latency (p50, p95, mean), throughput, the added worker's created → Ready time, and peak
  CPU and memory per Coordinator and worker pod.

A `NOT_OBSERVED` line makes the run exit 1 and no scale claim is made. Save the output and review it with Claude.
Do not loop the load, and do not use historical results instead.

**Screenshots** (all from this run):
- `AGENT_D02_GRAFANA_KEDA_SIGNALS.png`: Grafana → Explore → `prometheus`, the run's time range, both trigger queries
  on one graph:
  - `sum(ewallet_sandbox_delegations_inflight)` (the WorkerPool trigger);
  - `sum(ewallet_agent_inflight_tasks{agent="fraud-coordinator"})` (the Coordinator trigger).
- `AGENT_D03_COORDINATOR_SCALEOUT.png`: the `watch` terminal, or Console → Workloads, with `fraud-coordinator` at 3.
- `AGENT_D04_WORKERPOOL_SCALEOUT.png`: the `watch` terminal with the WorkerPool HPA and `business-agents` at 3, and the
  third worker pod Running.
- `AGENT_D05_THIRD_WORKER_ACTORS.png`: during the load, the Substrate status page (below), with session actors whose
  `ateomPodName` is the third worker.
- `AGENT_D06_AUTOSCALE_VERDICT.png`: the `autoscale` verdict: both 2→3→2, the added worker's actors, 100/100, latency
  and throughput.
- `AGENT_D07_RESOURCE_USAGE.png`: the third terminal (`kubectl top` with three workers and three Coordinator pods), or
  Console → Workloads → pod CPU/memory charts, during the load.

**Helper only: `keda demo`.** It sends 6 requests and is not part of this sequence. It may not reach the trigger
threshold, and it never replaces `autoscale`.

**Shared WorkerPool sessions (`[SESSIONS]`, in the capture scope, separate approval `REPLAY_SESSIONS_APPROVED`):**
Run it after `autoscale` has returned to 2, so the two loads do not overlap.

```bash
REPLAY_SESSIONS_APPROVED=yes scripts/llm_agent/agent_replay.sh workers    # ~6 min
```

The command runs 4 Feature-RAG sessions, then 4 Drift sessions, then 4 Feature-RAG sessions. It then prints
session outcomes and actor → worker placement (golden snapshot actors excluded).
- **PASS:** every session turn is ok.
- **Placement:** the line "one worker hosted actors of both Specialists" is `PASS` when observed. Placement varies
  with timing, so `NOT_OBSERVED` there is not a failure.
- **Scope:** this shows sharing of one pool by many session actors. It does not show WorkerPool autoscaling, which
  `autoscale` shows.

**Screenshot:** `AGENT_E01_FLEET_SESSIONS.png` (the verdict block). It needs the sessions approval; without it, record
`E01` as not captured, and do not replace it with an older image.

**Substrate status (read-only, required):** open
http://127.0.0.1:18383/api/substrate/status?namespace=ewallet-sandbox in the browser, after the Phase 3 requests (and
during `autoscale` for `D05`). Refresh it by hand, not automatically: every request makes `ateapi` log a full actor
listing. It
lists the WorkerPool, its worker pods, both ActorTemplates with their golden actors, and the session actors with
`status` and `ateomPodName`: which worker each actor ran on.

**Screenshot:** `AGENT_E02_SUBSTRATE_STATUS.png`. It shows the pool, the workers and actors of both agents.

### Fleet measurements

These turn the fleet claims into numbers. They are bounded and reuse existing tools.

| Measurement | Source | Result to record | Notes |
|---|---|---|---|
| Both autoscalers 2→3→2 | `autoscale` timeline | scale-out and scale-in from observed replicas | primary evidence; Ready=True is never a scale result |
| Added worker executing actors | `autoscale` actor samples, Substrate page | third worker pod and its Running actors | `D05` |
| Workload results and latency | `autoscale` load summary | tasks completed, p50/p95/mean, task/s | end to end through the Coordinator |
| New worker startup | `autoscale` worker-pod samples | created → Ready seconds of the added worker | |
| Resource usage | `autoscale` `kubectl top` samples, `D07` | peak CPU and memory per Coordinator and worker pod | compare with worker requests 500m / 1Gi and limits 1500m / 2Gi |
| Session sharing, new vs resumed, suspend/resume | `workers` | actors per worker per agent; median/p95 per phase | end to end through kagent (activation + turn) |
| Cost | the measured usage with the `cloud_lab` prices | node, warm and scale-out cost | every figure labelled an estimate |

These numbers come only from this campaign's run. The historical B′ figures in the reference section are context,
not a substitute.

**MCP failover (optional experiment, separate approval):**
- Context only (not a substitute for `G02`): 2026-11-15 run-04. The baseline with 1 replica and no retry was 1129/1189 (94.95 %). The
  optimized arm with 2 replicas and client retry was 1188/1188 (100 %), with 1 client retry.
- `REPLAY_EXPERIMENT_APPROVED=yes scripts/llm_agent/agent_replay.sh failover` creates `ewallet-mcp-exp`,
  force-deletes a pod there and removes the namespace. It never touches `ewallet-mcp`, and it exits 1 if
  production differs.
- Do not run it in the default replay. Five nodes are already enough for its optimized arm.
- Conditional screenshot, only if approved and run: `AGENT_G02_FAILOVER.png` (the two arm summaries and
  `production ewallet-mcp unchanged`).

## Phase 6: observability

First run fresh requests, for example `e2e nl-fraud` and `e2e combined`: in-cluster metrics, logs and traces do not
survive a sleep. Open Grafana at http://127.0.0.1:13000 and copy the admin password to the clipboard without
printing it:

```bash
kubectl --context "$CTX" -n ewallet-agents get secret ewallet-grafana-admin -o jsonpath='{.data.password}' | base64 -d | xclip -sel clip
```

| View | What to open | Must show | Screenshot |
|---|---|---|---|
| Agent and MCP metrics | dashboard **E-Wallet Agent & MCP** (`ewallet-agent-mcp`), last 30 min | agent calls by outcome, MCP tool calls per tool (`ewallet_ewallet_mcp_tool_calls_total`), MCP latency p95 | `AGENT_F01_GRAFANA_AGENT_MCP.png` |
| LLM | dashboard **E-Wallet LLM Runtime** | tokens, time to first token, end-to-end latency for the NL request | `AGENT_F02_GRAFANA_LLM.png` |
| Logs | Explore → `loki`: `{namespace="ewallet-agents"} \|= "coordinator.task_completed"` | your request ids and their status | `AGENT_F04_LOKI_LOGS.png` |
| Trace | Explore → `tempo`: search the `trace_id` printed in the `e2e combined` hops, or TraceQL `{ resource.service.name = "fraud-coordinator" && name = "g3.coordinator.execute" }` | one trace with the services below | `AGENT_F03_TEMPO_TRACE.png` |

A `combined` trace holds the services `agentgateway`, `kagent-controller`, `fraud-coordinator`,
`feature-rag-specialist`, `drift-specialist`, `feature-rag-mcp` and `drift-mcp`. On 2026-11-17 one had 228 spans,
21 of them from the kagent controller.

In the waterfall:
- the A2A Gateway entry, then `g3.coordinator.delegate`;
- the A2A Gateway `POST /api/a2a-sandboxes/…`;
- the kagent controller hop;
- the SandboxAgent's `g3.feature_rag_specialist.execute`, with `MCP send initialize`, `tools/list` and
  `tools/call …`;
- the MCP Gateway `tools/call`;
- the server spans `mcp.tool *`, `feast-online.get_features` and `kserve.infer`.

An NL trace also holds `g3.coordinator.nl_intake.llm_tool_selection`, with `gen_ai.usage.*`, and the LLM Gateway
call to vLLM.

**CI (conditional):** if you run Jenkins job `edai2-agent-ci` during the wake, capture the build page with the
`mcp-agents`, `agents` and `prompt-management` stages green as `AGENT_H01_JENKINS_AGENT_CI.png`. The CD jobs for
agents and MCP servers have never been executed, and this replay does not run them (rows 34–35 stay open for CD).

## Phase 7: final verification and sleep

**1. Stop traffic and local tools:**

```bash
scripts/llm_agent/agent_replay.sh status                                  # verdict PASS, KEDA back at 2
kubectl --context "$CTX" get ns ewallet-mcp-exp                           # NotFound
scripts/llm_agent/agent_replay.sh stop-pf
pgrep -af "port-forward" || echo "no port-forwards left"                 # stop any others you started by hand
```

**2. Preserve evidence** (raw output, no secret values):

```bash
(cd "$REPLAY_DIR" && find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.txt)
```

**3. Record a fresh recovery manifest** (reads the cluster and writes local files only). The next wake uses this
manifest:

```bash
python3 ops/gcp/cloud_lab.py record-manifest --out "$REPLAY_DIR/recovery-manifest" \
  --campaign "$RC" --source-commit "$(git rev-parse HEAD)"
```

**4. Plan the sleep (read-only).**
- The guarded sleep's `--manifest` is the approved PDB record `$P`, which has `pdb-after-scale.txt`. It is not
  the recovery manifest.

```bash
python3 ops/gcp/cloud_lab.py plan-sleep --manifest "$P"
```

- Expect `active_work: []`.
- `pdb_blockers` lists the MCP and Coordinator PDBs; `guarded_preparation` splits them into `covered_by_manifest`
  and `not_covered`.
- Pass `--also-approve <ns/name>` only for a PDB that this plan lists under `not_covered`, after checking
  that it is a current platform PDB. If `not_covered` is empty, pass none. On 2026-11-17 the list held
  `ewallet-mcp/feature-rag-mcp`; do not copy that unless your plan shows it.
- The WorkerPool and the Coordinator ScaledObjects need `--pause-keda`.

**5. Dry run, then the guarded sleep.** Both approvals are scoped to your campaign `$RC`; never reuse an older
campaign's tokens.

```bash
ALSO=()      # one pair per PDB under not_covered in YOUR plan-sleep output: ALSO+=(--also-approve <ns/name>); empty if none
python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest "$P" --campaign "$RC" "${ALSO[@]}"   # dry run
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$RC CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:$RC \
  python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --pause-keda --manifest "$P" --campaign "$RC" "${ALSO[@]}" \
  --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$REPLAY_DIR/recovery-manifest"
```

Two manifests, never swapped: `$P` (the approved PDB record) is for the guarded sleep only, and
`$REPLAY_DIR/recovery-manifest` (recorded in step 3) is for `verify` and the next wake.

What the guarded sleep does, in order:
1. It pauses `ewallet-sandbox/business-agents-workerpool` at 2 and `ewallet-agents/fraud-coordinator` at 0, each
   with a cloud-lab marker.
2. It scales the PDB-guarded Deployments to 0 and waits for their PDBs to release.
3. It resizes `cpu-pool` to 0.

If the preparation fails, it restores the pauses and the replicas. The next `wake-full` unpauses only what it
paused.

**The sleep is done only when `verify --expect sleep` passes:** no failures, `cpu-pool` at 0.

**If the guard refuses** (`REFUSED: SLEEP_BLOCKED …`, exit 2):
- Do not force the sleep, do not scale or annotate anything by hand, and do not drop `--scale-pdb-guarded` or
  `--pause-keda`.
- Save the output. The cluster stays awake and keeps costing money.
- Fix the reason it names, then rerun the dry run:
  - **active work:** wait for it to finish;
  - **a PDB not in the approved manifest:** add it to `ALSO` only if `plan-sleep` lists it under `not_covered`
    and it is a known platform PDB;
  - **missing approval:** set the campaign-scoped variable.
- If you cannot resolve it, tell Claude and keep the cluster awake. A failed preparation has already restored
  KEDA and the replicas; `verify --expect awake --manifest "$M"` confirms it.

**6. Remove the temporary /32 and confirm the original CIDRs:**

```bash
ORIG=$(cat ~/.cache/ewallet-agi/$RC-original-cidrs.txt)
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --enable-master-authorized-networks \
  --master-authorized-networks "$ORIG"
NOW=$(gcloud container clusters describe ewallet-dev --zone asia-southeast1-b \
  --format='value(masterAuthorizedNetworksConfig.cidrBlocks[].cidrBlock)' | tr ';' ',')
[ "$NOW" = "$ORIG" ] && echo "restored: exactly the original $(echo "$NOW" | tr ',' '\n' | wc -l) CIDRs" || echo "MISMATCH: fix before leaving"
python3 ops/gcp/cloud_lab.py status                                       # pool_vms: 0
```

## Screenshot checklist

Capture each item yourself during this campaign; none is complete until you add it. Rows refer to the EDAI-2 rubric
(the sheet is the authority). Replacing the seven Agent documents with the final reports waits until this capture is
finished.
- **Class:** **M** mandatory (`E01` needs its separate sessions approval); **C** conditional (only with the named
  optional step). There is no historical class: an older image or result never stands in for a screenshot here.
- **Prefer the UI** (Console, kagent, Registry, Grafana, Tempo, Loki, the Substrate status page). Use terminal output
  where only an assertion or a CRD shows the fact.

| File | Class | Rows | Where / action | Expected | Proves | Does not prove |
|---|---|---|---|---|---|---|
| **A. Platform and governance** |  |  |  |  |  |  |
| `AGENT_A01_GKE_WORKLOADS.png` | M | 16, 19, 25 | Console → Kubernetes Engine → Workloads, 3 namespaces | Coordinator 2/2, gateways, worker Deployment, both MCP 2/2, OK | the platform runs on GKE with replicas | that the Specialists are SandboxAgents (A02) |
| `AGENT_A02_PLATFORM_STATUS.png` | M | 16, 20, 22 | `agent_replay.sh status` | `verdict: PASS`, revisions, 1 BYO Agent, 2 SandboxAgents, WorkerPool, 2 ScaledObjects, retired absent | the final architecture, checked | business execution (B) |
| `AGENT_A03_KAGENT_AGENTS.png` | C | 40, 20 | kagent UI → Agents | Coordinator and, if listed, the SandboxAgents | the agent UI works (port-forward) | the NGINX/edge requirement (not deployed) |
| `AGENT_A04_REGISTRY_AGENTS.png` | M | 8, 21, 26, 41 | Registry UI → Agents, open `fraud-coordinator` | 3 agents, versions, runtime labels, image digest | agents published | that the entries equal the runtime (A06) |
| `AGENT_A05_REGISTRY_MCP_PROMPTS.png` | M | 8, 21 | Registry UI → MCP Servers, Prompts | 2 MCP servers, 3 prompts with versions | tools and prompts published | — |
| `AGENT_A06_REGISTRY_READBACK.png` | M | 8, 21, 26 | `agent_replay.sh registry` | 11 PASS lines, `verdict: PASS` | registered equals deployed | registry-driven discovery (not used) |
| **B. Business integration** |  |  |  |  |  |  |
| `AGENT_B01_KAGENT_CHAT_FRAUD.png` | M | 22, 25, 40 | kagent UI → `fraud-coordinator` → Chat, fraud prompt | probability 0.0639895560144887, features, citations | NL fraud through the UI with Feast, KServe, RAG | which hops it took (B02, F03) |
| `AGENT_B02_FRAUD_HOPS.png` | M | 20, 22, 23, 24 | `agent_replay.sh e2e fraud` | `COMPLETE`, Sandbox route, `actor_id sandbox-feature-rag`, `feature_lookup`, `fraud_scoring`, `knowledge_search` ok | Coordinator → SandboxAgent → MCP Gateway → backends | autoscaling |
| `AGENT_B03_DRIFT_HOPS.png` | M | 18, 19, 20 | `agent_replay.sh e2e drift` | `COMPLETE`, `psi 0.00047955…`, `actor_id sandbox-drift` | drift through the Sandbox and the MCP tool | — |
| `AGENT_B04_COMBINED_WORKFLOW.png` | M | 25 | `agent_replay.sh e2e combined` | `COMPLETE`, `workflow combined`, both ok, `unavailable []` | multi-agent orchestration | NL combined (rejected by design) |
| `AGENT_B05_KAGENT_CHAT_DRIFT.png` | C | 22 | kagent UI chat, drift prompt | "Từ drift-specialist", PSI | NL drift through the UI | — |
| **C. Security** |  |  |  |  |  |  |
| `AGENT_C01_SANDBOX_GVISOR_RBAC.png` | M | 20 | Phase 4 commands 1–4 | `gvisor`, `automount=false`, discovery-only RBAC | Sandbox isolation and identity | egress refusal (unit-tested) |
| `AGENT_C02_GATEWAY_POLICIES.png` | M | 23, 24 | Phase 4 commands 5–6 | principals `sa/default` on 8080/4317; `ewallet-mcp` policies | policy objects behind both gateways | that the policies deny (C03) |
| `AGENT_C03_SECURITY_NEGATIVES.png` | M | 23, 24 | `agent_replay.sh negative` | 3 PASS lines with controls | credentials, grants and routes deny as designed | the Ambient caller check (port-forward bypasses it) |
| **D. Dual KEDA** |  |  |  |  |  |  |
| `AGENT_D01_KEDA_BASELINE.png` | M | 19, 25 | `agent_replay.sh keda` | both ScaledObjects Ready, min 2/max 3, HPAs at 2, `verdict: PASS` | both autoscalers configured and healthy | any scale-out |
| `AGENT_D02_GRAFANA_KEDA_SIGNALS.png` | M | 19, 25, 51 | Grafana Explore during `autoscale` | both trigger metrics rise and fall | the demand signals KEDA reads | replica changes (D03, D04) |
| `AGENT_D03_COORDINATOR_SCALEOUT.png` | M | 19, 25 | `watch` terminal or Console Workloads during `autoscale` | `fraud-coordinator` at 3 | the Coordinator autoscaler scaled out | scale-in (D06) |
| `AGENT_D04_WORKERPOOL_SCALEOUT.png` | M | 19, 25, 27 | `watch` terminal during `autoscale` | WorkerPool HPA and `business-agents` at 3, third worker Running | the WorkerPool autoscaler scaled out | that the new worker ran actors (D05) |
| `AGENT_D05_THIRD_WORKER_ACTORS.png` | M | 27, 20 | Substrate status page during `autoscale` | session actors on the third worker pod | the added worker executed Sandbox actors | throughput gain |
| `AGENT_D06_AUTOSCALE_VERDICT.png` | M | 19, 25, 27 | end of `autoscale` | both 2→3→2 PASS, added worker's actors, 100/100, p50/p95, task/s | both autoscalers react and return under the B′ workload | a comparison with the fixed pool (not rerun) |
| `AGENT_D07_RESOURCE_USAGE.png` | M | 19, 27 | `kubectl top` or Console pod charts during `autoscale` | CPU and memory of 3 workers and 3 Coordinator pods | resource usage under load | cost (an estimate from these numbers) |
| **E. Agent fleet** |  |  |  |  |  |  |
| `AGENT_E01_FLEET_SESSIONS.png` | M | 27 | `agent_replay.sh workers` | turns ok, new/resume latency, placement, suspend/resume | sessions of both agents share one pool | that every worker hosts both agents |
| `AGENT_E02_SUBSTRATE_STATUS.png` | M | 27, 20 | browser: Substrate status page | pool, workers, both templates, actors with worker pods | actor placement on the shared WorkerPool | autoscaling |
| **F. Observability** |  |  |  |  |  |  |
| `AGENT_F01_GRAFANA_AGENT_MCP.png` | M | 51 | Grafana dashboard E-Wallet Agent & MCP | calls by outcome, tool calls, p95 | agent and tool telemetry | — |
| `AGENT_F02_GRAFANA_LLM.png` | M | 50 | Grafana dashboard E-Wallet LLM Runtime | tokens, TTFT, latency | LLM telemetry | — |
| `AGENT_F03_TEMPO_TRACE.png` | M | 49, 22 | Tempo, the `e2e combined` trace | agentgateway, kagent-controller, Coordinator, both SandboxAgents, both MCP servers, `feast-online.get_features`, `kserve.infer` | one distributed trace end to end | — |
| `AGENT_F04_LOKI_LOGS.png` | M | 51 | Loki query | `coordinator.task_completed` with your request ids | correlated audit logs | — |
| **G. Reliability** |  |  |  |  |  |  |
| `AGENT_G01_MCP_REPLICAS_PDB.png` | M | 16, 18 | Phase 5 MCP commands | 2 Ready pods per server on different nodes, PDB allowed 1, 2 endpoints | MCP multi-replica and disruption budget | a failover (G02, H) |
| `AGENT_G02_FAILOVER.png` | C | 18 | `failover` (separate approval) | optimized 0 failed, production unchanged | service-level failover | — |
| **H. CI/CD** |  |  |  |  |  |  |
| `AGENT_H01_JENKINS_AGENT_CI.png` | C | 34, 35 | Jenkins `edai2-agent-ci` build | agent and MCP suites green | CI for agents and MCP servers | CD (never executed) |

## Reference: history

The procedures above replaced the following earlier steps. Do not use them in the current replay, and do not use the
results below in place of this campaign's screenshots or measurements.
- The 2026-11-06 recovery manifest and a 4-node wake.
- Manually scaling the MCP Deployments after the wake.
- Three BYO Agents and three agent ScaledObjects. These were before `sp-cleanup` (rev 55), when the Specialists
  were Deployments with a fallback path.
- The 100-task AGR-06 load in `keda` (now the primary `autoscale` command, with both autoscalers and the Sandbox-only
  harness).
- The 6-request `keda demo` as the autoscaling demonstration (now a helper only).
- A fixed `--also-approve ewallet-mcp/feature-rag-mcp` on the guarded sleep.
- Manually annotating ScaledObjects before a sleep (superseded by `cloud_lab sleep --pause-keda`).
- Publishing the registry from this guide.

Measured results of those runs:
- **AGR-06 (2026-11-16, `rem-20261116T050511Z`):** Coordinator 2→3→2, 100/100 tasks, p95 158.8 s, 0.128 task/s
  on the fixed 2-worker pool.
- **B′ (§9):** WorkerPool autoscaling, 100/100 at 0.185 task/s.
- **AGR-03:** rolling update, 170/170 calls during a production `rollout restart`.

The full record is in [AGENT_PLATFORM_COMPLETION.md](AGENT_PLATFORM_COMPLETION.md) §3–§12.
