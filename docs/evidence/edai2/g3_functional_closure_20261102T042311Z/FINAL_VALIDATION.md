# G3 functional closure campaign — final validation checkpoint

Validated source checkpoint: `27043f2ed23c5a9dd34d2fd16e7c796170ec538b`

## Functional verdict

The campaign closed the typed ingress, bounded-context, deterministic-routing,
and task-polling source contracts, but the live Coordinator business workflow
remains blocked. At Helm revision 15 the model emitted a textual pseudo-call
instead of selecting the registered native Agent tool
`ewallet_agents__NS__feature_rag_specialist`. The task reached a terminal
state, preserved the full transaction, and did not overflow context, but no A2A
Gateway/Specialist/MCP call occurred. The two bounded correction attempts were
therefore exhausted without claiming business success.

The independent RAG phase completed a real six-task Airflow 3 run. Its full
runtime evidence is in
`docs/evidence/llm_agent/rag_g3-functional-rag-20261102t045700z/`.
Publication used new immutable GCS objects and Redis DB1, leaving canonical ML
Feast keys and production RAG DB0 untouched. DataHub remained plan-only because
the existing local stack was stopped while local swap was exhausted; no current
DataHub publication/readback is claimed.

## Locked source validation

Commands:

```bash
uv lock --check --project infra/docker/llm-agent-tooling
infra/docker/llm-agent-tooling/.venv/bin/python -m unittest \
  tests.llm_agent.test_phase_d_tools \
  tests.llm_agent.test_phase_d_security \
  tests.llm_agent.test_mcp_sdk_conformance \
  tests.llm_agent.test_mcp_production_authenticator \
  tests.llm_agent.test_phase_d_chart \
  tests.llm_agent.test_agents_chart \
  tests.llm_agent.test_g3_orchestration \
  tests.llm_agent.test_g3_runtime_config \
  tests.llm_agent.test_g3_business_request -v

uv lock --check --project infra/docker/rag-ingestion
uv run --locked --project infra/docker/rag-ingestion python -m pytest -q \
  tests/llm_agent/test_rag_airflow_ingestion.py \
  tests/llm_agent/test_rag_online_retriever.py \
  tests/llm_agent/test_rag_chunk_lookup.py \
  tests/llm_agent/test_phase_c.py \
  tests/llm_agent/test_rag_dag_reconciliation.py
```

Results:

- G3/MCP/A2A/chart regression: `167 passed`, `0 failed`, `0 skipped`, 6.896 s.
- RAG regression: `61 passed`, `1 skipped`, `26 subtests passed`, 3.81 s.
- Both lock checks: PASS (`120` and `164` resolved packages respectively).
- `py_compile` of the changed Python entry points: PASS.
- `bash -n scripts/validation/run_rag_dag_isolated.sh`: PASS.
- `git diff --check`: PASS.

The single RAG skip is the existing opt-in model-dependent test; it is not
hidden or counted as a pass.

## Runtime readback

- `llm-agent`: deployed, revision 15.
- `llm-agent-mcp`: deployed, revision 3.
- `kagent`: deployed, revision 2, chart 0.10.3.
- Coordinator, both Specialists, A2A Gateway, agentgateway, MCP Gateway,
  vLLM, and both isolated MCP pods: Running/Ready with zero restarts in the
  readback.
- All three Agent CRs: Ready/Accepted.
- Both RemoteMCPServer CRs: Accepted.
- Feature Redis: Running/Ready.
- KServe `fraud-detector`: Ready.
- Current invalid-bearer regression: HTTP 401 with
  `authentication_required` (`security-negative-current.json`).
- Current positive production `knowledge_search`: PASS through the official
  MCP SDK (`rag_g3-functional-rag-20261102t045700z/current-mcp-knowledge-search.json`).

## Final gates

```text
SOURCE_READY=PASS
LOCKED_TESTS=PASS
COORDINATOR_PAYLOAD=PASS
COORDINATOR_ROUTING=BLOCKED
SPECIALIST_CONTEXT_BUDGET=PASS
A2A_TASK_LIFECYCLE=PARTIAL
MCP_BUSINESS_CALLS=PASS
FEAST_E2E=PASS
KSERVE_E2E=PASS
RAG_RETRIEVAL=PASS
RAG_AIRFLOW_DAGRUN=PASS
RAG_FEAST_PUBLICATION=PASS
RAG_DATAHUB_LINEAGE=BLOCKED
RAG_GOVERNANCE=PARTIAL
COORDINATOR_BUSINESS_E2E=BLOCKED
FULL_BUSINESS_E2E=BLOCKED
SECURITY_REGRESSION=PASS
RUNTIME_HEALTH=PASS
REPRODUCIBILITY=PASS
FINAL_SCREENSHOT_EVIDENCE=PENDING
POST_MERGE_REPRODUCED=PENDING
RUBRIC_VERIFIED=PENDING
```

`A2A_TASK_LIFECYCLE=PARTIAL` means the Coordinator task now terminates under
the bounded client, but the intended downstream Agent task was not created in
attempt 3 because native tool selection never occurred. `RAG_GOVERNANCE` is
partial because all five quality checks passed locally while DataHub runtime
publication/readback is blocked.

## Remaining targeted work

1. Correct the kagent/model function-selection contract using the actual
   registered tool name/schema or a supported deterministic tool-choice
   mechanism; do not perform another prompt-only retry.
2. Re-run one bounded Coordinator transaction and require observed A2A Gateway,
   Specialist, MCP, Feast, KServe, and RAG traces before claiming E2E success.
3. Start/reuse DataHub only after the host resource gate is safe, publish the
   already constructed 25 records, and independently read back lineage and
   assertions.
4. Perform the separately deferred native-UI screenshot/rubric campaign only
   after functional closure.
