# Living Decision milestone drafts

This file is the exact synchronization fallback when Google Docs write/readback
is unavailable. An entry remains `SYNC_PENDING` until the canonical document is
saved and the inserted text is read back.

## Phase A milestone — SYNC_PENDING

Date: 30/10/2026 (ICT)

Git SHA: TO_BE_FILLED_AFTER_COMMIT

Implemented components: LLM/Agent typed contracts and W3C request/version
context; fail-closed `llm-agent-release/v1` component inventory; pinned
multi-stage Gate A validation image; Helm chart with dev/GCP overlays, Istio
Ambient namespace, Kubernetes Gateway API HTTPS route, dedicated service
accounts, Istio AuthorizationPolicy default deny and Kubernetes NetworkPolicy
default deny; deterministic source preflight; Jenkins-compatible exact-SHA,
changed-component, validation, immutable-image, scan, atomic Helm deploy,
smoke/evidence and rollback pipeline skeleton; existing 93-row rubric evidence
matrix reused without changing source-only rows to VERIFIED.

Tests: TO_BE_FILLED_AFTER_GATE_A_TESTS.

Runtime evidence: none. Source/config/render/test evidence only. No cloud or
Kubernetes deployment ran. `runtime_verified=false`.

Rubric status: active workbook rows remain governed by OPEN/PARTIAL/VERIFIED.
This milestone affects Phase A source readiness and does not award runtime
points. Architecture equivalence is Istio/Gateway API for historical NGINX rows
and kagent native A2A for historical standalone A2A Gateway row.

Blockers: DP2 is active; Feast Online/Redis, registered ML model/KServe and
runtime agent dependencies are not assumed complete. Paid/cloud deployment,
IAM changes, CI deployment execution, promotion, merge and push require user
approval.

Next gate: Phase B source implementation for CPU-first vLLM, agentgateway model
routing, bounded timeout/retry/circuit-breaker/fallback and controlled benchmark
harness. Runtime VERIFIED remains gated on approved deployment and real measured
readback.
