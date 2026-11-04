# P0 source validation

Validated against the source change that follows root-cause checkpoint
`af6b762`.

## Locked environment

- `uv lock --check`: PASS, 120 packages resolved.
- `uv sync --locked --extra feast --extra rag-query --group dev`: PASS.
- Interpreter: `infra/docker/llm-agent-tooling/.venv/bin/python`.
- The host `PATH` did not expose `uv`; the existing locked executable was read
  from `/home/huyy-giaa/miniconda3/envs/fintech/bin/uv`.

## Focused and regression tests

Command group:

```text
python -m unittest
  tests.llm_agent.test_phase_d_tools
  tests.llm_agent.test_phase_d_security
  tests.llm_agent.test_mcp_sdk_conformance
  tests.llm_agent.test_mcp_production_authenticator
  tests.llm_agent.test_phase_d_chart
  tests.llm_agent.test_agents_chart
  tests.llm_agent.test_g3_orchestration
  tests.llm_agent.test_g3_runtime_config
  tests.llm_agent.test_g3_business_request -v
```

Result: **167 tests PASS in 8.027 seconds**; no skips and no hidden failures.

Additional checks:

- Focused new/affected group: 66/66 PASS.
- `py_compile` for the business request builder and A2A client: PASS.
- Helm lint with the G3 profile and immutable fixture digests: PASS.
- Prompt manifest SHA-256 checks and served CRD structural checks: PASS.
- `git diff --check`: PASS.
- Canonical business request builder: PASS; output is stable sorted compact
  JSON and remains under the 2,048-byte ingress budget.

After runtime attempt 1 exposed a Coordinator that answered before delegation,
the explicit-target/fail-closed router correction passed 62/62 affected
business-request, chart and G3 runtime-config tests plus Helm lint.

## Resource note

Validation was sequential. At completion the host had approximately 10 GiB
available RAM; no build, image scan or local container stack was started.
