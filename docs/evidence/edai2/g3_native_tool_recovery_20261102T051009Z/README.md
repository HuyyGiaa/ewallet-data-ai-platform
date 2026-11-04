# G3 native Agent tool-calling recovery — 2026-11-02

## Verdict

The native Agent tools are registered with the expected kagent 0.10.3 schema,
but the current production model request is not reliable enough for the
Coordinator contract. No deployment was performed.

The production-route diagnostic established three distinct outcomes:

| Probe | HTTP / finish | Structured `tool_calls` | Identity fidelity | Verdict |
|---|---|---:|---:|---|
| Coordinator-like, three tools, `auto` | 200 / `stop` | No | No | FAIL: pseudo `<tool_call>` in assistant text |
| Minimal one Agent tool, `auto` | 200 / `tool_calls` | Yes | No | Capability exists; request reduced to `capability_probe` |
| Minimal one Agent tool, forced function | 200 / `stop` | Yes | No | FAIL: malformed/truncated nested JSON and wrong finish semantics |

This rules out “tool not registered” and demonstrates that the serving route,
Hermes parser and model can produce structured tool calls. It does **not**
prove kagent dispatch because the failing Coordinator response has no
structured call to dispatch. Exact kagent 0.10.3 source shows that OpenAI
chat-completions requests use `tool_choice=auto` whenever tools are present;
the deployed G3 Agent/ModelConfig does not expose a per-Agent deterministic
tool choice. The demonstrated fault is therefore model selection plus argument
fidelity under the full Coordinator prompt/tool contract, not a proven parser
or A2A Gateway defect.

Changing shared model/parser/chat-template configuration, forking the kagent
worker, parsing arbitrary assistant text into executable actions, or bypassing
native Agent-as-Tool would exceed the approved G3-owned correction scope. The
runtime stays at `llm-agent` revision 15, `llm-agent-mcp` revision 3 and
`kagent` revision 2; no rollback was necessary.

## Provenance

- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Starting HEAD: `7ef4dd9d7b3d18bba922308cc22de2e017faa125`
- Starting tree: `0b75cecb4079dce47c7c2a4948441405bd1bb8ec`
- Exact upstream kagent tag `v0.10.3`: commit
  `8878c39ac64421cb665c8e62ecc6411dd12f2fc2`, tree
  `2c41e13a4c725c8f12f37dfdee9fab1644b0f804`
- Worker image:
  `ghcr.io/kagent-dev/kagent/golang-adk:0.10.3@sha256:5ee9592cb7d959538d0f0fe36acf31ba3a06de084f84048b1459c3bdfafcda1f`
- Controller image:
  `ghcr.io/kagent-dev/kagent/controller:0.10.3@sha256:b2572a59386b6bac1f5e652e135fdad54c0ee603d3f9ee60eb7c80bbaedf9b76`
- Model route: `ewallet-fraud-llm` through agentgateway 1.7.0 to
  `ewallet-fraud-llm-v1` on the existing Qwen3-0.6B vLLM deployment.

## Layer matrix

| Layer | Result | Evidence |
|---|---|---|
| Agent CR/config translation | PASS | Live sanitized Coordinator config contains both `remote_agents` |
| Registered tool inventory | PASS | Worker startup logs and Agent Card show both namespaced Agents plus `ask_user` |
| Agent schema | PASS | Exact upstream source: one required string argument, `request` |
| Outbound tool policy | VERIFIED FROM PINNED SOURCE | kagent 0.10.3 adds tools and hard-codes `tool_choice=auto`; actual body was not transparently intercepted |
| Coordinator-like model response | FAIL | `finish_reason=stop`, `tool_calls=null`, pseudo markup in `content` |
| Minimal model capability | PARTIAL PASS | Same endpoint returns a real structured Agent call, but loses the complete wire request |
| Forced function capability | FAIL CONTRACT | Structured field exists, but nested JSON is malformed/truncated and finish reason is `stop` |
| kagent response parser | CAPABLE, NOT ROOT CAUSE PROVEN | It receives valid OpenAI `tool_calls` in upstream tests; same serving parser emits structured calls in minimal probes |
| kagent dispatch | NOT REACHED | No structured call in the Coordinator-like failure |
| A2A Gateway | UNCHANGED / NOT RETESTED | No deployment; previous routes remain in place |
| Coordinator business E2E | BLOCKED | No Coordinator → A2A → Specialist call from the failing turn |

## Source and tests

Added `scripts/llm_agent/g3_native_tool_probe.py`, a loopback-only,
non-executing diagnostic that never dispatches returned tools or writes the
API key. It rejects pseudo markup, missing/non-terminal structured calls,
unknown/drift tools, malformed arguments and any byte-level mutation of the
canonical two-line request.

- New focused tests: 6/6 PASS.
- Locked G3/MCP/A2A/chart group: 173/173 PASS in 6.964 s.
- Locked RAG group: 61 PASS, 1 explicit opt-in SKIP, 26 subtests PASS in 3.59 s.
- `py_compile`: PASS.
- `git diff --check`: PASS.

## DataHub read-only result

The existing DataHub v1.7.0.1 Docker project and named MySQL/OpenSearch/Kafka
volumes still exist. GMS, frontend, actions, MySQL, OpenSearch and Kafka are all
stopped; no GMS Service exists in GKE and the probed local endpoints are not
listening. Host swap remained 8/8 GiB used, with approximately 9–10 GiB RAM
available during sequential validation. The stack was not started.

The RAG artifacts still contain 25 prepared metadata records and five passing
local quality checks, but the status remains `PLAN_ONLY_NOT_PUBLISHED`.
`DATAHUB_LINEAGE=BLOCKED`; there is no GMS readback. A safe publication requires
either a reachable, owner-identified GMS endpoint/token or explicit approval
to restore the stopped persistent stack after freeing swap and reserving enough
memory for MySQL, OpenSearch, Kafka, GMS and frontend.

## RAG DB1 versus production DB0

- Isolated Airflow publication: Redis DB1, four hashes, online readback MATCH.
- Production Feature-RAG MCP: deployment revision 3, image digest
  `sha256:b80c07c76e4d240328804218c46ff66ecc7e4ba45694072f94774c57548fa058`,
  Redis DB0, immutable registry SHA-256
  `52cd294cfebf6d3bc47a613eab0835531d3b481f65811bfdb6258d6fbdbd4a1f`.
- Direct DB1 and production MCP returned the same chunk identities, citations
  and near-identical scores. This is semantic correlation only; it does not
  prove that production read the newly ingested DB1 records.

The smallest reversible proof is a temporary canary using the production MCP
image and exact application contract, a new immutable ConfigMap for the DB1
registry, and a cloned connection Secret targeting DB1. First validate via
the official MCP SDK through a port-forward. To prove the complete production
query path, add one exact temporary shadow backend/route in the existing MCP
Gateway, use a distinct least-privilege credential, run one bounded query, then
revert the Gateway revision and delete only the canary objects. This requires a
separate semantic diff/security/rollback approval; DB0 was not modified here.

## Reproduction

Run the locked regression without starting local infrastructure:

```bash
cd /home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2
infra/docker/llm-agent-tooling/.venv/bin/python -m unittest \
  tests.llm_agent.test_phase_d_tools \
  tests.llm_agent.test_phase_d_security \
  tests.llm_agent.test_mcp_sdk_conformance \
  tests.llm_agent.test_mcp_production_authenticator \
  tests.llm_agent.test_phase_d_chart \
  tests.llm_agent.test_agents_chart \
  tests.llm_agent.test_g3_orchestration \
  tests.llm_agent.test_g3_runtime_config \
  tests.llm_agent.test_g3_business_request \
  tests.llm_agent.test_g3_native_tool_probe -v
```

For a model-only probe, create a non-sensitive two-line payload file, start one
loopback port-forward, load the gateway key into the environment without
printing it, and run the versioned script. The script stops before dispatch:

```bash
kubectl -n ewallet-agents port-forward svc/agentgateway 19091:3000
export EWALLET_VLLM_API_KEY="$(kubectl -n ewallet-agents get secret \
  agentgateway-client-key -o jsonpath='{.data.api-key}' | base64 -d)"
infra/docker/llm-agent-tooling/.venv/bin/python \
  scripts/llm_agent/g3_native_tool_probe.py \
  --endpoint http://127.0.0.1:19091 \
  --wire-request-file /path/to/non-sensitive-two-line-probe.txt \
  --tool-choice auto \
  --evidence /path/to/redacted-probe.json
unset EWALLET_VLLM_API_KEY
```

## Final gates

```text
SOURCE_DIAGNOSTIC=PASS
NATIVE_AGENT_REGISTERED=PASS
MINIMAL_STRUCTURED_TOOL_CALL=PASS_WITH_IDENTITY_FAILURE
COORDINATOR_NATIVE_TOOL_CALL=FAIL
KAGENT_DISPATCH=NOT_REACHED
A2A_GATEWAY_ROUTE=UNCHANGED
COORDINATOR_BUSINESS_E2E=BLOCKED
FULL_BUSINESS_E2E=BLOCKED
DATAHUB_PUBLICATION_READBACK=BLOCKED
RAG_DB1_READBACK=PASS
RAG_DB1_PRODUCTION_PATH=NOT_PROVEN
SECURITY_REGRESSION=PASS
RUBRIC_VERIFIED=NO
```

Raw requests/responses, sanitized runtime inventories, exact pinned source
excerpts and test logs are in the sibling `runtime/`, `preflight/` and
`source-audit/` directories. `SHA256SUMS` binds the final archive.
