# G5-B: KEDA serving autoscaling deployment, 2026-11-07

Deployment and development validation only. **The final autoscaling experiment is owner-run and not here** (see [G5_B_OWNER_EVIDENCE_GUIDE.md](../../../../runbooks/edai2/G5_B_OWNER_EVIDENCE_GUIDE.md)). Page: [SERVING_AUTOSCALING_KEDA.md](../../../../edai2/mlops/SERVING_AUTOSCALING_KEDA.md).

| Path | Content |
|---|---|
| `preflight/` | Cluster, serving, Redis, KServe and Valkey state; capacity calculation; KEDA install dry run |
| `keda/` | KEDA install output, CRDs, pods, external metrics API, access-policy apply |
| `serving/` | Helm upgrade results; the exact Helm arguments (`helm-args-g5b.txt`); the server dry-run diff that enabled autoscaling; ScaledObjects and HPAs after enabling |
| `prometheus/` | `llm-agent` upgrade; scrape targets and the KEDA queries evaluated against live Prometheus |
| `r1/` | R1 handover: ownership readback, empty keda-mode diff, handover and verify log |
| `validation/smoke/` | One Feature lookup and one Fraud prediction after deployment |
| `validation/valkey/`, `validation/a2a/` | Valkey readback (HEALTHY) and synthetic A2A (`completed`) |
| `validation/dev-reaction-not-submission/` | Development check that the HPA reacts to real metrics: 10 users for 2 minutes, Feature API only. **Not submission evidence** |
