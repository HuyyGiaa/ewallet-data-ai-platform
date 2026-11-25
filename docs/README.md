# Documentation

This index is the reviewer entry point for the E-Wallet Data & AI Platform.
Technical reports explain the design, implementation, execution results,
evidence and limitations.

## Canonical navigation

| Area | Entry point | Contents |
|---|---|---|
| System architecture | [Architecture in the root README](../README.md#system-architecture) | Owner-approved [SVG](architecture/ewallet_system_architecture.svg) and [editable Excalidraw source](architecture/ewallet_system_architecture.excalidraw) |
| EDAI-1 | [Data Engineering reports](edai1/README.md) | Data generation, Spark, Flink, Airflow, DataHub, schema design, storage and Docker |
| EDAI-2 | [ML, LLM and Agent Platform reports](edai2/README.md) | Feast, Kubeflow, MLflow, KServe, LLM serving, RAG, agents, gateways, CI/CD, testing and observability |
| Runbooks | [Operations and evidence runbooks](runbooks/README.md) | Reproducible operator procedures and evidence-collection boundaries |
| Evidence | [Evidence index and handling rules](evidence/README.md) | Preserved runtime artifacts, screenshots and provenance requirements |

Infrastructure provisioning is documented in the compatibility-path
[Terraform GCP foundation runbook](cloud/terraform_gcp_foundation.md). That
path remains stable because repository contract tests and operator references
depend on it.

The owner-approved architecture is available directly as
[SVG](architecture/ewallet_system_architecture.svg). A diagram is not runtime
evidence by itself.
Historical documents and earlier architecture material are preserved in the
[legacy-document archive](../archive/legacy-docs/README.md), but are not part of
this primary navigation.

## Historical documents

Historical and superseded reports were relocated with history-preserving
`git mv` operations and are catalogued in the
[EDAI-1 historical-document register](edai1/HISTORICAL_DOCUMENTS.md) and the
[EDAI-2 historical-document register](edai2/HISTORICAL_DOCUMENTS.md). Runtime
evidence payloads were not relocated or regenerated; only documentation links
to those artifacts were repaired.
