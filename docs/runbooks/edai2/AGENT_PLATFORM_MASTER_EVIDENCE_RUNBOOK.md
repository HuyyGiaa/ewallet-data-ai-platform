# Agent platform master evidence runbook (AGI, AGR, AGS)

This runbook collects the evidence for rubric rows 16–27: integration (AGI-01–14),
reliability and autoscaling (AGR-01–07) and the sandbox and Substrate pool (AGS-01–07).
It extends the
[AGI runbook](AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md), which stays the source for
AGI-01–14, and follows the design in the
[completion audit](../../edai2/agents/AGENT_PLATFORM_COMPLETION_AUDIT.md) (sections 4–9).
The campaign state lives in
[`docs/workplans/edai2/agent-platform-campaign/`](../../workplans/edai2/agent-platform-campaign/).

## Rules

- Every step is labelled `[READ-ONLY]`, `[EXPERIMENT]` (new, campaign-labelled objects
  in `ewallet-sandbox` or `ewallet-mcp-exp`, removed by the same step) or
  `[APPROVAL Pn]` (changes a shared release or a cluster-scoped object; needs the
  owner approval named in section 1).
- A step is PASS only if its pass rule holds in the raw output. Screenshots show the
  raw output; they never replace it. A missing screenshot is reported as missing.
- Status words, in increasing order: `SOURCE_IMPLEMENTED`, `LOCAL_TESTED`,
  `GKE_TESTED`, `EVIDENCE_CAPTURED`, `RUBRIC_COMPLETE`. A row is `RUBRIC_COMPLETE`
  only when every evidence item it lists is `EVIDENCE_CAPTURED`.
- A fixed-size WorkerPool is never reported as KEDA-scaled. AGS-07 is the only
  WorkerPool scaling evidence, and it needs approval P3.
- No writes to production Redis DB 0, no canonical GCS changes, no Secret values in
  logs, process arguments or evidence files. Tokens move through files with mode 600
  or through stdin.
- One or two bounded fixes per failure, then mark the item `BLOCKED` with the error.

## 1. Approval boundaries

| Gate | Changes | Unlocks | Rollback |
|---|---|---|---|
| none | wake and sleep through `ops/gcp/cloud_lab.py`; images; `[EXPERIMENT]` objects in `ewallet-sandbox` and `ewallet-mcp-exp`; read-only probes and port-forwards | AGS-01, AGS-02, AGS-04 (boot and assignment without MCP), AGS-05, AGS-06, AGR-02 | the step's own cleanup |
| P1 | `llm-agent-mcp` upgrade: `g3/mcp-failover.yaml` (2 replicas, PDB) and the two sandbox MCP token entries | AGR-01, AGR-03, AGS-03, the tool calls of AGS-04 | `helm rollback llm-agent-mcp <preflight revision>`; delete the sandbox token Secrets |
| P2 | `llm-agent` upgrade with `agi/agents.yaml` and `agi/keda.yaml` (AGI campaign Stage 1, `AGI_APPROVAL=A1:<campaign>`) | AGI-01–14, AGR-04–07 | `agi_campaign.sh rollback agents` (`A2:<campaign>`) |
| P3 | WorkerPool CRD selector patch (`infra/kubernetes/substrate-scale/crd_patch.sh apply`, `CRD_PATCH_GKE_APPROVED=<context>`) and KEDA `watchNamespace` += `ewallet-sandbox` | AGS-07 | `crd_patch.sh revert`; KEDA values rollback |
| P4 | the Coordinator's sandbox route (`a2aGateway.sandboxRoutes`, `agents.coordinator.byo.sandboxSpecialists`) | AGI-09-sbx to AGI-11-sbx | the P2 rollback, or the same release without the two values |

Approvals are never self-issued. Without one, the item stays `APPROVAL_PENDING`.

## 2. Campaign setup `[READ-ONLY]`

```bash
cd ~/Develop/ewallet-data-ai-platform
export CAMPAIGN=agi-$(date -u +%Y%m%dt%H%M%Sz)          # lower case: it names GCS prefixes and labels
export EV=~/ewallet-evidence/agents/platform/$CAMPAIGN
export CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
mkdir -p "$EV"/{wake,images,ags,agr,sleep} && chmod 700 "$EV"
python3 ops/gcp/cloud_lab.py status     | tee "$EV/wake/status-before.txt"
python3 ops/gcp/cloud_lab.py cost-status | tee "$EV/wake/cost-before.txt"
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest "$M" | tee "$EV/wake/plan-wake.txt"
```

Wake only when `plan-wake` passes:

```bash
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --execute --confirm-cluster ewallet-dev \
  | tee "$EV/wake/wake.txt"
kubectl --context "$CTX" get nodes -o wide > "$EV/wake/nodes.txt"
kubectl --context "$CTX" -n ewallet-feature-store get pods -o wide > "$EV/wake/valkey.txt"   # Valkey durability: watch the first wake
```

Before any `[EXPERIMENT]` object is created, append its record to
`$EV/resources.tsv`: namespace, kind, name, owner label, image digest, limits,
lifetime and cleanup command.

## 3. Images `[READ-ONLY]` for the cluster

The BYO image runs all three agents (`ai_platform.llm_agent.byo_coordinator`,
`byo_feature_rag_specialist`, `byo_drift_specialist`). Build it from one clean commit,
scan it, push it, and use the digest everywhere: `AGENT_IMAGE=<registry>/...@sha256:<64 hex>`.
Record the commit, the digest and the scan result in `$EV/images/`.

## 4. AGS — sandbox and the shared Substrate pool

### AGS-01 — Two Specialists on one shared warm WorkerPool `[EXPERIMENT]`

```bash
kubectl --context "$CTX" apply -f infra/kubernetes/agent-sandbox/namespace.yaml     # WorkerPool business-agents, 2 workers
python3 scripts/experiments/render_sandbox.py fleet --image "$AGENT_IMAGE" --campaign "$CAMPAIGN" > "$EV/ags/fleet.yaml"
# The two token Secrets (sandbox-feature-rag-mcp-token, sandbox-drift-mcp-token, key "value") must exist first.
# Without P1 they hold a placeholder: the agents boot, and MCP calls are denied.
kubectl --context "$CTX" apply -f "$EV/ags/fleet.yaml"
kubectl --context "$CTX" -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide > "$EV/ags/AGS-01-objects.txt"
kubectl --context "$CTX" -n kagent port-forward svc/kagent-controller 18383:8083 > "$EV/ags/pf.log" 2>&1 &
curl -sf "http://127.0.0.1:18383/api/substrate/status?namespace=ewallet-sandbox" > "$EV/ags/AGS-01-status.json"
```

- **PASS only if:** both SandboxAgents are Ready; both ActorTemplates are Ready with a
  golden snapshot; both reference `workerPoolRef business-agents`; the pool has 2
  workers.
- **Screenshot:** `AGS-01_shared_workerpool.png`. **Rows:** 20, 27.

### AGS-02 — Isolation `[READ-ONLY]`

```bash
kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents \
  -o custom-columns=POD:.metadata.name,RUNTIME:.spec.runtimeClassName,SA:.spec.serviceAccountName > "$EV/ags/AGS-02-workers.txt"
for sa in sandbox-feature-rag sandbox-drift; do
  kubectl --context "$CTX" auth can-i --list --as=system:serviceaccount:ewallet-sandbox:$sa -n ewallet-sandbox
done > "$EV/ags/AGS-02-rbac.txt"
```

- **PASS only if:** workers run the gVisor sandbox class; the service accounts have
  no RBAC beyond the API-discovery defaults; `automountServiceAccountToken` is false;
  an actor's request to a host outside `allowedDomains` fails (from the agent log).
- **Screenshot:** `AGS-02_sandbox_isolation.png`. **Row:** 20.

### AGS-03 — Authorized calls succeed, unauthorized are denied `[APPROVAL P1]`

Needs the dedicated sandbox tokens in both MCP servers' token tables. Then one
Feature-RAG task and one drift task through the sandbox route complete with tool
evidence, and `agi_probe.py mcp --expect denied` with a sandbox token on an ungranted
tool prints PASS. Without P1: `APPROVAL_PENDING`. **Rows:** 20, 23.

### AGS-04 — Many sessions of both agents on few workers `[EXPERIMENT]`

```bash
python3 -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
  --out "$EV/ags/AGS-04-status.jsonl" --interval 2 --duration 600 &
for agent in feature-rag-specialist drift-specialist; do
  fixture=configs/experiments/fixtures/agent-load-request.json
  [ "$agent" = drift-specialist ] && fixture=configs/experiments/fixtures/agent-load-drift-request.json
  python3 -m ops.experiments.sandbox_client --url "http://127.0.0.1:18383/api/a2a/ewallet-sandbox/$agent/" \
    --fixture "$fixture" --run-id "$CAMPAIGN-$agent" --tasks 6 > "$EV/ags/AGS-04-$agent.log" &
done
wait
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl" | tee "$EV/ags/AGS-04-summary.json"
```

- **PASS only if:** the summary verdict is `SHARED_WORKERS_OBSERVED` (one worker pod
  hosted actors of both agents during the run), every actor ran in pool
  `business-agents`, 12 sessions were served by 2 workers, and every session reached a
  terminal state. Without P1 the tasks end in a recorded tool denial; that still
  proves assignment and reuse, but not tool results, and the report must say so.
- **Screenshot:** `AGS-04_actor_worker_assignment.png`. **Row:** 27.

### AGS-05 — Cold against warm start `[EXPERIMENT]`

The `LOAD_ROW` lines of AGS-04 hold each task's phase: `first` (template restore),
`new` (a new session restored from the golden snapshot) and `resume` (a suspended
actor resumed). Report the medians with n ≥ 5 per phase and per agent, and nothing
beyond the measured numbers. **Screenshot:** `AGS-05_cold_warm_latency.png`. **Row:** 27.

### AGS-06 — Utilization and cost `[READ-ONLY]`

`kubectl top pods -n ewallet-sandbox` during AGS-04, against the requests of the
two workers and of the Deployment Specialists in `ewallet-agents`. Report as measured.
**Screenshot:** `AGS-06_utilization_cost.png`. **Row:** 27.

### AGS-07 — KEDA scales the shared pool `[APPROVAL P3]`

On kind, `scripts/experiments/bprime_kind.sh` validates the control loop: the
unpatched CRD fails with an HPA selector error, the patched CRD scales the WorkerPool
1 → 3 → 1, atecontroller keeps the HPA's decision, and the patch reverts cleanly. The
kind run uses pause-image workers and no actors, so it is not evidence for row 27 on
GKE. On GKE: P3, then the chart's sandbox ScaledObject with
`agents.autoscaling.workerPoolScaleSelector: spec.scaleSelector`, load from AGS-04, and
the replica sampler on the WorkerPool. Without P3: `APPROVAL_PENDING`, and row 27 is
reported as a fixed warm pool. **Rows:** 19, 27.

## 5. AGR — reliability and autoscaling

### AGR-01 — Two MCP replicas on different nodes `[APPROVAL P1]`

`kubectl get deploy,pods,pdb -o wide -n ewallet-mcp` and the MCP Gateway in
`ewallet-agents`: 2/2 Ready on distinct nodes, PDB `minAvailable 1`. **Row:** 18.

### AGR-02 — Pod loss does not fail read-only calls `[EXPERIMENT]`

```bash
export KUBE_CONTEXT=$CTX MCP_IMAGE=<digest ref> PROBE_IMAGE=<digest ref>
for arm in baseline optimized; do
  EXP_APPROVED=yes scripts/experiments/mcp_experiment.sh $arm run "$EV/agr/AGR-02-$arm"
done
```

- **PASS only if:** the optimized arm has 0 failed calls with retries > 0, the baseline
  arm shows the failures the arm is meant to show, and the production
  `production-before/after.txt` files are equal. The script deletes only pods in
  `ewallet-mcp-exp` and removes the namespace at the end.
- **Screenshot:** `AGR-02_mcp_failover_run.png`. **Row:** 18.

### AGR-03 — Rolling update without failed calls `[APPROVAL P1]`

After P1, `kubectl rollout restart deploy/drift-mcp -n ewallet-mcp` during a 60 s loop
of typed read-only calls: 0 failed calls, new ReplicaSet Ready. **Row:** 18.

### AGR-04 to AGR-07 — KEDA on the agents `[APPROVAL P2]`

After the P2 upgrade (`-f agi/agents.yaml -f g3/keda.yaml -f agi/keda.yaml`):

- **AGR-04:** `kubectl get scaledobject,hpa -n ewallet-agents -o yaml`: three
  ScaledObjects Ready, no fallback active, HPAs owned by `ScaledObject/<agent>`.
- **AGR-05:** `python3 scripts/experiments/keda_preflight.py` reports the
  `ewallet_agent_inflight_tasks` gauge READY per agent, and the external metric equals
  the PromQL value.
- **AGR-06:** typed load at concurrency ≤ 8 through the Coordinator
  (`ops/experiments/agent_load.py`), with `ops/experiments/replica_sampler.py --hpa`
  on each Deployment: desired and Ready reach 3, the new pod completes tasks, and the
  count returns to 2 after the window. In-flight tasks finish within the 25 s graceful
  shutdown, inside the default 30 s termination period.
- **AGR-07:** AGI-09–11 complete during or right after AGR-06.

Without P2 all four are `APPROVAL_PENDING`. **Rows:** 19, 22, 25.

## 6. Cleanup and sleep (mandatory)

Order: stop load → stop jobs → stop port-forwards (match by command line) → export
evidence → remove `[EXPERIMENT]` objects (`kubectl delete -f "$EV/ags/fleet.yaml"`;
`mcp_experiment.sh <arm> down`) → delete the placeholder Secrets → delete only this
campaign's snapshot prefix `gs://ate-snapshots/ewallet-sandbox/$CAMPAIGN/` → verify
nothing labelled `ewallet.io/campaign=$CAMPAIGN` remains → sleep:

```bash
python3 ops/gcp/cloud_lab.py plan-sleep | tee "$EV/sleep/plan-sleep.txt"        # never bypass a failed plan
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev | tee "$EV/sleep/sleep.txt"
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$M" | tee "$EV/sleep/verify.txt"
python3 ops/gcp/cloud_lab.py status | tee "$EV/sleep/status.txt"
python3 ops/gcp/cloud_lab.py cost-status | tee "$EV/sleep/cost.txt"
```

`SLEEP_VERIFIED` only when `verify --expect sleep` passes; otherwise `SLEEP_BLOCKED`
with its output.
