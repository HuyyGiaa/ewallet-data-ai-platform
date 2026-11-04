# Block B Online Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the source-only Block B fraud inference path with a validated FastAPI boundary, exact frozen 41-feature assembly, Feast Online and KServe V2 adapters, observability hooks, and bounded local tests.

**Architecture:** Keep the frozen path `FastAPI -> Feast Online/Redis -> 41-feature assembly -> KServe Standard -> response -> observability`. Reuse the executable training contract and derived formulas; isolate external systems behind async adapters and dependency injection so local tests use explicit fakes without claiming Redis/KServe parity.

**Tech Stack:** Python 3.10, FastAPI, Pydantic v2, pandas, Feast 0.66, OpenTelemetry API, stdlib HTTP transport, unittest.

**Spec:** `/home/huyy-giaa/.codex/attachments/c0e8db1b-8380-4d27-be08-f83751774b3d/pasted-text.txt`; canonical architecture `https://docs.google.com/document/d/1uShjF2P0t45SNVGOxFg185I62YLTZeMNXImEiHMNPbc` through Living Decision 21.128, including the independent source-audit correction request.

## Global Constraints

- Preserve the exact frozen split: 4 request-time + 31 historical + 6 derived = 41 model inputs.
- Reuse `MODEL_FEATURE_COLUMNS`, `FEATURE_SPECS`, and `add_derived_features`; do not redefine feature formulas or business semantics.
- Preserve Block A freshness policy: FeatureView TTL 30 days 10 minutes, maximum staleness 10 minutes, no assumed Redis physical key expiry.
- Do not install or upgrade packages, change locks, mutate cloud/GCS/GKE/Redis/KServe, modify other worktrees, merge, push, commit, or clean worktrees.
- Mocked local tests prove source contracts only, never live Feast Online/Redis or registered-model/KServe parity.

## Review Focus

- Merchant-null requests must preserve explicit nullable merchant semantics without silently defaulting merchant features.
- Every non-applicable exception aside, missing values, missing feature timestamps, stale features, and future feature timestamps must fail closed with distinct error classes.
- The 41 ordered DataFrame columns and two MLflow PyFunc output columns are source-established; the KServe tensor layout is not. Runtime inference must fail closed until a reviewed wire contract is injected.
- JSON nulls caused by frozen zero/missing denominator semantics must remain null, never become zero or non-standard NaN.
- Logs, spans, errors, and metrics may carry correlation/model/stage metadata but must not carry amount or entity payloads.

---

### Task 1: API and model-input contracts

**Files:**
- Create: `ai_platform/inference/contracts.py`
- Create: `ai_platform/inference/schemas.py`
- Create: `ai_platform/inference/errors.py`
- Test: `tests/inference/test_contracts_and_schemas.py`

**Interfaces:**
- Consumes: frozen training and Feast schemas.
- Produces: validated `FraudPredictionRequest`, response/error models, ordered KServe input specifications, and classified exceptions.

- [ ] Write request-validation and exact 41-column/type/order tests.
- [ ] Run the focused tests and observe missing-module failures.
- [ ] Implement the minimal contracts and Pydantic models.
- [ ] Rerun focused tests to green; checkpoint without committing.

### Task 2: Deterministic feature assembly

**Files:**
- Create: `ai_platform/inference/assembler.py`
- Test: `tests/inference/test_assembler.py`

**Interfaces:**
- Consumes: `FraudPredictionRequest`, `HistoricalFeatureSet`, and existing `add_derived_features`.
- Produces: immutable `ModelInputVector` in exact `MODEL_FEATURE_COLUMNS` order.

- [ ] Write failing tests for 41 inputs, deterministic order, formula parity, missing/extra/type drift, and merchant-null behavior.
- [ ] Verify RED.
- [ ] Implement strict assembly with explicit null handling and no default values.
- [ ] Verify focused and adjacent training-contract tests are green; checkpoint without committing.

### Task 3: Feast Online adapter

**Files:**
- Create: `ai_platform/inference/feast_online.py`
- Test: `tests/inference/test_feast_online.py`

**Interfaces:**
- Consumes: Feast 0.66 `get_online_features(..., full_feature_names=True)` and canonical entity/view mappings.
- Produces: `OnlineFeatureProvider.get_features(request)` with values and per-feature event timestamps.

- [ ] Write failing tests for entity rows, all 31 refs, missing feature/timestamp, stale/future data, backend failure, and merchant-null exception.
- [ ] Verify RED.
- [ ] Implement async request-scoped retrieval and distinct errors using the frozen 10-minute stale threshold.
- [ ] Verify focused tests in `ewallet-feast`; checkpoint without committing.

### Task 4: KServe V2 adapter

**Files:**
- Create: `ai_platform/inference/kserve.py`
- Test: `tests/inference/test_kserve.py`

**Interfaces:**
- Consumes: `ModelInputVector`, KServe V2 metadata/readiness/infer endpoints.
- Produces: validated `ModelPrediction` carrying the existing `fraud_probability` and `fraud_prediction` output semantics.

- [ ] Write failing tests for request shape/order, metadata drift, identity/version mismatch, malformed outputs, timeout, and backend errors.
- [ ] Verify RED.
- [ ] Implement an async transport abstraction plus stdlib HTTP transport, bounded timeout, metadata validation, and response validation.
- [ ] Verify focused tests; checkpoint without committing.

### Task 5: Service, observability, configuration, and FastAPI

**Files:**
- Create: `ai_platform/inference/config.py`
- Create: `ai_platform/inference/observability.py`
- Create: `ai_platform/inference/service.py`
- Create: `ai_platform/inference/app.py`
- Create: `ai_platform/inference/__init__.py`
- Test: `tests/inference/test_service_and_app.py`

**Interfaces:**
- Consumes: feature provider, assembler, predictor, optional OpenTelemetry hooks.
- Produces: `/v1/fraud/predict`, `/health/live`, `/health/ready`, structured errors, correlation propagation, and stage latency/error metrics.

- [ ] Write failing DI service and raw-ASGI FastAPI tests, including invalid payloads and no-sensitive-logging assertions.
- [ ] Verify RED.
- [ ] Implement validated environment settings, service orchestration, OTel hooks, middleware, handlers, and health endpoints.
- [ ] Verify focused tests; checkpoint without committing.

### Task 6: Bounded verification and handoff

**Files:**
- Modify only if a defect is reproduced by a new failing test.

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: exact test evidence, diff/status inventory, and explicit live-integration blockers.

- [ ] Run all Block B tests under the existing `ewallet-feast` environment.
- [ ] Run adjacent Feast/training/modeling tests in their compatible existing environments and report environment-split limitations.
- [ ] Run compileall, `git diff --check`, contract count/hash inspection, and final Git status.
- [ ] Review for secret/payload logging, unrelated changes, architecture drift, and unverified runtime claims.

### Task 7: Bounded Feast Online retrieval

**Files:**
- Modify: `ai_platform/inference/errors.py`
- Modify: `ai_platform/inference/config.py`
- Modify: `ai_platform/inference/feast_online.py`
- Modify: `ai_platform/inference/app.py`
- Test: `tests/inference/test_feast_online.py`
- Test: `tests/inference/test_service_and_app.py`

**Interfaces:**
- Consumes: synchronous Feast 0.66 `FeatureStore`, Redis `connection_string` parameters, and `EWALLET_FEAST_TIMEOUT_SECONDS`.
- Produces: coroutine deadline classification plus verified Redis `socket_connect_timeout` and `socket_timeout` configuration before the lazy client is created.

- [ ] Add failing tests for request/readiness deadline classification and for Redis timeout injection/validation without connecting to Redis.
- [ ] Verify RED and record that cancelling `asyncio.to_thread` does not terminate its worker.
- [ ] Add `FeatureTimeoutError`, validated settings, bounded adapter calls, and a fail-closed Redis timeout configurator.
- [ ] Verify focused tests are green; checkpoint without committing.

### Task 8: Fail-closed KServe wire contract

**Files:**
- Modify: `ai_platform/inference/contracts.py`
- Modify: `ai_platform/inference/kserve.py`
- Modify: `tests/inference/test_contracts_and_schemas.py`
- Modify: `tests/inference/test_kserve.py`

**Interfaces:**
- Consumes: exact `MODEL_FEATURE_COLUMNS`, local logical dtypes, `FraudPyFuncModel` output columns, and optional KServe V2 metadata `versions`.
- Produces: source-level model schema fingerprint plus an injected `KServeWireContract`; no default tensor layout is supplied before registered-model evidence.

- [ ] Add failing tests proving missing wire contract blocks calls, null-valued merchant/undefined-ratio vectors require explicit nullable transport support, and omitted metadata `versions` is accepted only on the version-scoped endpoint.
- [ ] Verify RED.
- [ ] Separate model column schema from serving tensors; make metadata layout encoding/decoding an explicit injected dependency while retaining strict name and present-version checks.
- [ ] Verify focused tests are green; checkpoint without committing.

### Task 9: Readiness scope and pinned Feast timestamp boundary

**Files:**
- Modify: `ai_platform/inference/feast_online.py`
- Modify: `ai_platform/inference/schemas.py`
- Modify: `ai_platform/inference/app.py`
- Modify: `tests/inference/test_feast_online.py`
- Modify: `tests/inference/test_service_and_app.py`

**Interfaces:**
- Consumes: Feast 0.66 `OnlineResponse.to_dict(include_event_timestamps=True)` behavior.
- Produces: an explicit dependency-connectivity readiness response that never represents materialization or offline/online parity evidence.

- [ ] Add a narrow Feast 0.66 characterization test for full-name keys, `__ts` integer seconds, and missing value behavior; keep the exact 10-minute threshold test.
- [ ] Add a failing API test for readiness scope/materialization/parity flags.
- [ ] Implement the minimal schema/endpoint clarification and adapter documentation.
- [ ] Verify focused tests are green; checkpoint without committing.

### Task 10: Correction verification and inventory

**Files:**
- Modify only if a defect is reproduced by a new failing test.

**Interfaces:**
- Consumes: Tasks 7–9.
- Produces: fresh Block B evidence, unchanged 4+31+6 order/formulas, complete untracked inventory, and explicit live integration gates.

- [ ] Run the full bounded Block B suite, compileall, frozen contract inspection, whitespace scan, and Git status.
- [ ] List all untracked files and identify every path outside `ai_platform/inference` and `tests/inference`.
- [ ] Self-review the complete uncommitted source because no new reviewer task was requested; stop for independent review without commit, merge, push, or cleanup.
