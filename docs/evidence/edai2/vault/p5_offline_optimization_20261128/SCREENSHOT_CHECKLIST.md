# Campaign A2: owner screenshot checklist (two-node run)

These 15 screenshots go with the two-node run in
[VAULT_GKE_REHEARSAL.md §9](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md#two-node-run-vault-a2-partial-wake-the-p5-recommendation).

- **You** capture them by hand.
- **Claude** records the technical evidence (`results.jsonl`, verify and plan JSON) and never captures or
  describes a screenshot as taken.
- None of them exists yet.
- A screenshot is never an approval. Each step that changes the cluster still needs your `A2_APPROVAL=<step>:$ID`, or the approval the block names.

Save them in `$REHEARSAL_DIR/screenshots/`. Each one comes before the step that would change what it shows.
When a row says **pause**, wait for the shot before you paste the next block.

| # | Phase | Screen or command | Expected | File | Pause |
|---|---|---|---|---|---|
| 1 | Before T1, asleep | Console › Billing › Account management | the project is linked to an **open** billing account | `a2-01-billing-open.png` | **pause**: no wake without it |
| 2 | Before T1, asleep | Console › Security › Key Management › key ring `ewallet-vault` › key `vault-unseal` | purpose Symmetric encrypt/decrypt, location `asia-southeast1`, primary version 1 **Enabled**, no rotation | `a2-02-kms-key.png` | **pause** |
| 3 | Before T1, asleep | Console › Compute Engine › Disks, filter `pvc-` | 16 disks, 90 GB, none in use | `a2-03-disks-before.png` | **pause** |
| 4 | After `BLOCK T1 PASS` | Console › Kubernetes Engine › ewallet-dev › Nodes | `cpu-pool`: **2** nodes Ready | `a2-04-two-nodes.png` | no (state stays) |
| 5 | After `BLOCK T1 PASS` | terminal: `jq . "$REHEARSAL_DIR/verify-partial.json"` and `tail -n 3 "$REHEARSAL_DIR/results.jsonl"` | `"profile": "vault-a2"`, `"failures": []`; `gke-preflight`, `kms-preflight` PASS | `a2-05-partial-verify.png` | **pause** before block 2 |
| 6 | After `BLOCK 2 PASS` | Console › Kubernetes Engine › Workloads, namespace `ewallet-vault` | StatefulSet `vault` 1/1 OK | `a2-06-vault-ready.png` | **pause**: block 2 ends with Vault up; T3 sleeps it |
| 7 | After `BLOCK 2 PASS` | terminal: `jq -c 'select(.step \| test("auto-unseal")) \| {step, result, seal_type, manual_unseal, label}' "$REHEARSAL_DIR/results.jsonl"` | every line `PASS`, `manual_unseal: none`, `label: KMS_GKE`; `seal_type: gcpckms` on `auto-unsealed` (init) and `rebound-initialised-auto-unsealed`. The restart records carry no seal type, but their phase checked it | `a2-07-kms-auto-unseal.png` | **pause** |
| 8 | After `BLOCK 2 PASS` | Console › APIs & Services › Cloud Key Management Service (KMS) API › Metrics, *Traffic by credential* | requests from `ewallet-vault-unseal@…` since the wake | `a2-08-kms-api-traffic.png` | **pause** |
| 9 | After `BLOCK 2 PASS` | terminal: `kubectl --context $CTX -n vault-rehearsal get externalsecret,secret` and `kubectl --context $CTX get pv \| grep ewallet-vault` | ExternalSecrets `SecretSynced` True; the two Vault PVs `Bound`, `Retain` | `a2-09-eso-and-retain.png` | **pause**: last view before the first sleep |
| 10 | After `BLOCK T3 PASS` | Console › Compute Engine › Disks, filter `pvc-` | 18 disks: the 16 plus the two Vault disks (5 GB, 1 GB), none in use; no VM in the instance group | `a2-10-mid-sleep-disks.png` | **pause** before T4 |
| 11 | After `BLOCK T4 PASS` | terminal: `grep '"vault-after-wake"' "$REHEARSAL_DIR/results.jsonl" \| jq .` | `seal_type: gcpckms`, `expected_seal_type: gcpckms`, `sealed: false`, `manual_unseal: none` | `a2-11-after-wake-gcpckms.png` | **pause**: block 5 deletes Vault |
| 12 | After `BLOCK 5 PASS` | Console › Compute Engine › Disks, filter `pvc-` | back to the 16 disks; both Vault disks gone | `a2-12-clean-disks.png` | **pause** before T6 |
| 13 | After `BLOCK T6 PASS` | Console › Kubernetes Engine › ewallet-dev › Nodes, and Compute Engine › Instance groups | 0 nodes; instance group size 0 | `a2-13-a2-asleep.png` | **pause**: R1 is its own approval |
| 14 | After `BLOCK R1 PASS` | Console › Kubernetes Engine › ewallet-dev › Workloads (all namespaces), and terminal `jq . "$REHEARSAL_DIR/verify-restored.json"` | 5 nodes; the platform's workloads OK; `"failures": []` | `a2-14-platform-restored.png` | **pause**: R2 records the manifest and sleeps |
| 15 | The next day | Console › Billing › Reports, this project, the campaign day | the day's cost; it can lag up to a day | `a2-15-cost-report.png` | no |

If a block fails, take one extra shot before you choose F1, F2, T-F2 or T-FB: the terminal with the
failed line and `$R inspect`, saved as `a2-fail-<block>.png`.
