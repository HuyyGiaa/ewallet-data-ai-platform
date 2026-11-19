# Owner evidence replay `replay-20261118T161231Z`: mirrored evidence

This directory holds the evidence that [AGENT_PLATFORM_EVIDENCE.md](../../../../edai2/agents/AGENT_PLATFORM_EVIDENCE.md) embeds and links.

**Source:**

- Cluster `ewallet-dev` (`ewallet-data-ai-platform-hg26`, `asia-southeast1-b`).
- Releases `llm-agent` rev 60 and `llm-agent-mcp` rev 19.
- Captured 2026-11-19 to 2026-11-20 (UTC).

## `screenshots/` (36 files)

- **Who took them:** the owner, on the live cluster: GKE console, terminal, kagent UI, agentregistry UI and Grafana.
- **Copy:** each file was copied byte for byte from the owner's `~/Pictures/Screenshots/AGENT_*.png`. Each copy was compared with `cmp` against its original, and every file has a valid PNG header.
- **Unchanged:** no image was edited, cropped or generated.
- **Earlier inventory:** the first 18 files (`A*`, `B*`, `C*`) were already inventoried and hashed before the interim sleep of 2026-11-19 (`interim-sleep-20261119T111823Z/SCREENSHOTS.SHA256SUMS.txt` in the local evidence store). That inventory still verifies against the originals.
- **Clock:** terminal timestamps are UTC; Grafana axes and the `watch` header are ICT (UTC+7).

## `artifacts/`

Verdict and plan files written by `scripts/llm_agent/agent_replay.sh` and `ops/gcp/cloud_lab.py` during the campaign, copied unchanged from the local evidence store `~/ewallet-evidence/agents/replay/replay-20261118T161231Z/`. Files were renamed only to say what they are.

| File | Source in the local evidence store |
|---|---|
| `g3-security-negatives-verdict.json` | `g3-security-20261119/negative/verdict.json` |
| `g4-platform-status-verdict.json` | `g4-platform-status-20261119/status/verdict.json` |
| `g4-registry-readback-verdict.json` | `g4-registry-readback-20261119/registry/verdict.json` |
| `g4-e2e-{fraud,drift,combined}-verdict.json` | `g4-b0{2,3,4}-*-20261119/e2e/*-verdict.json` |
| `g5-d01-keda-baseline-verdict.json` | `g5-d01-baseline-20261120/keda-status/verdict.json` |
| `g5-autoscale-verdict.json` | `autoscale/verdict.json` |
| `g5-autoscale-replica-timeline.txt` | `autoscale/timeline.txt` |
| `g5-load-summary.json` | `autoscale/load/optimized/raw/run-01/summary.json` |
| `g6-workers-verdict.json`, `g6-workers-fleet.txt` | `g6-fleet-20261120/workers/` |
| `final-sleep-plan-sleep.json`, `final-sleep-mcp-coordinator-strategy-pdb.txt`, `final-sleep-verify-sleep.json` | `final-sleep-20261119T212026Z/{preflight,sleep}/` |

**Kept local only:** large raw samples such as `actors.jsonl` (8.5 MB) and `status.jsonl` (17.7 MB), request and response bodies, and anything that names a Secret or holds an address. They stay in the local evidence store, which has its own `SHA256SUMS.txt` per campaign directory.

**Scanned:** all copied text files were checked for IP addresses, tokens and Secret values before commit.

## Integrity

[`SHA256SUMS.txt`](SHA256SUMS.txt) covers every file in this directory except itself and this README. To verify:

```bash
cd docs/evidence/edai2/agents/replay-20261118T161231Z && sha256sum -c SHA256SUMS.txt
```
