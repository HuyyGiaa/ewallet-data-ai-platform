# G3-A0 closure: kagent-ui probe policy and Cloud Lab sleep safety

Two fixes before G3-A1. No GKE mutation: GKE stayed asleep; the only GKE calls were
read-only lookups (probe definitions, CRD/VAP conflicts). Kind results are LOCAL_KIND and
say nothing about Calico.

| Fix | Root cause | Change |
|---|---|---|
| P0 kagent-ui probes | The edge NetworkPolicy is the first one on the kagent-ui Pod (ambient namespace, Calico) and admits only the gateway; ambient kubelet probes arrive SNATed from 169.254.7.127 and would be dropped (readiness and startup probes, GET /health on 8080) | `destination-access.yaml`: for an ambient route with `access.probePort`, a separate ingress rule from `ambientProbeSource` (169.254.7.127/32) to that port only; `values.yaml`: `probePort: 8080` on kagent-ui. G3-A render diff: exactly that rule (`helm/g3a-render-before-after.diff`) |
| Cloud Lab sleep | `cmd_sleep` paused the WorkerPool ScaledObjects before the guarded-approval, manifest, unexpected-PDB and selector checks; a refusal left them paused. A failed resize was not undone; a failed undo was not reported | Every gate first, then all changes in one try; on failure undo what was changed (pauses, and only the Deployments actually scaled) and print `ROLLBACK INCOMPLETE` for any step that failed |

## Results

| Check | Result |
|---|---|
| Cloud Lab focused tests on the old code (`tests/cloud-lab-focused-BEFORE-fix.txt`) | 6 of 15 fail: guarded approval, unexpected PDB, invalid manifest, invalid selector, failed resize, incomplete rollback |
| Same tests after the fix | 15/15 |
| `tests/ops` (chart, edge checks, all Cloud Lab tests) | 217/217 |
| Component CI (pytest, unittest) | 131/131, 63/63 |
| Kind foundation | status 14/14, HTTP 32/32 |
| Kind full (+ probe-rule check) | status 27/27, HTTP 48/48, after restart 48/48 |
| Staged rollout on kind (`staged-rollout/`) | first Envoy config already secured; anonymous 401 |
| Ownership preflight of the G3-A render | EDGE_OWNERSHIP_OK |
| CRD artifact | sha256 equals the GitHub release digest; no conflicting object on GKE |

`G3A1_PLAN.md` is the updated approval package. `SHA256SUMS` covers every file.
