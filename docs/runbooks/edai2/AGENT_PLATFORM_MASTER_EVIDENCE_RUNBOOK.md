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
  └─ PKG-K  kagent watches ewallet-sandbox (GKE_TESTED, kagent rev 6)       kagent/kagent
       └─ PKG-R-IAM  repository read for ate-system/atelet                  IAM (repository only)
            └─ PKG-R  atelet authenticates its own image pulls             substrate/ate-system
                 └─ AGS S0: namespace, WorkerPool, fleet, sessions         [EXPERIMENT]
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
| PKG-R-IAM | Repository `ewallet-platform` (`asia-southeast1`): `roles/artifactregistry.reader` for `principal://iam.googleapis.com/projects/198818809719/locations/global/workloadIdentityPools/ewallet-data-ai-platform-hg26.svc.id.goog/subject/ns/ate-system/sa/atelet`. No service account, no key, not project-wide. | — | `IAM_APPROVAL=PKG-R-IAM:ewallet-data-ai-platform-hg26` | `substrate_pull_iam.sh grant` | `IAM_APPROVAL=PKG-R-IAM-REVOKE:… substrate_pull_iam.sh revoke` (removes only that member) |
| PKG-R | Release `substrate` (namespace `ate-system`): DaemonSet `atelet` `args[0]` `--gcp-auth-for-image-pulls=true`, nothing else; the 3 atelet pods restart once | PKG-R-IAM, PKG-K | `PKG_R_APPROVAL=PKG-R:$CTX` | `substrate_pull_auth.sh upgrade` | `PKG_R_APPROVAL=PKG-R-ROLLBACK:$CTX substrate_pull_auth.sh rollback` |
| GOLDEN-RESIDUE | Two Suspended golden actors in atespace `ate-golden` (`golden-residue.json`), deleted through ate-api `DeleteActor` | — | `RESIDUE_APPROVAL=GOLDEN-RESIDUE:$CTX` | `substrate_golden_residue.sh delete` | none (they hold no pod or snapshot) |
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
| PKG-R | Every atelet restarts. Actors are suspended and then resumed, the pilot's included. atelet asks for a Google token at start-up and exits if it cannot get one, so a Workload Identity fault shows up at once. | The lookup-render identity check fails, the diff is more than atelet `args[0]`, the IAM binding is absent, a node has no Running `gke-metadata-server`, or atelet restarts or logs `Failed to create GCP registry authenticator`. Then roll back. |
| PKG-R-IAM | One reader binding on one repository. | The policy after the change differs from the policy before by more than that one pair. |
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

### 2.3a Sandbox S0 with PKG-R (the next runtime campaign)

The capacity baseline is the 2026-11-15 run: `agent-sandbox` on 3 nodes scheduled both warm
workers. That profile keeps the five PDB-guarded Deployments at 0, so a plain sleep works.

```bash
# 1. Wake (owner-approved profile, at most 3 nodes)
python3 ops/gcp/cloud_lab.py plan-wake --mode experiment --profile agent-sandbox --manifest "$M"
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-experiment --profile agent-sandbox --manifest "$M" \
  --nodes 3 --execute --confirm-cluster ewallet-dev | tee "$EV/wake/wake.txt"
# 2. Baseline health: nodes, PDB-guarded Deployments at 0, ate-system and kagent Running, Substrate API answering
kubectl --context "$CTX" get nodes -L ewallet.io/g3-llm,iam.gke.io/gke-metadata-server-enabled
kubectl --context "$CTX" get pods -n ate-system -o wide; kubectl --context "$CTX" get pods -n kagent
kubectl --context "$CTX" -n kube-system get pods -l k8s-app=gke-metadata-server -o wide
# 3. Repository-scoped IAM (PKG-R-IAM; may be granted before the wake, it needs no node)
scripts/llm_agent/substrate_pull_iam.sh plan "$EV/pkg/iam"
IAM_APPROVAL=PKG-R-IAM:ewallet-data-ai-platform-hg26 scripts/llm_agent/substrate_pull_iam.sh grant "$EV/pkg/iam"
scripts/llm_agent/substrate_pull_iam.sh verify
# 4.-5. PKG-R: identity (lookup render), exact diff, metadata server, server dry-run, upgrade, verify
export KUBE_CONTEXT=$CTX
scripts/llm_agent/substrate_pull_auth.sh dry-run "$PRIVATE/pkg-r"
PKG_R_APPROVAL=PKG-R:$CTX scripts/llm_agent/substrate_pull_auth.sh upgrade "$PRIVATE/pkg-r"
scripts/llm_agent/substrate_pull_auth.sh verify "$PRIVATE/pkg-r" | tee "$EV/pkg/PKG-R-verify.txt"
cp "$PRIVATE/pkg-r/substrate/package.log" "$PRIVATE/pkg-r/substrate/diff.txt" "$EV/pkg/"   # paths only, no values
scripts/llm_agent/substrate_pull_auth.sh cleanup "$PRIVATE/pkg-r"   # the renders hold the live TLS key
# 6.-9. Redeploy the fleet, then check the private pulls and the golden snapshots (AGS-01)
#    namespace.yaml (WorkerPool, 2 workers), placeholder Secrets and fleet.yaml exactly as in AGS-01 below;
kubectl --context "$CTX" -n ewallet-sandbox get actortemplate -o \
  'jsonpath={range .items[*]}{.metadata.name} phase={.status.phase} golden={.status.goldenSnapshot}{"\n"}{end}'
kubectl --context "$CTX" -n ate-system logs deploy/ate-controller --since=15m | grep -c "pullCache.Fetch" || true   # 0 expected
# 10. AGS-01, AGS-02 and AGS-04 (section 4); 11. raw evidence into $EV; 12. cleanup (section 6, plus the
#     residue check below); 13. plan-sleep, plain sleep, verify --expect sleep
```

PKG-R stays installed after the campaign. Roll it back only if a step fails:
`PKG_R_APPROVAL=PKG-R-ROLLBACK:$CTX scripts/llm_agent/substrate_pull_auth.sh rollback "$PRIVATE/pkg-r"`.
Do not run `cleanup` before a rollback, because rollback compares against the saved baseline.

**Residual golden actors.** Substrate 0.0.9's ActorTemplate controller returns on deletion
without deleting the golden actor. Every deleted ActorTemplate therefore leaves one actor
in atespace `ate-golden`. The two from 2026-11-15 are listed in
`docs/workplans/edai2/agent-platform-campaign/golden-residue.json`.

```bash
scripts/llm_agent/build_kubectl_ate.sh ~/.local/ate          # from the pinned v0.0.9 source, offline modules
export KUBECTL_ATE=~/.local/ate/kubectl-ate
scripts/llm_agent/substrate_golden_residue.sh list docs/workplans/edai2/agent-platform-campaign/golden-residue.json "$EV/residue"   # read-only
RESIDUE_APPROVAL=GOLDEN-RESIDUE:$CTX scripts/llm_agent/substrate_golden_residue.sh delete \
  docs/workplans/edai2/agent-platform-campaign/golden-residue.json "$EV/residue"
```

`delete` removes only the listed IDs. It removes each one only if it is Suspended, has no
pod, no snapshot and no snapshot in progress, and its ActorTemplate no longer exists. It
goes through ate-api `DeleteActor`, never Valkey. These records are not evidence of worker
sharing. After a campaign's own fleet is deleted, add that campaign's golden actors to a
reviewed file in the same way.

### 2.3b Session resume verification (owner approval: `agent-sandbox` wake, at most 3 nodes)

Root cause: `docs/workplans/edai2/agent-platform-campaign/resume-rca/RCA.md`. kagent 0.10.3
suspends a session actor asynchronously after each turn. A follow-up during that window is
never resumed. The run below changes nothing shared. PKG-K, PKG-R and the IAM binding stay
as deployed, and it needs no new image: the driver runs on the laptop.

Footprint: 3 nodes, about 1 hour, about $0.50. Steps:

1. Wake and check baseline health (section 2.3a, steps 1–2).
2. Redeploy the fleet (AGS-01).
3. Wait for both golden snapshots.
4. Port-forward the controller, then run the A/B:

```bash
python3 -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
  --out "$EV/ags/RESUME-status.jsonl" --interval 1 --duration 900 &
# A: control, follow-ups sent at once. Expected: resume rows rpc_error after about 120 s; actor stays Suspended.
python3 -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "$CAMPAIGN-a" --agents drift-specialist \
  --contexts 1 --messages 2 --no-settle > "$EV/ags/RESUME-A.txt"
# B: paced, the fix. Expected: settled_before_send true, then the same actor goes Suspended -> Resuming -> Running.
# One agent at a time: two concurrent first turns can exceed kagent's 30 s --substrate-call-timeout (see below).
python3 -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "$CAMPAIGN-b2" --agents drift-specialist \
  --contexts 1 --messages 4 > "$EV/ags/RESUME-B2.txt"
# B3: sharing check, the other agent on the same free worker.
python3 -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "$CAMPAIGN-b3" \
  --agents feature-rag-specialist --contexts 1 --messages 2 > "$EV/ags/RESUME-B3-sharing.txt"
wait
kubectl --context "$CTX" -n kagent logs deploy/kagent-controller --since=30m > "$EV/ags/RESUME-kagent.txt"
kubectl --context "$CTX" -n ate-system logs deploy/ate-api-server-deployment --since=30m > "$EV/ags/RESUME-ate-api.txt"
for p in $(kubectl --context "$CTX" -n ate-system get pods -l app=atelet -o name); do
  kubectl --context "$CTX" -n ate-system logs "$p" --since=30m; done > "$EV/ags/RESUME-atelet.txt"
kubectl --context "$CTX" -n ate-system logs deploy/atenet-router --all-containers --since=30m > "$EV/ags/RESUME-atenet.txt"
```

- **PASS (B) only if:**
  - every `resume` row has `settled_before_send: true` and its task reached a terminal
    A2A state, without `rpc_error`;
  - in `RESUME-status.jsonl` the row's `actor` ID (unchanged from its `new` row) moves
    Suspended → Resuming → Running at least 3 times over the 4 messages of a context;
  - no second actor appears for the same context;
  - AGS-04's sharing still holds, with golden actors excluded.
- Under S0 the turns end `failed` with `mcp_transport_error`, because there is no MCP
  Gateway. That proves the resume lifecycle, not business results, and the result must
  say so.
- **FAIL:** a paced resume still times out. Then capture `RESUME-*.txt` and stop.
- **Result, 2026-11-15 (`agi-20261115T084916Z`):** A reproduced the race. B2 passed:
  3 resumes of one actor ID, all restores clean, no `rpc_error`. B3 showed one worker
  serving both agents. Details are in `resume-rca/RCA.md` section 7.
- **Actor stuck `RESUMING`, worker held, `no free workers`:**
  - Cause: a first-turn restore was cancelled at kagent's 30 s `--substrate-call-timeout`.
    Concurrent restores on one node make this likely.
  - Recovery within the campaign: delete that actor's worker pod. The ReplicaSet
    recreates it, Substrate marks the actor `SUSPENDED`, and the worker is free.
  - Do not delete the actor. A SandboxAgent finalizer also loops on such an actor, and
    the same pod restart unblocks it.
- **Cleanup and sleep:** as in section 2.3a (fleet, Secrets, WorkerPool, `plan-sleep`,
  plain `sleep`, `verify`).
- **Residual actors:** the new session actors are listed with
  `kubectl-ate get actors --atespace ewallet-sandbox`. Deleting them needs a separate
  approval.

### 2.3c PKG-T runtime stability (owner approval: `agent-sandbox` wake, at most 3 nodes)

What it deploys (root causes and design: `resume-rca/RCA.md` section 8):

- **PKG-T-SUBSTRATE.** The ate-api image is replaced by the patched v0.0.9 build.
  Workflows are detached from the caller, and a failed restore rolls back. atelet memory
  becomes 512Mi:1536Mi (`SUBSTRATE_ATELET_MEMORY`): at 512Mi its in-memory pull cache
  OOM-killed it on GKE.
- **PKG-T-KAGENT also adds a capacity wait.** A new session waits up to 20 s for a worker
  that a post-turn suspend is about to release, instead of failing at once with
  `no free workers`.
- **PKG-T-KAGENT.** The kagent controller image is replaced by the patched v0.10.3 build
  (session turns), and `SUBSTRATE_CALL_TIMEOUT=90s` is set.
- **Campaign WorkerPool.** `namespace.yaml` uses the patched ateom (failed activations tear
  down their sandbox). `kagent-pilot` keeps the release ateom.

Images, patches and pins:

- Images come from `scripts/llm_agent/build_runtime_fix_images.py` and
  `scripts/llm_agent/scan_and_push.sh`, as recorded in
  `docs/workplans/edai2/agent-platform-campaign/images.json` (`runtime_fix`).
- Patches, hashes and the build recipe are in
  `infra/kubernetes/kagent-pilot/patches/runtime-fix.json`.
- The digests are in `infra/kubernetes/kagent-pilot/patches/runtime-fix-images.env`.

```bash
# G0: wake (section 2.3a step 1) and record the baseline (read-only)
export KUBE_CONTEXT=$CTX
for t in substrate kagent; do scripts/llm_agent/runtime_fix.sh $t dry-run "$PRIVATE"; done   # identity, exact diff, server dry-run
helm --kube-context "$CTX" history kagent -n kagent --max 3 > "$EV/pkg/T-helm-before.txt"
helm --kube-context "$CTX" history substrate -n ate-system --max 3 >> "$EV/pkg/T-helm-before.txt"
# G1: ate-api first (backward compatible with the release kagent), then kagent
RUNTIME_FIX_APPROVAL=PKG-T-SUBSTRATE:$CTX scripts/llm_agent/runtime_fix.sh substrate upgrade "$PRIVATE"
RUNTIME_FIX_APPROVAL=PKG-T-KAGENT:$CTX scripts/llm_agent/runtime_fix.sh kagent upgrade "$PRIVATE"
cp "$PRIVATE"/runtime-fix-*/package.log "$EV/pkg/"; for t in substrate kagent; do scripts/llm_agent/runtime_fix.sh $t cleanup "$PRIVATE"; done
#     S0 as in AGS-01 (namespace.yaml now carries the patched ateom), then wait for both golden snapshots
kubectl --context "$CTX" apply -f infra/kubernetes/agent-sandbox/namespace.yaml
python3 -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
  --out "$EV/ags/T-status.jsonl" --interval 1 --duration 3600 &
D="python3 -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383"
# G2 single session, paced: >= 3 genuine Suspended -> Resuming -> Running cycles on one actor
$D --run-id "$CAMPAIGN-g2" --agents drift-specialist --contexts 1 --messages 4 > "$EV/ags/T-G2.txt"
# G3 rapid multi-turn, unpaced: arrivals while RUNNING / SUSPENDING / SUSPENDED, then a burst
$D --run-id "$CAMPAIGN-g3" --no-settle --contexts 2 --messages 5 --burst 2 > "$EV/ags/T-G3.txt"
# G4 concurrent first-turn restores of both Specialists on the shared pool, three rounds
$D --run-id "$CAMPAIGN-g4" --no-settle --contexts 3 --messages 1 > "$EV/ags/T-G4.txt"
# G5 forced failure: the caller gives up during the first restore; nothing is restarted
$D --run-id "$CAMPAIGN-g5" --agents drift-specialist --context-id "$CAMPAIGN-g5-cancel" --contexts 1 --messages 1 --timeout 5 > "$EV/ags/T-G5a.txt"
#     ... wait until the actor is Suspended and its worker free (T-status.jsonl), then the same context:
$D --run-id "$CAMPAIGN-g5" --agents drift-specialist --context-id "$CAMPAIGN-g5-cancel" --contexts 1 --messages 2 --no-settle > "$EV/ags/T-G5b.txt"
# G6 bounded concurrency: both agents, 3 contexts x 3 unpaced messages each
$D --run-id "$CAMPAIGN-g6" --no-settle --contexts 3 --messages 3 > "$EV/ags/T-G6.txt"
# G7 controller restart: one turn, delete the controller pod during its suspend, then the same context
python3 -m ops.experiments.runtime_stability_report --rows "$EV"/ags/T-G*.txt --status "$EV/ags/T-status.jsonl" > "$EV/ags/T-report.json"
```

- **PASS-1:** G2 and G3 serve every turn without `rpc_error`, with one actor per context.
  G3 records arrivals in at least two distinct states.
- **PASS-2:** in every G4 round both restores succeed, and no actor stays `Resuming` past the
  call timeout.
- **PASS-3:** in G5 the actor ends `Suspended` with its worker free, no pod restarts, and the
  follow-up is served on the same actor ID.
- **PASS-4:** the worker placement and resource use are measured. The WorkerPool uses only
  fields of the installed CRD.
- **PASS-5:** the `uv` commands in the RCA collect and run.

Under S0, turns end `failed` with `mcp_transport_error`. That is lifecycle evidence, not
business success.

Rollback, if a step fails (each records its own pre-upgrade revision and image):

- `RUNTIME_FIX_APPROVAL=PKG-T-KAGENT-ROLLBACK:$CTX scripts/llm_agent/runtime_fix.sh kagent rollback "$PRIVATE"`
- the same with `substrate` and `PKG-T-SUBSTRATE-ROLLBACK`.

- **Incremental steps.** `runtime_fix.sh` renders its baseline with whatever part of PKG-T
  is already live, so a later step shows only the remaining diff. Rollback always returns
  to the revision recorded before the first step.
- **Result, 2026-11-15 (`rts-20261115T114235Z`):** `RUNTIME_STABILITY_GKE_PASS`. Details
  are in `resume-rca/RCA.md` section 8.
- **Live after that run:** kagent revision 8 and Substrate revision 5.

`substrate_pull_auth.sh` renders without `--ateapi-image`. After PKG-T, its identity preflight
therefore fails closed until it passes the same post-renderer argument.

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

### 2.6b PKG-T1: Sandbox OTLP export (owner-approved 2026-11-15)

The mesh denied OTLP from `cluster.local/ns/ewallet-sandbox/sa/default`, so the Sandbox
Specialists' own spans were missing from the business trace (AGI-12). This package appends
one flow to the live `crossNamespace.flows`: the business-agents workers may reach
`otel-collector` on port 4317, and nothing else.

```bash
AGI_PACKAGE=t1-agents scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=t1-agents scripts/llm_agent/agi_campaign.sh gate agents   # adds exactly 1 AuthorizationPolicy and 1 NetworkPolicy
AGI_APPROVAL=T1-AGENTS:$CAMPAIGN AGI_PACKAGE=t1-agents scripts/llm_agent/agi_campaign.sh upgrade agents
```

- **Risk:** a compromised actor could write fake telemetry. It cannot read any.
- **Rollback:** `T1-AGENTS-ROLLBACK`.

### 2.6c P2-C: kagent controller tracing (Phase 2 foundation)

The kagent controller hop was missing from the business trace for two reasons. Its OTLP endpoint
had no URL scheme, and the mesh had no rule admitting `cluster.local/ns/kagent/sa/kagent-controller`
to the collector. The fix ships as two packages, applied in this order:

1. **Network.** Append one flow to the live `crossNamespace.flows`: the controller may reach
   `otel-collector` on port 4317, and nothing else.
2. **Controller.** Set the controller's endpoint to
   `http://otel-collector.ewallet-agents.svc.cluster.local:4317`.

```bash
AGI_PACKAGE=tr-agents scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=tr-agents scripts/llm_agent/agi_campaign.sh gate agents   # adds exactly 1 AuthorizationPolicy and 1 NetworkPolicy
AGI_APPROVAL=TR-AGENTS:$CAMPAIGN AGI_PACKAGE=tr-agents scripts/llm_agent/agi_campaign.sh upgrade agents
# then the controller, only after the flow is live:
KUBE_CONTEXT=$CTX scripts/llm_agent/runtime_fix.sh kagent-otel dry-run "$PRIVATE"   # 1 ConfigMap key + checksum annotation
RUNTIME_FIX_APPROVAL=P2C-KAGENT-OTEL:$CTX KUBE_CONTEXT=$CTX scripts/llm_agent/runtime_fix.sh kagent-otel upgrade "$PRIVATE"
```

- **Risk:** the controller can write telemetry to the collector. It cannot read any.
- **Rollback:**
  - controller: `P2C-KAGENT-OTEL-ROLLBACK`;
  - network: `TR-AGENTS-ROLLBACK`.

  Roll back the controller first.

### 2.6d Legacy Specialist Deployment cleanup (`sp-cleanup`, after B′)

The Feature-RAG and Drift Specialists run only as SandboxAgents on the shared WorkerPool. This package
renders `agi/specialists-sandbox-only.yaml` (`runtime: sandbox`) over the live values. It removes their BYO
Agent CRs, which makes kagent delete their Deployments and Services, plus 40 Deployment-only objects:
ServiceAccounts, PDBs, ScaledObjects, policies and prompt ConfigMaps. The Sandbox routes and the Coordinator
stay.

```bash
AGI_PACKAGE=sp-cleanup scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=sp-cleanup scripts/llm_agent/agi_campaign.sh gate agents   # 42 removed, 6 changed (exact identities)
AGI_APPROVAL=SP-CLEANUP:$CAMPAIGN AGI_PACKAGE=sp-cleanup scripts/llm_agent/agi_campaign.sh upgrade agents
```

- **Kept:** the MCP credentials of the retired Specialists. Retiring them needs a separate approval.
- **Rollback:** `SP-CLEANUP-ROLLBACK`. Helm restores the Agent CRs and kagent recreates the Deployments.
  *Superseded by 2.6e:* after the credential retirement, the restored Deployments would have no valid MCP
  credential, so this is no longer a working path.

### 2.6e Legacy MCP credential retirement (`cr-mcp`, `cr-agents`, after `sp-cleanup`)

The nine `mcp-token-{drift,feature-rag}-specialist-*` credentials are retired. The procedure goes Inventory →
Prepare → Finalize → Verify → Revoke.

1. **Inventory.** Read the auth table IDs only; never print a token.
   ```bash
   AGI_ENV=… scripts/llm_agent/mcp_credential_check.sh <out> pre-migration   # per replica, read-only
   ```
2. **Prepare** the final server set from the live values. Specialists with `runtime: sandbox` are retired
   automatically. `--carry-release` keeps the active client releases, so no client moves.
   ```bash
   python -m ops.llm_agent.g3_mcp_credentials --context "$CTX" --values <live specialists + live mcpServices> \
     --mode final --release fin-<date> --server-set fin-<date> --previous-server-set sbx-20261115 \
     --previous-server-namespace ewallet-mcp --server-namespace ewallet-mcp \
     --carry-release r1-release1 --carry-release sbx-20261115 [--dry-run]
   ```
   For `ewallet-agents/drift-mcp`, add `--server-namespace ewallet-agents --drop-subject kagent-controller` and
   carry only `sbx-20261115`: its image (695279967) has no discovery subjects.
3. **Finalize.** Set `FINAL_SERVER_SET` in the campaign file; each package has its own preflight and gate.
   ```bash
   AGI_PACKAGE=cr-mcp agi_campaign.sh preflight && AGI_PACKAGE=cr-mcp agi_campaign.sh gate mcp
   AGI_APPROVAL=CR-MCP:$CAMPAIGN AGI_PACKAGE=cr-mcp agi_campaign.sh upgrade mcp
   AGI_PACKAGE=cr-agents agi_campaign.sh preflight && AGI_PACKAGE=cr-agents agi_campaign.sh gate agents
   AGI_APPROVAL=CR-AGENTS:$CAMPAIGN AGI_PACKAGE=cr-agents agi_campaign.sh upgrade agents
   ```
4. **Verify** with `mcp_credential_check.sh <out> post-migration`. Every row must be PASS: retired → `revoked`,
   `sandbox-*` → `ok`, kagent-controller → `call-denied`. Then check zero references across cluster objects and
   both releases' manifests. *Superseded by 2.6f:* the retired client Secrets are deleted, so the check now uses
   only live identities and a locally generated invalid credential, and its exit code is the verdict.
5. **Revoke.** Run `kubectl delete secret` on exactly the nine names, with `--dry-run=server` first.

- **No rollback** for `cr-mcp`/`cr-agents`: `agi_campaign.sh rollback` refuses them. Fix forward with a new final
  set; never re-select `sbx-20261115`, `r1-transition` or `legacy` (they still contain the retired tokens).
- **AGI-08 principals** are now `sandbox-feature-rag`, `sandbox-drift` and the kagent controller (`call-denied`).

### 2.6f Legacy MCP infrastructure cleanup (`lc-agents`, `lc-mcp`, auth tables; after 2.6e)

Removes what the credential retirement left: the unused in-release `ewallet-agents/drift-mcp` (old image, no
route, no client) with everything it exclusively owned, the obsolete auth tables, and the retired client
references in the stored values. The active `ewallet-mcp` servers are not changed.

1. **Per-replica credential check** (read-only; the exit code is the verdict, from
   `ops/llm_agent/mcp_credential_verdict.py`):
   ```bash
   AGI_ENV=… LEGACY_SERVER=present scripts/llm_agent/mcp_credential_check.sh <out> pre    # before lc-agents
   AGI_ENV=… scripts/llm_agent/mcp_credential_check.sh <out> post                         # after: absent
   ```
   Per Ready replica of each `ewallet-mcp` server it proves, separately, authentication, authorization and business
   execution:
   - `sandbox-*` must be authorized and its business execution must be ok;
   - the kagent controller must list the tool and be denied the call;
   - a random invalid credential must get 401.
   The negatives run with the active credential as the control on the same replica. It fails on a failed, missing
   or malformed probe, a replica count different from the Deployment (or below `MCP_MIN_REPLICAS`, default 2),
   or a stale or lost port-forward. With `LEGACY_SERVER=present`, the old server's `drift_baseline_unavailable` is
   reported as `backend_unavailable`, never as success. With the default `absent`, its Deployment, Service, PDB,
   ServiceAccount and pods must all be gone.
2. **Auth-table matrix** (read-only, token-free; scans live objects, both current releases and every Helm revision):
   ```bash
   CTX=… LC_CAMPAIGN=<campaign> EXPECT_DELETE="<ns>/<secret> …" scripts/llm_agent/mcp_auth_tables.sh plan <out>
   ```
   DELETE needs all of the following, otherwise the table is KEEP or UNKNOWN:
   - an explicitly obsolete set (`legacy`, `r1-transition`, `sbx-20261115`);
   - no live or current-Helm reference;
   - the server is absent or reads a verified final table;
   - the computed list equals the reviewed `EXPECT_DELETE`.
   Final sets (`fin-20261117`, `fin-20261117-b`) are KEEP. The only exception is the two unused `ewallet-agents`
   copies, which [2.6g](#26g-final-orphan-and-auth-table-cleanup-lf-agents-proposed-not-executed) handles.
3. **Retire the in-release server**, then the values-only cleanup. Each package has its own preflight and an
   exact gate:
   ```bash
   AGI_PACKAGE=lc-agents scripts/llm_agent/agi_campaign.sh preflight
   AGI_PACKAGE=lc-agents scripts/llm_agent/agi_campaign.sh gate agents   # 8 removed + network-flows ConfigMap changed
   AGI_APPROVAL=LC-AGENTS:$CAMPAIGN AGI_PACKAGE=lc-agents scripts/llm_agent/agi_campaign.sh upgrade agents
   AGI_PACKAGE=lc-mcp scripts/llm_agent/agi_campaign.sh preflight
   AGI_PACKAGE=lc-mcp scripts/llm_agent/agi_campaign.sh gate mcp         # zero changed objects
   AGI_APPROVAL=LC-MCP:$CAMPAIGN AGI_PACKAGE=lc-mcp scripts/llm_agent/agi_campaign.sh upgrade mcp
   ```
   `lc-agents` renders `agi/agents-retire-in-release-mcp.yaml`. `mcpServices.services.drift-mcp.retired: true` drops
   the Deployment, Service, PDB, ServiceAccount, kubelet-probe NetworkPolicy and the otel-collector flow policies.
   The MCP Gateway route, both RemoteMCPServers and the cross-namespace policies all target `ewallet-mcp/drift-mcp`
   and stay.
4. **Delete the obsolete tables** (exact names; it re-plans first, runs a server-side dry run, and re-plans after):
   ```bash
   LC_APPROVAL=AUTH-TABLES:<campaign> CTX=… LC_CAMPAIGN=<campaign> EXPECT_DELETE="…" \
     scripts/llm_agent/mcp_auth_tables.sh delete <out>
   ```
5. **KEEP fingerprints** around steps 3–4 (`python -m ops.llm_agent.object_fingerprints snapshot|compare`): the
   differences must equal the reviewed matrix exactly.

**Rollback and recovery (after 2.6f).**
- **Direct historical Helm rollback is prohibited.** `llm-agent-mcp` ≤ 17 and `llm-agent` ≤ 57 select the deleted
  auth tables (`legacy`, `r1-transition`, `sbx-20261115`) or the deleted Specialist client Secrets. Such a revision
  is intentionally nonfunctional: its pods would fail with `CreateContainerConfigError`, and it would never re-trust
  a retired credential.
- `agi_campaign.sh rollback`, `scripts/llm_agent/rollback.sh` (Jenkins) and `scripts/llm_agent/deploy_atomic.sh` all
  refuse any manifest that names a retired credential (`ops/llm_agent/retired_credentials.py`).
- `LC-AGENTS-ROLLBACK` targets `llm-agent` rev 58. That revision recreates the old server on `fin-20261117-b`, which
  is kept; the guard accepts it. Prefer a fix forward.
- `LC-MCP-ROLLBACK` targets rev 18, which renders identically.
- `kubectl rollout undo` on an MCP Deployment could select a zero-replica ReplicaSet from before the final sets.
  That ReplicaSet references a deleted table and cannot start. Recover by fixing forward from the live values.
- `g3/g3.yaml` is a legacy reference profile and is not deployable without migration.

### 2.6g Final orphan and auth-table cleanup (`lf-agents`; PROPOSED, not executed)

Prepared offline in `lf-offline-20261117`, with no cluster access. Nothing here has run on GKE. Every step needs
the owner's approval. The plan, the exact matrices and the evidence of the historical basis are in
[AGENT_PLATFORM_COMPLETION.md §13](../../edai2/agents/AGENT_PLATFORM_COMPLETION.md#13-offline-audit-and-final-cleanup-proposal-lf-offline-20261117).

1. **Preflight** (read-only). Expect `llm-agent` rev 59 and `llm-agent-mcp` rev 19, and a live
   `helm get manifest llm-agent` whose sha256 is `01b90e1d…`, the reviewed render. Then run the credential check
   and the auth-table plan with the reviewed list:
   ```bash
   AGI_ENV=… scripts/llm_agent/mcp_credential_check.sh <out> pre
   CTX=… LC_CAMPAIGN=<campaign> EXPECT_DELETE="ewallet-agents/mcp-auth-drift-mcp-fin-20261117 ewallet-agents/mcp-auth-drift-mcp-fin-20261117-b" \
     scripts/llm_agent/mcp_auth_tables.sh plan <out>
   ```
   The plan must compute exactly those two names; any difference stops the campaign. The two `ewallet-mcp`
   `fin-20261117` tables must be KEEP, each read by a live server, verified immutable, with no retired subject.
2. **Retire the disabled in-release `feature-rag-mcp`** (exact gate, server-side dry run, `--atomic`):
   ```bash
   AGI_PACKAGE=lf-agents scripts/llm_agent/agi_campaign.sh preflight
   AGI_PACKAGE=lf-agents scripts/llm_agent/agi_campaign.sh gate agents   # 9 removed + network-flows ConfigMap changed
   AGI_APPROVAL=LF-AGENTS:$CAMPAIGN AGI_PACKAGE=lf-agents scripts/llm_agent/agi_campaign.sh upgrade agents
   ```
   It renders `agi/agents-retire-feature-rag-mcp.yaml` (`mcpServices.services.feature-rag-mcp.retired: true`). It
   removes the ServiceAccount, the kubelet-probe policy, the otel-collector flow policies and the two
   crossNamespace flows. The flows go from `ewallet-agents`, and from `feature-store`/`fraud-serving` as the
   `allow-ewallet-agents-*` objects. The active `ewallet-mcp/feature-rag-mcp` keeps its own `allow-ewallet-mcp-*`
   peer policies (release `llm-agent-mcp`, untouched). The MCP Gateway route, RemoteMCPServer and the gateway's
   egress to `ewallet-mcp` do not change. `llm-agent-mcp` gets no package.
3. **Delete the two unused tables** (the same tool and safeguards as 2.6f step 4):
   ```bash
   LC_APPROVAL=AUTH-TABLES:<campaign> CTX=… LC_CAMPAIGN=<campaign> EXPECT_DELETE="<the two names above>" \
     scripts/llm_agent/mcp_auth_tables.sh delete <out>
   ```
4. **KEEP fingerprints** around steps 2–3. Expect 9 removed objects, the 2 Secrets removed, and
   `ConfigMap/ewallet-agents/llm-agent-network-flows` changed; nothing else.
5. **Post checks:** `mcp_credential_check.sh post`, then the full regression (§3).

**Rollback.**
- `LF-AGENTS-ROLLBACK` targets rev 59. That revision only recreates the 9 policy/ServiceAccount objects, which
  select no pod. It names no auth table, and the guard accepts it.
- After step 3, `llm-agent` rev 58 (`LC-AGENTS-ROLLBACK`) and rev 56 are also nonfunctional: they recreate the
  retired in-release drift-mcp on a deleted table. Its pods fail closed with `CreateContainerConfigError`, and the
  `ewallet-mcp` servers are unaffected. Fix forward; never recreate a deleted table.

### 2.7 Image roll after P2 (`img-mcp`, `img-agents`)

Use these when a code fix changes an image input after the packages are live. Example: the
AGR-06 fix in `ai_platform/llm_agent/gateway_backoff.py`.

1. Rebuild all four images from the fix commit.
2. Set `SHA`, `IMAGE_SHA` and the four image references in the campaign env.
3. Roll MCP first, then the agents:

```bash
AGI_PACKAGE=img-mcp scripts/llm_agent/agi_campaign.sh preflight            # expects the BYO drift-specialist
AGI_PACKAGE=img-mcp scripts/llm_agent/agi_campaign.sh gate mcp              # only the 2 MCP Deployments change
AGI_APPROVAL=IMG-MCP:$CAMPAIGN AGI_PACKAGE=img-mcp scripts/llm_agent/agi_campaign.sh upgrade mcp
AGI_PACKAGE=img-agents scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=img-agents scripts/llm_agent/agi_campaign.sh gate agents        # only the 3 Agents change
AGI_APPROVAL=IMG-AGENTS:$CAMPAIGN AGI_PACKAGE=img-agents scripts/llm_agent/agi_campaign.sh upgrade agents
```

A change that touches only the Coordinator rolls the Coordinator alone. The Specialists keep their live
images. P2-D (Sandbox capacity metrics) used this path:

```bash
AGI_PACKAGE=img-coordinator scripts/llm_agent/agi_campaign.sh preflight
AGI_PACKAGE=img-coordinator scripts/llm_agent/agi_campaign.sh gate agents   # only Agent fraud-coordinator changes
AGI_APPROVAL=IMG-COORDINATOR:$CAMPAIGN AGI_PACKAGE=img-coordinator scripts/llm_agent/agi_campaign.sh upgrade agents
```

- The render is the live values plus the image references, and nothing else, so no
  package's configuration changes.
- `<release>-<package>-diff.txt` keeps each package's reviewed diff. `<release>-diff.txt`
  is the latest gate.
- **Rollback:** `IMG-AGENTS-ROLLBACK`, then `IMG-MCP-ROLLBACK`. A plain Helm rollback to
  the preflight revision restores the previous images.

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

### AGS-01 — Two Specialists on one shared warm WorkerPool `[EXPERIMENT]`, needs PKG-K and PKG-R

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

kagent 0.10.3 serves SandboxAgents under `/api/a2a-sandboxes/<ns>/<name>`. The
`/api/a2a/` path returns 404 for them.

```bash
python3 -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
  --out "$EV/ags/AGS-04-status.jsonl" --interval 2 --duration 600 &
python3 -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "$CAMPAIGN" \
  --contexts 4 --messages 2 > "$EV/ags/AGS-04-sessions.log"            # both agents in parallel
wait
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl" | tee "$EV/ags/AGS-04-summary.json"
kubectl --context "$CTX" -n ewallet-sandbox get actortemplate -o 'jsonpath={range .items[*]}{.status.goldenActorID}{"\n"}{end}' \
  > "$EV/ags/golden-ids.txt"   # exclude these when you count sharing: golden actors are not sessions
```

- **PASS only if:**
  - the verdict is `SHARED_WORKERS_OBSERVED`;
  - with the golden actor IDs excluded, each worker hosted session actors of both agents;
  - every actor ran in `business-agents`.
- **2026-11-15 result (`agi-20261115T064905Z`):**
  - 14 session actors on 2 workers; each worker hosted 3 distinct session actors of
    both agents, at most 1 at a time.
  - The `new` tasks ended `failed` with `mcp_transport_error`: the MCP Gateway is held at
    0 by the profile, and S1 is not deployed.
  - Every `resume` failed after 120 s with `session actor … not reachable via
    atenet-router`. The cause is the kagent race in `resume-rca/RCA.md`: the driver sent
    follow-ups during the asynchronous suspend. The driver now waits for `Suspended`
    (section 2.3b). With that pacing, resumption was shown on GKE on 2026-11-15 (2.3b result).
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
