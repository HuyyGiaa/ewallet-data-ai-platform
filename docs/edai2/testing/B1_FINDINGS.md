# B1 Findings: Defects Found While Building the Test Suites

The B1 suites found six defects in production code. **None was fixed**: B1 only
adds tests, and each fix is a business decision for the owner.

Each defect has a strict `xfail` test in
[`tests/property/test_known_defects.py`](../../../tests/property/test_known_defects.py).
The test states the correct behaviour. It reports `xfailed` today and turns the
run red once the defect is fixed, which is the signal to delete the marker.

| ID | Severity | Summary | Location |
|---|---|---|---|
| B1-F6 | **High** (PII exposure), **fixed in the working tree, not deployed** | A valid card number next to another number or in a common format was not masked and reached the LLM | `ai_platform/llm_agent/prompt_safety.py` |
| B1-F7 | Medium (PII exposure), open | A phone or email address followed by `-` or `.` and a digit, or glued to a word, is not masked | `ai_platform/llm_agent/prompt_safety.py` |
| B1-F1 | Medium | A schema-valid edge timestamp answers 500 instead of a client error | `ai_platform/inference/feast_online.py` |
| B1-F2 | Low | `internal_error` is counted twice per request | `ai_platform/inference/service.py`, `app.py` |
| B1-F3 | Low | The 500 response has no `x-correlation-id` header | `ai_platform/inference/app.py` |
| B1-F4 | Low | `fraud_prediction` values such as `1.9`, `true` and `"1"` are accepted | `ai_platform/inference/wire.py` |
| B1-F5 | Low | `amount: true` and `amount: "100"` are accepted | `ai_platform/inference/schemas.py` |

## B1-F6: card number leaks to the LLM (High, fixed)

**Found by:** the randomized `b1-thorough` profile. The default derandomized
profile did not find it, so both profiles belong in the final run.

**Status.** Fixed in the working tree. The running `llm-agent` release still has
the defect until a new agent image is built and deployed by the owner.

**Cause.** The old `_CARD` regex made one greedy match across digits, spaces and
hyphens, then checked Luhn on the whole match. Two faults followed:

- a card next to another number was checked as one longer number, which usually
  failed Luhn and returned the text unchanged;
- the `(?<![\w.-])` and `(?![\w.-]*\d)` boundaries rejected any card attached to
  a word, a dot or a dash, and only one space or hyphen was accepted between
  digits.

The reported input was one case of a wider gap. Before the fix, the following
all reached the LLM unmasked:

| Class | Examples |
|---|---|
| Number after the card | `card 4111111111111111 5 items`, `4111111111111111-5`, `4111111111111111.5` |
| Number before the card | `5 4111111111111111`, `5 4111 1111 1111 1111` |
| Attached to text | `card4111111111111111`, `x-4111111111111111`, `ref.4111111111111111`, `card_4111111111111111` |
| Other group separators | `4111.1111.1111.1111`, `4111/1111/1111/1111`, `4111  1111  1111  1111`, tab, no-break space, en dash |

When the longer number passed Luhn by chance, the neighbour was masked too:
`paid 4111111111111111 300 VND` lost the `300`.

**Path.** Only one place sends analyst text to an LLM:
A2A `message/stream` → `CoordinatorExecutor` or the Specialist's executor →
`plan()` → `screen_prompt()` → `OpenAIToolPlanner.select()` → agentgateway → vLLM.
`plan()` replaces the text with the screened value before the UUID check,
grounding and the LLM call, so the fix belongs in `screen_prompt`.

**Fix.** `prompt_safety.py` now judges a card on its own digits:

1. a solid run of 13 to 19 digits that passes Luhn is masked first, whatever is
   next to it;
2. in the remaining text, digit groups joined by whitespace or dashes, or by
   `. , / _` between groups of four or more digits, are tested as every
   consecutive sequence of groups, and all valid sequences are masked;
3. UUIDs and ISO timestamps are never touched, and digits inside an email are left
   for the email detector.

No business logic, dependency, metric name or placeholder changed.

**Verification.**

- `tests/property/test_card_masking_regression.py` covers valid and invalid
  candidates, with public test card numbers only.
- `tests/llm_agent/test_b1_f6_card_masking.py` drives the real Coordinator and
  checks the LLM request bytes, the A2A event stream, log records at INFO and all
  span attributes, including FastAPI and HTTPX instrumentation spans.
- Against the old code these suites fail, so they detect the defect.
- An old-versus-new comparison on 200,000 seeded random texts found no input
  where the old code masked a card and the new code did not.
- On 100,000 generated legitimate prompts the same number of prompts changed
  with both versions, so no new false positive was seen.

**Known limits of the card screen.**

- A number that is 17 digits or longer because digits were glued to a card, such
  as `41111111111111115`, is not searched inside. Searching would mask one window
  in ten of any long number.
- Punctuated groups need four or more digits each, so `4222.2222.2222.2` is not
  joined. Spaces and dashes have no such limit.
- A card split across lines, or by zero-width characters, is not joined.
- A grouped card with a neighbouring number can mask that number too when the
  longer sequence also passes Luhn: `4111 1111 1111 1111 300`. This errs towards
  masking. If the lost number was the amount, grounding rejects the request.
- A grouped card whose last group runs straight into a date and time, such as
  `4111 1111 1111 1111-06-01 10:00`, reads as a timestamp and is not masked.

**Raw prompt outside the LLM path.** These do not involve the LLM and were not
changed. Both need an owner decision:

- **A2A task history.** The SDK stores the original user message in the task. In
  the cluster `KAgentTaskStore` sends it to the kagent controller, so the raw
  value is stored there. Measured with `InMemoryTaskStore`.
- **DEBUG logging.** `a2a.server.apps.jsonrpc.jsonrpc_app` logs the request body
  at DEBUG. The chart does not set `LOG_LEVEL`, so the default INFO keeps this
  off. Enabling DEBUG on a live agent would write raw prompts to its logs.

Declarative kagent agents in `infra/helm/llm-agent-platform/templates/agents.yaml`
call the model through kagent and do not run `screen_prompt`. Any such agent that
is exposed to chat is outside this screen.

## B1-F7: phone or email next to a digit is not masked (Medium, open)

Found while reviewing B1-F6. It has the same family of cause but is outside the
authorized card fix, so it is reported only and pinned by a strict xfail.

| Input | Output of `screen_prompt` |
|---|---|
| `0912345678 5 items` | `[REDACTED_PHONE] 5 items` |
| `0912345678-5` | unchanged |
| `0912 345 678-5` | unchanged |
| `sdt0912345678` | unchanged |
| `a@b.com-5` | unchanged |

**Cause.** `_PHONE` and `_EMAIL` reject matches through their boundary lookarounds
`(?<![\w+.-])`, `(?![\w.-]*\d)` and `(?![\w-])`.

**Suggested direction.** Limit the phone boundaries to "no adjacent digit", as the
card fix does, and let an email end before `-`. Phone numbers have no checksum,
so glued digits raise the false-positive risk more than for cards; use the
old-versus-new comparison before accepting a change.

## B1-F1: edge timestamps answer 500 (Medium)

`FraudPredictionRequest` accepts `0001-01-01T00:00:00+05:00` and
`9999-12-31T23:59:59-05:00`. `request.event_timestamp.astimezone(timezone.utc)`
in `FeastOnlineAdapter` then raises `OverflowError`, which is outside the
`InferenceError` hierarchy, so a client input produces a 500. The same values in
UTC do not overflow and answer 424.

**Behaviour change if fixed.** These requests move from 500 to a 4xx. No valid
transaction uses these dates. Reject instants outside a supported range in the
schema, or catch `OverflowError` beside the two `astimezone` calls.

**Regression risk.** Low. Choose bounds that keep every historical timestamp in
the training data, and apply them to the typed and natural-language paths alike.

## B1-F2: `internal_error` counted twice (Low)

`InferenceService.predict` records the error, then the application's `Exception`
handler records it again, so `fraud_inference_errors_total{error.code="internal_error"}`
rises by two per failed request. `InferenceError` failures are counted once.

**Behaviour change if fixed.** API responses stay the same; the error metric
halves for this one code. Record it in one place only, preferably the `Exception`
handler, because it also sees failures raised outside `predict`.

**Regression risk.** Low, but any alert threshold or dashboard tuned to the
doubled count must be rechecked.

## B1-F3: no correlation header on 500 (Low)

The `Exception` handler runs in Starlette's outermost middleware, outside the
correlation middleware. The body carries the correlation id but the
`x-correlation-id` response header is absent.

**Behaviour change if fixed.** A header is added to 500 responses; the body is
unchanged. Set the header in the handler from the correlation id it already has.

**Regression risk.** Low. Moving exception handling into the middleware would
carry more risk, because it changes the error flow.

## B1-F4: model output coerced (Low)

`decode_response` checks the declared `INT64` datatype but then calls `int()` on
the value, so `1.9` becomes `1`, and `true` and `"1"` become `1`. The same
applies to `float()` for the probability. A malformed model response should be a
`ModelResponseError`.

**Behaviour change if fixed.** A malformed model response becomes a controlled
error instead of a score. Check the type before converting, and accept only real
integers for `fraud_prediction`.

**Regression risk.** Medium. Some serving runtimes emit `1.0` for an integer
output. Check a captured response from the deployed KServe model first, or a
valid model could start failing.

## B1-F5: lax amount parsing (Low)

Pydantic's lax mode turns `true` into `1.0` and `"100"` into `100.0`.

**Behaviour change if fixed.** Those requests move from 200 to 422. Use a strict
float for `amount`, keeping integer JSON numbers such as `100` valid.

**Regression risk.** Medium. This is a public contract change, and
`FraudPredictionRequest` is also used by the agent intake and the typed A2A path.
Confirm that no client sends string amounts, and test integer amounts explicitly.

## Behaviour confirmed, not a defect

**Overflow differs between training and serving.** `safe_divide` returns `inf`
when a quotient overflows, for example `1.0 / 2.2e-309`. The online assembler then
turns `inf` into null, while training keeps `inf`. This needs a denominator near
`1e-308`, which real amounts do not produce, so it is recorded only as an
observation.

**No deduplication in the Fraud API.** The Fraud API has no idempotency store. A reused `transaction_id` with a
different payload is scored independently and both requests answer 200. Only the
Coordinator detects conflicts. The property suite documents this behaviour and
does not claim any deduplication for `/v1/fraud/predict`.
