# Payment anomaly review policy

This demonstration policy requires enhanced review for unusual payment amount, velocity, merchant, device, or geography patterns. The evidence bundle must separate historical Feast features, request-time attributes, derived features, registered model output, and policy knowledge. Retrieval citations must identify the document, chunk, checksum, corpus revision, embedding revision, and feature index revision.

An automated agent may collect and summarize evidence but must not invent missing facts. Empty retrieval, low similarity, stale features, serving errors, or policy conflicts must be visible to the coordinator and may require human review. Production promotion requires correlated traces, security-policy checks, rollback readiness, and runtime readback rather than source configuration alone.
