# Evidence: campaign agi-20261115T145608Z (Agent Platform final GKE integration)

This directory mirrors the campaign's result summaries; the raw evidence (680 files) stays at
`~/ewallet-evidence/agents/integration/agi-20261115T145608Z/` on the owner's workstation:

- `RAW_SHA256SUMS.txt` is the SHA-256 manifest of all of the raw files.
- `MIRROR_SHA256SUMS.txt` covers the files here.

No Secret, token or credential is included; the mirrored files were checked against the live MCP tokens.

| Path | Content |
|---|---|
| `agr/AGR-06/AGR-06-RESULT.txt` | KEDA scaling runs 01–05 (pre-P4), failed runs kept |
| `agr/AGR-06/inpod/inpod-validation.txt` | agentgateway shed shapes, in-pod validation of the backoff fixes |
| `agr/AGR-02/AGR-02-RESULT.txt` | isolated MCP failover runs 01–04 |
| `g4/` | deadline chain, latency measurements, actor lifecycle |
| `stage3/NL-intake-inpod.txt` | natural-language tool selection findings |
| `stage4/` | registry readback and dry-run, MCP tool metrics, security review |
| `ags/` | shared WorkerPool runs (parallel NOT_OBSERVED, sequential SHARED_WORKERS_OBSERVED) |
| `final-regression/` | final bounded regression: status, 5 E2E with hops, KEDA after P4, shared workers, failover, Sandbox concurrency reproduction |
| `approvals.txt`, `blockers.txt` | every owner approval and blocker, with timestamps |

Owner screenshots are captured later with [the replay runbook](../../../../edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md).
They are stored apart from this directory, which holds only Claude-run results.
