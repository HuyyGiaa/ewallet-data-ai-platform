# N14 screenshot authenticity remediation

## Decision

The 11 programmatic terminal-like renders identified by the N14 audit are no
longer embedded in submission-facing documentation. The original PNGs remain
in the evidence tree for historical integrity, but they are not presented as
screenshots.

This environment did not provide graphical control of a real desktop terminal.
No synthetic replacement was created. Under the owner-approved Priority C
policy, the documentation now embeds concise raw excerpts and links the
preserved JSON, log or command transcript. A literal screenshot requirement is
marked `SCREENSHOT_MISSING` unless an independently retained genuine UI
capture already exists.

## Replacement matrix

| Original image | Original source | Replacement | Capture method | Referenced Markdown | Rubric row | Status |
|---|---|---|---|---|---|---|
| `edai1/docker/01_docker_image_ab.png` | `docker/raw/docker_datahub_publisher_ab.json` | Raw JSON excerpt + source link | Preserved historical file; no new execution | `docs/edai1/docker/DOCKER_OPTIMIZATION.md` | EDAI-1 3–4 | `RAW_EVIDENCE_ONLY` |
| `edai1/docker/02_docker_compose_runtime.png` | Compose config/build/run/inspect files under `docker/raw/` | Raw transcript summary + source links | Preserved historical files; no new execution | `docs/edai1/docker/DOCKER_OPTIMIZATION.md` | EDAI-1 3–4 | `RAW_EVIDENCE_ONLY` |
| `edai1/storage/07_postgres_warehouse_index_ab_v2.png` | `storage/artifacts/postgres_warehouse_index_ab_v2.json` | Raw benchmark excerpt + JSON/log links | Preserved historical file; no new benchmark | `docs/edai1/storage/STORAGE_OPTIMIZATION.md` | EDAI-1 21 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/01_agents_replicas_hpa_pdb.png` | `runtime/raw/01_agents_replicas_hpa_pdb.txt` | Raw transcript link/excerpt | Preserved N13 output; no cluster query | `docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md` | EDAI-2 16,19,22,25 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/02_kagent_agent_crs.png` | `runtime/raw/02_kagent_agent_crs.txt` | Raw transcript link/excerpt | Preserved N13 output; no cluster query | same | EDAI-2 16,22,25 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/03_sandbox_substrate.png` | `runtime/raw/03_sandbox_substrate.txt` | Raw transcript link/excerpt | Preserved N13 output; no cluster query | same | EDAI-2 20,27 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/04_mcp_rollingupdate.png` | `runtime/raw/04_mcp_rollingupdate.txt` | Raw transcript link/excerpt | Preserved N13 output; no cluster query | same | EDAI-2 18 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/05_kserve_isvc.png` | `runtime/raw/05_kserve_isvc.txt` | Raw transcript; existing historical genuine KServe UI retained separately | Preserved N13 output; no cluster query | `docs/edai2/mlops/ML_PIPELINE_SERVING.md` | EDAI-2 15 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/06_kserve_v2_ready_metadata.png` | `runtime/raw/06_kserve_v2_ready_metadata.txt` | Raw transcript; existing historical genuine KServe UI retained separately | Preserved N13 output; no cluster query | same | EDAI-2 15 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/07_fraud_api_predict_via_kserve.png` | `runtime/raw/07_fraud_api_predict_via_kserve.txt` | Raw transcript; existing historical genuine prediction capture retained separately | Preserved N13 output; no inference call | same | EDAI-2 15 | `RAW_EVIDENCE_ONLY` |
| `edai2/screenshots/runtime/08_fraud_api_pydantic_and_health.png` | `runtime/raw/08_fraud_api_pydantic_and_health.txt` | Raw transcript link/excerpt | Preserved N13 output; no API call | `docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md` | EDAI-2 16–17 | `RAW_EVIDENCE_ONLY` |

## Counts

- Replaced with original application UI: **0**.
- Replaced with genuine terminal screenshots: **0**.
- Converted to raw Markdown evidence: **11**.
- Blocked by missing original source records: **0**.
- Literal genuine screenshot captures still missing: **8 evidence groups**:
  Docker A/B, Docker Compose, PostgreSQL 2M-row benchmark, HPA/scale state,
  MCP rollout, Sandbox runtime, KServe r41 command state, and Fraud API
  health/validation. Existing historical KServe/Grafana/kagent captures remain
  valid only for the behavior and run identity visible in those UIs.

## Provenance and integrity

- Baseline: `origin/main`
  `b4b461175fc228db12ecb986a0dce457da2ad67f`.
- N14 remediation performed in an isolated worktree without workload
  execution, deployment changes, merge or push.
- Docker raw files were copied from
  `/home/huyy-giaa/ewallet-evidence/edai1-final-evidence/docker/`. The curated
  `compose_build.txt` and `compose_run.txt` remove trailing spaces from four
  lines so the Git whitespace gate remains clean; no command, value or status
  text changed. Their original SHA-256 values are respectively
  `458c5e882aeaf5dcc0caf3bbae7139fb370c4627791cb0c912800d710c0487e4`
  and `e43abedd78ced8c2f85fe9fa8808a836777e65ec758f71296ec7d91e6fd2decb`.
  The curated hashes are recorded in `SHA256SUMS`.
- PostgreSQL and N13 raw files were already retained in the canonical evidence
  tree.
- Checksums are recorded in [SHA256SUMS](SHA256SUMS).

## Reviewer interpretation

Raw excerpts are supporting evidence. They are not screenshots and do not
satisfy a rubric deliverable that literally requires a specific application UI
or a demonstrated scale/rollout screenshot. The rubric indexes and screenshot
audits preserve those gaps explicitly.
