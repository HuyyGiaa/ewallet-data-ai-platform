# B1 Testing Quality: Design and Implementation

This document covers EDAI-2 rows 28, 30, 31 and 32: what was built, what each
suite measures, and where the measurement stops.

> **Status: implementation prepared, final evidence pending.** No rubric row is
> promoted by this document. Final numbers come from the owner's runs of the
> [runbook](../../runbooks/edai2/B1_TESTING_RUNBOOK.md).

| Task | Row | Requirement | Entry point |
|---|---|---|---|
| B1.1 | 28 | Coverage above 90% | `scripts/testing/run_coverage.sh` |
| B1.2 | 31 | Property-based testing and idempotency | `scripts/testing/run_property_tests.sh` |
| B1.3 | 30 | Mutation testing with mutmut | `scripts/testing/run_mutation.sh` |
| B1.4 | 32 | Locust load test with HTML report | `scripts/testing/run_locust.sh` |

Defects found along the way are listed in [B1_FINDINGS.md](B1_FINDINGS.md).

## 1. Architecture

```text
ai_platform/inference/pyproject.toml   dependency groups: test, load
tests/
  inference/support.py        in-memory Feast and KServe network edges (shared)
  inference/test_*.py         unittest suites; CI and coverage run these
  property/                   Hypothesis suites (need the test group)
    strategies.py             generators only
    assertions.py             oracles and assertions only
    fraud_api_harness.py      synchronous API driver
    test_*_properties.py      one file per invariant family
    test_card_masking_regression.py  B1-F6 valid and invalid card candidates
    test_known_defects.py     strict xfail reports of confirmed defects
  llm_agent/test_b1_f6_card_masking.py  B1-F6 through the real Coordinator
  load/                       Locust harness
    scenarios.py              scenario and fault catalog
    settings.py               validated settings and the target guard
    payloads.py               seeded synthetic requests
    mock_server.py            real API with fake network edges, loopback only
    locustfile.py             user behaviour and outcome checks
    test_load_harness.py      harness tests that do not start Locust
  quality/
    coverage.ini              coverage scope
    mutmut.cfg                mutation scope
    equivalent_mutants.txt    reviewed equivalent mutants
scripts/testing/              runners and report generators
```

### Design decisions

- **Only the network edge is fake.** `EntityFeatureStore` replaces the Feast
  store and `DeterministicModelTransport` replaces the HTTP transport. The real
  `FeastOnlineAdapter`, `CanonicalFeatureAssembler`, `FraudV2WireContract`,
  `KServeV2Client`, `InferenceService` and FastAPI application all run.
- **Fakes are pure functions.** Feature values and scores are hashes of their
  inputs, so a test can state what a repeated request must return.
- **Dependency groups, not new projects.** `tests/ci/test_catalog.py` pins the
  list of Python projects. `uv sync --no-dev` does not install `test` or `load`,
  so the production image and the `uv.lock` versions of runtime packages do not
  change.
- **Hypothesis stays out of `tests/inference/`.** The CI command runs
  `unittest discover -s tests/inference` without the `test` group, so importing
  Hypothesis there would break CI.
- **CI is unchanged.** Adding coverage or mutation stages to Jenkins is a
  separate decision for the owner.

## 2. Coverage (B1.1, row 28)

Statement and branch coverage, using `pytest-cov` over the `unittest` suites in
`tests/inference/`. The run fails below 90% (`fail_under`). It writes terminal,
HTML, XML and JSON reports.

### Scope

| Measured | Why |
|---|---|
| All 13 modules of `ai_platform/inference/`, including `server.py` and `observability.py` | The whole Fraud API |
| `ai_platform/ml/training/derived.py` | Formulas of the six derived features |
| `ai_platform/ml/training/contract.py` | The 41-column contract |
| `ai_platform/features/feast/schema.py` | Feature view mapping |

**Exclusions:** no module, line or branch inside this scope is excluded. There
are no `pragma: no cover` markers and no `exclude_lines` rules. The only `omit`
is the project's `.venv`, which sits inside the package directory.

**Outside the scope:** `ai_platform/serving/` belongs to another component and
needs its own interpreter. `tests/integration/test_fraud_api_e2e.py` is not part
of the measurement because it needs a real Redis and the serving interpreter.

Property suites are not counted, so the coverage figure comes from deterministic
tests only and is the same on every run.

### Behaviours added

| File | Behaviour |
|---|---|
| `test_app_error_paths.py` | Status, code and retry flag for each dependency failure; sanitized 500; malformed JSON; correlation handling; production wiring |
| `test_assembler_boundaries.py` | Vector guards, type validation, JSON conversion, merchant rules, null and timestamp rules |
| `test_config_boundaries.py` | Fail-closed settings and Redis deadline injection |
| `test_feast_online_failures.py` | Construction limits, timestamp parsing, timeout detection, freshness limits, readiness |
| `test_kserve_failures.py` | Wire contract requirements, metadata identity, response identity, urllib transport failures |
| `test_wire_contract.py` | Encoding per datatype, metadata validation, response decoding |
| `test_observability.py` | Metrics and spans read from in-memory SDK readers, allowlists, redacted events |
| `test_server_entrypoint.py` | Telemetry resource, registry mirror order, trace logging, port selection |
| `test_service_failures.py` | Stage failures and the model schema self-check |

### Known uncovered paths

| Location | Reason |
|---|---|
| `assembler.py:142` | Defensive guard; non-merchant columns always contribute a timestamp or are reported missing first |
| `feast_online.py:210` | Merchant values are set to `[None]` by the adapter itself just before this check |
| `feast_online.py:130->135` | Requires a result with no timestamps, which `_get_features` cannot return |
| `server.py:82` | The `__main__` guard |
| `contract.py:91,93`, `derived.py:48` | Import-time and post-condition guards that a valid contract cannot trigger |

## 3. Property-based testing (B1.2, row 31)

### Idempotency: what the system actually guarantees

| Component | Guarantee | Mechanism |
|---|---|---|
| `POST /v1/fraud/predict` | Functional repeatability only: same inputs and backend state give the same score. **No deduplication, no conflict detection** | None; the API is stateless |
| Read-only MCP tools | Identical calls give identical responses for a fixed backend | None; no store |
| Coordinator | At-most-once delegation per scoped request id, result replay, conflict rejection | `IdempotencyStore` |

### Invariant catalog

| Family | File | Invariants |
|---|---|---|
| Request schema | `test_request_schema_properties.py` | Valid payloads accepted; 18 invalid partitions rejected; the four request-time features; JSON round trip; whitespace-insensitive ids; immutability; probability and prediction bounds |
| 41-input vector | `test_feature_vector_properties.py` | Frozen order and positions; derived values match an independent oracle; mapping order cannot change the vector; determinism; oldest timestamp; merchant nullability; required nulls reported by name; dropped columns refused; every vector encodes to 41 ordered inputs |
| Derived formulas | `test_derived_feature_properties.py` | `safe_divide` matches the oracle row by row; index preserved; zero denominator is missing, never infinite; non-numeric input is missing; the input frame is not mutated |
| Prediction API | `test_prediction_properties.py` | Bounded output; 422 before scoring with sanitized details; repeats identical; only the correlation id varies; the same instant in another offset scores the same; reused transaction id scored independently; concurrent requests agree and keep their own correlation id |
| Failure handling | `test_failure_properties.py` | Each fault always maps to one status, code and retry flag; failures leave no state behind; errors never echo request values; freshness limits are exact |
| Idempotency store | `test_idempotency_store_properties.py` | Stable, collision-free keys; one active owner; conflict in every live state; replay until TTL; bounded inputs; one winner under concurrent claims; **state machine compared with an independent reference model** |
| Coordinator | `test_coordinator_idempotency_properties.py` | Duplicates replay without new delegation; changed payload is a conflict; users and callers are isolated; a lost delegation is never repeated and ends ambiguous; cancelled claims can retry; concurrent duplicates delegate once |
| Tools | `test_tool_idempotency_properties.py` | `feature_lookup`, `fraud_scoring` and `knowledge_search` are repeatable; unknown features are rejected every time |
| Prompt safety | `test_prompt_safety_properties.py` | Clean text unchanged; exact placeholders; every detection counted; failed checksums kept; UUIDs and timestamps kept; masking is idempotent |

### Reproducibility

| Profile | Examples | Mode | Use |
|---|---|---|---|
| `b1-ci` (default) | 50 | Derandomized | Same inputs on every run |
| `b1-thorough` | 300 | Random, replay with `HYPOTHESIS_SEED` | Bug hunting; found B1-F6 |
| `b1-mutation` | 15 | Derandomized | Fast enough to run per mutant |

A failure prints the minimal example and a `@reproduce_failure` line. Deadlines
are off, so results do not depend on machine load. Time-dependent properties use
a fake clock and concurrent ones use barriers, so no assertion relies on `sleep`.

## 4. Mutation testing (B1.3, row 30)

### Scope

| Module | Reason |
|---|---|
| `ai_platform/inference/schemas.py` | Request validation |
| `ai_platform/inference/assembler.py` | Feature assembly |
| `ai_platform/inference/feast_online.py` | Freshness and failure classification |
| `ai_platform/inference/wire.py` | Model wire contract |
| `ai_platform/ml/training/derived.py` | Business formulas |
| `ai_platform/llm_agent/idempotency.py` | Idempotency |
| `ai_platform/llm_agent/prompt_safety.py` | Prompt safety |

Tests executed against mutants: `tests/inference`, `tests/property` (without
`test_known_defects.py`, whose strict xfails would count as kills), and the two
Coordinator suites in `tests/llm_agent`.

### Workspace

The runner copies tracked and new files into a temporary directory outside the
repository, installs `tests/quality/mutmut.cfg` there as `setup.cfg`, and runs
mutmut. `mutants/` therefore never appears in the repository, and local changes
in the working tree cannot be touched.

### Result categories

| Category | Meaning |
|---|---|
| Killed | At least one test failed |
| Survived (actionable) | No test failed; a possible test gap |
| Survived (equivalent) | Listed in `tests/quality/equivalent_mutants.txt` with a reason |
| Timeout | The mutant ran past the limit |
| No tests | No selected test reaches the mutated function |
| Error | Any other status, such as a segfault |

`scripts/testing/mutation_report.py` reports two scores: raw is killed divided by
exercised mutants, and actionable also removes reviewed equivalents from the
denominator. Timeouts stay in both denominators and do not count as killed, which
is the conservative reading. Only a survivor can be marked equivalent.

### Limitations

- mutmut 3 mutates function bodies. Class-level declarations such as
  `Field(gt=0)` and `CanonicalId` are not mutated; property tests check those
  constraints, but mutation does not measure them.
- The score applies to the seven modules above, not the whole repository.
- Survivors that only change an exception message stay in the actionable
  denominator. A message change is observable, so they are not equivalent, and
  pinning message text would add tests with no business value.
- Four `_is_backend_timeout` mutants loop forever and end as timeouts. They are
  detected in practice but are not counted as killed.
- Equivalence is a human review. Each entry states its reason so it can be
  challenged.
- `prompt_safety.py` mutants run against the property suite only, because
  `tests/llm_agent/test_n12_agent_telemetry.py` needs `httpx`, which this
  environment does not include.

## 5. Load testing (B1.4, row 32)

### Scenarios

| Scenario | Default weight | Request | Counted as success when |
|---|---|---|---|
| `valid` | 60 | Seeded valid request | 200, probability in range, transaction id preserved |
| `invalid` | 10 | One schema violation | 422 with `invalid_request` |
| `concurrent` | 15 | The same request from every user | 200 and the same score as every other response |
| `dependency_failure` | 10 | One of six faults | The declared status and code |
| `health` | 5 | `/health/live` or `/health/ready` | 200 |

A Locust failure means an **unexpected outcome**, not an HTTP error. A 503 from
the fault scenario is the correct result, and a 200 there would be a failure.

| Fault | Trigger | Expected |
|---|---|---|
| `feature-outage` | Synthetic `user_id` | 503 `feature_backend_error` |
| `feature-timeout` | Synthetic `user_id` | 504 `feature_timeout` |
| `feature-missing` | Synthetic `user_id` | 424 `missing_feature` |
| `feature-stale` | Synthetic `user_id` | 424 `stale_feature` |
| `model-outage` | Synthetic correlation id | 502 `model_backend_error` |
| `model-timeout` | Synthetic correlation id | 504 `model_timeout` |

Faults answer immediately, so their latencies do not represent a real timeout.

### Safety

- The default profile starts a local API on `127.0.0.1` with fake backends.
- A non-loopback host is refused twice: by a preflight before anything starts and
  by Locust's `init` listener, using `SystemExit` because Locust logs and ignores
  ordinary exceptions raised by listeners.
- A remote host needs `EWALLET_LOAD_PROFILE=integration` and
  `EWALLET_LOAD_ACKNOWLEDGE_REMOTE=I_OWN_THIS_TARGET`. That profile has no fault
  scenario, and a mix that requests one is rejected.
- The runner refuses more than 200 users or 900 seconds.
- All data is synthetic and marked with a `synthetic-` prefix.

### What the numbers mean

The local profile is a **mock-backed benchmark**. It measures this application's
own overhead on one machine, with Locust on the same host, and a simulated 5 ms
model latency by default. It does not include Redis, the network or a real model,
and must not be presented as production latency. Every summary carries this label.

## 6. Acceptance checklist

| Item | State |
|---|---|
| Coverage tooling, scope and four report formats | Implemented |
| Coverage above 90% from an owner run | **Pending owner run** |
| Hypothesis strategies, invariants and profiles | Implemented |
| Property suites pass in an owner run, both profiles | **Pending owner run** |
| mutmut configuration, runner and report | Implemented |
| Mutation score from an owner run | **Pending owner run** |
| Locust scenarios, guards, reports and cleanup | Implemented |
| Locust HTML report from an owner run | **Pending owner run** |
| Screenshots and evidence | **Pending owner run** |
| `TESTING.md`, `RUBRIC_EVIDENCE_INDEX.md`, `GAP_LEDGER.md`, `SCREENSHOT_AUDIT.md` | **Phase B, after evidence** |

## 7. Known limitations

- Coverage says which code ran, not whether results were checked. Mutation
  testing is the check on that.
- Fakes implement the observed Feast 0.66 and KServe V2 behaviour; only
  `tests/integration` verifies those against real services.
- The tool properties use the Phase D fixtures, so they prove repeatability of the
  tool code, not of a live feature store.
- The concurrency properties run on one event loop or one process. They do not
  model multi-replica behaviour. The Redis authority is covered by the existing
  durable-idempotency tests, not by these properties.
- The six defects in [B1_FINDINGS.md](B1_FINDINGS.md) remain open.
