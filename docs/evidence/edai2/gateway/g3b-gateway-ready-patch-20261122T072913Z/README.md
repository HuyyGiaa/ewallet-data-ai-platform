# G3-B gateway readiness patch (offline)

Base 91296297. Root cause, from the first Full G3-B attempt `g3b-full-20261122T070154Z` (raw evidence
kept outside Git): during wake, the `ewallet-edge-gateway` ReplicaSet's pod creation was rejected by
istiod's sidecar-injector webhook, which was still starting. The ReplicaSet retries with back-off,
so `edge-restricted` found no gateway pod (fail-closed: no LB, DNS untouched, cleanup COMPLETE, 0 nodes).

New phase `gateway-ready` (after `baseline`, before `temporary-certificate`/`edge-restricted`; also
in the dns-transition scope). It is read-only:
1. `kubectl get deployment ewallet-edge-gateway -o jsonpath={.spec.replicas} {.status.readyReplicas}`: zero or unknown desired replicas means FAIL (rollout status could pass on 0);
2. `kubectl rollout status deployment/ewallet-edge-gateway --timeout=<remaining>s`, asked again if it returns early, all inside one `GATEWAY_READY_SECONDS` = 1500 s budget capped by the deadline (`DEADLINE_EXPIRED` if none left);
3. at least one Ready replica.

On failure: no edge change, no LB, no DNS change; cleanup and guarded sleep run. No restart, delete,
scale, injection-policy change or webhook bypass. In the Simulator without a fake clock it stops
after 3 attempts.

| Tests | Result |
|---|---|
| `tests/ops/test_g3b_gateway_ready.py` (new) | 11 passed |
| G3-B regression | 331 passed |
| full `tests/ops` | 612 passed |

Updated tests: `test_g3b_prelive.py` (phase order lists `gateway-ready`; the Secret-sync tests target the sync's own `rollout status … --timeout=300s`).
Next live wake keeps the G3-A1 recovery manifest. Rows 37–43 stay INCOMPLETE. Simulation only; no cloud change.
