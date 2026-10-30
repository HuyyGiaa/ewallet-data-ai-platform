# Living Decision milestone drafts

This file is the exact synchronization record and fallback when Google Docs
write/readback is unavailable. An entry remains `SYNC_PENDING` until the
canonical document is saved and the inserted text is read back.

## 21.217 — Phase A milestone — SYNCED_READBACK_VERIFIED

Date: 30/10/2026 (ICT)

Canonical Google Docs status: saved to Drive and exported readback verified.
Readback matched decision number, implementation SHA, 27-PASS test summary,
`runtime_verified=false`, rubric status/blockers and the Phase B next gate.

Git SHA: `5fd25ebe8e32b4b18d86b9be07f4aece8cd0d30a` (implementation),
based on rubric reuse commit `0ae2f43a9de4d633d1ac6d30b5440f0549a1f5ea`
and reviewed consolidation base
`acad36beb81a8424db6ee0a3a04905dbb040ce0d`.

Implemented components: LLM/Agent typed contracts and W3C request/version
context; fail-closed `llm-agent-release/v1` component inventory; pinned
multi-stage Gate A validation image; Helm chart with dev/GCP overlays, Istio
Ambient namespace, Kubernetes Gateway API HTTPS route, dedicated service
accounts, Istio AuthorizationPolicy default deny and Kubernetes NetworkPolicy
default deny; deterministic source preflight; Jenkins-compatible exact-SHA,
changed-component, validation, immutable-image, scan, atomic Helm deploy,
smoke/evidence and rollback pipeline skeleton; existing 93-row rubric evidence
matrix reused without changing source-only rows to VERIFIED.

Tests: 27 PASS total (8 new LLM/Agent contract/foundation tests, 7 bounded
kagent pilot tests, 12 rubric fail-closed tests); Gate A preflight PASS with
both dev and GCP Helm renders; Helm lint PASS for both overlays; rubric matrix
validation PASS for all 93 active scored rows and exact 100 + 100 raw points;
Python compileall and `git diff --check` PASS.

Runtime evidence: none. Source/config/render/test evidence only. No cloud or
Kubernetes deployment ran. `runtime_verified=false`.

Rubric status: active workbook rows remain governed by OPEN/PARTIAL/VERIFIED.
Source readiness was added for edai-2 rows 5, 23, 24, 28, 33–43 and 55, but no
row was promoted and no points were awarded. Architecture equivalence is
Istio/Gateway API for historical NGINX rows and kagent native A2A for the
historical standalone A2A Gateway row.

Blockers: DP2 is active; Feast Online/Redis, registered ML model/KServe and
runtime agent dependencies are not assumed complete. Paid/cloud deployment,
IAM changes, CI deployment execution, promotion, merge and push require user
approval.

Next gate: Phase B source implementation for CPU-first vLLM, agentgateway model
routing, bounded timeout/retry/circuit-breaker/fallback and controlled benchmark
harness. Runtime VERIFIED remains gated on approved deployment and real measured
readback.
