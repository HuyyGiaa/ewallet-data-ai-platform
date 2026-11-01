# Observability Rubric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver source-ready, statically validated and locally testable observability for EDAI-2 rows 46-51 without claiming unexecuted GKE evidence.

**Architecture:** Extend the existing `llm-agent-platform` chart and current OpenTelemetry hooks. OTLP flows through one redacting collector; Prometheus, Loki and Tempo provide the single metric, log and trace backends; Grafana provisions three focused dashboards. Runtime components stay disabled by default and in GKE overlays.

**Tech Stack:** Python 3.10+, OpenTelemetry, Prometheus, Grafana, Loki, Tempo, Helm, unittest, YAML/JSON.

**Spec:** `docs/superpowers/specs/2026-11-01-observability-rubric-design.md`

## Global Constraints

- Work only in `/home/huyy-giaa/Develop/ewallet-pc-observability` on `codex/pc-observability-20261101`.
- Do not change CI/CD-owned files or application business logic.
- Do not deploy to GKE, create cloud resources, scale or restart workloads, push, merge or rebase.
- Do not fabricate telemetry, screenshots or runtime evidence.
- Keep all new backends disabled by default and apply bounded requests/limits and retention.
- Use only metric labels with bounded cardinality; never user, transaction or request IDs.
- Commit once after each implementation phase.

## Review Focus

- A missing metric series must remain missing, not be converted to zero.
- A transport-successful MCP response with `isError=true` must count as failure.
- Sensitive prompt, credential, authorization and tool-argument values must not survive collector processing or structured logging.
- GKE/default renders must not deploy the local observability backends.
- Dashboard queries must use metric names proven by source/runtime inventory or be visibly marked pending.

---

### Task 1: Phase O1 — foundation, backends and platform dashboard

**Files:**
- Create: `tests/observability/test_foundation.py`
- Create: `infra/helm/llm-agent-platform/templates/observability-stack.yaml`
- Create: `infra/helm/llm-agent-platform/dashboards/platform-overview.json`
- Modify: `infra/helm/llm-agent-platform/templates/otel-collector.yaml`
- Modify: `infra/helm/llm-agent-platform/templates/dashboards.yaml`
- Modify: `infra/helm/llm-agent-platform/values.yaml`
- Modify: `infra/helm/llm-agent-platform/values-local.yaml`

**Interfaces:**
- Consumes: existing Helm helpers, collector config and workload policy conventions.
- Produces: optional `observabilityStack` values, Prometheus/Loki/Tempo/Grafana services, Grafana provisioning and the Platform Overview dashboard.

- [ ] **Step 1: Write failing foundation tests**

  Assert default/GKE renders contain no stack workloads; an enabled local render contains one bounded Prometheus, Loki, Tempo and Grafana deployment; collector pipelines use `memory_limiter -> redaction -> batch`; Prometheus scrape configuration covers collector, agentgateway, vLLM, kubelet/cAdvisor and kube-state-metrics; Platform Overview includes API rate/error/latency and Kubernetes resource/health queries without `or vector(0)`.

- [ ] **Step 2: Run tests and verify RED**

  Run: `python -m unittest tests.observability.test_foundation -v`
  Expected: FAIL because the stack values/template/dashboard do not exist.

- [ ] **Step 3: Implement the minimal optional stack and dashboard**

  Add pinned images, retention/configuration, non-root security contexts and bounded resources. Extend collector log/trace exporters for Loki/Tempo endpoints. Keep defaults and GKE disabled.

- [ ] **Step 4: Run tests and verify GREEN**

  Run: `python -m unittest tests.observability.test_foundation tests.llm_agent.test_observability_chart -v`
  Expected: PASS.

- [ ] **Step 5: Commit Phase O1**

  `git commit -m "feat(observability): add bounded telemetry foundation"`

### Task 2: Phase O2 — LLM, agent and MCP telemetry contracts

**Files:**
- Create: `tests/observability/test_ai_telemetry.py`
- Create: `ai_platform/llm_agent/observability.py`
- Create: `infra/helm/llm-agent-platform/dashboards/llm-runtime.json`
- Create: `infra/helm/llm-agent-platform/dashboards/agent-mcp.json`
- Modify: `ai_platform/llm_agent/tools/service.py`
- Modify: `infra/helm/llm-agent-platform/templates/dashboards.yaml`

**Interfaces:**
- Consumes: MCP `isError`, request context, agentgateway/vLLM metric inventory and the Task 1 data sources.
- Produces: low-cardinality telemetry helpers, semantic outcome classification and the LLM Runtime and Agent & MCP dashboards.

- [ ] **Step 1: Write failing AI telemetry tests**

  Assert semantic failure classification treats `isError=true` as failure despite HTTP 200; metric attributes reject high-cardinality keys; structured events redact authorization, credentials, prompts and tool arguments while preserving trace/span IDs; dashboard queries expose real vLLM/agentgateway names or visibly pending span-metric contracts and never coerce absence to zero.

- [ ] **Step 2: Run tests and verify RED**

  Run: `python -m unittest tests.observability.test_ai_telemetry -v`
  Expected: FAIL because the telemetry module and split dashboards do not exist.

- [ ] **Step 3: Implement minimal telemetry contracts and dashboards**

  Add a backend-neutral recorder/logger, semantic MCP outcome helper and bounded attributes. Integrate only at the existing MCP response boundary without changing tool behavior.

- [ ] **Step 4: Run tests and verify GREEN**

  Run: `python -m unittest tests.observability.test_ai_telemetry tests.llm_agent.test_phase_d_tools tests.llm_agent.test_observability_chart -v`
  Expected: PASS.

- [ ] **Step 5: Commit Phase O2**

  `git commit -m "feat(observability): add llm agent and mcp telemetry contracts"`

### Task 3: Phase O3 — validation, evidence and rubric mapping

**Files:**
- Create: `tests/observability/test_evidence.py`
- Create: `scripts/observability/validate.py`
- Create: `docs/observability.md`
- Create: `docs/evidence/observability/source_validation/manifest.json`
- Modify: `docs/README.md`
- Modify: `docs/rubric/rubric_evidence_matrix.csv`

**Interfaces:**
- Consumes: Task 1/2 configs, dashboards and tests.
- Produces: reproducible validation command, SHA-256 manifest, exact rubric mapping and runbook with honest status levels.

- [ ] **Step 1: Write failing evidence tests**

  Assert the validator checks YAML/JSON, Helm render, dashboard data sources, prohibited zero fallbacks, collector privacy processors and manifest hashes; require exact EDAI-2 rows 46-51 mapping and matching EDAI-1 observability rows with status vocabulary from the spec.

- [ ] **Step 2: Run tests and verify RED**

  Run: `python -m unittest tests.observability.test_evidence -v`
  Expected: FAIL because validation, evidence and mapping do not exist.

- [ ] **Step 3: Implement validation, documentation and evidence**

  Record initial/final Git identity, UTC run metadata, commands/results and SHA-256 hashes. Document reproduction, architecture, metrics inventory, resource profile, exclusions, pending integrations and copy-ready Living Decision entry. Do not add screenshots.

- [ ] **Step 4: Run tests and full local validation**

  Run: `python -m unittest discover -s tests/observability -v && python scripts/observability/validate.py`
  Expected: PASS with a machine-readable summary.

- [ ] **Step 5: Commit Phase O3**

  `git commit -m "docs(observability): bind validation evidence to rubric"`

- [ ] **Step 6: Run repository regression gates**

  Run targeted LLM/agent and inference suites, Helm lint/render, `uv lock --check` for touched Python projects, `python -m compileall` for changed Python, `git diff --check`, then the broadest practical repository test suite. Report every failure by name without promoting status.

