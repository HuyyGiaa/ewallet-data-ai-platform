# Centralized secrets with HashiCorp Vault

This page describes the Vault integration for EDAI-2 row 54. It is implemented and validated on a disposable local deployment. **It is not deployed on GKE**, so the row is not complete until the owner runs package P7.

| Status | State |
|---|---|
| `IMPLEMENTED` | Yes |
| `LOCAL_VALIDATED` | Yes: official chart, Raft, TLS, real ServiceAccount tokens, on kind |
| `CLOUD_DEPLOYED` | No |
| `CLOUD_RUNTIME_VERIFIED` | No |
| `RUBRIC_EVIDENCE_COMPLETE` | No |

## How a workload gets a secret

```text
Fraud API pod (ServiceAccount training)
  ↓ projected token, audience "vault", 10 minutes
Vault Kubernetes auth (TokenReview against the API server)
  ↓ role fraud-api: one ServiceAccount, one namespace, one audience
Vault token: 5-minute TTL, policy fraud-api only
  ↓ read ewallet/data/fraud-api/feast-redis
EWALLET_REDIS_CONNECTION_STRING in the process, then the token is revoked
  ↓
Feast Online opens Redis; the Fraud API serves predictions
```

The loader is [`ai_platform/inference/vault.py`](../../../ai_platform/inference/vault.py). It runs before Feast reads the variable, and logs only the path and version.

## Design decisions

| Topic | Choice | Reason |
|---|---|---|
| Deployment | Official chart `vault-0.34.1`, Vault 2.0.4, image pinned by digest | Not a substitute product, not dev mode |
| Storage | Integrated Raft on a 5 GiB PVC, audit log on a 1 GiB PVC | Vault is stateful; data survives a restart |
| TLS | cert-manager Issuer inside `ewallet-vault` | Reuses what is installed; clients trust one CA |
| Unseal | Shamir, 3 shares, threshold 2 | KMS auto-unseal is a new billable resource and needs new IAM |
| Injection | Native loader, not the Agent Injector | Feast reads one variable at startup, so a sidecar adds cost with no benefit; no new webhook to complicate a cluster wake |
| Failure | Fail closed once enabled | A workload must not start on a stale secret |
| First workload | Fraud API Redis connection | No agent depends on it; rollback is one `rollout undo` |

## Important behaviour

- **Every restart seals Vault.** A cluster wake therefore needs `ops/vault/cli.py unseal` before the Fraud API can start in Vault mode.
- **Revocation stops new reads, not loaded values.** A running pod keeps its value until it restarts. Removing the role blocks new pods at once.
- **Rotation needs a restart.** Vault holds a new version immediately; the Fraud API reads it at startup. Changing the actual Redis password is a separate step.
- **Key material stays out of Git.** `init` refuses a path inside the repository and writes a mode-0600 file. The root token is retired after bootstrap.

## Limitation of the first integration

The Fraud API runs as ServiceAccount `training`, which MLflow also uses. The role is bound to that account, so an MLflow pod could also log in, though only to read this one path. A dedicated account needs a new Workload Identity grant, which is an IAM change. It is listed as an option in P7.

## Existing secrets

Read on 2026-11-07, names only. Secrets stay in Kubernetes unless listed as migrated. Existing paths are untouched until the new one is verified.

| Group | Examples | Plan |
|---|---|---|
| Fraud API Redis | `ml-platform/feast-online-redis` | **First migration** |
| MCP credentials | `mcp-token-*`, `mcp-auth-*` | Later; a staged rotation design already exists |
| Gateway and vLLM keys | `agentgateway-client-key`, `vllm-api-key` | Later; consumed by `secretKeyRef` in several pods |
| Databases | `mlflow-backend-store`, `prompt-registry-db`, `kagent-postgresql` | Later |
| Jenkins | `jenkins-admin` | Later; Jenkins has its own credential store |
| TLS and webhook | `kserve-webhook-server-cert`, `ateapi-tls` | Not candidates; controllers manage them |

## Local validation

`scripts/vault/local_validate.sh` on kind, results in [`docs/evidence/edai2/vault/local_20261106T183340Z`](../../../docs/evidence/edai2/vault/local_20261106T183340Z/results.jsonl). All steps passed:

| Check | Result |
|---|---|
| Initialise, unseal, apply policies and roles | Pass |
| Authorized pod reads the secret | Pass, value hash matches |
| Wrong ServiceAccount | Denied, 403 |
| Wrong token audience | Denied, 403 |
| Right identity, other path | Denied by policy, 403 |
| Rotation | Version 2 read, new hash |
| Restart | Vault sealed; reads fail with 503 |
| After unseal | Version 2 still readable from the PVC |
| Role revocation | New login denied |
| Audit log | 56 lines, 0 plaintext matches |
| Root token retirement | Old token rejected |

The run also found a real defect that unit tests could not: with `token_no_default_policy`, a token cannot revoke itself, and that error hid the real failure. Both are fixed.

Local validation used an OpenSSL CA in place of cert-manager and did not run the Fraud API image, Feast or Redis. Those are P7 checks.
