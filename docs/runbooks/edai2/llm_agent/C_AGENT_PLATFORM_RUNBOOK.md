# Agent platform completion runbook (Batch C-B)

This runbook takes the Batch C-B work from local code to cloud evidence. **Nothing in it has run on the cluster yet.** Every package below needs the owner's approval before it starts.

Run the packages in this order. Each one lists what it changes, what it costs, how to check it and how to undo it. Run one heavy package at a time.

| Package | Purpose | New cost | Touches production |
|---|---|---|---|
| [P1](#p1-jenkins-cd) | Jenkins CD for rows 34–36 | Cloud Build minutes | No (`ewallet-cicd` only) |
| [P2](#p2-exp-v1-vllm-thread-binding) | EXP-V1 | about $0.43, cap $0.71 | No |
| [P3](#p3-exp-v2-kubernetes-service-versus-llm-d) | EXP-V2 | about $0.80, cap $1.42 | No |
| [P4](#p4-keda) | KEDA and EXP-C5-KEDA | no new nodes | Yes |
| [P5](#p5-mcp-failover) | MCP failover and EXP-C5-MCP | no new nodes | Yes, deletes one MCP pod |
| [P6](#p6-sandbox) | Business agent in Sandbox | no new nodes | Adds network rules |
| [P7](#p7-gpu) | EXP-V3 on L4 | about $1.63 for 3 h Spot | No, blocked by quota |

Costs use the Singapore list prices checked on 2026-11-05: e2-standard-4 at $0.1653/h, plus 50 GB pd-balanced and one IP, for about $0.18 per node-hour. Durations are estimates from the historical runs' wall time plus an assumed 8–9 minutes of vLLM startup per run.

## Common setup

```bash
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export VLLM_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c
```

Before any package, confirm that nothing is pending and the DP2 and LLM workloads are healthy:

```bash
kubectl --context "$KUBE_CONTEXT" get pods -A --field-selector=status.phase=Pending
kubectl --context "$KUBE_CONTEXT" top nodes
```

Evidence goes under `docs/evidence/edai2/`, one new directory per experiment. The scripts refuse an existing directory, so a run cannot replace older evidence.

Do not use `o1_experiment.sh`, `g2_experiment.sh` or `g1_operator.sh` on this cluster. They uninstall `llm-agent` and resize `cpu-pool`, and they now refuse to run while that release exists.

## P1: Jenkins CD

**Changes:** Jenkins configuration gains the job `edai2-cd-agents`. Three CD runs build images and create temporary objects in `ewallet-cicd`, which are parked at zero replicas on success. Jenkins RBAC does not change.

**Prerequisites:**

- The branch is merged or pushed to `release/mcp-agents`, `release/agents` and `release/streaming`, each at one reviewed commit.
- Live Jenkins picks up `infra/kubernetes/jenkins/jenkins.yaml`.
- The agents run needs `PROMPT_REGISTRY_URL`.

**Resources:** the largest run, agents, requests 300m CPU and 704Mi across Redis, two agents and the smoke Job, inside the 2 CPU / 6Gi namespace quota.

**Steps:** in Jenkins, run each job with `DEPLOY_APPROVED=true`, `EXPECTED_GIT_SHA` set to the release commit, and approve at the input step. Run the jobs one at a time.

| Job | Row | PASS means |
|---|---|---|
| `edai2-cd-mcp-agents` | 34 | digest readback matches, `/ready` 200, anonymous 401, `tools/list` 200 |
| `edai2-cd-agents` | 35 | digest readback matches, agent cards served, malformed task rejected |
| `edai2-cd-streaming` | 36 | smoke summary shows operators, duplicate removal, history and online updates |

**Evidence:** `verdict.json` per run, Jenkins console output, and `kubectl get deploy -n ewallet-cicd` showing zero replicas afterwards.

**Rollback:** a failed run restores the previous objects by itself. To remove the new job, revert the `jenkins.yaml` line.

**Scope limit:** these runs verify the release in `ewallet-cicd`. They do not update production agents, because Jenkins has no rights in those namespaces. That needs a separate RBAC decision.

## P2: EXP-V1 vLLM thread binding

**Changes:** creates `bench-pool` (1 e2-standard-4, tainted) and namespace `ewallet-bench`, runs both arms, and removes both. About 2.4 hours; the script stops at 240 minutes.

**Preview, no cluster access:**

```bash
scripts/experiments/vllm_bench.sh exp-v1 plan /tmp/exp-v1-plan
cat /tmp/exp-v1-plan/plan/arm-difference.diff
```

The diff must show only `VLLM_CPU_OMP_THREADS_BIND`.

**Run:**

```bash
EXP_APPROVED=yes scripts/experiments/vllm_bench.sh exp-v1 session docs/evidence/edai2/exp-v1-$(date -u +%Y%m%dT%H%M%SZ)
```

**PASS means:** exit code 0, `verdict.md` written, `production-before.txt` identical to `production-after.txt`, and no `bench-pool` left.

**If it was interrupted:**

```bash
scripts/experiments/vllm_bench.sh exp-v1 down /tmp/exp-v1-down
gcloud container node-pools list --cluster ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26
```

**Screenshots:** print the list with `python -m ops.experiments.cli checklist --spec configs/experiments/exp-v1-vllm-cpu.json`. Take them during the run, because the namespace is removed at the end. Prometheus does not scrape `ewallet-bench`, so resource panels come from Cloud Monitoring, which already collects cAdvisor and kubelet metrics for the cluster.

## P3: EXP-V2 Kubernetes Service versus llm-d

**Changes:** same as P2 with 2 nodes, plus the llm-d standalone router installed in `ewallet-bench` during optimized runs only. The router's Role stays inside that namespace. About 2.25 hours.

```bash
scripts/experiments/vllm_bench.sh exp-v2 plan /tmp/exp-v2-plan
EXP_APPROVED=yes scripts/experiments/vllm_bench.sh exp-v2 session docs/evidence/edai2/exp-v2-$(date -u +%Y%m%dT%H%M%SZ)
```

The plan diff must show only the backend URL and the `x-ewallet-backend` label. A request served by any other backend marks the level `ATTRIBUTION_MISMATCH`, and the comparison then refuses a verdict.

## P4: KEDA

**Changes:** installs KEDA 2.21 (cluster-scoped CRDs, namespace `keda`, 125m CPU / 264Mi requested). Upgrades production agents to images that export the in-flight gauge. Replaces each agent HPA with a ScaledObject.

**Capacity:** the third coordinator replica needs 100m / 256Mi. The LLM-labelled nodes had 0.59 and 0.56 CPU free on 2026-11-06.

**Order matters.** The baseline must run on the same rebuilt image as the candidate, so build first.

1. Build the agent images through P1 and record their digests.
2. Upgrade `llm-agent` with those digests, keeping the HPA. Check that `ewallet_agent_inflight_tasks` appears in Prometheus.
3. Measure the baseline, twice:
   ```bash
   EXP_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-keda baseline run "$EVIDENCE"
   ```
4. Install KEDA and the access rules:
   ```bash
   scripts/llm_agent/keda_install.sh            # dry run
   scripts/llm_agent/keda_install.sh --execute
   kubectl --context "$KUBE_CONTEXT" apply -f infra/kubernetes/experiments/keda-prometheus-access.yaml
   ```
5. Upgrade `llm-agent` with `-f infra/helm/llm-agent-platform/g3/keda.yaml`. Check that `kubectl get scaledobject -n ewallet-agents` shows Ready and that no plain-named HPA remains.
6. Measure the candidate twice, with `optimized` in place of `baseline`.
7. Compare: `python -m ops.experiments.cli compare --spec configs/experiments/exp-c5-keda.json --dir "$EVIDENCE"`

Each run sends at most 40 tasks at concurrency 4 through production vLLM, so check the gateway's remaining token budget first. The task path has never been load-tested from a Job: try one run with `EXP_MAX_REQUESTS=1` before the first measured run. That run proves the load image has `python3` and that the new AuthorizationPolicy admits the Job.

**Unproven until run:** operator-only ambient enrolment, and whether the Prometheus query matches the gauge's labels. If the scaler cannot query, KEDA's `fallback` holds 2 replicas.

**Rollback:** remove `g3/keda.yaml` from the release, which renders the HPAs again. Then delete the access rules and `helm uninstall keda -n keda`.

## P5: MCP failover

**Changes:** `feature-rag-mcp` and `drift-mcp` go to 2 spread replicas in release `llm-agent-mcp`. `mcp-gateway` goes to 2 replicas in release `llm-agent`. This adds 250m CPU and 576Mi in total. The measured run **deletes one production MCP pod**, so failed tool calls are expected during the baseline.

`agentgateway` does not retry MCP backends; a local test on v1.7.0 got one backend call and a 503. Failover is therefore a client retry, limited to read-only tools.

**Order:** both arms run the same rebuilt Specialist image.

1. Baseline: keep 1 replica and add `-f g3/mcp-retry-off.yaml` to the `llm-agent` upgrade. Run twice:
   ```bash
   EXP_APPROVED=yes EXP_DISRUPT_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-mcp-failover baseline run "$EVIDENCE"
   ```
2. Candidate: remove `mcp-retry-off.yaml`, then apply `g3/mcp-failover.yaml` to `llm-agent-mcp` and `g3/mcp-gateway-failover.yaml` to `llm-agent`. Run twice with `optimized`.
3. Compare with `configs/experiments/exp-c5-mcp-failover.json`.

The script stops if replica count or `EWALLET_MCP_MAX_ATTEMPTS` does not match the arm.

**Rollback:** remove the overlays and upgrade both releases.

## P6: Sandbox

Follow the stages in [`infra/kubernetes/agent-sandbox/README.md`](../../../../infra/kubernetes/agent-sandbox/README.md). Stop at the first failed stage, and record the failure as the result: BYO on `SandboxAgent` is unproven.

Run S0 first, with only its two Secrets. Apply `worker-egress.yaml` only after S0 passes, because that step changes `ewallet-agents` network rules.

EXP-C5-SANDBOX compares runtime cost. Its hypothesis has no expected winner: gVisor is an isolation choice, and slower can be an acceptable result.

## P7: GPU

**Blocked.** `GPUS_ALL_REGIONS` is 0, although the regional L4 quotas are 1. Raising it is the owner's decision; see [LLM A/B design](../../../edai2/llm/LLM_AB_DESIGN.md).

## After the evidence

Update the rubric only from a package that finished with its evidence in place. A row changes to `COMPLETE` when its cloud run passed, and an optimization needs both arms with a real verdict. `NO_SIGNIFICANT_CHANGE` and `REGRESSED` are valid results and should be reported as they are.
