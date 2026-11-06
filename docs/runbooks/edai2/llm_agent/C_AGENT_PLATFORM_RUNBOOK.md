# Agent platform approval packages (Batch C-B)

This runbook takes the Batch C-B work from local code to cloud evidence. **No cloud mutation is authorized by this document, and nothing in it has run on the cluster.** The owner approves and runs each package.

Every step carries one label:

| Label | Meaning |
|---|---|
| `READ-ONLY` | Reads the cluster, changes nothing |
| `LOCAL TEST` | Runs on this machine only |
| `CLOUD MUTATION` | Requires owner approval first |

Run one heavy package at a time.

| Package | Purpose | Cost | Production impact |
|---|---|---|---|
| [P1](#p1-jenkins-cd) | Jenkins CD, rows 34–36 | Cloud Build minutes | None (`ewallet-cicd` only) |
| [P2](#p2-exp-v1-thread-binding) | EXP-V1 | about $0.43, cap $0.71 | None |
| [P3](#p3-exp-v2-routing) | EXP-V2 | about $0.80, cap $1.42 | None |
| [P4](#p4-keda) | KEDA and EXP-C5-KEDA | no new nodes | **Changes production agents** |
| [P5](#p5-isolated-mcp-failover) | EXP-C5-MCP | no new nodes | None |
| [P6](#p6-sandbox) | EXP-C5-SANDBOX | no new nodes | None until S1 |

GPU work (EXP-V3) stays blocked until the owner's quota request succeeds. See the [LLM A/B design](../../../edai2/llm/LLM_AB_DESIGN.md).

Costs use Singapore list prices read on 2026-11-05: about $0.18 per e2-standard-4 node-hour with disk and IP. Durations are estimates from historical run times plus an assumed 8–9 minutes of vLLM startup per run.

## Common setup

`READ-ONLY`

```bash
export PROJECT=ewallet-data-ai-platform-hg26
export KUBECONFIG=$(mktemp)
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --project "$PROJECT" --dns-endpoint
export KUBE_CONTEXT=gke_${PROJECT}_asia-southeast1-b_ewallet-dev
kubectl --context "$KUBE_CONTEXT" get pods -A --field-selector=status.phase=Pending
kubectl --context "$KUBE_CONTEXT" top nodes
```

Stop if anything is pending. Evidence goes in a new directory under `docs/evidence/edai2/` for each experiment; the scripts never overwrite one.

Do not use `o1_experiment.sh`, `g2_experiment.sh` or `g1_operator.sh` on this cluster. They uninstall `llm-agent` and resize `cpu-pool`, and they now refuse to run.

## P1: Jenkins CD

**Source.** Build from the merged commit, not from C-B alone: a streaming image from C-B lacks C-A's watermark fix, and the smoke can't detect that. See [the integration order](../../../edai2/platform/C_INTEGRATION_ORDER.md). MCP and Agents have no such dependency.

| Component | Job | Branch | Image | Objects in `ewallet-cicd` | Requests | PASS |
|---|---|---|---|---|---|---|
| MCP (row 34) | `edai2-cd-mcp-agents` | `release/mcp-agents` | `mcp-tools` | Deployment, Service, Job, ConfigMaps | 150m, 384Mi | Digest matches, ready 200, anonymous 401, `tools/list` 200 |
| Agents (row 35) | `edai2-cd-agents` | `release/agents` | `g3-byo-coordinator` | Redis, 2 agents, Job, ConfigMaps | 300m, 704Mi | Digest matches, cards served, malformed task rejected |
| Streaming (row 36) | `edai2-cd-streaming` | `release/streaming` | `flink-block-a` | Job, ConfigMap | 100m, 256Mi | 7 operators, 1 duplicate removed, history and online writes |

The namespace quota is 2 CPU and 6 GiB, and jobs run one at a time.

| Step | Label |
|---|---|
| `python scripts/ci/rbac_matrix.py --context "$KUBE_CONTEXT" --state before` must report 34/34 | `READ-ONLY` |
| `uv run ... pytest tests/ci` on the release commit | `LOCAL TEST` |
| Reload Jenkins with the job added in `infra/kubernetes/jenkins/jenkins.yaml` | `CLOUD MUTATION` |
| Create the three release branches at the reviewed commit | `CLOUD MUTATION` |
| Run each job with `DEPLOY_APPROVED=true`, `EXPECTED_GIT_SHA` set, and approve the input step | `CLOUD MUTATION` |
| `kubectl -n ewallet-cicd get deploy` shows zero replicas | `READ-ONLY` |

**Evidence:** `verdict.json` per job, console output, and the zero-replica readback.

**Rollback:** a failed run restores the previous objects itself. A passed run leaves workloads at zero. To remove the new job, revert its line in `jenkins.yaml`.

**Limit:** this verifies a release candidate. It doesn't update production, because Jenkins has no rights there. The [RBAC design](../../../edai2/cicd/PRODUCTION_ROLLOUT_RBAC.md) is a separate decision, and its rollout step isn't built.

## P2: EXP-V1 thread binding

Read the [safety audit](../../../edai2/llm/EXP_V1_V2_SAFETY_AUDIT.md) first.

**Creates** `bench-pool` (1 e2-standard-4, tainted) and namespace `ewallet-bench`, and removes both. About 2.4 hours, stopping at 240 minutes.

| Step | Label |
|---|---|
| `VLLM_IMAGE=... scripts/experiments/vllm_bench.sh exp-v1 plan /tmp/exp-v1-plan`, then read `plan/arm-difference.diff` | `LOCAL TEST` |
| `gcloud container node-pools list ...` shows no `bench-pool` | `READ-ONLY` |
| `EXP_APPROVED=yes scripts/experiments/vllm_bench.sh exp-v1 session docs/evidence/edai2/exp-v1-<UTC>` | `CLOUD MUTATION` |
| Compare `production-before.txt` and `production-after.txt` | `READ-ONLY` |

`VLLM_IMAGE` is `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c`.

The diff must show only `VLLM_CPU_OMP_THREADS_BIND` (`auto` versus `0-1`).

**PASS:** exit 0, `verdict.md` written, production files identical, no `bench-pool` left. Any verdict counts, including `REGRESSED`.

**Keep the machine awake.** If the host dies, nothing cleans up, so run the rollback.

**Rollback:** `scripts/experiments/vllm_bench.sh exp-v1 down /tmp/exp-v1-down` (`CLOUD MUTATION`), which deletes only objects carrying the ownership mark.

**Screenshots:** `python -m ops.experiments.cli checklist --spec configs/experiments/exp-v1-vllm-cpu.json`. Take them during the run, because the namespace is removed. Prometheus doesn't scrape `ewallet-bench`, so use Cloud Monitoring.

## P3: EXP-V2 routing

As P2, with `exp-v2` and 2 nodes, about 2.25 hours. The llm-d router is installed in `ewallet-bench` for optimized runs only, and its Role stays in that namespace.

The plan diff must show only the backend URL and the `x-ewallet-backend` label. A request served by the wrong backend marks its level `ATTRIBUTION_MISMATCH`, and the comparison refuses a verdict.

Use historical G2 results for context only. Never mix them with these runs.

## P4: KEDA

**Changes production.** Installs KEDA 2.21 (cluster-scoped CRDs, namespace `keda`, 125m CPU and 264Mi requested), rolls agents to rebuilt images, and replaces each agent's HPA with a ScaledObject.

**The order below is required.** KEDA's webhook refuses a ScaledObject while an HPA targets the same Deployment, and Helm creates new objects before deleting old ones. A plain `helm upgrade` would therefore fail.

### Local evidence

The pinned chart and the ScaledObject our chart renders ran on a local cluster on 2026-11-06, against a stub Prometheus and a stub workload.

| Check | Result |
|---|---|
| ScaledObject while the HPA exists | Refused by `vscaledobject.kb.io` |
| HPA deleted first | Accepted; `keda-hpa-fraud-coordinator` created with range 2–3 |
| Value 0 | Stays at 2 |
| Value 5, threshold 2 | 2 → 3 in about 70 s, including the stub's value update |
| Value 100 | Stays at the cap of 3 |
| Prometheus down while at 3 | `Fallback=True`; settles at 2 after about 300 s (scale-down window) |
| Prometheus back with load | 2 → 3 in about 20 s |
| Load gone | 3 → 2 in about 360 s |
| Rollback: HPA created, then ScaledObject deleted | Allowed; HPA takes over at 2 replicas |

This doesn't prove the production path: the stub skips the real gauge, ambient mTLS and both access rules, and the local cluster ran Kubernetes 1.37 where GKE runs 1.36. Steps 3 and 8 below cover that.

### Safety review

| Item | Verdict |
|---|---|
| Supported version | READY: the chart declares `>=1.23`; GKE is 1.36.4 |
| Prometheus connectivity | Needs the two access rules and the operator pod in the mesh; not yet proven |
| NetworkPolicy, AuthorizationPolicy | One added flow each, both only for `keda-operator` |
| HPA replacement | Safe in the order below; tested locally |
| Node capacity | READY: 3 extra replicas need 300m and 768Mi, and LLM nodes had 0.59 and 0.56 CPU free |
| Maximum replicas | 3 per agent, enforced locally |
| Failure recovery | Holds 2 replicas in an outage and recovers by itself; tested locally |

**Known limit:** a pod that dies mid-task leaves its gauge series in the OpenTelemetry collector for up to 5 minutes (the exporter's default). That can overstate load briefly, and the cap of 3 bounds the effect. This wasn't tested. Metrics also lag by up to about 30 s of export and polling.

### Steps

`IMAGE` digests come from P1.

| # | Step | Label |
|---|---|---|
| 1 | `python scripts/experiments/keda_preflight.py --context "$KUBE_CONTEXT"`. On 2026-11-06: six READY, two WAITING, as expected | `READ-ONLY` |
| 2 | Upgrade `llm-agent` to the rebuilt images, keeping HPAs. Rerun step 1: the gauge check must be READY | `CLOUD MUTATION` |
| 3 | One-task smoke: `EXP_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-keda baseline smoke "$EVIDENCE"`. Stop if it fails | `CLOUD MUTATION` |
| 4 | Baseline, twice: the same with `run` | `CLOUD MUTATION` |
| 5 | `scripts/llm_agent/keda_install.sh` (dry run), then `--execute`, then `kubectl rollout status` for all three `keda` Deployments | `CLOUD MUTATION` |
| 6 | `kubectl apply -f infra/kubernetes/experiments/keda-prometheus-access.yaml` | `CLOUD MUTATION` |
| 7 | Preflight with `--stage before-scaledobject`. Delete the three named HPAs, then at once upgrade `llm-agent` with `-f infra/helm/llm-agent-platform/g3/keda.yaml` | `CLOUD MUTATION` |
| 8 | `kubectl -n ewallet-agents get scaledobject` shows Ready with no fallback active: this is the first proof of the real Prometheus path | `READ-ONLY` |
| 9 | Smoke, then two runs, with `optimized` | `CLOUD MUTATION` |
| 10 | `python -m ops.experiments.cli compare --spec configs/experiments/exp-c5-keda.json --dir "$EVIDENCE"` | `LOCAL TEST` |

Each run sends at most 40 tasks at concurrency 4 through production vLLM, so check the gateway's token budget first. The smoke sends one, and its result stays out of the comparison. Replicas keep their count between steps 7's delete and upgrade, but nothing autoscales in that gap.

**Rollback** (`CLOUD MUTATION`): upgrade without `g3/keda.yaml`, which restores the HPAs (order tested). Then `kubectl delete -f` the access rules and `helm uninstall keda -n keda`.

## P5: Isolated MCP failover

**No production pod is touched.** The experiment runs the released MCP server in its own namespace, `ewallet-mcp-exp`, with a per-run token and no backends, and deletes one pod there. Both arms use the same two images; only replicas and retry attempts differ.

| | Baseline | Candidate |
|---|---|---|
| Replicas | 1 | 2 (spread across nodes) |
| `EWALLET_MCP_MAX_ATTEMPTS` | 1 | 3 |

**Footprint:** at most 250m CPU and 640Mi on non-LLM nodes for a few minutes. `MCP_IMAGE` and `PROBE_IMAGE` come from P1, and the probe needs the Specialist image, because it runs the production retry code.

| Step | Label |
|---|---|
| `scripts/experiments/mcp_experiment.sh baseline plan /tmp/mcp-plan` | `LOCAL TEST` |
| `EXP_APPROVED=yes scripts/experiments/mcp_experiment.sh baseline run "$EVIDENCE"`, twice | `CLOUD MUTATION` |
| The same with `optimized`, twice | `CLOUD MUTATION` |
| `python -m ops.experiments.cli compare --spec configs/experiments/exp-c5-mcp-failover.json --dir "$EVIDENCE"` | `LOCAL TEST` |

**PASS:** runs exit 0 with `production-before.txt` and `production-after.txt` identical.

**Rollback:** `scripts/experiments/mcp_experiment.sh baseline down /tmp/mcp-down` (`CLOUD MUTATION`).

**What this proves:** the retry logic and replica behaviour under a lost pod. It says nothing about production until the overlays are rolled out, and that rollout is a later, separate approval:

- `g3/mcp-failover.yaml` on release `llm-agent-mcp` adds 200m and 512Mi.
- `g3/mcp-gateway-failover.yaml` on `llm-agent` adds 50m and 64Mi.

## P6: Sandbox

The staged plan, identity and token design are in the [Sandbox README](../../../../infra/kubernetes/agent-sandbox/README.md). Stop at the first failed stage and record it: BYO on `SandboxAgent` is unproven.

Both arms run in `ewallet-sandbox`, so production agents aren't involved, and the baseline is an identical copy of the production Specialist. `AGENT_IMAGE` must be a rebuilt image from P1, because older images can't serve on port 80.

**Footprint:** worker 500m and 1 GiB requested, limit 1.5 CPU and 2 GiB. Node `08s1` had 1.44 CPU free on 2026-11-06.

| Step | Label |
|---|---|
| `scripts/experiments/sandbox_experiment.sh optimized plan /tmp/sb` | `LOCAL TEST` |
| Apply `namespace.yaml`, and create Secrets with placeholder values | `CLOUD MUTATION` |
| `scripts/experiments/sandbox_experiment.sh optimized check /tmp/sb` | `READ-ONLY` |
| **S0:** `EXP_APPROVED=yes ... optimized run "$EVIDENCE"`. Pass means Ready with a golden snapshot; tasks are expected to fail until S1 | `CLOUD MUTATION` |
| Add the dedicated token to `feature-rag-mcp`'s Secret; this restarts MCP pods | `CLOUD MUTATION` (production) |
| **S1:** label the namespace for ambient and apply `s1-access.yaml` | `CLOUD MUTATION` (production network rules) |
| **S2:** two runs per arm, with `EXP_RECOVERY=yes` for the recovery time | `CLOUD MUTATION` |
| `python -m ops.experiments.cli compare --spec configs/experiments/exp-c5-sandbox.json --dir "$EVIDENCE"` | `LOCAL TEST` |

The driver measures initialization, first successful A2A task, latency for new and resumed sessions, CPU and memory, worker load against its limit, MCP calls and recovery. Its pod deletion is opt-in and only inside `ewallet-sandbox`.

`successful_mcp_calls` is derived: the Specialist completes a task only when every requested tool returned valid evidence.

**Rollback:** the steps in the Sandbox README, including deleting snapshots and revoking the token.

## After the evidence

Update the rubric only from a package that finished with its evidence in place. A row becomes `COMPLETE` when its cloud run passed, and an optimization needs both arms with a real verdict. `NO_SIGNIFICANT_CHANGE` and `REGRESSED` are valid results.
