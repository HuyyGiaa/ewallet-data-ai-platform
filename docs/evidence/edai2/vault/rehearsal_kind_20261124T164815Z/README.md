# Vault + ESO technical rehearsal: local dry run on kind (`vault-rehearsal-20261124T164815Z`)

The complete rehearsal procedure of [VAULT_GKE_REHEARSAL.md](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md),
run on the local kind cluster before any GKE execution. **It is not GKE evidence.** It shows that
every phase, command and assertion of [`gke_rehearsal.sh`](../../../../../scripts/vault/gke_rehearsal.sh)
works, and that cleanup returns the cluster to its earlier state.

| | |
|---|---|
| Date | 2026-11-24, 16:48–16:52 UTC (10 phases, 207 s) |
| Cluster | kind `kind-llm-agent-local` (1 node), storage class `standard` (`values-local.yaml`) |
| Software | Vault 2.0.4 (chart 0.34.1), ESO v2.14.0 (chart 2.14.0), both chart digests verified; cert-manager v1.16.2 installed for this run only and removed afterwards; Redis `7.4.2-alpine` (the digest the platform already uses) |
| Script | `scripts/vault/gke_rehearsal.sh`, sha256 `835c85bdcd408bf1…` (the committed version) |
| Values | synthetic only; never printed. The evidence holds statuses, key names and 12-character hash prefixes |
| Result | 43 recorded steps, no failure; nothing left afterwards (`verify-clean`) |

## Files

| File | Content |
|---|---|
| `results.jsonl` | one line per step and phase |
| `vault-describe.json` | live Vault configuration from `ops/vault/cli.py describe`: engines, auth methods, audit device, roles, ACL policy text (no secret data) |
| `eso-cluster-objects.json` | the ESO chart's cluster-scoped objects and 25 CRDs, as installed |
| `volumes.json` | the two Vault PVs (reclaim `Delete`); cleanup checked they are gone |
| `baseline.json` | the unrelated state compared before and after (Helm releases and revisions, LoadBalancer Services) |

## What the run shows

| Phase | Result |
|---|---|
| preflight | chart digests; cert-manager available; no Vault/ESO namespace, release, CRD, chart cluster object or volume |
| vault | cert-manager issued `vault-ca` and `vault-server-tls` (SANs include `vault.ewallet-vault.svc.cluster.local`); pinned image; 2 PVCs Bound; no PDB; ClusterIP only; HTTPS verifies with the CA |
| init | `initialized: true` (3/2); **Pod Ready `True` while `sealed: true`**; unsealed |
| configure | official bootstrap, then the rehearsal role; `describe`; orphan operator token; root retired (403); operator token cannot administer (403); untrusted CA refused |
| eso | 3 deployments available; 25 CRDs; 9 cluster-scoped RBAC/webhook objects; no cluster-scoped stores |
| consumer | KV v1 → `SecretSynced`, owner `ExternalSecret/rehearsal-redis-vault`, value equal to Vault v1; Redis `PING` and `SET`/`GET` succeed with the ESO-managed Secret |
| negative | unauthorized path, missing key, wrong ServiceAccount, another namespace's role, wrong audience, untrusted CA: each `SecretSyncedError` with its expected cause and **no target Secret**; a non-ESO pod cannot reach Vault (control: it reaches Redis) |
| lifecycle | v2 (deliberately wrong) reaches the consumer (`AUTH_FAILED`); v3 restores it; restart → sealed while Pod Ready → ESO `vault-sealed`, Secret unchanged, consumer still works → manual unseal → new reconciliation |
| cleanup | both releases uninstalled, PVCs and PVs deleted, 3 namespaces deleted, synthetic material removed |
| verify-clean | no Vault/ESO object of any kind; no local material or port-forward; unrelated state unchanged |

## Observations carried into the GKE plan

- Vault 2.0.4 adds a built-in `agent-registry/` mount and a `default-ceiling` policy. Neither is
  used here; the rehearsal roles set `token_no_default_policy`.
- ESO 2.14.0 installs 25 CRDs, none annotated `keep`: `helm uninstall` removes them.
- Two script defects were found and fixed by earlier dry runs on the same day, and are absent
  from this run: a backgrounded shell function orphaned its `kubectl port-forward`, and a
  read-only phase recreated the private directory.

## Not shown here (GKE only)

Persistent Disk provisioning and deletion, Calico enforcement of the Vault NetworkPolicy, GKE
TokenReview for Kubernetes auth, image pulls by GKE nodes, and the effect on the shared platform.
Those are what the GKE rehearsal is for.
