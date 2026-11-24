# Evidence: G3-B owner acceptance campaign `g3b-owner-evidence-20261124T083106Z`

Published evidence for the external gateway's GKE acceptance campaign on 2026-11-24. It is
interpreted in [EXTERNAL_GATEWAY_EVIDENCE.md](../../../../edai2/gateway/EXTERNAL_GATEWAY_EVIDENCE.md).

| | |
|---|---|
| Campaign | `g3b-owner-evidence-20261124T083106Z`; scope `g3b`, mode `supervised`, TLS reuse; not simulated |
| Date | 2026-11-24, `run` 08:43:06 UTC, cleanup complete 09:56:46 UTC |
| Environment | GKE `ewallet-dev` (`asia-southeast1-b`), project `ewallet-data-ai-platform-hg26`; domain `ewallet-hg.gleeze.com` |
| Deployment | releases `ewallet-edge-gateway` (official `istio/gateway` 1.30.7) and `ewallet-edge` (repository chart) in `ewallet-ingress`; public overlays `values-gateway-public.yaml` + `values-gateway-public-open.yaml` |
| Source | branch `feature/edai2-external-gateway-api`, head `4ba50241` at campaign time, from Git history. **Not verified from the campaign artifacts:** `state.json` records no commit, and the deployed Helm revision numbers were not captured by this campaign |
| Raw origin | the owner's local directory `~/ewallet-evidence/gateway/g3b-owner-evidence-20261124T083106Z/`; not modified |

## Contents

| Path | What it is |
|---|---|
| `screenshots/*.png` (28) | the owner's screenshots, **byte-identical copies** of the raw archive |
| `screenshots/SHA256SUMS.txt` | the owner's checksum manifest, copied unchanged; `sha256sum -c` passes 28/28 in this directory |
| `results/02-backends.jsonl`, `results/baseline.json` | backend readiness before exposure |
| `results/07-private-*.jsonl` | negative and end-to-end checks through a port-forward, before exposure |
| `results/10-*.jsonl` | the same checks from the public Internet, approved network |
| `results/11-l4-denied.jsonl` | checks from a network outside the approved CIDRs (agents host 403) |
| `results/14-*.jsonl` | final public checks, including the rate-limit burst |
| `results/evidence-window-continue` | the owner's `continue` marker (timestamp only) |
| `state-summary.json` | **derived** from the private `state.json`: verdicts, phases, gates, cleanup steps and the fields used in the report, values unchanged |

## Selection and withheld material

All 28 screenshots are published; none was edited or redacted. They contain no credentials,
tokens, keys or Secret values. They do show:

- **Terminal screenshots:** the operator's local username and hostname.
- **Three browser screenshots** (`GW-05`, `GW-42-agents-basic-auth`, `GW-42-registry-basic-auth`):
  the browser's new-tab page, with bookmarks and a weather widget.
- **The business screenshots:** synthetic entity UUIDs from the project's test dataset and a
  kagent chat session id. The session id is not a credential; the UI is behind Basic Auth and the
  address allowlist, and was taken offline after the campaign.
- **The public IPv4** of the released load balancer, and the gateway's in-cluster Service address.

Withheld from publication:

- **`state.json`:** local paths, the credential-file locations and process fields. It is replaced
  by `state-summary.json`.
- **`l0-edge.yaml`, `l0-gateway.yaml` and `private/`:** the rendered and private values contain
  the approved client CIDR (a personal address).
- **`recovery-manifest/`:** an operational wake record, not evidence for this report.

The result files were checked before publication: they contain no IPv4 addresses and no
credential values. Words like "password" appear only in test names.

## Verification

```bash
cd docs/evidence/edai2/gateway/g3b-owner-evidence-20261124T083106Z/screenshots
sha256sum -c SHA256SUMS.txt
```
