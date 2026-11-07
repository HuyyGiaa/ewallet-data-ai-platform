# G5-B owner evidence guide: serving autoscaling

You run these experiments and capture the screenshots yourself. Everything is already deployed; nothing here installs or reconfigures anything. Background and results table: [SERVING_AUTOSCALING_KEDA.md](../../edai2/mlops/SERVING_AUTOSCALING_KEDA.md).

## 0. Setup (once per session)

```bash
cd ~/Develop/ewallet-data-ai-platform/.worktrees/main-integration
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
kubectl config use-context "$KUBE_CONTEXT"
export UV=~/miniconda3/envs/fintech/bin/uv      # the runner exports the locked Locust requirements with uv
export IMAGE_DIGEST=sha256:dbaf8b7060e6bebf477f2bff02dfede382102410b4ab68b51e836de9f7c1c803 EWALLET_LOAD_MODEL_VERSION=1
EV=docs/evidence/edai2/keda/owner_$(date -u +%Y%m%dT%H%M%SZ)   # one folder for the session
```

Your public IP must be one of the cluster's authorized networks, or `kubectl` times out.

**Grafana** (optional, for the replica panel):
```bash
kubectl -n ewallet-agents port-forward svc/grafana 3000:3000
kubectl -n ewallet-agents get secret ewallet-grafana-admin -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n ewallet-agents get secret ewallet-grafana-admin -o jsonpath='{.data.password}' | base64 -d; echo
```
Open http://localhost:3000 with that user name and password, dashboard **E-Wallet Serving Load and Autoscaling**.
- Use the panels *Feature API request rate*, *Fraud API request rate*, *Replicas scraped* and *Serving CPU*.
- **Do not use the p95 panels:** the API histograms are too coarse. Take percentiles from Locust.

**Stop and report instead of continuing if:**
- pods crash-loop;
- the Locust failure rate is above 5%;
- KServe or Redis is not Ready;
- a GKE operation starts: `gcloud container operations list --zone asia-southeast1-b --filter='status!=DONE'`.

## 1. Experiment A: Feature API

Run it in three terminals.

| Step | Terminal | Command | Expected |
|---|---|---|---|
| 1 | T1 | `scripts/experiments/serving_autoscale_experiment.sh status feature-api` | ScaledObject `READY True`; HPA `0/60 (avg)`, `REPLICAS 1`; Deployment `1/1`; one pod |
| 2 | T2 | `scripts/experiments/serving_autoscale_experiment.sh watch feature-api` | Refreshes every 5 s: metric, current and desired replicas, pods |
| 3 | T3 | `scripts/experiments/serving_autoscale_experiment.sh run feature-api "$EV"` | Starts the runner pod if needed (about 1 minute), then 5 minutes of Locust, then waits for scale-down (up to 12 minutes) |
| 4 | T1, during the load | `kubectl -n ml-platform describe hpa keda-hpa-feature-api \| tail -15` | Events `New size: 2 … above target`, possibly `New size: 3` |
| 5 | T1, after the load | `kubectl -n ml-platform get events --field-selector involvedObject.name=keda-hpa-feature-api --sort-by=.lastTimestamp` | `New size: … below target`, down to 1 |

`run` refuses to start unless the ScaledObject is Ready, the API is at 1 replica and no GKE operation is running. It prints progress and ends with the paths of the summaries.

**Effects:**
- `run` creates the temporary pod `real-load-runner`, three ConfigMaps and two NetworkPolicies in `ml-platform`.
- It sends 20 users for 5 minutes; KEDA changes the replica count.
- The script changes nothing else.

## 2. Experiment B: Fraud API

Start only after Experiment A's API is back at 1 replica. Repeat section 1 with `fraud-api`; the threshold reads `0/17 (avg)`. Expect scale-out to 2. A third replica is possible but limited: the single KServe predictor caps total throughput. Record what actually happens.

## 3. Experiment C: comparison with G5-A

There is nothing extra to run. Each `run` above uses the G5-A "high" profile: 20 users, spawn rate 5/s, 5 minutes, the same fixture and the same in-cluster runner. Compare each `summary.json` with the G5-A "high" runs:

| Value | KEDA run (yours) | G5-A fixed run |
|---|---|---|
| Requests, failure rate, req/s, p50/p95/p99 | `$EV/<api>/<target>-incluster-high/summary.json` | `docs/evidence/edai2/load/g5a_20261107T183037Z/<target>-incluster-high/summary.json` |
| Replicas over time, HPA metric and decisions | `$EV/<api>/replicas.jsonl` | Always 1 |
| First scale-out, time to peak, scale-down to 1, steady-state req/s at peak replicas | `$EV/<api>/autoscale-summary.json` | n/a |
| CPU and memory per pod (10 s samples) | `$EV/<api>/<target>-incluster-high/resources-ml-platform.jsonl` | Same file in G5-A |
| HPA events, describe, before and after state | `$EV/<api>/hpa-events.txt`, `hpa-describe.txt`, `state-before.txt`, `state-after.txt` | n/a |
| Locust HTML report | `$EV/<api>/<target>-incluster-high/report.html` | Same file in G5-A |

`<target>` is `feature` or `inference`.

**Cold versus steady state:** the run average includes the time before new pods were Ready, which is about 45 s per pod. `steady_state_at_peak_replicas.mean_requests_per_second` covers only the time after replicas peaked. Report both and label them.

Fill in section 7 of the G5-B page only from these files.

## 4. Experiment D: cleanup

```bash
scripts/experiments/serving_autoscale_experiment.sh cleanup
kubectl -n ml-platform get pod,netpol,cm -l ewallet.io/temporary=real-load   # expect: No resources found
```

This removes only the temporary runner pod, its ConfigMaps and its two NetworkPolicies. KEDA, the ScaledObjects, the HPAs, the serving Deployments, Prometheus and every evidence folder stay.

## 5. Screenshots to capture

Capture the real terminal or browser window. Keep the cluster name or prompt visible where possible.

| # | Proves | Command or UI | Capture when | Filename | PASS if |
|---|---|---|---|---|---|
| G5B-01 | KEDA configuration | `kubectl -n ml-platform get scaledobject -o wide; kubectl -n ml-platform get hpa`, then `kubectl -n ml-platform get scaledobject feature-api -o yaml \| sed -n '/^spec:/,$p'` | Before any load | `g5b_01_keda_config.png` | Both ScaledObjects `READY True`; HPAs `/60 (avg)` and `/17 (avg)`, min 1, max 3; trigger `prometheus`, `AverageValue` |
| G5B-02 | Baseline | Experiment step 1 (`status feature-api`) | Before `run` | `g5b_02_feature_baseline.png` | Deployment `1/1`, one pod, HPA `REPLICAS 1` |
| G5B-03 | Scale-out | T2 `watch` and T1 `describe hpa` | Two or three pods Running and Ready under load | `g5b_03_feature_scale_out.png` | Metric above 60, desired above 1, extra pods `1/1 Running`, a `New size` event |
| G5B-04 | Scale-down | T2 `watch` plus the events command | After the load, once back at 1 | `g5b_04_feature_scale_down.png` | One pod; a `below target` event |
| G5B-05 … 08 | The same for the Fraud API | Same, with `fraud-api` | Same | `g5b_05` … `g5b_08_fraud_*.png` | Same, against 17 |
| G5B-09 | Performance | Open `report.html` of each KEDA run in your browser | After each run | `g5b_09_feature_locust.png`, `g5b_10_fraud_locust.png` | Real report: requests, 0 or low failures, RPS and response-time charts |
| G5B-11 (optional) | Replicas follow load | Grafana: *Replicas scraped* beside *request rate* over the run | After the run, time range covering it | `g5b_11_grafana_replicas.png` | The replica step lines up with the rate rise and fall |

Save the images under `docs/evidence/edai2/screenshots/keda/`. Review them with ChatGPT, then replace the placeholders on the G5-B page.

## 6. PASS and FAIL for the whole experiment

| Check | PASS | FAIL |
|---|---|---|
| Scale-out | `peak_ready_replicas` ≥ 2 during the load | Stays at 1 while the metric is above the threshold. Check `hpa-describe.txt` conditions |
| Scale-down | `returned_to_min: true` | Still above 1 after 12 minutes |
| Availability | Locust failure rate 0% or near 0, all failures explained | Sustained failures |
| Health | No restarts in `state-after.txt`; KServe and Redis Ready | Restarts or unhealthy dependencies |
