# EDAI-2 curated screenshots

Classification of every embedded image is in [SCREENSHOT_AUDIT.md](../../../edai2/SCREENSHOT_AUDIT.md). Every image here is embedded and explained in an EDAI-2 topic document. This folder is not a gallery to be read on its own; start from the [rubric evidence index](../../../edai2/RUBRIC_EVIDENCE_INDEX.md).

| Folder | Content | Provenance |
|---|---|---|
| `llm/`, `agents/`, `mlops/`, `cicd/`, `observability/` | 57 **authentic native UI screenshots**: kagent, agentregistry, Grafana/Tempo/Loki, Kubeflow, MLflow, Jenkins. Captured in N9, N10-E1/E2, N11 and N12 (2026-11-03/04) | [`PROVENANCE.json`](PROVENANCE.json): source path, SHA-256 (identical to the original), campaign, run/trace/build identity, rubric rows, caption. Produced by [`curate.py`](curate.py) |
| `runtime/raw/01–08` | Original read-only command transcripts recorded in N13 on Helm r41 at 2026-11-04T09:32:31Z. Submission reports link these text files; they are not screenshots | Historical capture script [`capture_runtime_state.sh`](runtime/capture_runtime_state.sh) |
| `runtime/01–08.png` | Historical programmatic renders, retained for integrity but no longer embedded in submission-facing reports | Deprecated renderer retained as provenance only; do not use as screenshot evidence |
| `runtime/09_kagent_ui_agents_r41.png` | Native kagent UI agents page, captured read-only in N13 (r41) | Page metadata `09_kagent_ui_agents_r41.png.json` |
| `runtime/10_grafana_agent_pods_per_replica.png` | Native Grafana Explore (Prometheus/cAdvisor), read-only query showing 2 pods per agent, captured in N13 (r41) | Page metadata `10_grafana_agent_pods_per_replica.png.json` (credential-free URL and text excerpt) |
| `runtime/*_live_runtime.png`, `runtime/05–08*.png`, `runtime/11_*.png` | Nine genuine Ubuntu terminal captures from N15: live Agent/HPA/PDB, MCP, Sandbox, KServe and Fraud API state | [N15 provenance and SHA-256 manifest](../../n15-genuine-screenshots/README.md) |

**Integrity rules:**
- No image was edited or cropped.
- No result was fabricated.
- Programmatic terminal-like renders are not used as submission screenshots.
- Rows whose image does not exist are marked `SCREENSHOT_MISSING` in the [gap ledger](../../../edai2/GAP_LEDGER.md).

**Related folders:**
- Small text evidence copied into the repository: [`../text/`](../text) (with `PROVENANCE.json`).
- The earlier screenshot sets under `docs/evidence/cloud/mlops-campaign-20261031/` and `docs/evidence/llm_agent/` are kept unchanged.
