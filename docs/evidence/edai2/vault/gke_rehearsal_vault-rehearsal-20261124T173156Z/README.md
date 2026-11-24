# Vault + ESO GKE technical rehearsal (Campaign A): `vault-rehearsal-20261124T173156Z`

The synthetic rehearsal of [VAULT_GKE_REHEARSAL.md](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md)
on the real cluster, run by Claude with the owner's approval. **Verdict: `VAULT_GKE_REHEARSAL_COMPLETE`.**
It is not row 54 evidence: the official campaign (Campaign B) is run by the owner with the real
credential and screenshots.

| | |
|---|---|
| Environment | GKE `ewallet-dev`, `asia-southeast1-b`, project `ewallet-data-ai-platform-hg26`, Kubernetes v1.36.4-gke.2377000, node pool `cpu-pool` 5 × e2-standard-4, Calico NetworkPolicy |
| Software | Vault 2.0.4 (chart 0.34.1), ESO v2.14.0 (chart 2.14.0), digests verified; cert-manager v1.16.2 (already on the cluster); synthetic Redis `7.4.2-alpine` (the platform's digest) |
| Source | `feature/edai2-vault-eso` at `ea0c21dc` (script `scripts/vault/gke_rehearsal.sh`) |
| Approvals (owner, before the run) | R2–R8; R9 and R10 conditionally (all gates passed); for R10 additionally `--also-approve ewallet-mcp/feature-rag-mcp` for this campaign only |
| Timeline (UTC, 2026-11-24) | wake 17:39:07 → awake 17:40:28 → platform settled ≈17:47 → R2–R8 17:52–18:01 → R9 18:02–18:04 → sleep 18:07:48 → 0 nodes 18:11:31 |
| Values | synthetic only; shares, tokens and passwords never printed; the private directory was removed at cleanup |

## Results

`results.jsonl` holds 49 steps. One is a FAIL: the first R2 `preflight`, run about one minute
after the wake, stopped because cert-manager was not yet available. Nothing had been changed;
the re-run five minutes later passed. Every other step passed.

| Step | Result |
|---|---|
| Pre-wake gates | HEAD `ea0c21dc` clean; pinned kubeconfig/context/project; preflight READY ([`preflight/preflight-prewake.jsonl`](preflight/preflight-prewake.jsonl)); chart digests; 0 nodes; newest manifest `g3b-owner-evidence-20261124T083106Z` |
| R2 Wake and baseline | `wake-full` 5 nodes; `verify --expect awake` no failures; cert-manager 3/3; awake preflight READY ([`preflight/preflight.jsonl`](preflight/preflight.jsonl)); no collisions; baseline: 18 Helm releases, Feature API generation 5 on `feast-online-redis`, edge gateway `ClusterIP`, no LoadBalancer |
| R3 Vault | cert-manager issued `vault-ca` and `vault-server-tls` (SANs incl. `vault.ewallet-vault.svc.cluster.local`); pinned image; PVCs `data-vault-0` 5 GiB and `audit-vault-0` 1 GiB Bound on `standard-rwo`, reclaim `Delete`, two pd-balanced disks confirmed in Compute Engine; no PDB; ClusterIP only; HTTPS verified; `initialized: false` |
| R4 Init and bootstrap | 3/2 init; **Pod Ready `True` while `sealed: true`**; unsealed; official and rehearsal bootstrap; [`vault-describe.json`](vault-describe.json) (KV v2 `ewallet/`, Kubernetes auth, file audit, 6 roles, 8 policies); orphan operator token; root retired, then 403; operator token cannot administer (403); untrusted CA refused |
| R5 ESO and sync | 3 deployments available; 25 CRDs; 9 ESO cluster objects ([`eso-cluster-objects.json`](eso-cluster-objects.json)); no cluster-scoped stores; SecretStore `Valid` (Kubernetes auth through GKE TokenReview, audience `vault`); ExternalSecret `SecretSynced`; target owned by the ExternalSecret; value equal to Vault v1 (hash prefix) |
| R6 Consumer | redis-cli `PING` and `SET`/`GET` round trip with the ESO-managed Secret: `OK` |
| R7 Negative | unauthorized path, missing key, wrong ServiceAccount, another namespace's role, wrong audience, untrusted CA: each `SecretSyncedError` with its expected cause and no target Secret; **Calico blocked a non-ESO pod from Vault** (control: it reached Redis); Vault Services ClusterIP only |
| R8 Lifecycle | v2 (deliberately wrong) propagated, consumer `AUTH_FAILED`; v3 restored, `OK`; restart → sealed while Pod Ready → ESO `vault-sealed`, Secret unchanged, consumer `OK` → unseal → new reconciliation, `OK`; production snapshot equal to the baseline |
| R9 Cleanup | before any deletion: namespaces labelled with this ID, ESO objects only in `vault-rehearsal` and cluster objects as recorded, live PVC → PV → disk chain equal to [`volumes.json`](volumes.json) with reclaim `Delete`, both disks present. Removed: the inventory in [`pre-cleanup-inventory.txt`](pre-cleanup-inventory.txt). Both disks then **absent**, proven by an HTTP 404 naming each disk; no PV left. `verify-clean`: no Vault/ESO object, no local material or port-forward, preflight READY ([`preflight/preflight-after.jsonl`](preflight/preflight-after.jsonl)), production snapshot equal |
| R10 Guarded sleep | fresh recovery manifest (83 workloads; `feature-rag-mcp`, `drift-mcp`, `fraud-coordinator` at 2); plan `SAFE_AFTER_REVERSIBLE_PREPARATION`, no active work or identity problems ([`plan-sleep-final.json`](plan-sleep-final.json)); `sleep --scale-pdb-guarded --pause-keda --also-approve ewallet-mcp/feature-rag-mcp`; [`verify --expect sleep`](verify-sleep.json) no failures; 0 nodes, 0 VMs; 0 LoadBalancer Services, 0 forwarding rules; 16 disks / 90 GB as before the wake; asleep preflight READY ([`preflight/preflight-asleep-after.jsonl`](preflight/preflight-asleep-after.jsonl)) |

### Removed by R9 (all created by this rehearsal)

Namespaces `vault-rehearsal`, `external-secrets`, `ewallet-vault`; Helm releases `vault` (vault-0.34.1)
and `external-secrets` (external-secrets-2.14.0); the 25 `*.external-secrets.io` CRDs; ClusterRoles
`external-secrets-{cert-controller,controller,edit,servicebindings,view}`, ClusterRoleBindings
`external-secrets-{cert-controller,controller}` and `vault-server-binding`, ValidatingWebhookConfigurations
`externalsecret-validate` and `secretstore-validate`; PVCs `audit-vault-0` and `data-vault-0`, PVs and disks
`pvc-c731b990-6c5b-4b83-8133-3814881bc9b9` (1 GB) and `pvc-ab32bcd6-0576-43bd-aff1-13266b2ff7e2` (5 GB);
the local synthetic material.

## Production integrity

| Checked | How | Result |
|---|---|---|
| Helm releases and revisions (18) | snapshot after R8, after R9, and after the sleep | unchanged |
| `ml-platform/feast-online-redis` | `resourceVersion` only (value never extracted, printed or copied) | unchanged |
| Feature API | generation and Secret reference after R8/R9; Secret reference after the sleep | `feast-online-redis`, unchanged |
| Edge gateway | Service type | `ClusterIP`, unchanged |
| LoadBalancers | Services and forwarding rules | none before, during or after |
| `ewallet-serving` vs repository chart | preflight `serving-no-drift` before and after | identical |

The Deployment `generation` is compared only while awake: the sleep's scale-to-zero raises it by design
([`production-after-sleep.json`](production-after-sleep.json) compares the configuration fields).
Not covered by this snapshot: Agent Platform objects beyond their Helm revisions.

## Findings

- **cert-manager takes ≈3 min after a wake**, while `cloud_lab.py verify --expect awake` already passes
  (it checks workload replicas, not availability). The rehearsal's preflight caught it and stopped
  without changes. Campaign B's G1 must re-run the preflight until `cert-manager-ready` is PASS.
- **Pre-existing, unrelated, not changed:** `kubeflow/ml-pipeline-persistenceagent` and
  `ml-pipeline-scheduledworkflow` stay in `Init` because two NetworkPolicies on `ml-pipeline`
  (20–22 days old, `infra/helm/llm-agent-platform/n11/`) admit only `otel-collector` and
  `ml-pipeline-ui` ([`finding-kfp-netpol.json`](finding-kfp-netpol.json)).
- **Guarded sleep needs `--also-approve ewallet-mcp/feature-rag-mcp`**, as in earlier campaigns; each
  campaign needs its own approval.
- Vault 2.0.4 also ships an `agent-registry/` mount and a `default-ceiling` policy (unused).
- One evidence label was corrected after the run: the cleanup line in `results.jsonl` said
  `gcloud disks list`; the check that ran (code at `ea0c21dc`) was `gcloud compute disks describe`
  with an HTTP 404 naming each disk. The script's label is fixed in the same commit.

## Cost

Nodes were up from 17:39 to 18:11 UTC, about 33 minutes: 5 × e2-standard-4 at ≈ US$0.83/h is
≈ US$0.45, plus the cluster fee (≈ US$0.10/h) and the two synthetic disks for about 18 minutes
(fractions of a cent). Estimate from `cloud_lab.py cost-status` rates, not a billing export. Asleep
afterwards: ≈ US$0.11/h plus the cluster fee, as before.

## Not published

`wake.log`, `sleep.log` (local paths), the recovery manifest (operational), and the private
directory (deleted). The recovery manifest stays in `~/ewallet-evidence/vault/vault-rehearsal-20261124T173156Z/recovery-manifest`
and is now the newest one for the next wake.
