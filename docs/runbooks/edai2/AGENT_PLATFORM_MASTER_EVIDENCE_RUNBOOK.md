# Agent platform master evidence runbook (AGI, AGR, AGS)

The evidence for rubric rows 16–27 comes from three groups:

- **AGI-01–14**, integration. Defined in the
  [AGI runbook](AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md).
- **AGR-01–07**, MCP reliability and KEDA. Section 5.
- **AGS-01–07**, the sandbox and the Substrate pool. Section 4.

The design is in the
[completion audit](../../edai2/agents/AGENT_PLATFORM_COMPLETION_AUDIT.md). The
campaign state is in
[`docs/workplans/edai2/agent-platform-campaign/`](../../workplans/edai2/agent-platform-campaign/):
`checkpoints.json`, `HANDOFF.md`, and `images.json` (the image digests).

## Rules

- Every step is labelled:
  - `[READ-ONLY]`;
  - `[EXPERIMENT]`: new, campaign-labelled objects in `ewallet-sandbox` or
    `ewallet-mcp-exp`, removed by the same step;
  - `[PKG-x]`: changes a shared release or a cluster-scoped object, and needs that
    package's own owner approval (section 1).
- A wake does not authorize any package. Five nodes need PKG-C.
- Status words, in increasing order: `SOURCE_IMPLEMENTED`, `LOCAL_TESTED`,
  `KIND_TESTED`, `GKE_TESTED`, `EVIDENCE_CAPTURED`, `RUBRIC_COMPLETE`. A row is
  `RUBRIC_COMPLETE` only when every evidence item it lists is `EVIDENCE_CAPTURED`.
- A step is PASS only if its pass rule holds in the raw output. Screenshots show
  the raw output; they never replace it.
- **A+S** is the immediate GKE path:
  - KEDA scales the three BYO agent Deployments.
  - The two sandbox Specialists share one fixed WorkerPool of 2 workers.
  - It is never reported as a KEDA-scaled WorkerPool.
- **B′** is AGS-07 only. It needs PKG-P3.
- The kind B′ run (CP2) validates the control loop. It is not GKE evidence with
  real actors.
- No writes to production Redis DB 0. No canonical GCS changes. No Secret values in
  logs, process arguments or evidence files.
- One or two bounded fixes per failure, then mark the item `BLOCKED` with the error.

## 1. Packages and their order

```text
PKG-0  images (Feast 0.67.0 MCP image, three BYO images; one commit)        no cluster change
  └─ PKG-K  kagent watches ewallet-sandbox                                   kagent/kagent
       └─ AGS S0: namespace, WorkerPool, fleet, sessions                    [EXPERIMENT]
            └─ PKG-C  capacity for the MCP experiment (5 nodes)             cloud_lab wake --nodes 5
                 └─ AGR-02 isolated MCP failover                            [EXPERIMENT] ewallet-mcp-exp
                      └─ PKG-P1  MCP reliability + AGI MCP overlay          llm-agent-mcp
                           └─ PKG-S1  sandbox MCP access (S1)               credentials, llm-agent-mcp, llm-agent
                                └─ PKG-P2  AGI agents + KEDA on agents      llm-agent
                                     └─ PKG-P4  Coordinator sandbox route   llm-agent
                                          └─ AGI-09..11-sbx, AGR, AGS evidence
                                               ├─ optional PKG-W + PKG-P3: B′ WorkerPool scaling (AGS-07)
                                               └─ cleanup and sleep (section 6)
```

PKG-K comes first because nothing in `ewallet-sandbox` is reconciled without it.
PKG-C is independent of the others; take it only for AGR-02.

| Package | Scope (exact) | Needs first | Approval token | Mutation | Rollback |
|---|---|---|---|---|---|
| PKG-0 | Images in `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform` | — | none (images only) | none | none (immutable digests) |
| PKG-K | Release `kagent` (namespace `kagent`): ConfigMap `kagent-controller` `WATCH_NAMESPACES`, plus 3 Role/RoleBinding pairs in `ewallet-sandbox`. The controller restarts once. | Namespace `ewallet-sandbox` | `WATCH_APPROVAL=PKG-K:$CTX` | `sandbox_watch.sh kagent upgrade` | `WATCH_APPROVAL=PKG-K-ROLLBACK:$CTX sandbox_watch.sh kagent rollback` |
| PKG-C | Node pool `cpu-pool`: 4 → 5 nodes for the campaign window | — | owner approval of a 5-node `wake-full` | `cloud_lab.py wake-full --nodes 5` | sleep (section 6) |
| PKG-P1 | Release `llm-agent-mcp` (namespace `ewallet-mcp`): AGI MCP overlay, corrected image, 2 replicas per server, PDB `feature-rag-mcp`, hostname spread, RollingUpdate `maxUnavailable 0` | PKG-0 | `AGI_APPROVAL=P1-MCP:$CAMPAIGN` | `AGI_PACKAGE=p1-mcp agi_campaign.sh upgrade mcp` | `AGI_APPROVAL=P1-MCP-ROLLBACK:$CAMPAIGN … rollback mcp` |
| PKG-S1 | (a) A transition credential server set that adds subjects `sandbox-feature-rag` and `sandbox-drift`; (b) a copy of those two client Secrets into `ewallet-sandbox`; (c) `llm-agent-mcp` tool grants and `serverSet`; (d) `llm-agent` ingress flow `sandbox-workers-to-mcp-gateway`; (e) namespace `ewallet-sandbox` in ambient | PKG-P1, PKG-K | `AGI_APPROVAL=S1-MCP:$CAMPAIGN`, then `S1-AGENTS:$CAMPAIGN` | section 2.5 | section 2.5 |
| PKG-P2 | Release `llm-agent` (objects in `ewallet-agents`): AGI agents overlay (BYO drift, global ModelConfig resolver) plus `g3/keda.yaml` and `agi/keda.yaml`. 3 ScaledObjects adopt the 3 HPAs. | PKG-P1 | `AGI_APPROVAL=P2-AGENTS:$CAMPAIGN` | `AGI_PACKAGE=p2-agents agi_campaign.sh upgrade agents` | `P2-AGENTS-ROLLBACK`: deletes the 3 ScaledObjects first, then rolls back, then checks the HPAs |
| PKG-P4 | Release `llm-agent`: A2A Gateway routes `sandbox-<agent>` and Coordinator `EWALLET_SANDBOX_*` | PKG-P2, PKG-S1, AGS S0 | `AGI_APPROVAL=P4-AGENTS:$CAMPAIGN` | `AGI_PACKAGE=p4-agents … upgrade agents` | `P4-AGENTS-ROLLBACK`: the Deployment Specialists serve again |
| PKG-W | Release `keda` (namespace `keda`): `WATCH_NAMESPACE` += `ewallet-sandbox` on 3 Deployments, plus 1 RoleBinding in `ewallet-sandbox`. The 3 KEDA pods restart once. | — | `WATCH_APPROVAL=PKG-W:$CTX` | `sandbox_watch.sh keda upgrade` | `WATCH_APPROVAL=PKG-W-ROLLBACK:$CTX … rollback` |
| PKG-P3 | Cluster CRD `workerpools.ate.dev`: `scaleSelector` and `labelSelectorPath`. Then `scaleSelector` on WorkerPool `business-agents` and one ScaledObject. | PKG-W, AGS S0 | `CRD_PATCH_APPROVED=yes` + `CRD_PATCH_GKE_APPROVED=$CTX` | `crd_patch.sh apply` (section 4, AGS-07) | `crd_patch.sh revert` (refuses while a WorkerPool uses `scaleSelector`) |

### Blast radius and stop conditions

| Package | Risk | Stop (FAIL) if |
|---|---|---|
| PKG-K | One kagent-controller restart. Agent CRs stay; UI sessions reconnect. | The identity check fails (the chart, live values and post-renderer do not reproduce the live manifest), the diff differs from the reviewed one, or the server-side dry-run fails. |
| PKG-W | The KEDA metrics API server restarts. HPAs keep their replica counts while external metrics are briefly unavailable. | The same three checks as PKG-K, or any ScaledObject is not Ready after the change. |
| PKG-C | +$0.18/h while awake ($0.997/h for 5 nodes against $0.819/h for 4, excluding the cluster fee). | `mcp_experiment.sh … capacity` still says `CAPACITY_INSUFFICIENT`. Do not add a sixth node. |
| PKG-P1 | MCP pods roll with one surge pod. +200m CPU, +512Mi. | The identity diff is not exactly 2 Deployments changed and 1 PDB added, or the image is not `images.json`'s `mcp-tools`. |
| PKG-S1 | The MCP servers read a new token table; old credentials stay valid during the transition. Sandbox token copies hold real credentials. | The credential dry-run lists unexpected subjects, the diff is not 2 Deployments (MCP) or 1 AuthorizationPolicy + 1 NetworkPolicy (agents), or gVisor workers carry no mesh identity (S1 probe denied for the wrong reason). |
| PKG-P2 | Agents roll; KEDA takes over the HPAs (forward path tested: no gap). | The diff differs from the reviewed one (Agents, Role, RoleBinding, NetworkPolicy, otel ConfigMap and Deployment, 3 ScaledObjects added, 3 HPAs removed). |
| PKG-P4 | The Coordinator's Specialist calls move to the sandbox. | Workflows fail. Then roll back P4; the Deployment Specialists stay deployed. |
| PKG-P3 | A cluster-scoped CRD change on a Helm-managed CRD. | The verify step does not read back selector `ate.dev/worker-pool=business-agents`. Then revert. |

## 2. Fresh terminal setup `[READ-ONLY]`

```bash
cd ~/Develop/ewallet-data-ai-platform
git fetch origin && git checkout feature/edai2-agent-platform-integration && git pull --ff-only
export UV=~/miniconda3/envs/fintech/bin/uv PATH=~/miniconda3/envs/fintech/bin:$PATH
export KUBECONFIG=~/.cache/ewallet-cloud-lab/kubeconfig     # written by cloud_lab.py (IAM DNS endpoint)
export CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export CAMPAIGN=agi-$(date -u +%Y%m%dT%H%M%SZ)             # one id for every package, label and prefix
export EV=~/ewallet-evidence/agents/platform/$CAMPAIGN PRIVATE=/tmp/agi-$CAMPAIGN
export M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
mkdir -p "$EV"/{wake,images,ags,agr,pkg,sleep} && chmod 700 "$EV" && mkdir -p -m 700 "$PRIVATE"
IMAGES=docs/workplans/edai2/agent-platform-campaign/images.json
img() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"][sys.argv[2]]["digest_ref"])' "$IMAGES" "$1"; }
export MCP_IMAGE=$(img mcp-tools) COORDINATOR_IMAGE=$(img g3-byo-coordinator) \
       FEATURE_RAG_IMAGE=$(img g3-byo-feature-rag-specialist) DRIFT_IMAGE=$(img g3-byo-drift-specialist)
export IMAGE_SHA=$(python3 -c 'import json; print(json.load(open("'"$IMAGES"'"))["git_sha"])')
```

The AGI campaign file (`AGI_ENV`) is the one in the
[AGI runbook, section 0](AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md#0-campaign-identity-and-shell-setup-read-only),
with these additions:

- `SHA=$(git rev-parse HEAD)`;
- `IMAGE_SHA` from above. `agi_campaign.sh` accepts an `IMAGE_SHA` older than `SHA` only
  when it is an ancestor and no image input changed, so the pods report the commit they
  were built from;
- for PKG-S1: `SANDBOX_SERVER_SET` and `SANDBOX_CREDENTIAL_RELEASE`, both 1–24
  character lowercase DNS labels, for example `sbx-1115`.

`AGI_PACKAGE` is set per command, not in the file. Run `preflight` again before each
package; it snapshots the live state that package starts from.

### 2.1 Wake `[READ-ONLY]` plan, then wake

```bash
python3 ops/gcp/cloud_lab.py status | tee "$EV/wake/status-before.txt"
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest "$M" | tee "$EV/wake/plan-wake.txt"
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest "$M" --execute --confirm-cluster ewallet-dev \
  | tee "$EV/wake/wake.txt"                                   # 4 nodes; with PKG-C approved, add --nodes 5
kubectl --context "$CTX" get nodes -L ewallet.io/g3-llm > "$EV/wake/nodes.txt"
kubectl --context "$CTX" -n ate-system get pods -o wide > "$EV/wake/substrate.txt"    # ate-api, valkey-cluster-0..5
kubectl --context "$CTX" -n kagent get pods > "$EV/wake/kagent.txt"   # the controller can crash-loop until Postgres resolves
```

Before any `[EXPERIMENT]` object, append its record to `$EV/resources.tsv`: namespace,
kind, name, owner label, image digest, limits, lifetime and cleanup command.

### 2.2 PKG-0 images (already done for the recorded `images.json`)

To rebuild from the checked-out commit:

```bash
SHA=$(git rev-parse HEAD)
for a in coordinator feature_rag_specialist drift_specialist; do
  bash scripts/llm_agent/build_g3_byo_$a.sh "$SHA" "$EV/images/byo-$a.json"
done
echo '{"components":["drift-mcp","feature-rag-mcp"]}' > "$EV/images/changed.json"
ARTIFACT_REGISTRY=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform \
  bash scripts/llm_agent/build_changed_images.sh "$EV/images/changed.json" "$SHA" "$EV/images/mcp.json"
python3 - "$EV/images" <<'EOF'
import glob, json, sys
merged = {"images": {}}
for path in sorted(glob.glob(sys.argv[1] + "/byo-*.json")) + [sys.argv[1] + "/mcp.json"]:
    data = json.load(open(path)); merged["git_sha"] = data["git_sha"]; merged["images"].update(data["images"])
json.dump(merged, open(sys.argv[1] + "/image-digests.json", "w"), indent=1, sort_keys=True)
EOF
bash scripts/llm_agent/scan_and_push.sh "$EV/images/image-digests.json"   # Trivy gate; pushes only on PASS
```

**PASS only if** all four images pass the gate (no CRITICAL/HIGH fixable vulnerability,
no secret) and `mcp-tools` reports `feast 0.67.0`
(`docker run --rm --entrypoint python <ref> -c 'import feast; print(feast.__version__)'`).
`scripts/llm_agent/feast_cross_version_check.sh "$EV/images/feast-compat"` (local)
shows the MCP runtime reading the 0.66.0-published registry and online store.

### 2.3 PKG-K and PKG-W: controller watch scope

```bash
export KUBE_CONTEXT=$CTX
kubectl --context "$CTX" apply -f infra/kubernetes/agent-sandbox/namespace.yaml     # [EXPERIMENT] needed by both
for t in kagent keda; do scripts/llm_agent/sandbox_watch.sh $t dry-run "$PRIVATE/pkg"; done     # [READ-ONLY]
cp "$PRIVATE"/pkg/*/diff.txt "$EV/pkg/" 2>/dev/null; ls "$EV/pkg"
# After the owner approves PKG-K:
WATCH_APPROVAL=PKG-K:$CTX scripts/llm_agent/sandbox_watch.sh kagent upgrade "$PRIVATE/pkg"
scripts/llm_agent/sandbox_watch.sh kagent verify "$PRIVATE/pkg" | tee "$EV/pkg/PKG-K-verify.txt"
# Rollback (owner): WATCH_APPROVAL=PKG-K-ROLLBACK:$CTX scripts/llm_agent/sandbox_watch.sh kagent rollback "$PRIVATE/pkg"
```

- `dry-run` fails closed unless the pinned chart, the live values and, for kagent, the
  pilot post-renderer reproduce every live object.
- The overlay diff must be exactly the reviewed objects (`ops/llm_agent/sandbox_watch_diff.py`).
- `verify` reads the watch value. It also reads the start-up log of the newest
  controller pod and the RBAC in `ewallet-sandbox`.
- Every step of both packages, including the refused approvals and the rollback, ran
  on kind (`scripts/experiments/sandbox_watch_kind.sh`, `PACKAGES_VALIDATED`).

PKG-W is the same with `keda`, `PKG-W` and `PKG-W-ROLLBACK`. Take it only with PKG-P3.

### 2.4 PKG-P1 and PKG-P2

```bash
export AGI_ENV=<campaign file>
AGI_PACKAGE=p1-mcp scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=p1-mcp scripts/llm_agent/agi_campaign.sh gate mcp            # exact identities + server dry-run
AGI_APPROVAL=P1-MCP:$CAMPAIGN AGI_PACKAGE=p1-mcp scripts/llm_agent/agi_campaign.sh upgrade mcp
AGI_PACKAGE=p2-agents scripts/llm_agent/agi_campaign.sh preflight
# KEDA gate before the move. Nothing may be BLOCKED. The in-flight gauge is WAITING until the
# P2 images run. The Prometheus access rules were applied with G5-B; if they show WAITING, applying
# infra/kubernetes/experiments/keda-prometheus-access.yaml is part of the P2 approval.
python3 scripts/experiments/keda_preflight.py --context "$CTX" --stage before-scaledobject | tee "$EV/pkg/P2-keda-preflight.txt"
AGI_PACKAGE=p2-agents scripts/llm_agent/agi_campaign.sh gate agents
AGI_APPROVAL=P2-AGENTS:$CAMPAIGN AGI_PACKAGE=p2-agents scripts/llm_agent/agi_campaign.sh upgrade agents
```

P2's rollback (`AGI_APPROVAL=P2-AGENTS-ROLLBACK:$CAMPAIGN … rollback agents`) deletes the
three ScaledObjects before `helm rollback` and fails unless the three HPAs are back. Then
run `keda_preflight.py --stage after-rollback`.

Then run AGI Stage 2–4 from the AGI runbook (AGI-01–14). They need no further package.

### 2.5 PKG-S1: sandbox MCP access

```bash
python3 -m ops.llm_agent.agi_packages merge-values --out "$PRIVATE/s1-mcp-values.yaml" \
  "$PRIVATE/llm-agent-mcp-values.yaml" infra/helm/llm-agent-platform/agi/mcp-sandbox-grants.yaml
PREVIOUS=$(python3 -c 'import yaml,sys; print((yaml.safe_load(open(sys.argv[1])).get("mcpCredentialLifecycle") or {}).get("serverSet","legacy"))' "$PRIVATE/llm-agent-mcp-values.yaml")
CRED=(python3 -m ops.llm_agent.g3_mcp_credentials --context "$CTX" --values "$PRIVATE/s1-mcp-values.yaml"
      --client-namespace ewallet-agents --server-namespace ewallet-mcp --release "$SANDBOX_CREDENTIAL_RELEASE"
      --server-set "$SANDBOX_SERVER_SET" --mode transition --previous-server-set "$PREVIOUS")
"${CRED[@]}" --dry-run | tee "$EV/pkg/S1-credentials-plan.json"          # names and fingerprint only
# After the owner approves PKG-S1:
"${CRED[@]}"                                                              # values go to Kubernetes only
for pair in sandbox-feature-rag:feature-rag-mcp sandbox-drift:drift-mcp; do
  python3 -m ops.llm_agent.g3_platform_inputs replicate-secret --context "$CTX" --source-namespace ewallet-agents \
    --destination-namespace ewallet-sandbox --name "mcp-token-${pair%%:*}-${pair#*:}-$SANDBOX_CREDENTIAL_RELEASE" \
    --approve-sensitive-copy
done
AGI_PACKAGE=s1-mcp scripts/llm_agent/agi_campaign.sh preflight
AGI_APPROVAL=S1-MCP:$CAMPAIGN AGI_PACKAGE=s1-mcp scripts/llm_agent/agi_campaign.sh upgrade mcp
AGI_PACKAGE=s1-agents scripts/llm_agent/agi_campaign.sh preflight
AGI_APPROVAL=S1-AGENTS:$CAMPAIGN AGI_PACKAGE=s1-agents scripts/llm_agent/agi_campaign.sh upgrade agents
kubectl --context "$CTX" label namespace ewallet-sandbox istio.io/dataplane-mode=ambient
```

- The two sandbox subjects get exactly the Deployment Specialists' grants
  (`agi/mcp-sandbox-grants.yaml`).
- The transition server set keeps every active credential, so the production agents are
  not interrupted.
- `s1-agents` appends one flow to the live `crossNamespace.flows`. A static overlay would
  delete the 16 existing policies.
- **Unverified until AGS-03:** that ztunnel captures traffic from gVisor worker pods, so
  the MCP Gateway sees `cluster.local/ns/ewallet-sandbox/sa/default`.
- **Rollback:**
  1. `S1-AGENTS-ROLLBACK` and `S1-MCP-ROLLBACK`, in that order.
  2. Delete the two copied Secrets in `ewallet-sandbox`.
  3. Remove the ambient label.
  4. A later `final` server set without the sandbox subjects revokes them. That is a
     separate approval.

### 2.6 PKG-P4

```bash
AGI_PACKAGE=p4-agents scripts/llm_agent/agi_campaign.sh preflight         # expects the BYO drift-specialist
AGI_PACKAGE=p4-agents scripts/llm_agent/agi_campaign.sh gate agents
AGI_APPROVAL=P4-AGENTS:$CAMPAIGN AGI_PACKAGE=p4-agents scripts/llm_agent/agi_campaign.sh upgrade agents
```

Then repeat AGI-09–11 in kagent UI. Record them as AGI-09-sbx to AGI-11-sbx, with the
sandbox actor IDs from `/api/substrate/status`.

## 3. PKG-C: MCP experiment capacity

The MCP experiment pods must avoid `ewallet.io/g3-llm` nodes, which are reserved for vLLM
and the agents. The optimized arm needs **two distinct eligible nodes**, each with
100m CPU and 256Mi free. The probe Job needs 50m and 128Mi on any node.

| Wake | Eligible nodes | Observed or expected | Verdict |
|---|---|---|---|
| 4 nodes (`wake-full`) | 1 (`cloud_lab` labels 3 nodes for LLM) | observed 2026-11-14: that node at 97% CPU requested | `CAPACITY_INSUFFICIENT` for both arms |
| 5 nodes (`wake-full --nodes 5`) | 2 | expected: the non-LLM workloads spread over 2 nodes | must be checked at runtime |

Removing the affinity would put experiment pods on the LLM nodes. Shrinking requests
below the measured 100m is not supported by any measurement. Both are rejected.

```bash
# EXP_PYTHON must be one executable: the tooling environment's interpreter.
export KUBE_CONTEXT=$CTX MCP_IMAGE PROBE_IMAGE=$COORDINATOR_IMAGE \
  EXP_PYTHON=$("$UV" run --frozen --project infra/docker/llm-agent-tooling python -c 'import sys; print(sys.executable)')
for arm in baseline optimized; do scripts/experiments/mcp_experiment.sh $arm capacity "$EV/agr/AGR-02"; done  # [READ-ONLY]
```

`run` repeats this check and stops with `CAPACITY_INSUFFICIENT` before it creates
anything. Plan: wake 5 nodes, run both arms of AGR-02 (about 15 minutes each, ≤ 45
minutes in total), then return to the 4-node state or sleep. Expected peak: 5 nodes.

## 4. AGS — sandbox and the shared Substrate pool (A+S unless stated)

Port-forward the kagent controller once:
`kubectl --context "$CTX" -n kagent port-forward svc/kagent-controller 18383:8083 > "$EV/ags/pf.log" 2>&1 &`.
Close it by command line at the end.

### AGS-01 — Two Specialists on one shared warm WorkerPool `[EXPERIMENT]`, needs PKG-K

```bash
REL=${SANDBOX_CREDENTIAL_RELEASE:-s0}
python3 scripts/experiments/render_sandbox.py fleet --image "$FEATURE_RAG_IMAGE" --campaign "$CAMPAIGN" \
  --credential-release "$REL" > "$EV/ags/fleet.yaml"
# Without PKG-S1 (S0 only): a random token per Specialist that no MCP server trusts. MCP calls are
# then denied, which AGS-04 records.
for pair in sandbox-feature-rag:feature-rag-mcp sandbox-drift:drift-mcp; do
  openssl rand -hex 32 | kubectl --context "$CTX" -n ewallet-sandbox create secret generic \
    "mcp-token-${pair%%:*}-${pair#*:}-$REL" --from-file=token=/dev/stdin
  kubectl --context "$CTX" -n ewallet-sandbox label secret "mcp-token-${pair%%:*}-${pair#*:}-$REL" ewallet.io/campaign=$CAMPAIGN
done
kubectl --context "$CTX" apply -f "$EV/ags/fleet.yaml"
kubectl --context "$CTX" -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide > "$EV/ags/AGS-01-objects.txt"
curl -sf "http://127.0.0.1:18383/api/substrate/status?namespace=ewallet-sandbox" > "$EV/ags/AGS-01-status.json"
```

The same BYO image runs either Specialist module; the fleet sets the module per agent.

- **PASS only if:**
  - both SandboxAgents are Accepted and Ready;
  - both ActorTemplates are Ready with a golden snapshot;
  - both use `workerPoolRef business-agents`;
  - the pool has 2 workers.
- **Screenshot:** `AGS-01_shared_workerpool.png`. **Rows:** 20, 27.

### AGS-02 — Isolation `[READ-ONLY]`

```bash
kubectl --context "$CTX" -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents \
  -o custom-columns=POD:.metadata.name,SA:.spec.serviceAccountName,NODE:.spec.nodeName > "$EV/ags/AGS-02-workers.txt"
kubectl --context "$CTX" auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox \
  > "$EV/ags/AGS-02-rbac.txt"
```

- **PASS only if:**
  - the workers run the `gvisor` sandbox class (ActorTemplate `sandboxClass`);
  - the default ServiceAccount has no RBAC beyond discovery and no mounted token;
  - an actor request to a host outside `allowedDomains` fails, as shown in the agent log.
- **Screenshot:** `AGS-02_sandbox_isolation.png`. **Row:** 20.

### AGS-03 — Authorized calls succeed, others are denied, needs PKG-S1

- One Feature-RAG task and one drift task through the sandbox route return tool evidence.
- `agi_probe.py mcp --expect denied` with a sandbox token on an ungranted tool prints PASS.

**Rows:** 20, 23.

### AGS-04 — Many sessions of both agents on two workers `[EXPERIMENT]`

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

- **PASS only if:**
  - the verdict is `SHARED_WORKERS_OBSERVED`, meaning one worker pod hosted actors of
    both agents;
  - every actor ran in `business-agents`;
  - all 12 sessions reached a terminal state.
- Without PKG-S1 the tasks end in a recorded tool denial. That proves assignment and
  reuse, not tool results; say so.
- **Screenshot:** `AGS-04_actor_worker_assignment.png`. **Row:** 27.

### AGS-05 — Cold against warm start, from the AGS-04 `LOAD_ROW` phases

Report medians with n ≥ 5 per phase and agent: `first`, `new` (restored from the golden
snapshot) and `resume` (a suspended actor resumed).
**Screenshot:** `AGS-05_cold_warm_latency.png`. **Row:** 27.

### AGS-06 — Utilization and cost `[READ-ONLY]`

`kubectl top pods -n ewallet-sandbox` during AGS-04, against the requests of the two
workers and of the Deployment Specialists. Report as measured. **Row:** 27.

### AGS-07 — B′: KEDA scales the shared pool, needs PKG-W and PKG-P3

```bash
infra/kubernetes/substrate-scale/crd_patch.sh backup "$CTX" "$EV/ags/crd-backup"
infra/kubernetes/substrate-scale/crd_patch.sh dry-run "$CTX"            # accepted on GKE on 2026-11-14
CRD_PATCH_APPROVED=yes CRD_PATCH_GKE_APPROVED=$CTX infra/kubernetes/substrate-scale/crd_patch.sh apply "$CTX"
kubectl --context "$CTX" -n ewallet-sandbox patch workerpool business-agents --type=merge \
  -p '{"spec":{"scaleSelector":"ate.dev/worker-pool=business-agents"}}'
infra/kubernetes/substrate-scale/crd_patch.sh verify "$CTX" business-agents ewallet-sandbox
```

- Then apply one ScaledObject on WorkerPool `business-agents` (minimum 1, maximum 3),
  with the AGS-04 load, and sample with `ops.experiments.replica_sampler`.
- **PASS only if:**
  - the HPA shows `ScalingActive=True`;
  - workers go 1 → 2 (→ 3) → 1;
  - sessions keep completing.
- **Rollback:** delete the ScaledObject, remove `scaleSelector`, run `crd_patch.sh revert`,
  then the PKG-W rollback.
- **Rows:** 19, 27.

## 5. AGR — reliability and autoscaling

| ID | Needs | Command | PASS only if | Screenshot |
|---|---|---|---|---|
| AGR-01 | PKG-P1 | `kubectl get deploy,pods,pdb -o wide -n ewallet-mcp`; `mcp-gateway` in `ewallet-agents` | 2/2 Ready per server on distinct nodes (the spread is `ScheduleAnyway`, so read the placement); PDB `minAvailable 1` | `AGR-01_mcp_replicas.png` |
| AGR-02 | PKG-C (capacity) | `EXP_APPROVED=yes scripts/experiments/mcp_experiment.sh <arm> run "$EV/agr/AGR-02"`, baseline then optimized | The optimized arm has 0 failed calls with retries > 0; the baseline shows failures; `production-before/after.txt` are equal | `AGR-02_mcp_failover_run.png` |
| AGR-03 | PKG-P1 | `kubectl rollout restart deploy/drift-mcp -n ewallet-mcp` during a 60 s typed read-only loop | 0 failed calls; the new ReplicaSet is Ready | `AGR-03_mcp_rolling_update.png` |
| AGR-04 | PKG-P2 | `kubectl get scaledobject,hpa -n ewallet-agents -o yaml` | 3 ScaledObjects Ready, no fallback, HPAs owned by `ScaledObject/<agent>` | `AGR-04_keda_scaledobjects.png` |
| AGR-05 | PKG-P2 | Prometheus `sum by (agent)(ewallet_agent_inflight_tasks)`; `kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/ewallet-agents/s0-prometheus?labelSelector=scaledobject.keda.sh/name=<agent>"` | One series per agent pod; the external metric equals the PromQL value | `AGR-05_inflight_metric.png` |
| AGR-06 | PKG-P2 | `ops/experiments/agent_load.py` (concurrency ≤ 8) plus `replica_sampler --hpa` | Desired and Ready reach 3, the new pod completes tasks, the count returns to 2; drains finish within 25 s | `AGR-06a/b_scale_out/in.png` |
| AGR-07 | PKG-P2 | AGI-09–11 during or after AGR-06 | completed, `status complete` | `AGR-07_workflow_after_scaling.png` |

## 6. Cleanup and sleep (mandatory)

Order:

1. Stop load.
2. Stop the samplers.
3. Close the port-forwards by command line
   (`pgrep -af port-forward`, then match `/proc/<pid>/cmdline`).
4. Export the evidence.
5. Remove the `[EXPERIMENT]` objects:
   - `kubectl delete -f "$EV/ags/fleet.yaml"`;
   - the campaign Secrets: `kubectl -n ewallet-sandbox delete secret -l ewallet.io/campaign=$CAMPAIGN`;
   - `mcp_experiment.sh <arm> down`.
6. Roll back or keep each package exactly as the owner decided.
7. Check that nothing labelled `ewallet.io/campaign=$CAMPAIGN` remains.

Snapshots live in the in-cluster RustFS store under the logical
`gs://ate-snapshots/ewallet-sandbox/$CAMPAIGN/`. They go when their SandboxAgents are
deleted.

```bash
python3 ops/gcp/cloud_lab.py plan-sleep --manifest "$M" | tee "$EV/sleep/plan-sleep.txt"   # read-only
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev | tee "$EV/sleep/sleep.txt"
```

If the plain `sleep` refuses with `SLEEP_BLOCKED` because of PDBs, PDB-guarded preparation
is a **separate owner approval**
([cloud lab runbook](CLOUD_LAB_RUNBOOK.md#sleep)):

```bash
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:$CAMPAIGN python3 ops/gcp/cloud_lab.py sleep \
  --execute --confirm-cluster ewallet-dev --scale-pdb-guarded --manifest "$M" --campaign "$CAMPAIGN" \
  [--also-approve ewallet-mcp/feature-rag-mcp]          # only after PKG-P1 added that PDB
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$M" | tee "$EV/sleep/verify.txt"
python3 ops/gcp/cloud_lab.py cost-status | tee "$EV/sleep/cost.txt"
```

Report `SLEEP_VERIFIED` only when `verify --expect sleep` passes. Without the guarded
approval, report `SLEEP_BLOCKED` with the plan output and leave the decision to the owner.
