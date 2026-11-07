# ML model A/B test

This page describes the A/B test between `fraud-detector` v1 and a candidate v2. Routing is implemented and validated locally with real predictors. **Nothing is deployed, and v2 is not registered**, so no production claim exists.

| Status | State |
|---|---|
| `IMPLEMENTED` | Router, metrics, manifests, tools |
| `LOCAL_VALIDATED` | Yes: real Redis, Feast, KServe predictors and models on localhost |
| `CLOUD_DEPLOYED` | No |
| `CLOUD_RUNTIME_VERIFIED` | No |
| `RUBRIC_EVIDENCE_COMPLETE` | No |

## Architecture

```text
Fraud API
  ↓ Feast Online, then the 41-feature vector
A/B router: sha256(experiment, salt, user_id) → bucket
  ├── model-a → fraud-detector-predictor     (v1)
  └── model-b → fraud-detector-v2-predictor  (v2)
  ↓
Per-arm metrics and an "experiment" block in the response
```

KServe here runs in Standard mode, which has no canary split, so the Fraud API routes. No mesh or new gateway is added. Without `EWALLET_AB_CONFIG_PATH` the API keeps its single-model path, and its response is unchanged.

| Topic | Rule |
|---|---|
| Assignment unit | `user_id`, so repeats and replays stay on one model |
| Splits | [90/10, 50/50 and 100/0](../../../configs/experiments/ml-ab/) |
| Attribution | `served_arm` and `model_version` come from the backend response; a wrong version is rejected |
| Candidate failure | Falls back to v1, sets `fallback: true`, counts a candidate failure |
| Control failure | The request fails; v2 never covers for v1 |
| Rollback | Apply `rollback-100-0`; no candidate traffic |

Source: [`ab_router.py`](../../../ai_platform/inference/ab_router.py), [`ab_assignment.py`](../../../ai_platform/inference/ab_assignment.py), [`ab_telemetry.py`](../../../ai_platform/inference/ab_telemetry.py).

## Model provenance

| | Model A (v1) | Model B (v2 candidate) |
|---|---|---|
| Registry | `fraud-detector` version 1, run `fd639926...` | **Not registered** |
| Estimator | Random forest, 200 trees, depth 12 | Random forest, 400 trees, depth 16 |
| Data | `training-20261031-151638-ml2` | Same snapshot |
| Selection | Validation PR-AUC | Validation PR-AUC; test not used |
| Config | `f9_mlflow_experiment.yaml` | [`c7_candidate_v2.yaml`](../../../ai_platform/ml/config/c7_candidate_v2.yaml) |

## Offline result

Trained locally by `ops/experiments/ml_ab_candidate.py` on the real snapshot. The control reproduced the MLflow v1 test PR-AUC exactly (`0.863982840570225`), so the data and code match v1. Scoring through real KServe predictors gave the same figures. Evidence: [training report](../../../docs/evidence/edai2/ml_ab/local_candidate_20261106T184405Z.json), [quality summary](../../../docs/evidence/edai2/ml_ab/local_e2e_20261106T184607Z/quality_summary.json).

| Held-out test, 5,565 rows, 143 fraud | v1 control | v2 candidate |
|---|---:|---:|
| PR-AUC | 0.8640 | 0.8698 |
| Precision | 0.9748 | 0.9750 |
| Recall | 0.8112 | 0.8182 |
| F1 | 0.8855 | 0.8897 |
| False positives | 3 | 3 |
| Brier score | 0.0139 | 0.0073 |

**Reading:** v2 found one more fraud case than v1. One case in 143 is too small a change to call v2 better. The Brier score suggests better calibration, but this is one split and one seed. Offline scoring of the split also took about twice as long (0.118 s against 0.060 s). Treat v2 as a valid candidate only.

## Local end-to-end run

Real Fraud API, Feast Online, Redis and two real KServe predictors, 300 fixture users, 2 requests each. Evidence: [`e2e_summaries.json`](../../../docs/evidence/edai2/ml_ab/local_e2e_20261106T184607Z/e2e_summaries.json).

| Scenario | Requests | Failed | Result |
|---|---:|---:|---|
| 90/10 split | 600 | 0 | Users 91.67% / 8.33%, inside tolerance; no user changed arm |
| Rollback 100/0 | 600 | 0 | 0 requests to v2 |
| v2 stopped, 50/50 | 600 | 0 | 302 fallbacks, all served by v1 |

Feature values were fixtures, so latency here measures nothing about production.

## Metrics

| Metric | Series |
|---|---|
| Requests and traffic share | `fraud_ab_requests_total` by arm, version, fallback, outcome |
| Latency | `fraud_ab_model_latency_seconds` |
| Candidate failures | `fraud_ab_candidate_failures_total` |
| Availability | `fraud_ab_readiness_checks_total` |
| Prediction distribution | `fraud_ab_score`, `fraud_ab_positive_predictions_total` |
| Resources | Container CPU and memory for each predictor |
| Quality | Fixed labelled set only, through `ops/experiments/ml_ab_evaluate.py` |

Online traffic has no labels, so it is never used as evidence of accuracy.

## Risks

- A second predictor adds 250m CPU and 768 MiB of requests.
- Fallback can add one candidate timeout (1 s) to a request.
- Both arms read the same features at the same moment, so feature freshness does not bias the comparison.
- Applying a new split restarts the Fraud API.

Deployment is package P8 in the [approval packages](../../runbooks/edai2/C_C_APPROVAL_PACKAGES.md).
