# EDAI-2 Accelerated Integration Plan

1. Freeze Git/Helm/GKE/RAM/cost identities and create an evidence directory.
2. TDD a deterministic DB1 canary manifest/verification tool; run focused RAG
   and MCP tests; commit the reusable source.
3. Render, security-lint and diff the canary; deploy, run authenticated MCP
   read-after-write, hash evidence and delete all owned resources.
4. Start the existing DataHub dependencies serially with memory checkpoints;
   publish the existing 25-record RAG plan with API readback; stop containers.
5. TDD namespace-scoped observability rendering without ClusterRole or
   cluster-discovery targets; run Helm/security tests and commit.
6. Render against live values, verify an additions-only semantic diff, deploy
   the existing stack, execute one bounded G3 smoke and query trace/metrics.
7. Run the combined focused regression and negative security checks, verify
   cleanup, hashes, Git diff/status and frozen G3 health.
8. Commit runtime evidence and a copy-ready Living Decision; do not merge/push.
