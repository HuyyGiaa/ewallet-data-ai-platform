# R1: hand the Fraud API's replica count to KEDA

Run this inside P-A3, after the fixed arms and before `autoscaling.enabled=true`. Nothing here has run on GKE.

## Why a plain apply is unsafe

The Fraud API is applied client-side (`kubectl apply`). Client-side apply is a three-way merge against the `last-applied-configuration` annotation.

A field that is in the annotation but missing from the new manifest is **deleted**, and a Deployment without `spec.replicas` defaults to **1**. Applying the KEDA manifest directly would therefore drop the Fraud API to one replica in the middle of the experiment.

## Two render modes

`python -m ops.serving.fraud_api_manifest` reads the template values from the environment: `FRAUD_API_IMAGE` (pinned by digest), `ONLINE_REGISTRY_URI`, `MODEL_VERSION` and `GIT_SHA`.

| Mode | `spec.replicas` | Owner | When |
|---|---|---|---|
| `--mode fixed --replicas N` | `N` (1–10) | kubectl | Fixed arms, and every deployment without KEDA |
| `--mode keda` | absent | KEDA's HPA `keda-hpa-fraud-api` | After the handover |

`envsubst < infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl` still works and gives the same output as `fixed`. `infra/helm/ewallet-serving` renders the ScaledObject that targets `Deployment/fraud-api` in `ml-platform`; no HPA is rendered anywhere else.

## Procedure

`scripts/serving/fraud_api_keda_handover.sh <action> "$EVIDENCE/r1" [--execute]`

| Step | Action | Mutates | What it checks |
|---|---|---|---|
| 1 | `readback` | No | `spec.replicas`, ready replicas, whether the annotation holds replicas, `managedFields` managers of `f:replicas`, HPAs targeting `fraud-api`, ScaledObjects |
| 2 | `plan` | No | `kubectl diff` of the KEDA manifest, and the replicas a server dry-run of a plain apply would store. Expect `1`, which shows the hazard |
| 3 | `handover --execute` | Annotation, then Deployment | Refuses if an HPA already targets `fraud-api`. Runs `set-last-applied`, which touches only the annotation, and checks live replicas are unchanged. Then runs a server dry-run of the KEDA manifest, which must keep the count; otherwise it restores the annotation and stops. Only then does it apply and check the count again |
| 4 | P-A3 Helm step | Yes | `helm upgrade ewallet-serving … --set autoscaling.enabled=true` |
| 5 | `verify` | No | ScaledObject Ready, only `keda-hpa-fraud-api` targets the Deployment, and the annotation no longer holds replicas |

**Replica preservation.** Every mutating step compares live `spec.replicas` with the value read before it and stops on a difference.

## Rollback

1. `helm upgrade ewallet-serving … --set autoscaling.enabled=false`. KEDA deletes its HPA; the Deployment keeps its current count.
2. `scripts/serving/fraud_api_keda_handover.sh rollback "$EVIDENCE/r1" --execute`. It refuses while the ScaledObject exists, then applies `fixed` with the current count, so kubectl owns replicas again with no change.

Remove KEDA itself only after every ScaledObject is gone, or the finalizer holds the namespace.

## The same hazard elsewhere

- **Feature API.** It is Helm-managed, and Helm also merges three ways. If it was first deployed with fixed replicas above 1, enabling autoscaling removes the field and drops it to 1 until KEDA scales it back. Deploy it with `replicas: 1` for the fixed arm, or accept the short dip.
- **Agents.** They use `transferHpa`, so they do not have this problem (C-B).

## Tests

`tests/unit/test_fraud_api_replica_modes.py`, in catalog component `fraud-serving`:
- the two modes, and refusal of unsafe input;
- identity and pod template parity between the modes;
- envsubst compatibility;
- no competing HPA, and the ScaledObject target.

It also runs the real script against a fake kubectl that models the three-way merge:
- a plain KEDA apply drops to 1;
- the handover keeps 3;
- a dry-run mismatch stops the handover and restores the annotation;
- an existing HPA blocks the handover;
- `verify` rejects a second HPA;
- `rollback` needs the ScaledObject gone first.
