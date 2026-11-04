# O1 — matched CPU vLLM optimization on the shared GKE pool

O1 extends G1 (`docs/llm_agent/gke_cpu_runtime.md`, `g1_runtime_report_20261101.md`);
it reuses the G1 image, chart, gateway, keys, operator stages and smoke tests.

## Capacity decision (read 2026-11-01, before deploying)

The 2 `cpu-pool` nodes run MLOps (KFP, MLflow, Feast Redis, KServe, fraud-api).
Per node, allocatable 3.92 CPU / 12.96 GiB; requested 2.36 / 5.77 and 2.21 / 4.85;
after a 0.3 CPU / 0.75 GiB headroom the safe O1 budget is ~1.3-1.4 CPU per node.
The G1 configuration needs 2 x (2 CPU / 6 GiB) for primary + fallback, so on
those nodes it is `BLOCKED_INSUFFICIENT_SAFE_CAPACITY`. The owner approved two
temporary nodes for the O1 session (same as G1); MLOps keeps its two nodes.

## Isolation

- The pool grows from 2 to exactly 4 nodes; the two new ones get
  `ewallet.io/o1-llm=true`; vLLM and agentgateway carry that nodeSelector
  (`o1/placement.yaml`). No tolerations, no PriorityClass, no autoscaling.
- `ops/llm_agent/o1_guard.py capacity`: every O1 request (primary, fallback,
  gateways) must fit the labelled nodes after existing requests and headroom.
- `o1_guard.py guard` (every 15 s) stops the benchmark when: an MLOps node
  exceeds 85 % CPU or 90 % memory, an O1 node exceeds 92 % memory, vLLM exceeds
  9.5 GiB (95 % of its limit), an MLOps pod stops being Ready, restarts or moves,
  an InferenceService stops being Ready, a node reports pressure, or an
  OOM/eviction/preemption event appears.
- MLOps health snapshots before, during and after (`mlops-health-*.json`).
- Cleanup drains and deletes exactly the two labelled instances
  (`delete-instances`), never a resize, so GKE cannot pick an MLOps node.

## Experiments (same resources: 2/3 CPU, 6/10 GiB, KV 2 GiB, 1 active backend)

| Config | Single change vs B0-prime |
|---|---|
| `b0-prime` | none (G1 as it ran: prefix caching on, OMP bind `auto`) |
| `c1-thread-bind` | `VLLM_CPU_OMP_THREADS_BIND=0-1` (G1 `auto` bound inference to one core) |
| `c2-batched-tokens` | `--max-num-batched-tokens 512` |
| `c3-no-prefix-cache` | prefix caching off (it is on in B0-prime) |

Workload (`scripts/llm_agent/o1_benchmark.py`, `o1-workload-v1`, sha256 in each
summary): short and long chats, a repeated 700-token prefix, unique prefixes,
real tool calls (stream) and a non-streamed chat; a cold-prefix phase, a
warm-up pass, then concurrency 1, 2 and 4. Selection rules: `scripts/llm_agent/o1_compare.py`.

```bash
cd <checkout at the O1 commit>
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export DIGESTS=docs/evidence/llm_agent/g1_20261031T212736Z/image-digests.json O1_APPROVED=yes
E=docs/evidence/llm_agent/o1_$(date -u +%Y%m%dT%H%M%SZ); mkdir -p $E; cp $DIGESTS $E/
scripts/llm_agent/o1_experiment.sh $E
```

Recovery after host loss (from any authenticated machine): uninstall the
release, then delete only the instances labelled `ewallet.io/o1-llm=true`:

```bash
helm uninstall llm-agent --kube-context $KUBE_CONTEXT -n default
for n in $(kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/o1-llm=true -o name | sed 's|node/||'); do
  kubectl --context $KUBE_CONTEXT drain $n --ignore-daemonsets --delete-emptydir-data; done
gcloud compute instance-groups managed delete-instances <cpu-pool MIG> --zone asia-southeast1-b \
  --instances=<the labelled node names>
```
