# G2 runbook: llm-d routing vs standard routing (two C1 replicas)

Report: [docs/edai2/llm/llmd_g2_routing_benchmark.md](../../archive/legacy-docs/docs/edai2/llm/llmd_g2_routing_benchmark.md).
Design and compatibility decisions are in that report, §4–6.

## What runs

| Run | Path (agentgateway authenticates and rate-limits in both) |
|---|---|
| B2 | client → agentgateway → Service `vllm-primary` (connection-level load balancing) → 2 × C1 vLLM |
| O2 | client → agentgateway → `llmd-router-epp:8081` (Envoy + llm-d EPP v1.0.0, standalone) → 2 × C1 vLLM |

What is the same in both runs:
- **Serving:** the C1 vLLM Deployment (checked by `g2_preflight.py` and by
  the live readback `g2/<run>/serving.json`) and fresh vLLM pods before each run.
- **Workload:** `o1-workload-v1` at concurrency 1, 2, 4 and 8.
- **Gateway:** the token budget (`g2/benchmark-gateway.yaml`, 150 000/h,
  session only) and the fallback.

Files:

| Purpose | Path |
|---|---|
| Overlays | `infra/helm/llm-agent-platform/g2/{common,b2,o2,benchmark-gateway}.yaml` |
| Router values | `infra/helm/llm-d/standalone-values-g2.yaml` |
| Pins | `infra/helm/llm-d/versions.env` (`LLMD_STANDALONE_*`) |
| Scripts | `scripts/llm_agent/g2_{experiment.sh,preflight,benchmark,readback,compare}.py`, `ops/llm_agent/g2_guard.py` |
| Tests | `tests/llm_agent/test_g2.py`, simulated cluster in `tests/llm_agent/g2_sim/` |

## Offline checks (no cluster)

```bash
d=$(mktemp -d)
helm pull oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v1.0.0 -d "$d"
echo "c4787dcda88cae57124d8eaf7672d56e9993ce25d6ce62d8a5f71097640c09d3  $d/llm-d-router-standalone-v1.0.0.tgz" | sha256sum -c
G2_ROUTER_CHART=$d/llm-d-router-standalone-v1.0.0.tgz python3 -m pytest -q tests/llm_agent/test_g2.py
G2_DRY_RUN=yes scripts/llm_agent/g2_experiment.sh "$(mktemp -d)"   # local preflight only
```

The tests run `g2_experiment.sh` end to end against fake
`kubectl`/`gcloud`/`helm`, using the real `helm template`, a mock gateway and a
mock EPP. They cover:
- the full campaign;
- gate, deploy and rollout failures;
- missing metrics (the run stops before O2);
- O2 served by failover (recorded as INCONCLUSIVE);
- SIGTERM during the benchmark (exits 130, cleans up, no leaked process).

## Re-deriving the accepted results (no cluster)

The accepted run is `docs/evidence/llm_agent/g2_20261101T124444Z`, produced by
source `0c89af3`. Verify it, then recompute on a **copy**: `g2_compare.py`
rewrites `comparison.{json,md}` in the directory it is given, and the
originals must stay untouched.

```bash
(cd docs/evidence/llm_agent/g2_20261101T124444Z && sha256sum -c --quiet MANIFEST.sha256)
t=$(mktemp -d); cp -r docs/evidence/llm_agent/g2_20261101T124444Z/g2 "$t/"
python3 scripts/llm_agent/g2_compare.py "$t/g2"   # verdict MIXED, gains 4/8, regressions 1/2
```

## Cloud run (owner approval required)

```bash
cd <clean checkout at the G2 source commit>
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export DIGESTS=docs/evidence/llm_agent/o1_20261101T052939Z/image-digests.json G2_APPROVED=yes
E=docs/evidence/llm_agent/g2_$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$E"
scripts/llm_agent/g2_experiment.sh "$E"
```

The run grows `cpu-pool` from 2 to exactly 5 nodes. It labels the three new
nodes `ewallet.io/g2-llm=true`, and every G2 pod is restricted to those nodes.
It always cleans up, whether it succeeds, fails or is interrupted:
1. uninstalls both releases;
2. drains the three labelled instances and deletes exactly those
   (`delete-instances`, never a resize that could pick an MLOps node);
3. waits until their Node objects are gone;
4. reads back the pool, the releases, the namespace objects and MLOps health.

A failed step stops the run. There is no automatic retry.

Recovery after host loss, from any authenticated machine:

```bash
helm uninstall llmd-router --kube-context $KUBE_CONTEXT -n ewallet-agents
helm uninstall llm-agent --kube-context $KUBE_CONTEXT -n default
for n in $(kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/g2-llm=true -o name | sed 's|node/||'); do
  kubectl --context $KUBE_CONTEXT drain $n --ignore-daemonsets --delete-emptydir-data; done
gcloud compute instance-groups managed delete-instances <cpu-pool MIG> --zone asia-southeast1-b \
  --instances=<the labelled node names>
```
