# ML model A/B design

This is a design only: nothing here is implemented or deployed. It describes how the Fraud API could compare two fraud models on live traffic.

## Constraint

KServe runs in Standard mode here, and its canary traffic split needs Serverless mode. The Fraud API also has one backend, set by `EWALLET_KSERVE_BASE_URL`. A split therefore has to live in the Fraud API.

## Proposal

```text
Fraud API ──hash(user_id) < weight──► InferenceService fraud-model-b
          └──otherwise──────────────► InferenceService fraud-model-a
```

Two InferenceServices run side by side, one per MLflow model version. The API picks an arm from a stable hash of `user_id`, so a user stays on one arm and the arms stay comparable.

| Topic | Decision |
|---|---|
| Start | Shadow mode: B scores every request, only A's score is used |
| Then | 5% on B, then 25%, each after a review |
| Attribution | Log `model_version`, arm and request ID with each decision |
| Failure | If B fails, use A and count it as a B failure |
| Stop | Turn the weight to 0, with no redeploy |

## Metrics

Fraud labels arrive late, so online latency and offline quality are judged separately.

| Metric | When known | Better when |
|---|---|---|
| p95 latency, error rate | Immediately | Lower |
| Score distribution drift between arms | Hours | Smaller, unless expected |
| PR-AUC, precision at fixed review capacity | After chargeback labels | Higher |
| False-positive rate on legitimate payments | After review | Lower |

Fraud is rare, so the sample needed for a precision claim is large. Estimate it from the current fraud rate before starting, and expect shadow mode to give most of the early evidence.

## Risks

- A decision that blocks a payment affects a real customer, so B should not block anything until it has passed shadow review.
- Both arms must read features at the same point in time, or the comparison measures feature freshness.
- A second InferenceService needs capacity. Current nodes have about 0.5 CPU free on three of four, so this would need a node.
