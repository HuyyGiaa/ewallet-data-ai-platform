# Batch C-B handoff

Batch C-B is closed at local level. This page records what the final integration phase must still do. **Nothing from C-B has run on the cluster, and no rubric row changed.**

The work is on `feature/c-agent-platform-completion`, unmerged and unpushed. Owner decisions from the final review:

| Topic | Decision |
|---|---|
| Integration order B1 → C-A → C-B | Approved in principle; not authorization to merge |
| Production Jenkins RBAC and rollout | Not approved; the proposal is kept for review |
| ValidatingAdmissionPolicy | Deferred; not needed for the promotion path |
| GPU | L4 Spot preferred; no deployment until quota and cost are reconfirmed |
| EXP-V1, EXP-V2 | Prepared; the owner runs them |

## Integration blockers

| # | Blocker | Needed |
|---|---|---|
| 1 | Three branches unmerged | Owner authorization, then the [integration order](C_INTEGRATION_ORDER.md) |
| 2 | C-A paths have no catalog owner | Add in the integration commit |
| 3 | `streaming-sinks` isn't in `python_projects` and has no release selector | Add in the integration commit |
| 4 | `test_every_project_is_locked` has a stale list on `main` | Add `data_platform/generation` and `infra/docker/g3-byo-coordinator` |
| 5 | `test_g2_dag_builds_evidence...` checks a function that no longer exists | Fix or remove the test |
| 6 | Agent test venv lacks Feast, causing 11 environment failures | Use an environment with Feast for that scope |

## Cloud verification blockers

| # | Blocker | Package |
|---|---|---|
| 7 | No release image built from the merged commit | P1 |
| 8 | CD never run on Jenkins for rows 34–36 | P1 |
| 9 | Streaming CD fails by design until C-A is integrated | P1 after merge |
| 10 | EXP-V1, EXP-V2 not executed | P2, P3 |
| 11 | KEDA not installed; real gauge and mesh path unproven | P4 |
| 12 | KEDA rollback order unproven on the real chart | P4 |
| 13 | MCP failover unmeasured; production overlays are a later approval | P5 |
| 14 | BYO agent has never booted in gVisor | P6 |
| 15 | Non-root bind of port 80, ztunnel path, task-store auth and snapshot resume are open | P6 |
| 16 | Dedicated MCP token not created | P6 S1 |
| 17 | `release_state.py promote` has never run | Separate approval |
| 18 | GPU quota | Owner |

## Local-only evidence

These results are real but say nothing about GKE:

- KEDA behaviour used a stub Prometheus and, for Helm, a minimal chart.
- The streaming gate ran in Docker on images built locally.
- The Specialist boot ran as a local process, not in gVisor.
- `release_state.py drift` and `check` did read production, read-only, on 2026-11-06.

## Preserved for later

The approval packages are in the [runbook](../../runbooks/edai2/llm_agent/C_AGENT_PLATFORM_RUNBOOK.md). The next batch is C6 + C7: Vault, ML Model A/B, Agent Configuration A/B and LLM Model A/B. Its inputs are the [LLM A/B design](LLM_AB_DESIGN.md), the [ML A/B design](../mlops/ML_AB_DESIGN.md) and the comparison tooling in `ops/experiments`.
