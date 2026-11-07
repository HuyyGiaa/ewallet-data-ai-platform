# R8: PII exposure in the agent workflow

R8 is the release gate for agent images. It covers every place an analyst's text goes after it enters the Coordinator. The planner's regex only governs the first of three surfaces, so each surface is judged separately.

| Surface | Status | Evidence |
|---|---|---|
| 1. LLM transport | **Fixed in source** (G2) | `prompt_safety.py`; `tests/llm_agent/test_r8_pii_screen.py`; property suites |
| 2. Conversation persistence | **Open (R8-P1)** | Strict expected failure in `test_r8_pii_screen.py` |
| 3. Logs and traces | **Clean in source**; cluster-side logs unverified | Same end-to-end test; audit field allowlist |

The running release r41 has none of these fixes. The B1-F6 card fix and the F7 fix reach GKE only with a Coordinator and Specialist image built from the integration branch.

## 1. LLM transport

**Path.** A2A `message/stream` → `CoordinatorExecutor` or the Specialist's executor → `nl_intake.plan()` → `screen_prompt()` → `OpenAIToolPlanner.select()` → agentgateway → vLLM.

`plan()` is the only place that sends text to an LLM. It screens first and passes on only the masked text, so grounding and the LLM never see a value. Both Agent A/B variants call the same `plan()`: `ExperimentPlanner.for_context()` only picks the model settings. A test runs 40 conversations through both variants and checks that the two receive identical masked text.

**What G2 changed (B1-F7 and evasion forms).**

| Input | Before | After |
|---|---|---|
| `0912345678-5`, `0912 345 678-5`, `sdt0912345678` | Unchanged | Phone masked |
| `a@b.com-5`, `a@b.com5` | Unchanged | Email masked |
| `0 9 1 2 3 4 5 6 7 8`, `8 4 3 …` | Unchanged | Phone masked |
| Full-width or other-script digits, `a＠b.com` | Unchanged | Masked |
| Zero-width space or soft hyphen inside a value | Unchanged | Masked |
| NBSP, en dash or thin space between groups | Unchanged | Masked |

How it works:

- A phone or email is judged on its own characters. Only a digit directly attached to a phone, or a letter directly attached to an email's top-level domain, stops a match.
- Text is canonicalised first: invisible format characters are removed, and full-width ASCII and decimal digits of any script become ASCII. The LLM receives that canonical text.
- Phone matches that overlap a UUID or ISO timestamp are skipped, so grounding keeps every identifier.

**Not masked by design:**
- A run of 11 or more digits is not a Vietnamese mobile number.
- Numbers written as words are not detected, for any type.

**False positives.** Old and new screens were compared on 1,019 lines from the repository: the Agent A/B suite, the RAG policy corpus and test prompts. The output did not change on any line.

## 2. Conversation persistence (R8-P1, open)

**The kagent controller stores the analyst's original message, before any screen.**

1. The A2A `TaskManager` puts the incoming message into `task.history`.
2. `KAgentTaskStore.save()` posts the whole task to `POST /api/tasks` on the kagent controller.
3. The controller writes it to `kagent-postgresql`.

**Local proof.** One chat task with a card, a phone and an email was run against a recording controller. Seven task bodies were saved, and all seven held the three values unmasked. The test `test_the_persisted_conversation_holds_no_raw_value` states the correct behaviour and stays an expected failure until this is fixed.

**Not verified locally.** Whether the kagent UI also stores its own copy of the chat message, for example in session events, before forwarding it.

**Proposed separate fix.** This needs owner decisions, because it changes stored history and touches production data.

1. **Wrap the task store.** A `ScreeningTaskStore` would wrap `KAgentTaskStore` and screen the text parts of user messages in `task.history` before `save()`. The masked text is already what the LLM saw, so multi-turn context is unchanged. The kagent UI would then show the masked prompt.
2. **Check the controller and UI copies.** On a woken cluster, query `kagent-postgresql` read-only for a test prompt. If the UI or controller keeps a raw copy, mitigating it needs a kagent-side change or a policy that analysts enter no PII in chat.
3. **Retention.** Existing rows may already hold raw values. Purging or rewriting them is an owner decision; this patch does not touch production history.
4. **Release gate.** Do not run sensitive-data traffic experiments until 1 is in place and 2 is answered.

## 3. Logs and traces

The end-to-end test captures every log record and every span, including the FastAPI and HTTPX instrumentation spans. None of them contains a value:

- Audit records go through an allowlist of fields. `coordinator.prompt_pii_masked` carries only PII types.
- `record_prompt_screen` exports counts per type, never values.
- `nl_intake` spans hold the model, tool, token counts and A/B variant, never prompt text.
- HTTPX logs record URLs, not bodies.

**Not verified locally:** agentgateway and kagent controller access logs on the cluster. Check them during the agent CPU campaign with a synthetic PII prompt before any real traffic.
