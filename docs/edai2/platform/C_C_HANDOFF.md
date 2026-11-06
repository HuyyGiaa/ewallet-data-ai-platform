# Batch C-C handoff

C-C implemented Vault and the three A/B tests at source level and validated them locally. **Nothing is deployed.** GKE stayed at zero nodes and no rubric row is complete. Rows 52, 53 and 54 need the owner's cloud runs.

Branch: `feature/c-vault-ab-testing`, based on `46f86b3`. Local commits only; nothing merged or pushed.

## Rubric status

| Row | Requirement | Implemented | Local | Cloud | Evidence | Blocker |
|---|---|---|---|---|---|---|
| EDAI-2 52 | Agent A/B | Yes | Pass, real models | Not run | Not complete | New Coordinator image, P9 |
| EDAI-2 53 | LLM A/B | Yes | Pass on CPU | Not run | Not complete | GPU quota 0, P10 |
| EDAI-2 54 | Vault | Yes | Pass on kind | Not run | Not complete | New Fraud API image, P7 |
| Final Coursework | ML A/B | Yes | Pass, real predictors | Not run | Not complete | v2 not registered, P8 |
| EDAI-2 32 | Locust on real backends | Fixture tool only | Not run | Not run | Not complete | B1 change needs approval |

## Documents

| Topic | Page |
|---|---|
| Vault | [VAULT.md](VAULT.md) |
| ML A/B | [ML_AB_DESIGN.md](../mlops/ML_AB_DESIGN.md) |
| Agent A/B | [AGENT_AB.md](../agents/AGENT_AB.md) |
| LLM A/B | [LLM_AB_DESIGN.md](../llm/LLM_AB_DESIGN.md) |
| Row 32 | [ROW_32_LOCUST_PLAN.md](../testing/ROW_32_LOCUST_PLAN.md) |
| Approvals | [C_C_APPROVAL_PACKAGES.md](../../runbooks/edai2/C_C_APPROVAL_PACKAGES.md) |

## Dependencies on other branches

- **C-A is not needed.** C7 uses only C-B and earlier code. The 41-feature contract is unchanged.
- **Integration conflict to expect:** B1 and C-C both touch tests near `tests/inference`. Merge order stays B1, C-A, C-B, then C-C.
- **Row 32** needs a small `tests/load/payloads.py` change on B1.

## Open blockers

1. `GPUS_ALL_REGIONS` is 0; the billing account probably must leave Free Trial first.
2. Credit lasts about 18 days asleep at list prices. Trial end stops workloads, then deletion follows a grace period.
3. v2 is not registered; MLflow is asleep.
4. New images are needed: Fraud API, Coordinator, candidate predictor, GPU vLLM.
5. The GPU image has not been built or scanned, and the CUDA 13 driver match is unchecked.
6. Vault shares the `training` ServiceAccount with MLflow until a dedicated account gets IAM approval.
7. Vault seals on every restart, which adds an unseal step to every cluster wake.
8. The Drift Specialist is outside the Agent A/B.
9. Valkey may not re-form after a wake (Cloud Lab runbook).
10. KServe metrics for v2 need a scrape target; Fraud API `fraud_ab_*` metrics already cover latency per arm.
