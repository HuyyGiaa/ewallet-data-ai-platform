# Phase A–C P0 remediation (source-only)

Status: **source-only, uncommitted, awaiting independent review.** Nothing here
was deployed, no model was loaded, no agent ran, and no rubric row moved to
VERIFIED. Kubernetes admission, NetworkPolicy enforcement and Istio Ambient
identity remain runtime gates.

## Provenance

| Item | Value |
|---|---|
| Worktree / branch | `.worktrees/llm-agent-rubric-20261030` / `codex/llm-agent-rubric-20261030` |
| Base HEAD | `baf2a42f4207a6b08bf29b4ced6a19e910256fa9` (descends from `b483064`, `acad36b`) |
| Reviewer patch | `phase-abc-p0-fixes.patch`, SHA256 `0a21aa8bd1598fcc00b30ba04a6082df5a53988d8c58dde5b0a8db8ce71fcbc4`; already applied before this task (every pre/post blob matched), then extended here |
| Rubric workbook | `EDAI K11.xlsx`, SHA256 `1b0f880124da3cab4e7cca8e24568d89a3d67c119029f1a71ab39f9eb6fa6081`; read-only extract in `docs/rubric/edai_k11_workbook_extract.json` |

## P0 results

| ID | Status | Change |
|---|---|---|
| P0-1 vLLM tool calling | Source fixed | `--enable-auto-tool-choice --tool-call-parser hermes` behind `vllm.toolCalling`; render fails if enabled without a parser. vLLM v0.32.0 documents both flags and `--max-num-queued-reqs`; its parser table names `hermes` for Qwen2.5/QwQ, not Qwen3, so a real-model tool-call smoke stays a runtime gate. Fixture smoke only (`P0ToolCallFixtureSmokeTests`). |
| P0-2 Pod Security | Source fixed | `SYS_NICE` and `seccomp: Unconfined` removed (they only enable optional NUMA tuning per vLLM CPU docs); pod and container `RuntimeDefault`; dedicated `vllm` ServiceAccount; namespace PSA `enforce=baseline`, `warn/audit=restricted`. `ops/llm_agent/policy_lint.py` flags the original HEAD render (4 violations) and passes the fixed render. |
| P0-3 allow-flows | Partially fixed by design | Istio and Kubernetes default-deny kept. Flows render from `networkFlows` only when both ends are chart-owned workloads (labels + ServiceAccount): DNS and agentgateway→vLLM. Every other required flow is listed in `networkFlows.unresolved` and in the `llm-agent-network-flows` ConfigMap, still denied. The reviewer patch's agents→agentgateway rules were withdrawn: they guessed agent pod labels and ServiceAccounts, and the kagent pilot shows SandboxAgent egress runs on a Substrate WorkerPool in another namespace. |
| P0-4 Citation contract | Source fixed; trust bound to the source corpus | RAG `Citation` now carries `embedding_model_revision`. `to_agent_citation(citation, index=TrustedChunkIndex)` prefixes `sha256:` and rejects unknown documents, checksum/URI mismatches, any chunk ID not in the trusted index (including a fabricated `<document_id>:9999`), chunks of another document, quotes that are not the chunk's leading words, revision mismatches, and non-finite, boolean, negative or >1 scores (no clipping). The index is rebuilt from checksum-verified documents; a publication artifact is accepted only if its rows equal that set exactly. **Incomplete guarantee:** no Feast-published index readback exists, so the trust anchor is the source corpus, not the online store. |
| P0-5 Airflow injection | Source fixed | Params are templated only into env values (`rag_params.py`) and expanded once inside quoted `"${VAR}"`; anchored JSON-schema patterns (also reject trailing newlines); `validate_rag_params` rejects unexpected keys; a Bash guard fails a run whose `evidence_dir` already exists, before any embedding call; runners keep create-only `open("x")`. |

### Additional defects found

1. **The RAG DAG could not be imported.** `python_module_command` only allowed
   `ops.gcp.*`, so `rag_fraud_policy.py` raised `ValueError` at parse time;
   the old tests only read the DAG as text. Fixed with a separate
   `LLM_AGENT_MODULES` allowlist; the interpreter check for DP1–DP3 is unchanged.
   A real isolated DagBag parse (Airflow 3.3.1, throwaway `AIRFLOW_HOME` and
   SQLite, `scripts/validation/validate_rag_dag_import.sh`) now reports no
   import errors, the four tasks and their dependencies, no Jinja in any
   `bash_command`, and `ParamValidationError` for five malicious values
   (`docs/evidence/llm_agent/source_review/rag_dag_isolated_parse.json`). The
   same parse of HEAD `baf2a42` fails with the allowlist `ValueError`.
2. **Ledger point values for 15 EDAI-2 rows disagreed with the workbook**
   (rows 15, 17, 19, 21, 22, 40–42, 47, 48, 50–54); the hard-coded tuple was
   shifted, so the validator passed wrong data. Corrected tuple, CSV and JSON;
   `tests/unit/test_rubric_workbook_extract.py` now pins both to the extract.
   Awarded points are unchanged (27 raw, 13.5 weighted).

## Network flow contract

| Flow | State | Gate to enforce |
|---|---|---|
| DNS → kube-dns:53 UDP/TCP | Enforced (NetworkPolicy) | Confirm labels / NodeLocal DNSCache on the cluster |
| agentgateway → vLLM:8000 | Enforced (Istio principal + NetworkPolicy pair) | Runtime allow/deny test |
| Agents → agentgateway:4000 | Unresolved | Read back SandboxAgent/WorkerPool identity |
| Coordinator → specialists (native A2A) | Unresolved | Read back kagent controller (`:8083 /api/a2a/<ns>/<agent>`) and WorkerPool identity |
| Agent → MCP Gateway → MCP servers | Unresolved | Phase D Deployments, ports and labels |
| MCP → RAG / Feast / KServe / fraud scoring | Unresolved | ML Platform namespaces, labels and ports |
| OTLP exporters → collector:4317/4318 | Unresolved | Collector Deployment |
| vLLM model bootstrap → huggingface.co:443 | Unresolved | Approve pre-staged model cache or egress gateway / FQDN policy; **vLLM cannot become Ready under default-deny until then** |
| External Gateway → coordinator:8083 | Unresolved | Define A2A backend Service; Gateway pod ingress and health-check ports |

## Rubric-to-architecture equivalence

| Original rubric requirement | Approved architecture | Implemented equivalent | Evidence required | Instructor acceptance risk |
|---|---|---|---|---|
| EDAI-2 r37–42: NGINX Ingress with routes, basic auth, rate limit | Istio Ambient + Gateway API | HTTPS Gateway + one HTTPRoute (coordinator); no Grafana/Kibana/Jaeger/UI routes, auth or rate limit yet | Routes, auth and rate-limit policies, runtime readback, written equivalence rationale | **High**: rubric names NGINX |
| EDAI-2 r24: agentgateway as A2A Gateway | Native kagent A2A, no standalone A2A gateway | `AgentDelegationRequest` contract only | Coordinator→specialist delegation trace + rationale | **High**: rubric names agentgateway |
| EDAI-2 r23: agentgateway as MCP Gateway | Policy-only MCP Gateway | ServiceAccount and architecture contract only | MCP Gateway config + allow/deny/rate-limit/audit/PII proof | Low once implemented (same product) |
| EDAI-2 r5: agentgateway security & policy | ModelConfig → agentgateway → vLLM | Rate limits, timeouts, retry, failover, Istio/NetworkPolicy | Live 401/429/failover tests | Low |

None of these rows is VERIFIED.

## Validation run (2026-10-31, local)

| Command | Result |
|---|---|
| `python3 -m unittest tests.llm_agent.test_contracts tests.llm_agent.test_foundation tests.llm_agent.test_phase_b tests.llm_agent.test_phase_c tests.llm_agent.test_phase_abc_p0_fixes tests.unit.test_rubric_evidence_matrix tests.unit.test_rubric_workbook_extract tests.unit.test_kagent_bounded_pilot` | Ran 76 tests, OK (`docs/evidence/llm_agent/source_review/p0_unittest.log`) |
| `scripts/validation/validate_rag_dag_import.sh <airflow-env python>` | DagBag: 0 import errors, 4 tasks (`rag_dag_isolated_parse.json`) |
| `helm lint` default / `values-dev.yaml` / `values-gcp.yaml` | 0 charts failed (INFO: icon recommended) |
| `python3 -m ops.llm_agent.policy_lint <gcp render>` | `SOURCE_ONLY_PASS`; HEAD render: 4 Baseline violations |
| `python3 -m ops.rubric.validate_evidence_matrix` | PASS, 27 raw / 13.5 weighted (unchanged) |
| Branch coverage (coverage 7.6.10) of `ai_platform/llm_agent`, `ops/llm_agent`, `data_platform/orchestration/airflow` excluding DAGs | **67%** total: `rag_params.py` 100%, `policy_lint.py` 90%, `rag/pipeline.py` 90%. Below the >90% rubric threshold |
| Mutation score, property-based tests, Locust | Not measured |
| `tests.unit.test_canonical_airflow_scripts`, `tests.unit.test_cloud_airflow_dags` | 6 failures + 1 error, identical on a pristine HEAD export (no `python` on PATH, no `airflow` package): pre-existing environment gaps |

## Known integration risks

| Risk | Classification | Evidence |
|---|---|---|
| Jenkins scripts `build_changed_images.sh`, `scan_and_push.sh`, `deploy_atomic.sh`, `rollback.sh` | STILL OPEN | Referenced by `Jenkinsfile.llm-agent`, absent from the tree |
| Changed-component detection uses `git diff HEAD^ HEAD` | STILL OPEN | `Jenkinsfile.llm-agent:24`; misses multi-commit changes |
| Feast RAG publication is a JSON artifact, no apply/materialize/readback | BLOCKED BY RUNTIME | No Feast write path in `ops/llm_agent` |
| Redis online store has no Feast vector search | REQUIRES DESIGN APPROVAL | `assess_vector_compatibility` → `PUBLICATION_ONLY_RUNTIME_BLOCKED` |
| DataHub lineage only planned | BLOCKED BY RUNTIME | `datahub_lineage_plan` requires readback |
| Embedding endpoint revision not verified | STILL OPEN | Request sends model name only (`rag_pipeline.py`) |
| Benchmark identity from CLI arguments | STILL OPEN | `--git-sha/--image-digest/--model-revision/--config-revision` are operator input |
| TTFT/TPOT vs SSE chunk timing | STILL OPEN | Inter-token values derive from chunk gaps or mean TPOT but are labelled `actual`; non-streaming sets TTFT = E2E |
| RAG evaluation fixture size | STILL OPEN | 2 documents, 2 queries, top_k 2 |
| vLLM model download blocked by default-deny | REQUIRES DESIGN APPROVAL | See network flow contract |
| HTTPRoute backend `fraud-coordinator-a2a` undefined | REQUIRES DESIGN APPROVAL | Not rendered by any chart; kagent serves A2A via its controller |

## Proposed Phase D scope (source-only)

1. MCP servers for `feature_lookup`, `fraud_scoring`, `knowledge_search`,
   `drift_detection` (typed async FastAPI, health, Helm rolling update +
   fallback) returning `ToolCallResponse` with manifest-bound citations.
2. MCP Gateway policy config on agentgateway (AuthZ, allowlist, rate limit,
   audit, PII redaction) with allow/deny tests.
3. Coordinator and two specialist SandboxAgent manifests with explicit
   ServiceAccounts, replicas/autoscale and registry publication manifests.
4. Jenkins scripts, merge-base change detection, Hypothesis idempotency tests,
   mutation testing configuration and Locust scenario.

Runtime gates deferred until ML Platform is ready: vLLM model staging, real
tool-call smoke, kagent/Substrate identity readback to resolve the flow
contract, Feast vector store decision, DataHub publication, benchmarks.
