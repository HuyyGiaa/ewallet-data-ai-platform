# G3 runtime continuation evidence — 2026-11-02 ICT

This append-only evidence set records the bounded EDAI-2 G3 continuation that
started from source commit `b4111a57fc0285ce118ea036038f6abb7b046867`.
The runtime implementation deployed in the final MCP image is commit
`642553f40cf395fed06eba52d04f6fa0db3fec94`.

## Scope and final state

- Project: `ewallet-data-ai-platform-hg26`.
- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`.
- Branch: `codex/llm-g2-llmd-routing`.
- Image: `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/mcp-tools@sha256:b80c07c76e4d240328804218c46ff66ecc7e4ba45694072f94774c57548fa058`.
- Final Helm revisions: `llm-agent=12`, `llm-agent-mcp=3`, `kagent=2`.
- G1/G2 benchmark, node-pool, IAM, cluster-wide RBAC, credential rotation,
  GCS write, model promotion, canonical/main merge and Git push were not
  performed.

## Verified claims

| Claim | Verdict | Primary evidence |
|---|---|---|
| Source and locked regression tests | PASS | `tests/mcp-envelope-regression-corrected.log` (163 tests), `tests/jenkins-contract.log` (7 tests) |
| Image build, scan, push and remote digest | PASS | `build/adapter/scan-and-push.log`, `build/adapter/image-digests.json`, `build/adapter/scans/mcp-tools.json` |
| Bounded MCP deployment | PASS | `deploy/helm-upgrade-mcp-rev3.log`, `runtime/final-platform-readback.log` |
| Remote MCP registration | PASS | `runtime/final-platform-readback.log` (both resources `Accepted=True`) |
| Real Feast Online feature lookup | PASS | `runtime/direct-flat-mcp-sdk-result.json` (31 historical features, 279.669 ms) |
| Real KServe fraud inference | PASS | `runtime/direct-flat-mcp-sdk-result.json` (probability `0.0639895560144887`, prediction `0`, 323.023 ms) |
| Real RAG retrieval | PASS | `runtime/direct-rag-knowledge-search.json` (2 source-corpus citations, Redis + exact cosine, no fallback) |
| MCP scope/revoked/unknown credential negatives | PASS | `security/mcp-negative-runtime.json`, `runtime/post-adapter-rollout-health-security.log` |
| A2A Gateway route exercised | PASS | `runtime/a2a-gateway-adapter-e2e.log`, `runtime/mcp-adapter-gateway-traces.log` |
| Coordinator business E2E | PARTIAL / BLOCKED | `runtime/coordinator-business-response-adapter.json`, `runtime/coordinator-task-session-refresh-readback.json` |
| Airflow RAG run | BLOCKED / NOT RUN | `runtime/airflow-rag-runs.json`, `runtime/rag-airflow-datahub-availability.log` |
| DataHub lineage publication | BLOCKED / UNAVAILABLE | `runtime/rag-airflow-datahub-availability.log` |
| Jenkins runtime execution | NOT VERIFIED | Source contract only; no live Jenkins runtime was available |
| Grafana/Tempo/OTLP trace backend | BLOCKED / UNAVAILABLE | Gateway trace IDs exist, but OTLP exporter reports zero resolver addresses |
| UI screenshot files | SCREENSHOT_PENDING | Genuine GKE and kagent UI views were inspected, but browser automation could not persist the image bytes locally |

## Coordinator E2E boundary

The first full request traversed Coordinator -> A2A Gateway -> Feature-RAG
Specialist and executed both `feature_lookup` and `fraud_scoring` successfully.
The Specialist final LLM response then exceeded the live model context limit
(`4097 > 4096`), so the Coordinator returned only a partial result.

A bounded Coordinator pod refresh created a new A2A context
`01a199aa-cc6a-7797-8806-f3fa02053887`, proving the old session cache was no
longer reused. The refreshed Coordinator nevertheless rewrote the complete
transaction into the literal string `{transaction_id}`, added an unrequested
drift action, and the downstream task remained `working` without tool
execution. No further paid or long-running retry was launched. Therefore the
campaign does not claim full business E2E PASS.

## Security interpretation

The raw network-bypass probe demonstrates that a TCP connection alone may be
possible inside the cluster. It does not establish authorization: the
application/Istio credential gates still denied unauthorized business calls.
This is recorded as defense-in-depth evidence, not as proof that every packet
is dropped at the network layer.

## Reproduction

See `docs/llm_agent/g3_runtime_evidence_runbook.md` and the committed helpers:

- `scripts/llm_agent/g3_runtime_readback.sh`
- `scripts/llm_agent/g3_mcp_smoke.py`
- `scripts/llm_agent/g3_a2a_smoke.py`

All helpers bind only to explicit loopback endpoints and never serialize MCP
credential material. Reproduction evidence is under `runtime/readback-script-test/`
and `runtime/repro-mcp-knowledge-search.json`.

## Resource closure

At final readback all relevant G3, Feast, KServe, MLflow and kagent workloads
were Ready. No new GKE node was created in this continuation. All temporary
local port-forwards were stopped. Local RAM still had about 10 GiB available;
no Docker build, image scan, Helm mutation or smoke-test process remained.

`SHA256SUMS.txt` is the integrity index for this directory. `manifest.json`
contains the machine-readable claim summary. No Secret value or credential
payload is intentionally included.
