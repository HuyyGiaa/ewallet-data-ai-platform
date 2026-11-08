# G5-B owner-run autoscaling experiments, 2026-11-08

Executed by the owner with `scripts/experiments/serving_autoscale_experiment.sh run <api>` (guide: [G5_B_OWNER_EVIDENCE_GUIDE.md](../../../../runbooks/edai2/G5_B_OWNER_EVIDENCE_GUIDE.md)). Analysis: [SERVING_AUTOSCALING_KEDA.md](../../../../edai2/mlops/SERVING_AUTOSCALING_KEDA.md) sections 8–11.

| API | Locust | Autoscaling |
|---|---|---|
| `feature-api/` | `feature-incluster-high/summary.json`: 24,270 requests, 0 failures, 81.071 req/s, p50/p95/p99 240/350/440 ms | `autoscale-summary.json`: 1 → 2 → 1; first scale-out 69.5 s; back to 1 after 137.0 s; steady 82.42 req/s over 225.5 s |
| `fraud-api/` | `inference-incluster-high/summary.json`: 6,595 requests, 0 failures, 21.985 req/s, p50/p95/p99 860/1,300/1,400 ms | 1 → 2 → 1; 81.4 s; 152.5 s; 22.14 req/s over 214.6 s |

Each API folder also holds:
- `replicas.jsonl`: Ready replicas and HPA decisions every 5 s;
- `hpa-before.yaml`, `hpa-after.yaml`, `hpa-describe.txt`, `hpa-events.txt`;
- `scaledobject.yaml`, `state-before.txt`, `state-after.txt`, `deployment-events.txt`;
- in the Locust run folder: `report.html`, the CSVs, `run-manifest.json`, resource samples and server metrics.

`server-metrics.json` is scraped from one pod.
