# Package 4: Feature API and Fraud API on GKE (2026-11-07)

Real backends only: Feast Online on Redis, KServe `fraud-detector` v1. Image `fraud-api@sha256:dbaf8b7060e6bebf477f2bff02dfede382102410b4ab68b51e836de9f7c1c803`, built from `5801bcf`.

## Changes made

| Resource | Change |
|---|---|
| KSA `ml-platform/fraud-api` | Created; annotated `ewallet-fraud-api@…`; automount off |
| KSA `ml-platform/feature-api` | Created with Helm ownership metadata, then adopted by release `ewallet-serving` |
| Deployment `ml-platform/fraud-api` | Rendered `fixed` mode, 1 replica. Fields changed: image, SA `training`→`fraud-api`, automount off, `EWALLET_GIT_SHA` |
| Helm release `ewallet-serving` r1 | Feature API Deployment and Service; additive Redis NetworkPolicy in `feature-store` |
| Node labels | `ewallet.io/g3-llm=true` on `cbl0`, `jt0h`, `wcka`, owner-approved during the GKE node auto-upgrade |

Rollback material is in `before/`: the previous Deployment, its last-applied annotation, and the ReplicaSet with digest `20e9c2ad…`.

## Results

| Check | Result | Evidence |
|---|---|---|
| Workload Identity, both KSAs | Runtime identity is the dedicated GSA. Registry read OK; object outside the prefix 403; bucket list 403 | `identity/results.txt` |
| Fraud API rollout | New pod Ready; registry mirrored with the new identity | `fraud-api-rollout.txt`, `after/fraud-api-pod.txt` |
| Feature API Helm install | Deployed; pod Ready; correct SA and digest | `helm-install.txt`, `after/feature-api-pod.txt` |
| Feature lookup, reviewed N11 entities | HTTP 200, real values, `oldest_feature_timestamp` | `e2e/lookup-*` |
| Fraud predict, same transaction | HTTP 200, `model_version: "1"`, `fraud_probability` 0.064, correlation id returned | `e2e/predict-*`, `e2e/fraud-api-correlation-log.txt` |
| Negative tests | 11 of 11 return the expected 422 or 424 code | `e2e/negative-tests.txt` |
| Fixture, 500 real transactions | prepare 500/500; verify 500/500 HTTP 200 | `fixture/` |
| Stages | `feature_retrieval`, `feature_assembly`, `model_inference`, `request` counted | `e2e/fraud-api-metrics.txt` |
| Latency, 30 sequential requests | Feature p50 307 ms, Fraud p50 332 ms | `e2e/latency-sample.txt` |

The latency sample went through a port-forward from the operator's laptop, so it is not a performance measurement.

## Why test-split entities returned `missing_feature`

The online store holds only the last materialized window, **2026-05-31 22:00–24:00 UTC**, from `canonical-training-v1-20261028-101106-recovered` (MLOps campaign `feast_online_incremental.json`). The test split's entities have no online rows.

The fixture therefore comes from real transactions in that window:
- Source: Gold `fact_transactions`, run `ab-20261025134342-optimized-gold-r261025181923`, Delta version 0, active files only, partition `event_date=2026-05-31`.
- 52,368 rows in the window; the 500 newest supported transactions were used. Extract sha256 is in `fixture/window-extract.json`.
- `fraud_entities` `prepare` and `verify` only read; nothing was written to Redis.

## Incidents during the package

1. **GKE node auto-upgrade.** The control plane upgraded at 15:58Z; `UPGRADE_NODES` (surge 1) started at 16:13Z.
   - The KServe predictor was drained mid-run, which caused the 112 `model_backend_error` results in `fixture/verify-run1-during-node-upgrade.*`. The clean rerun is `fixture/verify.txt`.
   - New nodes lack the `ewallet.io/g3-llm` label, so agent, vLLM, MCP and gateway pods went Pending until the owner approved relabelling.
   - The drain of `79wp` waits on the `drift-mcp` PDBs: minAvailable 1 with 1 replica.
2. **Valkey node 0 still advertises its old IP.** A new `ate-api-server` read the slot map from node 0, so it crash-loops on `10.216.1.95`, and `kagent-controller` crash-loops behind it. A repair needs a separate approval.
3. **Kubeflow.** `persistenceagent` and `scheduledworkflow` cannot reach `ml-pipeline:8888`. The NetworkPolicies `allow-ewallet-agents-otel-to-kfp` and `allow-kfp-ui-to-ml-pipeline` select `app=ml-pipeline` and admit neither pod. Not changed.

`/metrics` answers 307 to `/metrics/`; Prometheus follows the redirect by default.
