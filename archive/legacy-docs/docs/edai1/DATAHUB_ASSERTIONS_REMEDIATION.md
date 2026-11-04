# DataHub executed data-quality assertions remediation

## Scope and preserved evidence

This change does not alter the verified DataHub lineage publisher, its dataset,
DataFlow or DataJob URNs, or the 31 existing Cloud Delta Metadata Contract
assertions. Historical lineage remains 3 DataFlows, 4 DataJobs, 31 lineage
aspects, 42 atomic edges and 122 semantic metadata readbacks. That evidence is
bound to the 25–26 October canonical runs and is not presented as a newer
Method 2 republish.

## Six-state audit

| Pipeline | Validator source | Executed real result | Persisted artifact | Definition | Run | Readback |
|---|---|---|---|---|---|---|
| DP1 Bronze | `validate_bronze.py`; source-to-Bronze job gate | 8-table source/Bronze counts match | Airflow task log with embedded `r4-source-to-bronze-v1` JSON | SOURCE_READY | SOURCE_READY | BLOCKED: GMS stopped |
| DP2 Silver | `validate_silver.py`; transformation DQ gate | channel NULL=0; duplicate transaction IDs=0 | Airflow task log with timestamps/run identity | SOURCE_READY | SOURCE_READY | BLOCKED: GMS stopped |
| DP2 Gold | `validate_gold.py` | No persisted row-level result recovered | Table-set/job success only | NOT_EXECUTED | NOT_EXECUTED | NOT_EXECUTED |
| DP3 Features | feature validation + Feast PIT gate | 310/310 comparable values; mismatch=0; labels excluded | PIT JSON + successful Cloud Run execution JSON | SOURCE_READY | SOURCE_READY | BLOCKED: GMS stopped |

The development recovery artifact contains seven genuine PASS outcomes and one
explicit `NOT_EXECUTED`. The publisher refuses to create a run event for
`NOT_EXECUTED` or `BLOCKED`.

## Rubric rows 28–38

| Row | Original requirement | Evidence/status after this change |
|---:|---|---|
| 28 | DP1 linked with related tables — lineage between pipeline and tables | PRESERVED; historical lineage evidence only |
| 29 | DP1 data validation | PARTIAL: genuine source-to-Bronze reconciliation is source-ready; DataHub publish/readback pending |
| 30 | DP2 linked with related tables — lineage between pipeline and tables | PRESERVED; historical lineage evidence only |
| 31 | DP2 data validation | PARTIAL: two genuine Silver rules source-ready; Gold row-level result is `NOT_EXECUTED`; DataHub readback pending |
| 32 | DP3 linked with related tables — lineage between pipeline and tables | PRESERVED; historical lineage evidence only |
| 33 | DP3 data validation | PARTIAL: genuine four-view PIT/readback result source-ready; DataHub readback pending |
| 34 | Visualize tables on all zones | PRESERVED; no schema visualization changed |
| 35 | Dim SCD2 (`valid_from_ts`, `valid_to_ts`, `is_current`) | SEPARATE WORKSTREAM; unchanged |
| 36 | Feature tables with `event_timestamp` and `created` | SEPARATE WORKSTREAM; unchanged |
| 37 | Relationship between dimension and fact tables | PRESERVED; unchanged |
| 38 | Gold/Bronze/Silver naming convention | PRESERVED; unchanged |

No rubric row is upgraded to PASS solely from source code or a dry run.

## Development verification

- Locked runtime: Python 3.10.22, uv 0.13.5, acryl-datahub 1.7.0.5.
- Recovery: 8 outcomes (7 PASS, 1 NOT_EXECUTED).
- Publication plan: 8 definitions, 7 eligible run events, 1 suppressed run.
- Unit tests: 7 passed for result model, failure mapping, idempotency, raw
  readback and semantic mismatch detection.
- Evidence: `~/ewallet-evidence/datahub-assertions-20261101/development-2033ab3/`.

Actual GMS publication was not attempted because the pinned DataHub containers
are stopped while the host has high memory pressure and other operators own
active runtimes. This is a resource-isolation blocker, not a successful
publication/readback claim.

## Living Decision copy-ready entry

The latest canonical number observed read-only was 21.316. The following entry
is prepared for append after owner confirmation; it has not been written to the
Google Doc.

**21.317 — 01/11/2026 ICT — DATAHUB RESULT-BOUND QUALITY ASSERTIONS SOURCE AND
LOCKED REPRODUCIBILITY READY; LIVE GMS PUBLICATION BLOCKED BY HOST RESOURCE
ISOLATION**

SOURCE: Dedicated worktree
`/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-datahub-quality-assertions`,
branch `codex/datahub-quality-assertions`, base `origin/main` 2033ab3, feature
HEAD 3c71fa0 with commits 89f445d and 3c71fa0; no merge or push. Existing
DataHub lineage identities and 31 Cloud Delta Metadata Contract assertions are
unchanged. New modules recover actual DP1 source/Bronze reconciliation, DP2
Silver NULL/duplicate metrics and DP3 four-view Feast PIT results into a strict
PASS/FAIL/NOT_EXECUTED/BLOCKED artifact, then construct deterministic DataHub
definitions/run events and require typed plus raw REST semantic readback.

VALIDATION: Existing immutable evidence recovered 8 outcomes: 7 genuine PASS
(DP1=1, DP2 Silver=2, DP3=4) and DP2 Gold row-level integrity explicitly
NOT_EXECUTED. No PASS is inferred from table existence or successful job state.
Development artifacts are under
`~/ewallet-evidence/datahub-assertions-20261101/development-3c71fa0/`.

REPRODUCIBILITY/TEST: Python 3.10.22, uv 0.13.5,
`acryl-datahub==1.7.0.5`, `trino==0.336.0`; committed
`data_platform/metadata/datahub/{pyproject.toml,uv.lock}`; `uv lock --check` and
`uv sync --locked` PASS. Focused tests: 23 PASS / 5 Feast optional-dependency
SKIP plus 7/7 uv contract PASS. Clean-main rerun instructions are in
`docs/runbooks/DATAHUB_ASSERTIONS_REPRODUCIBILITY.md`.

STATUS: SOURCE_READY=YES, TEST_PASS=YES, VALIDATION_EXECUTED=YES for seven
outcomes, DATAHUB_PUBLISHED=NO, READBACK_VERIFIED=NO, RUNTIME_VERIFIED=NO,
EVIDENCE_COMPLETE=NO, POST_MERGE_REPRODUCED=NO, RUBRIC_VERIFIED=NO. GMS and its
support containers are stopped; host swap is full and unrelated operator
runtimes are active, so DataHub startup was blocked to preserve isolation. Next:
use a safe resource window to start pinned DataHub v1.7.0.1, publish seven run
events, retain raw readback/UI screenshots, execute the missing Gold row-level
validator, then merge only after review and repeat from the exact merged main
SHA.
