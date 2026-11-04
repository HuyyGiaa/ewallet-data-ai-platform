# Unified EDAI-2 CI/CD and observability evidence

Report: [`docs/edai2/cicd_observability_runtime_20261103.md`](../../../edai2/cicd_observability_runtime_20261103.md)

## Committed (sanitized)

| Path | Content |
|---|---|
| `jenkins/source-*.txt` | Snapshot SHAs, bundle sha256 and `git bundle verify` output |
| `jenkins/jobs/edai2-ml-ci-1/` | First ML build: Groovy CPS compile failure before `8d34a1a` |
| `jenkins/jobs/edai2-ml-ci-2/` | ML component CI: SUCCESS |
| `jenkins/jobs/edai2-agent-ci-1/` | Agent component CI: SUCCESS |
| `jenkins/jobs/edai2-docs-change-detection-1/` | Documentation-only change: SUCCESS, no component executed |
| `jenkins/jobs/edai2-agent-negative-1/` | Intentional negative gate: FAILURE, exit 42, release stage not reached |
| `regression_20261103/` | G3 request and responses, replays to each Coordinator replica, MCP auth probes, runtime state |
| `regression_20261103/observability/` | Tempo trace, Loki readback, Prometheus coverage, Grafana panel queries before and after the fix, Helm r31→r32 semantic diff |
| `screenshots/` | Jenkins stage views and Grafana dashboards captured from the live UI |
| `SHA256SUMS.txt` | Checksums of every file in this directory, including the private files below |

Each Jenkins job directory holds four files:

- `api.json`: build metadata
- `console.txt`: full console log
- `stages.json`: stage results from `wfapi`
- `artifacts.zip`: the archived `evidence/ci/<build>` directory

## Kept private (not committed)

These stay on the operator workstation and are listed in `SHA256SUMS.txt`:

- Trivy scan JSONs: `jenkins/scans/`
- Image build logs
- The rendered runtime manifest
- The raw cluster inventory: `preflight/`
- Earlier observability rollout logs: `observability/`, `tests/`

Helm values captured for the r32 upgrade are outside the repository.

## Reproduce a readback

```bash
kubectl -n ewallet-agents port-forward svc/tempo 13200:3200
curl -s -H 'Accept: application/json' \
  localhost:13200/api/traces/29e3c317b7ac34854c08e62088fda88d
sha256sum -c SHA256SUMS.txt
```
