# Phase D: MCP tool services (source only)

Status: **uncommitted, awaiting independent review.** Built on the local P0
commit `1215141`. Nothing was built into an image, deployed or connected to a
real backend. Every backend in the tests is an injected fixture.

## What exists

Package `ai_platform/llm_agent/tools/`, two services matching the release
inventory (`configs/llm_agent/release-v1.json`):

| Service | Tools | Production backends |
|---|---|---|
| `feature-rag-mcp` | `feature_lookup`, `fraud_scoring`, `knowledge_search` | Feast Online (`FeastOnlineAdapter`), KServe (`InferenceService` + `KServeV2Client`), knowledge retrieval **unavailable** |
| `drift-mcp` | `drift_detection` | Baseline registry and window sampler **unavailable** |

Existing contracts are reused, not duplicated: `ToolCallResponse`,
`VersionContext`, `RequestContext`, `ErrorResponse` and `Citation` from
`ai_platform/llm_agent/contracts.py`; `FraudPredictionRequest`,
`OnlineFeatureProvider`, `InferenceService`, the `InferenceError` hierarchy
and `MODEL_SCHEMA_SHA256` from `ai_platform/inference`; the 31 + 4 + 6 feature
contract from `ai_platform/ml/training/contract.py`; `TrustedChunkIndex` and
`to_agent_citations` from the RAG pipeline.

### Tools

| Tool | Input | Behaviour | Fail-closed cases |
|---|---|---|---|
| `feature_lookup` | `FraudPredictionRequest` + optional subset of the 31 authorized historical features | Values with event timestamps, request-time features; `materialization_verified: false`; derived features are never returned | Missing, stale or future features, backend down, non-numeric/boolean values |
| `fraud_scoring` | `FraudPredictionRequest` | KServe probability and prediction with `model_revision = <name>/<version>` | Other model or version, other transaction, no reviewed KServe wire contract (the Block B default), timeouts |
| `knowledge_search` | query (3..1000 chars, no control characters), `top_k` 1..5, `min_score` 0..1 | Ranked results with quotes and manifest-bound citations; query echoed only as SHA-256 | Production retriever reports `PUBLICATION_ONLY_RUNTIME_BLOCKED`; untrusted chunk, extra, duplicate, unranked or below-threshold hits |
| `drift_detection` | monitored feature, immutable `baseline_id`, current window (≤ 31 days) | PSI with per-bin contributions; method, bins, smoothing, minimum count and thresholds come from the baseline | No baseline registry or window source; unknown baseline; window overlapping the reference; identity mismatch; non-finite values; `insufficient_data`; `unclassified` without thresholds |

No drift policy was invented: there is no approved algorithm or threshold in
the repository, so the baseline must carry both.

### Service boundary (`service.py`)

- `POST /v1/tools/<tool>`: typed FastAPI routes (Pydantic body per tool);
  returns `ToolCallResponse` with HTTP 200/422/503/504/502 by status.
- `POST /mcp`: stateless MCP Streamable HTTP with JSON responses:
  `initialize` (2025-06-18, 2025-03-26), `ping`, `tools/list` with JSON
  schemas, `tools/call` returning `structuredContent` and `isError`.
  Notifications return 202; batches, bad JSON-RPC and unknown methods/tools
  return the standard error codes.
- `GET /health/live`: process liveness. `GET /health/ready`: transport
  readiness only (see "Readiness contract"). `GET /v1/capabilities`
  (authenticated): per-tool backend status for the caller's granted tools.
- Context from headers: `x-request-id`, `traceparent`, `x-ewallet-timeout-ms`
  (100..120000). The actor is the verified principal (see below).
- Code/data identity comes from `EWALLET_*` deployment variables; startup
  fails if any is missing or invalid.
- The executor bounds concurrency and deadlines, never logs arguments or
  results, and never returns exception text.

### Security boundary (`security.py`, hardening of 2026-10-31)

Before hardening, any client that could reach a server obtained tool access
by sending `x-ewallet-actor: <anyone>`, and `initialize`, `ping`,
`tools/list` and notifications needed no actor at all. Default-deny
NetworkPolicy was the only control, and no MCP Gateway exists to make it a
meaningful one.

Now every `/mcp` and `/v1/tools/*` request passes `RequestSecurityMiddleware`
before routing, body parsing or validation, in this order:

| Check | Rejection | Configuration |
|---|---|---|
| In-flight requests | 503 `server_busy`, `Retry-After: 1` | `EWALLET_MCP_MAX_INFLIGHT` (default 32, 1..1024) |
| `Origin` (DNS rebinding) | 403 `origin_not_allowed`; absent Origin allowed | `EWALLET_MCP_ALLOWED_ORIGINS`, exact `scheme://host[:port]`, default none |
| Declared and streamed body size | 413 `request_too_large`, 400 `invalid_content_length` | `EWALLET_MCP_MAX_BODY_BYTES` (default 64 KiB, 1 KiB..4 MiB) |
| Authentication | 401 `authentication_required` / `_failed` / `_not_configured`; 503 `authentication_unavailable` on verifier faults | `Authenticator` (see decision below) |
| Body read deadline (whole body, not per chunk; after authentication) | 408 `request_timeout`, `Connection: close`; the tool never runs and the in-flight slot is released | `EWALLET_MCP_BODY_TIMEOUT_SECONDS` (default 10, 0.05..60) |

Then, per request: the identity is the verified `Principal`; a client
`x-ewallet-actor` header that differs from it is 403 `actor_mismatch`. Tools
are authorized per principal by `ToolGrants` (`EWALLET_MCP_TOOL_GRANTS`, JSON
subject -> tool names, deny by default): `tools/list` shows only granted
tools, an ungranted `tools/call` is answered exactly like an unknown tool
(-32602, no enumeration), and an ungranted REST call is 403 `tool_forbidden`.
`/mcp` also parses media types per RFC 9110: `Content-Type` must be exactly
`application/json`, optionally with `charset=utf-8` (415 otherwise, so
`application/jsonp`, `application/json-seq` or unknown parameters are
refused); `Accept` must make `application/json` acceptable, the most specific
matching range wins and `q=0` refuses it (406). It requires integer/string
request ids and object params, and acknowledges client JSON-RPC responses
with 202. GET/DELETE are 405. Every path except `/health/` passes the
middleware (unknown paths are 401 before 404), FastAPI's `/docs`, `/redoc` and
`/openapi.json` are disabled, and a client that disconnects mid-body gets no
response and no tool execution (`request_aborted` event). Rejections log a `request_rejected` event with path, status and code only;
error bodies use fixed messages and never echo headers or verifier errors. The
Origin and size checks follow the official SDK's server transport
(`mcp/server/transport_security.py`, `streamable_http.py` in mcp 2.3.0).

**Authentication: DECISION_REQUIRED.** The production wiring uses
`DenyAllAuthenticator`, so deployed servers deny every tool request until a
mechanism is approved. The `Authenticator` protocol
(`async authenticate(headers) -> Principal`, optional `async readiness()`)
fits any mechanism. The compatibility preflight against kagent 0.10.3,
agentgateway and Istio Ambient, with one recommended option (Istio workload
identity behind the policy-only MCP Gateway), is in
`docs/llm_agent/phase_d_auth_preflight.md` and is PROPOSED — AWAITING
ARCHITECTURE APPROVAL.

No source-only test here is evidence of deployed authentication; the tests use
a fixture token table.

**Readiness contract (changed 2026-10-31).** Before: `/health/ready` was 503
unless every tool backend answered, so a missing drift baseline kept
`drift-mcp` NotReady forever (seen on kind): its Service had no endpoints,
even `initialize`/`tools/list` could not be routed, and a RollingUpdate with
`maxUnavailable: 0` could never complete. Now:

| Endpoint | Auth | Answers | 503 when |
|---|---|---|---|
| `/health/live` | none | process is serving | never (failure = no answer) |
| `/health/ready` | none | transport can authenticate and serve protected requests | the authenticator reports a code (`authentication_not_configured` for `DenyAllAuthenticator`, `authentication_unavailable` for verifier faults) |
| `/v1/capabilities` | required | `status`/`code` per granted tool from `ToolExecutor.readiness()` | — |

Tool backends (Feast, KServe, drift baselines, retrieval) never gate
readiness; their tools keep failing closed per call (`drift_detection` →
`drift_baseline_unavailable`). Authentication failures are not concealed: with
the production default the pods stay NotReady and say why, so enabling
`mcpServices` before an authentication decision deliberately yields no
endpoints. Health bodies carry no tool names or reasons.

### Helm (`templates/mcp-services.yaml`)

Disabled by default (`mcpServices.enabled: false`) because no reviewed server
image exists. When enabled, rendering fails unless the image is pinned by
digest, `identity.imageDigest` equals it, the Git SHA is 40 hex characters, and
all revisions and backend settings are set. Each service gets its own
ServiceAccount (token not mounted), 2 replicas, RollingUpdate
`maxUnavailable: 0`, a PodDisruptionBudget, live/ready/startup probes,
resource limits and a Pod Security Restricted-style context. No allow-flow is
added: the servers stay behind default-deny, and the MCP unresolved flow now
names the missing MCP Gateway workload.

### Dependency boundary

`infra/docker/llm-agent-tooling/pyproject.toml` + `uv.lock` (Living Decision
21.254 pattern: exact pins, `[tool.uv] package = false`, `uv lock --check`).
Runtime: FastAPI 0.159.1, Pydantic 2.14.1, Uvicorn 0.54.0, NumPy 2.2.6, pandas
2.3.3; extra `feast` (feast[redis] 0.66.0) for feature-rag-mcp; dev group
`mcp==2.3.0` (official SDK) for conformance tests only. `requires-python` is
`>=3.10,<3.13`: tests run on 3.10, the image on 3.12. The Dockerfile
`mcp-deps` stage now runs `uv sync --frozen --no-dev --extra feast
--no-install-project --no-build` with uv 0.13.5 copied from
`ghcr.io/astral-sh/uv` pinned by digest, into `/opt/venv` on the image's own
Python 3.12 (`UV_PYTHON_DOWNLOADS=never`). Every package in the lock has a
cp312 manylinux x86_64 or pure wheel (only `pywin32`, Windows-only, has none).
`requirements-mcp.in`/`.lock` are superseded, not used by the image and not
committed; `uv.lock` is the only dependency lock. The image has not been
built.

## Validation (2026-10-31, local, bounded)

| Check | Result | Evidence |
|---|---|---|
| Phase D tool/service tests (`ewallet-ml` env: FastAPI 0.159.1, Pydantic 2.14.1) | 43 tests OK | `docs/evidence/llm_agent/source_review/phase_d/phase_d_tools_unittest.log` |
| System-Python suite incl. Phase D chart tests | 80 tests OK | `.../phase_d/system_python_unittest.log` |
| Existing inference tests (regression) | 40 OK in `ewallet-ml`; `test_feast_online` needs `feast` (same on HEAD) and passes in `ewallet-feast` (15 OK) | `.../phase_d/inference_regression_unittest.log` |
| Branch coverage of `ai_platform/llm_agent/tools` | 94% (production `create_feature_rag_app` not exercised: needs a Feast repo) | `.../phase_d/phase_d_tools_coverage.txt` |
| Targeted mutation smoke (26 hand-written mutants on a temp copy) | 26/26 killed after adding 4 tests that the first run's survivors exposed; biased sample, **not** a mutmut score | `.../phase_d/targeted_mutation_smoke.json`, `scripts/validation/targeted_mutation_smoke.py` |
| `helm lint` default/dev/gcp; enabled render through `policy_lint` | 0 failures; `SOURCE_ONLY_PASS` | chart tests |

Security and protocol hardening (2026-10-31, after the rows above; evidence in
`docs/evidence/llm_agent/source_review/phase_d_security/`):

| Check | Result | Evidence |
|---|---|---|
| `uv lock --check` (uv 0.13.5) | lock consistent, 100 packages | `uv_lock_check.txt` |
| Tools, security and official-SDK tests in the locked project env (CPython 3.10.22, mcp 2.3.0) | 64 tests OK | `mcp_project_env_unittest.txt` |
| Same suites in the existing `ewallet-ml` env (no SDK) | 60 OK, 4 SDK tests skipped | `ewallet_ml_env_unittest.txt` |
| System-Python `tests/llm_agent` (chart, P0, Phases A–C) | 126 run, 62 OK, 64 FastAPI tests skipped | `system_python_llm_agent_unittest.txt` |
| `helm lint` dev/gcp/local; gcp/local render through `policy_lint` | 0 failures; `SOURCE_ONLY_PASS` | `helm_lint_policy.txt` |
| Targeted mutation smoke incl. 7 security mutants | 32/32 killed; still not a mutmut score | `targeted_mutation_smoke.json` |

All of it is SOURCE_ONLY_PASS: in-process ASGI with a fixture token table, no
deployed gateway, mesh or identity provider.

Final source hardening (readiness contract, RFC 9110 media types, disconnect,
schema endpoints, uv-based Dockerfile; evidence in
`docs/evidence/llm_agent/source_review/phase_d_final/`):

| Check | Result | Evidence |
|---|---|---|
| `uv lock --check` | consistent | `uv_lock_check.txt` |
| Tools, security, readiness and official-SDK tests (locked env, CPython 3.10.22, mcp 2.3.0, starlette 1.10.0) | 73 tests OK | `mcp_project_env_unittest.txt` |
| Same in `ewallet-ml` (starlette 1.7.0, no SDK) | 69 OK, 4 SDK tests skipped | `ewallet_ml_env_unittest.txt` |
| System-Python `tests/llm_agent` | 135 run, 62 OK, 73 FastAPI tests skipped | `system_python_llm_agent_unittest.txt` |
| `helm lint` dev/gcp/local, `policy_lint` gcp/local, `docker build --check` (no build) | 0 failures, `SOURCE_ONLY_PASS`, no warnings | `helm_docker_checks.txt` |
| Targeted mutation smoke (12 security mutants) | 37/37 killed; not a mutmut score | `targeted_mutation_smoke.json` |

Not run: Hypothesis (not installed), mutmut, Locust, any image build, any
deployment, any real backend.

## Rubric impact (EDAI-2, no new VERIFIED)

| Row | Points | Label | Change |
|---|---|---|---|
| 16 FastAPI + Pydantic + healthcheck | 1 | SOURCE_TESTED | Typed routes, Pydantic models, live/ready probes |
| 17 async concurrency | 1 | SOURCE_TESTED | Bounded async executor, concurrency tests |
| 18 MCP tool Helm + rolling update + fallback | 2 | SOURCE_IMPLEMENTED | Render-tested Deployments; no image, rollout or rollback evidence |
| 22 second agent with Feature Store, ML inference, RAG tools | 8 | SOURCE_IMPLEMENTED (tools only) | Agent itself is Phase E |
| 23 MCP Gateway | 2 | BLOCKED | Servers speak MCP; gateway not defined |
| 28 coverage > 90% | 1 | SOURCE_TESTED | 94% on the tools package only |
| 29 equivalence/boundary | 2 | SOURCE_TESTED | Explicit partitions and boundaries |
| 30 mutation > 80% | 2 | SOURCE_TESTED (partial) | Targeted smoke only |
| 31 property-based idempotency | 2 | SOURCE_TESTED (partial) | Idempotency + seeded property checks, no Hypothesis |
| 51 agent/MCP telemetry | 2 | SOURCE_IMPLEMENTED (partial) | Structured tool-call events, no counters |

`docs/llm_agent/rubric_gap_matrix.csv` carries these labels. The scored
ledger `docs/rubric/rubric_evidence_matrix.csv` is unchanged until this work
is reviewed and committed; proposed ledger edits after commit: rows 30 and 31
OPEN → PARTIAL with the evidence above, source paths for rows 16–18 and 22.

## Not implemented / runtime dependencies

1. Server image build from the uv-based Dockerfile (source exists, not built).
2. KServe wire contract for the registered model (Block B leaves it open), so
   `fraud_scoring` is an error in production until it is reviewed.
3. Approved vector-search backend for `knowledge_search`.
4. Approved drift baselines (bins, thresholds) and a current-window source.
5. MCP Gateway workload, its identity and the allow-flows to the two servers;
   Feast Online / KServe egress targets.
6. Authentication mechanism (DECISION_REQUIRED, options A–D above); until then
   production servers deny every tool request.
7. Image build from the uv-based Dockerfile and a runtime readback of
   `/health/ready` and `/v1/capabilities` on a cluster.
8. (Resolved) The superseded `requirements-mcp.in`/`.lock` are not committed.
9. Agents (coordinator, specialists), registry publication, autoscaling: Phase E.
