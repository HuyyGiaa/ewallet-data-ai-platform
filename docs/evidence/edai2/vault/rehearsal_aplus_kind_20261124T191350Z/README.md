# Campaign A+ on kind: `vault-rehearsal-20261124T191350Z`

The full A+ sequence of [VAULT_GKE_REHEARSAL.md §9](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md#9-campaign-a-the-feature-api-release-storage-and-wake)
on the local kind cluster. **KIND PASS; this is not GKE evidence.** Every value is synthetic.

| | |
|---|---|
| Date | 2026-11-24, 19:13–19:19 UTC (16 steps of the sequence, 345 s) |
| Cluster | kind `kind-llm-agent-local`; cert-manager v1.16.2 installed for the run and removed afterwards |
| Software | Vault 2.0.4 (chart 0.34.1), ESO v2.14.0 (chart 2.14.0), the serving chart `infra/helm/ewallet-serving` as Helm release `rehearsal-serving`, the Feature API image `localhost:5001/ewallet-inference@sha256:8a6106c3…` (built from this branch's `ai_platform/inference/Dockerfile`; on GKE the production image) |
| Script | `scripts/vault/gke_rehearsal.sh` sha256 `7a14eae8dd6789fc…`, post-renderer `serving_post_render.py` `9771dbdc7572e23c…` (the committed versions) |
| Result | 67 steps, no failure; afterwards no Vault/ESO/rehearsal object, PV, port-forward or local material |

## Sequence

| Step | What ran | Result |
|---|---|---|
| preflight … consumer | Campaign A, unchanged (Vault, init, bootstrap, ESO, synthetic Redis consumer) | PASS |
| feature-api | the rehearsal's "existing" Secret `rehearsal-feast-online-redis`; `shadow_compare.sh` → `SHADOW_MATCH` against the ESO Secret `feature-api-redis-vault`; Feast seed Job on the Feature API image (registry 9.4 KB, checked to contain no credential); `helm install rehearsal-serving` (revision 1) | lookup **HTTP 200, 31 features**, `BASELINE_OK` |
| switch | the official G8 command: `helm upgrade --reuse-values -f infra/helm/ewallet-serving/values-vault-eso.yaml` (unchanged file) → revision 2, Secret `feature-api-redis-vault`, no `EWALLET_VAULT_*`, existing Secret unchanged | lookup 200, `BUSINESS_LOOKUP_PASS` |
| negative | Campaign A's six denials and the NetworkPolicy probe | PASS |
| rollback | `helm rollback rehearsal-serving 1` → lookup 200; upgrade again → lookup 200; final state: store `Valid`, ExternalSecret `SecretSynced`, existing Secret unchanged, no test objects, production snapshot unchanged | PASS |
| lifecycle | KV v2/v3 propagation; Vault restart sealed while Pod Ready; **the Feature API keeps serving while Vault is sealed** (lookup 200); unseal, new reconciliation | PASS |
| retain | both PVs `Retain`; Vault scaled to 0; **both PVCs deleted** → PVs `Released`; runbook recovery (claimRef removed, PVC recreated with `volumeName`) → Vault back **initialised and sealed**, unsealed, KV version 3 and the operator token intact, ESO re-synced, lookup 200 | PASS |
| simulated wake | `vault-0` and the synthetic Redis pod deleted (kind cannot scale a node pool to zero) | — |
| after-wake | Vault initialised and sealed → unsealed; KV version 3; PVs `Bound/Retain`; ESO new reconciliation, value equal; Redis seeded again, Feature API restarted, lookup 200 | PASS |
| cleanup | ownership, ESO scope and PVC → PV identity checks; the two recorded PVs back to `Delete`; releases `rehearsal-serving`, `external-secrets`, `vault` and the three namespaces removed | PASS |
| verify-clean | nothing left; no local material or port-forward; snapshot equal to the baseline | PASS |

Seven lookups, all `BASELINE_OK` with HTTP 200 and 31 features: baseline, switch, rollback,
re-upgrade, while sealed, after the Released-PV recovery, after the simulated wake.

## Files

`results.jsonl` (every step), `lookup-request.json` (the synthetic fixture), `lookup-baseline.json`
and `lookup-after-wake.json` (two response bodies: synthetic feature values), `volumes.json`,
`baseline.json`, `eso-cluster-objects.json`. The synthetic registry and private material are not published.

## Limits

- kind's local-path volumes are host directories: the Released-PV recovery is proven here, a Persistent
  Disk detach and re-attach is not. The "wake" is two pod restarts, not a node-pool sleep.
- The Feature API reads a synthetic registry from a ConfigMap, not the production GCS registry through
  Workload Identity; the Redis is a disposable in-namespace instance.
