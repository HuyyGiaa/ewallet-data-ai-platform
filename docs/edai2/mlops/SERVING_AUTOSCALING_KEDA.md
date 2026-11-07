# Serving autoscaling with KEDA (G5-B)

| Item | State |
|---|---|
| KEDA on GKE for the Feature API and Fraud API | **Deployed and development-validated** (2026-11-07) |
| Final autoscaling experiment (owner-run) | **PENDING**: results and screenshots below are placeholders |

## 1. Which requirement this addresses

The EDAI-2 rubric names KEDA as its autoscaling tool. Rows 19 and 25 ask for it on the agents. Those rows stay with G5-C, which needs separate approval, and this page does not claim them.

G5-B applies KEDA to the two serving Web APIs measured for row 32 ([ROW_32_LOCUST_GKE.md](../testing/ROW_32_LOCUST_GKE.md)). It answers what happens to the row 32 throughput and latency when replicas follow load. It also proves the KEDA → Prometheus → HPA chain on GKE that G5-C will reuse.

## 2. How it works here

```text
Locust (in-cluster pod) ──► Feature API / Fraud API pods (ml-platform)
                                   │ /metrics (OpenTelemetry → Prometheus text)
Prometheus (ewallet-agents) ◄──────┘ per-pod scrape every 15 s, via headless *-metrics Services
        │ PromQL every 15 s
KEDA operator (keda) ──► external metric s0-prometheus ──► HPA keda-hpa-<api> ──► Deployment replicas
```

| Layer | What it does | Source |
|---|---|---|
| Request counters | Each lookup or prediction adds 1 to `feature_api_requests_total` or `fraud_inference_requests_total`, labelled by outcome. Health probes are not counted. | [feature_app.py (line 79)](../../../ai_platform/inference/feature_app.py#L79), [observability.py (line 163)](../../../ai_platform/inference/observability.py#L163) |
| Per-pod discovery | Headless Services `feature-api-metrics` and `fraud-api-metrics` return one address per pod | [metrics-discovery.yaml (line 17)](../../../infra/helm/ewallet-serving/templates/metrics-discovery.yaml#L17) |
| Scrape | Jobs `feature-api-pods` and `fraud-api-pods`: DNS discovery, 15 s interval (the global interval is 30 s) | [observability-stack.yaml (line 103)](../../../infra/helm/llm-agent-platform/templates/observability-stack.yaml#L103) |
| KEDA → Prometheus | The operator is enrolled in ambient, and one AuthorizationPolicy plus one NetworkPolicy admit only it to Prometheus on 9090 | [keda values (line 16)](../../../infra/helm/keda/values.yaml#L16), [keda-prometheus-access.yaml (line 16)](../../../infra/kubernetes/experiments/keda-prometheus-access.yaml#L16) |
| ScaledObject → HPA | One ScaledObject per API; KEDA generates `keda-hpa-<api>` | [scaledobjects.yaml (line 14)](../../../infra/helm/ewallet-serving/templates/scaledobjects.yaml#L14) |
| Guard | The chart refuses autoscaling without per-pod metrics, because a Service-level scrape samples one pod at a time | [scaledobjects.yaml (line 3)](../../../infra/helm/ewallet-serving/templates/scaledobjects.yaml#L3) |

## 3. Trigger, metric and thresholds

```promql
sum(rate(feature_api_requests_total{job="feature-api-pods"}[1m])) or vector(0)       # threshold 60
sum(rate(fraud_inference_requests_total{job="fraud-api-pods"}[1m])) or vector(0)    # threshold 17
```

| Question | Answer |
|---|---|
| Unit | Requests per second, summed over every pod of the API |
| Double counting | None. The `job` filter keeps one series per pod. The Fraud API is also scraped through its Service by the older `fraud-api` job; the filter excludes those series |
| Trigger type | `prometheus`, `metricType: AverageValue` ([line 50](../../../infra/helm/ewallet-serving/templates/scaledobjects.yaml#L50)) |
| HPA arithmetic | The HPA divides the query result by the current replica count and compares that with the threshold. So `desired = ceil(total req/s ÷ threshold)`, bounded to 1–3 |
| Why 60 and 17 | G5-A measured one-replica ceilings of about 86 req/s (Feature) and 24.5 req/s (Fraud), both CPU-bound at the 1-CPU limit. 70% of the ceiling scales out before a replica saturates ([values.yaml (line 45)](../../../infra/helm/ewallet-serving/values.yaml#L45)) |
| Latency trigger | Off ([line 72](../../../infra/helm/ewallet-serving/values.yaml#L72)). Under a closed load loop, latency rises with users even at matched capacity. For the Fraud API it is driven by the single KServe predictor, which more Fraud API replicas cannot relieve, so it would pin the API at 3 |
| Scale-up | No stabilization window; at most one pod per 30 s |
| Scale-down | 120 s stabilization window, then one pod per 60 s. `cooldownPeriod` applies only to scale-to-zero, which `minReplicas: 1` rules out |
| Metric failure | After 3 failed polls KEDA keeps the current count if it is higher than 1 (`currentReplicasIfHigher`) |

**Expected behaviour (reasoning, not measured):**
- **Feature API:** about 86 req/s at one replica gives `ceil(86/60) = 2`; with more capacity the rate can rise toward `ceil(170/60) = 3`.
- **Fraud API:** the singleton predictor caps total throughput near 29 req/s (34 ms per prediction), so `ceil(29/17) = 2`. A third Fraud API replica should rarely be justified, and **three Fraud API replicas will not triple throughput**.

The owner's experiment decides both.

## 4. Replica ownership

| API | Before | After |
|---|---|---|
| Feature API | Helm (`ewallet-serving`) set `replicas: 1` | The chart omits `replicas` when autoscaling is on ([feature-api.yaml (line 56)](../../../infra/helm/ewallet-serving/templates/feature-api.yaml#L56)). Revision 3 removed the field and the HPA owns it |
| Fraud API | Client-side `kubectl apply`; the last-applied annotation held `replicas: 1` | R1 handover ([fraud_api_keda_handover.sh (line 92)](../../../scripts/serving/fraud_api_keda_handover.sh#L92)): `set-last-applied` with the keda-mode manifest, a server dry run proving `replicas` stays 1, then apply. The live diff was empty: same image digest, SA, env and contracts |
| KServe predictor | 1 replica | Unchanged. No scaler (R3) |

`verify` confirmed that only `keda-hpa-fraud-api` targets the Fraud API and the annotation no longer carries `replicas`.

## 5. Deployment record (2026-11-07)

| Step | Result |
|---|---|
| KEDA 2.21.0 (`keda` release 1, chart digest verified) | 6 CRDs; operator, metrics API server and webhooks Ready; `v1beta1.external.metrics.k8s.io` Available |
| Access policies | AuthorizationPolicy and NetworkPolicy created in `ewallet-agents` |
| `ewallet-serving` r2 | `metricsDiscovery.enabled=true`: two headless Services plus Prometheus allow policies for the Feature API |
| `llm-agent` r42 | Two per-pod scrape jobs and the new `serving-keda` dashboard. Prometheus and Grafana restarted. Server dry run: no agent Deployment changed |
| Prometheus | Targets `feature-api-pods` and `fraud-api-pods` up at 15 s; both queries return 0 at idle |
| R1 | Handover and verify passed; same Fraud API pod, no restart |
| `ewallet-serving` r3 | `autoscaling.enabled=true`: ScaledObjects Ready; HPAs `ScalingActive=True (ValidMetricFound)` |
| Regression | One Feature lookup and one Fraud prediction: HTTP 200, the same values as Package 4. Valkey HEALTHY. A2A `completed` |

Evidence: [`docs/evidence/edai2/keda/g5b_20261107T204930Z`](../../evidence/edai2/keda/g5b_20261107T204930Z/README.md).

**Development check, not the owner's experiment:** Feature API, 10 users for 2 minutes.

| Time after the Locust start | Event |
|---|---|
| +63 s | The KEDA metric reached 77.2 req/s, so `desired = 2` |
| +110 s | The second pod was Ready (Feature API startup takes about 45 s) |
| After the load | The HPA scaled back to 1; event "New size: 1; reason: … below target" |

At 2 replicas the HPA showed 42.9 req/s per pod, which is 85.8 ÷ 2: the `AverageValue` arithmetic as expected. This proves the chain reacts; it is not a performance result.

## 6. Reproducing the experiments

The owner runs them; see [G5_B_OWNER_EVIDENCE_GUIDE.md](../../runbooks/edai2/G5_B_OWNER_EVIDENCE_GUIDE.md). Each run uses the G5-A in-cluster Locust runner and fixture: 20 users, spawn rate 5/s, 5 minutes, the same as the G5-A "high" profile. The runner records replicas and HPA decisions every 5 s and computes the timings from those samples ([serving_autoscale_experiment.sh (line 52)](../../../scripts/experiments/serving_autoscale_experiment.sh#L52), [autoscale_summary.py](../../../ops/experiments/autoscale_summary.py)).

## 7. Results (owner-supplied; PENDING)

| API | Arm | Requests | Failure rate | Avg req/s | Steady req/s at peak replicas | p50 / p95 / p99 ms | Peak replicas | First scale-out | Back to 1 replica |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Feature | G5-A fixed, 1 replica | 25,369 | 0% | 84.7 | n/a | 230 / 330 / 400 | 1 | n/a | n/a |
| Feature | KEDA 1–3 | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ |
| Fraud | G5-A fixed, 1 replica | 6,743 | 0% | 22.5 | n/a | 840 / 1,300 / 1,400 | 1 | n/a | n/a |
| Fraud | KEDA 1–3 | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ | _[owner]_ |

The average over the whole run includes the cold scale-up period. The steady-state column covers only the time after replicas peaked.

Screenshots (owner-captured; placeholders):

- _[G5B-01: ScaledObject and HPA configuration]_
- _[G5B-02: Feature API at 1 Ready replica before load]_
- _[G5B-03: Feature API HPA scaling out, extra pods Ready]_
- _[G5B-04: Feature API back at 1 replica]_
- _[G5B-05 … 08: the same for the Fraud API]_
- _[G5B-09: Locust reports of both KEDA runs]_

## 8. Rollback (live revisions read 2026-11-07)

| Change | Command |
|---|---|
| Autoscaling | `helm rollback ewallet-serving 2 -n ml-platform` (removes the ScaledObjects and their HPAs, and Helm sets the Feature API back to `replicas: 1`), then the R1 rollback |
| R1 | `scripts/serving/fraud_api_keda_handover.sh rollback <dir> --execute` (refuses while a ScaledObject exists) |
| Metrics discovery | `helm rollback ewallet-serving 1 -n ml-platform` |
| Prometheus scrape | `helm rollback llm-agent 41 -n default` (Prometheus and Grafana restart) |
| KEDA access | `kubectl delete -f infra/kubernetes/experiments/keda-prometheus-access.yaml` |
| KEDA | Delete the ScaledObjects first, then `helm uninstall keda -n keda` and the `keda.sh` CRDs |

## 9. Limitations

- **The KServe predictor stays at one replica.** It limits Fraud throughput whatever the Fraud API replica count.
- **A new Feature or Fraud API pod needs about 45 s to become Ready.** Short bursts end before new capacity arrives.
- **Closed-loop load.** Locust users wait for responses, so offered load grows only with capacity. Throughput at 3 replicas measures capacity, not demand.
- **The `serving-keda` dashboard's p95 panels are unreliable.** The API histograms use OpenTelemetry's default bucket edges with seconds as the unit (see row 32). Use Locust's percentiles instead.
- **Server metrics in each run's `server-metrics.json` come from one pod.** The runner scrapes one pod through a port-forward. Use Prometheus for totals across replicas.
- **The Feature API now admits only listed clients** (Prometheus, and the temporary runner while it exists), the same as the Fraud API.
