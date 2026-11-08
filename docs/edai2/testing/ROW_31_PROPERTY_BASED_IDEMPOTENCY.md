# Property-Based Testing and Idempotency Verification

Retries are routine in the fraud platform: clients resubmit predictions, agents repeat tool calls, and the Coordinator may see the same delegation twice. Repeating a call must therefore not change its result or create duplicate work. This report describes how that idempotency is verified with property-based tests in Hypothesis, which generate the inputs instead of relying on a few hand-written examples.

**Principal findings:**
- On the B1 commit `281db72`: 551 properties and tests pass, with 13 expected failures, at both 50 and 300 generated examples per property.
- On the integration HEAD `d388bd9`: 566 pass, with 9 expected failures.
- The expected failures are documented known defects, kept as strict `xfail` so that a fix is noticed.

## 1. Approach

A parameterized test checks the cases someone wrote down. A **property** states a rule that must hold for *every* input, and [Hypothesis](https://hypothesis.readthedocs.io/) generates the inputs, including edge cases and shrunk counter-examples. For idempotency the rule is *repeat the call and nothing changes*, which is exactly the kind of rule a few hand-picked cases would miss.

Configuration ([`conftest.py` (line 28)](../../../tests/property/conftest.py#L28)):
- `b1-ci`: 50 derandomized examples per property, identical on every run;
- `b1-thorough`: 300 randomized examples per property;
- `b1-mutation`: 15 examples, used inside the mutation run.

Hypothesis is pinned at `6.168.5` in the inference project's lockfile.

## 2. Properties under test

| Property | Where |
|---|---|
| **Model prediction is consistent:** repeating a request returns an identical response; the same instant written with another UTC offset scores identically; concurrent identical requests agree and keep their own correlation id | [`test_prediction_properties.py` (line 61)](../../../tests/property/test_prediction_properties.py#L61), [line 75](../../../tests/property/test_prediction_properties.py#L75), [line 96](../../../tests/property/test_prediction_properties.py#L96) |
| **Agent tools are repeatable:** `feature_lookup`, `fraud_scoring` and `knowledge_search` return the same response on every repeat for a generated transaction or query; unknown features are rejected every time | [`test_tool_idempotency_properties.py` (line 47)](../../../tests/property/test_tool_idempotency_properties.py#L47) |
| **Idempotency store:** stable, collision-free keys; one active owner; replay until TTL; one winner under concurrent claims. A Hypothesis **state machine** compares the store with an independent reference model | [`test_idempotency_store_properties.py` (line 343)](../../../tests/property/test_idempotency_store_properties.py#L343) |
| **Coordinator:** duplicates replay without a new delegation; a changed payload is a conflict; concurrent duplicates delegate once | [`test_coordinator_idempotency_properties.py`](../../../tests/property/test_coordinator_idempotency_properties.py) |
| Supporting invariants: schema, 41-input vector, derived formulas, failure mapping, masking idempotency | [B1_TESTING_QUALITY.md §3](B1_TESTING_QUALITY.md#3-property-based-testing-b12-row-31) |

What "idempotent" means differs per component: the Fraud API is stateless, so it guarantees functional repeatability only, not deduplication. The guarantee per component is stated in [B1_TESTING_QUALITY.md §3](B1_TESTING_QUALITY.md#3-property-based-testing-b12-row-31).

## 3. Results

| Run | Code | Profile | Result | Evidence |
|---|---|---|---|---|
| B1 session, 2026-11-06 | `281db72` | `b1-ci` (50) | 551 passed, 13 xfailed, 33 s | [raw](../../evidence/edai2/testing/b1_281db72_20261106/property/ci/terminal-b1-ci.txt) |
| B1 session, 2026-11-06 | `281db72` | `b1-thorough` (300) | 551 passed, 13 xfailed, 188 s | [raw](../../evidence/edai2/testing/b1_281db72_20261106/property/thorough/terminal-b1-thorough.txt) |
| Integration HEAD, 2026-11-08 | `d388bd9` | `b1-ci` (50) | 566 passed, 9 xfailed, 36 s | [raw](../../evidence/edai2/testing/head_property_20261108T082124Z/terminal-b1-ci.txt) |

The **xfails are known defects**, pinned as strict expected failures in [`test_known_defects.py`](../../../tests/property/test_known_defects.py), so a fix shows up as an unexpected pass. G2 fixed B1-F7 (phone and email masking at digit boundaries, four parametrized cases) in commit `8fe0f98`; it is now a regular regression test, which takes the count from 13 to 9. The remaining xfails are B1-F1 to B1-F5. R8-P1 (raw prompts persisted by the kagent task store) is pinned separately in `tests/llm_agent/test_r8_pii_screen.py` and is not part of this count.

> **Figure 1. An idempotency property for agent tools (`31_03`).** `@given(transactions, feature_subsets, duplicate_counts)` generates a transaction, a feature subset and a repeat count. `assert_repeatable` calls the tool that many extra times and requires every response to equal the first. Unlike a parameterized test, the inputs are generated rather than listed.

![31_03 idempotency property source](../../evidence/edai2/screenshots/testing/31_03_idempotency_property_source.png)

> **Figure 2. Hypothesis statistics, CI profile, B1 commit (`31_01`).** For each property Hypothesis reports, for example, `50 passing, 0 failing`. Filtered (invalid) examples are counted separately, and generation stops at `settings.max_examples=50`. The run ends with `551 passed, 13 xfailed`; the 13 are the known defects described above, not passes.

![31_01 Hypothesis CI results](../../evidence/edai2/screenshots/testing/31_01_hypothesis_ci_results.png)

> **Figure 3. Thorough profile, B1 commit (`31_02`).** The same properties with 300 examples each. `knowledge_search` stops early because Hypothesis exhausted its input space. The run ends with `551 passed, 13 xfailed in 188 s`.

![31_02 Hypothesis thorough results](../../evidence/edai2/screenshots/testing/31_02_hypothesis_thorough_results.png)

## 4. Limitations

- **CrossHair is not used.** It is a symbolic-execution backend for Hypothesis that could find inputs random generation misses.
- **No screenshot of the HEAD run exists;** its raw output is committed. No thorough-profile run exists for HEAD.
- **The properties run against in-process fakes** (`tests/property/fraud_api_harness.py`), not the deployed services. Live repeatability was observed separately in the GKE load tests ([Web API Load Testing and Performance Analysis on GKE](ROW_32_LOCUST_GKE.md)).

## 5. Reproducing the runs

```bash
scripts/testing/run_property_tests.sh docs/evidence/edai2/testing/<run-id>                         # b1-ci
HYPOTHESIS_PROFILE=b1-thorough scripts/testing/run_property_tests.sh docs/evidence/edai2/testing/<run-id>
```
