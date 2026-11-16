# Evidence: final remediation rem-20261116T050511Z (2026-11-16)

This directory mirrors the remediation summaries. The raw evidence (139 files) stays at
`~/ewallet-evidence/agents/remediation/rem-20261116T050511Z/`:

- `RAW_SHA256SUMS.txt` is the manifest of the raw files.
- `MIRROR_SHA256SUMS.txt` covers the files here.

Every mirrored 64-hex string was checked to be a named hash (schema, workload, render), never a credential.
Results: [AGENT_PLATFORM_COMPLETION.md §6](../../../../edai2/agents/AGENT_PLATFORM_COMPLETION.md#6-final-remediation-rem-20261116t050511z).

| Path | Content |
|---|---|
| `approvals.txt` | owner approvals A1–A5 and A2b, and the execution timestamps |
| `stage1/` | img-agents gate diff (3 Agents, images only), Sandbox fleet diff (2 SandboxAgents, image only) |
| `acceptance/e2e-*.txt`, `acceptance/status*.txt` | 5 E2E with hops, the final status and a combined regression |
| `acceptance/load/` | AGR-06 at concurrency 2, 4 and 8: summaries, admission waits, per-pod work, rescale events, duplicate-execution check, in-flight series |
| `nl-fraud-*.json` | Tempo trace `a38538611be4ffd81b5ae2d2e4402742` with the Sandbox Feature-RAG spans |
| `registry/` | readback before and after, publish report |
| `sleep/` | plan-sleep, KEDA-aware guarded sleep and `verify --expect sleep` |

These are Claude-run engineering results. The owner replay and its screenshots are still pending
(`OWNER_CAPTURE_PENDING`).
