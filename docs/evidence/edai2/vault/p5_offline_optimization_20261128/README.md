# Vault V2 P5: offline optimization of Campaign A2 (2026-11-28)

**OFFLINE ONLY.** Billing is closed ([P5-A](../p5a_gke_a2_readiness_20261128/README.md)), so nothing here
touched GKE, Cloud KMS, IAM, Terraform or any disk. The inputs were:

- the repository;
- the newest recovery manifest, copied to [`inventory/`](inventory/);
- chart renders;
- unit tests;
- one kind run.

Every GKE statement is a prediction until the live checks in §9 pass.

**Result:** the whole of Campaign A2 runs on **2 nodes**, both wakes included. It needs the partial-wake profile
`vault-a2`, the cloud-lab fixes in §1, and a fallback to the proven 5-node run at the first live check that
fails. Restoring the full platform afterwards needs **one 5-node wake** (R1/R2). It can run straight after
A2, or as the opening wake of the next campaign that needs the platform.

## 1. Audit findings

**Partial wake (`cloud_lab.py`).** No partial wake had ever been executed on this platform. Its
behaviour was checked against Campaign A2:

| # | Finding | Effect on a 2-node A2 | Change |
|---|---|---|---|
| H1 | `plan-wake` ignored the pods of WorkerPool-owned Deployments (`ewallet-sandbox/business-agents-deployment`, `kagent-pilot/kagent-pilot-deployment`). They are never recorded or scaled, so they come back with every wake: 1.5 CPU, 3 GiB | the old count said **1 node**, and 1 node does not fit | counted as `unholdable` |
| H2 | a partial wake unpaused **every** ScaledObject that cloud-lab had paused | the next sleep would need a KEDA approval again. `record-manifest` would also lose its guard and could record the held-down zeros as the platform | only allow-listed targets are unpaused |
| H3 | `verify --expect awake` could only check the whole platform | a partial wake could never pass it | `--mode/--profile` verify the partial state |
| H4 | KEDA held at 0 leaves its `external.metrics` APIService unavailable, and Kubernetes does not finish deleting a namespace while discovery fails | **cleanup would hang** | `vault-a2` keeps `keda-operator-metrics-apiserver` running. The partial verify fails on any APIService that is not `Available` |
| H5 | an admission webhook with `failurePolicy: Fail` whose server is held down would refuse the rehearsal's first install | block 2 would fail at `vault` | the partial verify lists such webhooks, for the API groups the rehearsal writes |
| H6 | a recorded StatefulSet with `whenScaled: Delete` would lose its PVCs when the wake scales it to 0 | data loss | T1 refuses to wake if one exists. The repository sets `Retain` explicitly once and `Delete` nowhere; the live values are checked by T1 |
| H7 | nothing compared the live replicas with a manifest | a stale manifest is noticed only after the wake | new read-only `diff-manifest` |

**Campaign A2 implementation (`gke_rehearsal.sh`, charts):**

| Area | Status (offline) |
|---|---|
| `gcpckms` seal | **PASS.** The rendered `vault-config` has `seal "gcpckms"` with project `ewallet-data-ai-platform-hg26`, region `asia-southeast1`, key ring `ewallet-vault`, key `vault-unseal`, and no `credentials` parameter. `kms_preflight.py --mode offline` reports no blocking gate |
| Workload Identity | repository **PASS**: KSA `ewallet-vault/vault` is annotated with `ewallet-vault-unseal@…`. Cloud side BLOCKED: P5-A had 7 PASS and 4 BLOCKED |
| Init | **PASS** (code): recovery keys 3/2, no unseal key, auto-unseal within 120 s, manual `unseal` refused |
| Auto-unseal check | **GAP, FIXED.** `after-wake` recorded `seal_type` but never compared it. The new `expect_seal` now runs after every auto-unseal: init, both restarts, the Released-PV rebind, and `after-wake`. On GKE it requires `gcpckms`; kind's `transit` is refused there, and `gcpckms` is refused on kind |
| ESO, consumer, Feature API, restart, Released-PV recovery, cleanup, rollback | unchanged since the A+ hardening. Re-run on kind in §8: PASS |
| Kind seal-down drill (K14) | **PRE-EXISTING RACE, FIXED.** The drill failed 3 times out of 3 as "unclassified" when ESO marked the store NotReady before the ExternalSecret reconciled; the network error is then logged under the store. The classifier now reads the store's lines in that case, and still accepts only `vault-unreachable` or `vault-sealed`. This drill is kind-only and never runs on GKE ([kind-v2-run.md](kind-v2-run.md)) |
| Manifest reference | **STALE, FIXED.** Runbook §3 named `g3b-owner-evidence-20261124T083106Z`; it now names `vault-rehearsal-20261124T173156Z`, and a live `diff-manifest` is required before the wake |
| Renders | Vault: ClusterIP only, no PDB, 1 replica, PVCs 5 Gi + 1 Gi `standard-rwo`, readiness `uninitcode=204`. ESO: no PDB, no LoadBalancer. `rehearsal-serving`: the overlay changes only the Secret name |

## 2. Node counts, step by step

| Step | Nodes | Needed because |
|---|---|---|
| T1 first wake | **2** | system pods + the WorkerPool pods + the rehearsal: 2.96 CPU peak; 1 node gives 2.55 |
| block 2 phases | 2 | the same pods |
| T3 mid-campaign sleep | 2 → 0 | plain sleep |
| T4 second wake | **2** | the same pods as T1, and the same two Vault disks in the same zone. Vault recovery (the rebind, a KMS auto-unseal, KV version, PVs, ESO, consumer, lookup) is verified here on 2 nodes. Nothing in it needs production |
| block 5 cleanup | 2 | — |
| T6 final A2 sleep | 2 → 0 | plain sleep |
| R1 restoration | **5** | the whole recorded platform: 10.3 CPU (4 nodes would be 104 % of the budget) |
| R2 sleep | 5 → 0 | guarded sleep, which records the fresh recovery manifest |

- **The second wake does not need 5 nodes.** Its pods are those of the first wake, and `after-wake` checks
  only rehearsal state plus the production *objects* (`compare_production`).
- **Deferring the full-platform restoration to R1 is safe:**
  - the cluster stays asleep;
  - T1 proved the live declared state equals `$MANIFEST` apart from the three guarded zeros;
  - T6 proves that afterwards it differs only by held-down zeros;
  - R1 restores and verifies against `$MANIFEST`.
- **Until R1, the next wake of this cluster must be R1** (or T-FB); a partial-wake manifest never
  exists. That is stated in T6's PASS line and in the runbook.

## 3. Capacity assumptions

From [`capacity-cost.json`](capacity-cost.json), which uses `cloud_lab.py`'s own `NodeShape` and `plan-wake`.

- **Node:** e2-standard-4, 3920 m / 13273 Mi allocatable, minus measured DaemonSets (918 m / 1585 Mi), at
  85 % packing. That leaves **2552 m / 9935 Mi** per node.
- **Inventory:** the manifest recorded on 2026-11-24 at 5 nodes, so `konnectivity-agent` (5) and
  `calico-typha` (2) are counted high.
- **Workloads that run in `vault-a2`:**
  - every system namespace: `kube-system`, `gmp-system`, `gke-managed-cim`, `istio-system`, `cert-manager`;
  - the two WorkerPool Deployments, which cannot be held down;
  - KEDA's metrics apiserver;
  - the rehearsal: Vault 100 m / 256 Mi, ESO 40 m / 128 Mi, Feature API 200 m / 768 Mi during a rollout,
    Redis 10 m / 16 Mi. Its Jobs request nothing.
- **Peak requests:** 2.96 CPU and 5.5 GiB.
- **Largest pod:** 500 m.
- **Scheduling:** no rehearsal pod has a nodeSelector it cannot meet. The Feature API needs
  `iam.gke.io/gke-metadata-server-enabled`, which every pool node has. Vault's anti-affinity does not
  matter with one replica.
- **Disks:** a single zone (`asia-southeast1-b`), so the Vault disks reattach to either node. Held-down
  StatefulSets leave their disks detached.

## 4. The workload profile: `experiment/vault-a2`

In [`ops/gcp/cloud_lab_profiles.json`](../../../../../ops/gcp/cloud_lab_profiles.json):

```json
"vault-a2": ["keda/keda-operator-metrics-apiserver", "ewallet-vault/*", "external-secrets/*",
             "vault-rehearsal/*", "vault-rehearsal-agents/*"]
```

**Held at 0:** every other recorded application workload, including:

- `keda-operator`, so nothing scales a held-down Deployment back;
- the controllers in `kagent`, `ate-system`, `kserve` and `kubeflow`, which are scaled first.

**Not touched:**

- the system namespaces;
- the WorkerPool Deployments. They run without their controller until the next full wake. That is
  harmless, and it is a live observation item.

**What A2 needs from production:** no running pod. `compare_production` compares Helm releases, Secret
resourceVersions, Deployment templates and Service types, and scaling changes none of them.

## 5. Runbook changes

[VAULT_GKE_REHEARSAL.md](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md):

- §3: the newest manifest, plus the live `diff-manifest` it requires.
- §9 *Two-node run*: T0 (helpers), T1, T3, T4, T6, R1 and R2. Blocks 2 and 5 are unchanged.
  - **Approvals.** Every step that changes the cluster has its own owner approval: T1, T3, T4, T6, R1,
    R2 and each recovery row through `approved <step>`, which needs the owner's `A2_APPROVAL=<step>:$ID`;
    blocks 2 and 5 through their existing `VAULT_REHEARSAL_APPROVED` and
    `VAULT_REHEARSAL_CLEANUP_APPROVED`. A plain sleep (`SAFE_TO_SLEEP`) saves only the **additional**
    guarded, KEDA and `--also-approve` approvals; R2 is a guarded sleep and needs them.
  - **No recovery manifest during A2.** A partial wake holds production at 0, and a manifest recorded
    then would make the next full wake restore those zeros. It is replaced by:
    - the pre-wake live check and snapshot (T1: `diff-manifest`, `live-before-wake.json`);
    - the rehearsal workloads recorded before the first sleep (T3: `rehearsal-workloads-mid.json`), which
      T4 requires again, same replicas and Ready, before `after-wake`;
    - `after-wake`'s Vault, KMS, PV, ESO, consumer and production checks;
    - R1's `verify --expect awake` against `$MANIFEST`;
    - R2's fresh manifest of the fully awake platform, as block 6 records.
  - **Failures:** T-F0 (the wake stopped), T-F1 and T-F2 (as F1/F2), T-FB (fall back to the 5-node blocks
    1–6 before anything of Vault is installed), T-FR (restoration failed).
- The cost line is updated.

[CLOUD_LAB_RUNBOOK.md](../../../../runbooks/edai2/CLOUD_LAB_RUNBOOK.md) documents the new partial-wake
behaviour. The 5-node blocks 1–6 are unchanged, and `wake-full` behaves as before.

**Side effect for other profiles:** `plan-wake` now counts the unholdable pods. Nine existing profiles
gain a node: `feature-store-verify`, `jenkins-smoke`, `kserve-small`, `mcp-fallback`, `sandbox`,
`serving-load` and `serving-keda` go from 1 to 2, and `one-agent` and `agent-sandbox` from 2 to 3.
`edge`, `vllm-cpu`, `agent-keda` and `agent-integration` are unchanged. `serving-keda`'s runbook
already used `--nodes 2`. No partial wake was ever executed, so no past campaign changes.

## 6. Owner screenshots

There are 15, listed in [SCREENSHOT_CHECKLIST.md](SCREENSHOT_CHECKLIST.md). Each one is taken before the
step that would change what it shows, and every wake, sleep and the cleanup pauses for it. None has been taken.

## 7. Cost comparison (estimate, every wake included)

List prices are the ones `cloud_lab.py` read on 2026-11-06. They were not re-read and no quota was
checked, because billing is closed. Step durations are estimates:

| Step | Minutes |
|---|---|
| full wake | 20–30 |
| partial wake | 10–15 |
| block 2 on GKE | 30–40 |
| guarded sleep | 15–30 |
| plain sleep | 5–10 |
| after-wake + cleanup | 15–20 |
| restoration verify | 5–10 |

Nodes are billed while they exist. Per node-hour, node, external IP and boot disk come to US$0.178.
Details are in [`capacity-cost.json`](capacity-cost.json).

| Sequence | Awake | Node-hours | Nodes, IPs, boot disks | Cluster fee (before the free tier) |
|---|---|---|---|---|
| Current runbook: blocks 1–6 on 5 nodes | 115–180 min | 9.6–15.0 | US$1.70–2.67 | US$0.19–0.30 |
| **Two-node A2 + R1/R2 straight after** | 115–180 min | 5.8–9.5 | **US$1.04–1.69** | US$0.19–0.30 |
| Two-node A2 only (R1 is the next campaign's opening wake) | 75–110 min | 2.5–3.7 | US$0.44–0.65 | US$0.12–0.18 |
| Three-node A2 + R1/R2 | 115–180 min | 7.1–11.3 | US$1.26–2.02 | US$0.19–0.30 |
| R1/R2 alone | 40–70 min | 3.3–5.8 | US$0.59–1.04 | US$0.07–0.12 |

- **With the restoration straight after A2:** about US$0.65–1.0 less than today (~40 %). The approvals for
  the guarded sleep and the KEDA pause are needed once (R2) instead of twice.
- **When R1 opens the next campaign:** A2 costs US$0.44–0.65, and the restoration costs that campaign only
  R1's verify.
- **A fallback** (T-FB) adds the cost of the short partial wake before it.

**Charges that continue whatever the node count:**

- ≈ US$0.11/h, ≈ **US$2.63/day**: GCS (3.4 TiB, US$0.094/h), the 16 PVC disks (90 GiB), Artifact Registry;
- the cluster fee, US$2.40/day, unless the free tier offsets it;
- the two Vault disks during the campaign (6 GiB, cents).

Those continuing charges, not the campaign, dominate the bill.

**Not included:** KMS key and operation charges, logging, monitoring, egress.

**What can stay suspended during A2:** every production workload, including KEDA's operator, Jenkins,
Kubeflow, the agents and vLLM. No acceptance gate uses them.

## 8. Offline test results

| Check | Result |
|---|---|
| `pytest tests/ops` (whole suite) | **1082 passed, 2 skipped** ([test-results.txt](test-results.txt)) |
| Focused | cloud lab, all modes including partial wake: 95 passed. Vault rehearsal, preflights, KMS guard, ESO, tools, shadow compare: 398 passed, 2 skipped |
| New `tests/ops/test_cloud_lab_partial_wake.py` | 12 tests: the profile, the unholdable count, 2 nodes for `vault-a2` and 5 for full on the recorded inventory, hold-down order, KEDA left paused, `record-manifest` refused, partial verify (pass; revived workload; APIService; webhook), full verify unchanged, `diff-manifest`, and a stateful dry run of T1 → T3 → T4 → T6 → full wake that restores every recorded replica |
| New rehearsal tests | `expect_seal` after every auto-unseal wait; a functional matrix (GKE: `gcpckms` passes, `transit` and `shamir` refused; kind: the reverse); runbook T-block structure (fail-fast, gates, no guarded flags, no `record-manifest`, PVC-retention check before the wake); `es_error` with a store-not-ready error, where a specific cause keeps priority |
| Regression | every existing cloud-lab test (guarded sleep, validation-first, record-manifest, WorkerPool) and every existing A+/V2 rehearsal test still pass |
| Helm renders | Vault (GKE values), ESO, `rehearsal-serving` with and without the official overlay |
| `kms_preflight.py --mode offline` | `VAULT_KMS_PREFLIGHT_OFFLINE_ONLY`, no blocking gate (never READY offline, by design) |
| Kind V2 run with the changed driver | runs 1–3 stopped at the pre-existing K14 race; after the fix, **run 4 PASS**: 117 records, 0 FAIL, three gates open, 8 lookups `BASELINE_OK`. `expect_seal` passed at init, both restarts, the rebind and after the wake, all `transit` ([kind-v2-run.md](kind-v2-run.md)) |
| One stub test, run while a kind run was live | it failed because `verify-clean` saw the run's real Feature API port-forward (`pgrep`). It passes with no port-forward open. Run the suite with no kind campaign in progress |

## 9. Remaining cloud-dependent checks

Each check runs once Billing is restored, and must pass before the step it gates.

1. Billing open; `kms_preflight.py --mode cloud` **READY** (11/11); the cluster describe succeeds; 0 nodes (P5-A §2).
2. `diff-manifest --manifest $MANIFEST`: only the three guarded Deployments at 0, nothing missing (T1 enforces it).
3. Live StatefulSets: none with `whenScaled: Delete` (T1 enforces it).
4. After the 2-node wake, `verify_partial` (T1): `keda-operator-metrics-apiserver` keeps
   `v1beta1.external.metrics.k8s.io` Available without the operator; no fail-closed webhook is left
   without its server; nothing outside the profile is revived. **Any failure: T-FB, the 5-node run.**
5. The cluster is reachable from the current network (`gke-preflight` `cluster-reachable`). The tool
   connects through the DNS endpoint.
6. Real Cloud KMS auto-unseal (`seal_type` `gcpckms`, `KMS_GKE`) at init, at the restarts, after the
   rebind and after the wake. KMS API traffic comes from the Vault GSA.
7. The Persistent Disks detach and reattach across the plain sleep (T3 checks both disks `present`), and
   cleanup's HTTP 404 for each disk.
8. The WorkerPool pods' behaviour while their controller is held down (observation only).
9. Image pulls (Docker Hub, Quay) within the pull limits.
10. After T6, `verify --expect sleep` reports no failure. The proof that the original platform is back is
    R1's `verify --expect awake --manifest $MANIFEST`. R2 then records a fresh manifest of it.

## Files

| File | What it holds |
|---|---|
| `inventory/` | `MANIFEST.json`, `workloads.json`, `workload-requests.json` of `vault-rehearsal-20261124T173156Z` (names, replicas, images, requests; no env, no Secret) |
| `capacity-cost.json` | capacity per node count, the cost per step and per sequence, and what it excludes |
| `SCREENSHOT_CHECKLIST.md` | the 15 owner screenshots |
| `kind-v2-run.md`, `kind/` | the four kind runs: run 4's records, the failure records of runs 1–3, ESO's state at run 3's failure |
| `test-results.txt` | the final offline test run |
| `SHA256SUMS` | checksums |
