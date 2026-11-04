# Documentation

This index is the reviewer entry point for the E-Wallet Data & AI Platform.
Technical reports explain the design, implementation, execution results,
evidence and limitations. Rubric status is intentionally kept in separate
internal tracking documents.

## Canonical navigation

| Area | Entry point | Contents |
|---|---|---|
| System architecture | [Architecture in the root README](../README.md#system-architecture) | End-to-end data and request flows, with an [editable Excalidraw source](architecture/ewallet_system_architecture.excalidraw) |
| EDAI-1 | [Data Engineering reports](edai1/README.md) | Data generation, Spark, Flink, Airflow, DataHub, schema design, storage and Docker |
| EDAI-2 | [ML, LLM and Agent Platform reports](edai2/README.md) | Feast, Kubeflow, MLflow, KServe, LLM serving, RAG, agents, gateways, CI/CD, testing and observability |
| Runbooks | [Operations and evidence runbooks](runbooks/README.md) | Reproducible operator procedures and evidence-collection boundaries |
| Evidence | [Evidence index and handling rules](evidence/README.md) | Preserved runtime artifacts, screenshots and provenance requirements |

The exported architecture is available directly as
[SVG](architecture/ewallet_system_architecture.svg). Historical documents and
earlier architecture material remain in the repository for provenance, but are
not part of this primary navigation.

## Internal evidence tracking

These files preserve the 93-row workbook crosswalk and known proof gaps. They
are review aids, not substitutes for the engineering reports.

| Sheet | Evidence index | Gap ledger |
|---|---|---|
| EDAI-1 | [Rubric evidence index](edai1/RUBRIC_EVIDENCE_INDEX.md) | [Gap ledger](edai1/GAP_LEDGER.md) |
| EDAI-2 | [Rubric evidence index](edai2/RUBRIC_EVIDENCE_INDEX.md) | [Gap ledger](edai2/GAP_LEDGER.md) |

The exact workbook wording, including the requirement, Deliverables/Proof text
and raw points for all 93 scored rows, is preserved in the
[workbook extract](rubric/edai_k11_workbook_extract.json). Its recorded SHA-256
matches the reviewed `EDAI K11.xlsx` workbook. The
[evidence matrix](rubric/rubric_evidence_matrix.csv) retains source, runtime and
missing-proof references for each sheet and row.

Historical and superseded reports are catalogued in the
[EDAI-1 historical-document register](edai1/HISTORICAL_DOCUMENTS.md) and the
[EDAI-2 historical-document register](edai2/HISTORICAL_DOCUMENTS.md). No
evidence artifact is relocated or deleted by this index.
