# G5-A: real Locust baseline on GKE, 2026-11-07

Submission page: [ROW_32_LOCUST_GKE.md](../../../../edai2/testing/ROW_32_LOCUST_GKE.md).

| Path | Content |
|---|---|
| `preflight.txt` | GKE operations, nodes, serving readiness, digests, Redis, no autoscaler, fixture provenance |
| `runner-up.txt`, `runner-down.txt` | Temporary runner created on node `zm4y`; afterwards deleted, with serving unchanged |
| `matrix.log` | Order and guards of the six 5-minute runs (no GKE operation, no restart, failure rate) |
| `<target>-incluster-<profile>/` | Per run: |
| | - `report.html` (Locust's own report); |
| | - `locust_stats*.csv`, `locust_failures.csv`, `terminal.log`; |
| | - `run-manifest.json` and `summary.json`; |
| | - `server-metrics.json` (API histogram deltas) and `kserve-metrics.json` (predictor, Fraud runs only); |
| | - `metrics/` raw before and after scrapes; |
| | - `resources-*.jsonl` (`kubectl top` every 10 s); |
| | - `state-before.txt` / `state-after.txt` (nodes and restart counts). |

`summary.json` takes its totals from `report.html`. `locust_stats.csv` is Locust's last periodic write and can be a few requests short.
