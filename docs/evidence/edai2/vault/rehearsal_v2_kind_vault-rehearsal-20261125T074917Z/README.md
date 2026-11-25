# Vault V2 full campaign on kind: `vault-rehearsal-20261125T074917Z`

`ENVIRONMENT=kind` · `AUTO_UNSEAL=SIMULATED_AUTO_UNSEAL` · **KIND PASS; not GKE evidence; Google Cloud KMS was not used.**

This is the complete V2 sequence of [VAULT_GKE_REHEARSAL.md §10](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md#10-vault-v2-on-kind),
run with the committed driver. Every value is synthetic.

| | |
|---|---|
| Date | 2026-11-25, 07:49–08:42 UTC (53 min, of which the `agents` phase with four real vLLM starts took 40 min) |
| Cluster | kind `kind-llm-agent-local` (1 node, capped at 4 CPUs and 9 GiB). cert-manager v1.16.2 was installed for the run with `scripts/vault/rehearsal/kind_cert_manager.sh` and removed afterwards |
| Software | Vault 2.0.4 (chart 0.34.1); ESO v2.14.0 (chart 2.14.0); the serving chart as `rehearsal-serving` with the Feature API image `localhost:5001/ewallet-inference@sha256:8a6106c3…`; the llm-agent-platform chart as `rehearsal-agents` with agentgateway v1.7.0 and vLLM CPU v0.32.0 (Qwen3-0.6B, staged); all digests pinned |
| Script | `scripts/vault/gke_rehearsal.sh` sha256 `15cf46275dd943f1…` (the version committed with this evidence) |
| Result | **129 records, 0 FAIL**. Gates `retain`, `after-wake` and `clean` opened. Afterwards there was no Vault, ESO or rehearsal object, no PV, no port-forward and no private material, and the production snapshot was unchanged |

## Results by test (audit §14)

| Test | Result | Records (`results.jsonl` step) |
|---|---|---|
| K1 Vault, TLS, storage | PASS: cert-manager chain verified; ClusterIP only; no PDB; 2 PVCs Bound; HTTPS verified with `vault-ca`; an untrusted CA was refused | `certificates`, `vault-runtime`, `vault-tls`, `tls-wrong-ca-rejected` |
| K2 simulated auto-unseal | PASS: the unsealer's token may only encrypt and decrypt with key `autounseal`, and its root was revoked; `seal_type` `transit`; `init` produced **3 recovery keys, threshold 2**; Vault **unsealed itself**; Ready = unsealed; manual `unseal` was refused (`vault_auto_unseal`, 0 keys submitted) | `unsealer`, `vault-init`, `auto-unsealed`, `manual-unseal-refused` |
| K3 ClusterSecretStore | PASS: the committed `ewallet-vault` reached `True Valid` (ESO validates a store by logging in) with conditions `[ml-platform, ewallet-agents]`; the rehearsal store was also `True Valid`; controller flags were correct; the CA was published once (`CA_PUBLISHED`, then `CA_UNCHANGED`) | `official-store-ready`, `rehearsal-store-ready`, `eso-runtime`, `vault-ca-published` |
| K4 multi-namespace sync | PASS: one store and one reader across two namespaces; three ExternalSecrets synced; each target owned by its ExternalSecret, with exactly one key, its hash equal to Vault | `secrets-synced` |
| K5 real Feature API | PASS: **9 lookups, all `BASELINE_OK`** (HTTP 200, 31 features): baseline, switch, rollback, re-upgrade, after the agents phase, after the credential rotation, while Vault was down, after the Released-PV recovery, after the wake | `lookup-*` |
| K6 real Agent Gateway | PASS: through the gateway to the real vLLM, the valid client key got 200, served by `primary`; a wrong key, no key and the backend key used as a client key each got 401 | `agents-baseline`, `agents-switched`, `agents-rolled-back`, `agents-reupgraded` |
| K7 vLLM backend key | PASS, real vLLM, not mocked: after the backend key was rotated in Vault and **only the gateway** restarted, the gateway returned 401. Called directly, vLLM accepted its own key (200) and refused the new key and no key (401). After the re-upgrade both sides used the new key (200; old key 401) | `vllm-enforces-its-own-key`, `agents-reupgraded` |
| K8 negative | PASS, each with its expected cause and no target Secret: out-of-policy path → `permission denied`; missing key; wrong ServiceAccount → `service account name not authorized`; same-named reader in a consumer namespace → `namespace not authorized`; wrong audience and untrusted CA → store NotReady with `invalid audience` / `x509`; unapproved namespace and `ewallet-sandbox` → `… is not allowed from namespace …: denied by spec.condition`. The reader's own token got 200 on a read inside its prefix and 403 on: an out-of-prefix read, metadata, list, write, `sys/policies/acl` read and write, auth roles, root generation. NetworkPolicy blocked a non-ESO pod (control: it reached Redis) | `negative` phase |
| K9 shadow sync | PASS: `SHADOW_MATCH` for the Feast pair and for both agent pairs; the original Secrets kept their resourceVersions | `shadow-compare`, `agents-shadow-compare` |
| K10 consumer switch | PASS: the official overlays, unchanged. The Feature API moved to `feast-online-redis-vault`; the gateway validator, the vLLM backend and the ModelConfig moved together to the `*-vault` Secrets; no key appeared in the rendered manifest | `switched`, `agents-switched` |
| K11 rotation | PASS: KV v2 and v3 propagated. **Real Redis rotation**: the server password changed with `CONFIG SET` and the data was kept, then Vault v4, a new ESO reconciliation, the Feature API restarted and served 200, and the original credential got `AUTH_FAILED`. Gateway client key: the old key worked until the restart (env read at start-up), then got 401 | `kv-v2-propagated`, `kv-v3-restored`, `credential-rotated`, `agents-client-key-rotated` |
| K12 rollback | PASS: Helm rollback of the Feature API (200) and of the agents release after a deliberately half-rotated backend key (200 on the original pair). Vault versions stayed at 2/2, the originals were unchanged, and the re-upgrade worked | `final-state`, `agents-rolled-back` |
| K13 persistence | PASS: after a restart Vault unsealed itself, the Raft data and operator token were intact, and ESO resumed. The Released-PV recovery came back initialised **and unsealed** with no manual step; KV version kept | `restart-auto-unsealed`, `restart-auto-unsealed-eso-resumed`, `released-pv-recovered` |
| K14 seal unreachable | PASS: Vault's API never answered, the pod never became Ready, and it exited on seal setup once (`error parsing Seal configuration`). ESO reported `vault-unreachable`; the synced Secret was unchanged; the consumer kept working; the Feature API **started** on the retained Secret and served 200. When the seal returned, Vault unsealed by itself (no pod delete, no unseal) and ESO re-synced | `seal-unreachable-never-unsealed`, `seal-back-auto-unsealed`, `seal-unreachable-consumers-keep-working`, `seal-back-eso-recovered` |
| K15 recovery-key ceremony | PASS: admin login; 1 recovery key could not finish (attempt cancelled); a forged share plus a genuine one was rejected (HTTP 400); 2 keys produced a root that read the configuration and was then retired (403); temporary files were removed | `ceremony` phase |
| K16 cleanup | PASS: identity and ownership checks; PVs returned to `Delete`; only the stores labelled with this ID were removed; namespaces, releases and the model PV were removed by recorded name. Fault-injection runs: [`rehearsal_v2_faults_kind_20261125`](../rehearsal_v2_faults_kind_20261125/README.md) | `cleanup`, `verify-clean` |

The simulated wake deleted `vault-0` and the synthetic Redis pod; kind cannot scale a node pool to zero.
`after-wake` then found Vault unsealed with `seal_type` `transit` and no manual step, KV version and
PVs `Bound/Retain`, a new ESO reconciliation, Redis re-seeded, and a lookup returning 200.

## Files

| File | What it holds |
|---|---|
| `results.jsonl` | every step: statuses, key names, 12-character hash prefixes, HTTP codes; never a value |
| `vault-describe.json` | the live engines, auth methods, audit device, roles and policy text (no secret data) |
| `eso-cluster-objects.json` | ESO's cluster-scoped objects and CRDs as installed |
| `volumes.json` | the two Vault PVs (names, reclaim policy) |
| `baseline.json` | the production snapshot compared after every mutating phase (names and resourceVersions only) |
| `lookup-request.json`, `lookup-baseline.json`, `lookup-while-vault-down.json`, `lookup-after-wake.json` | the synthetic lookup fixture and three response bodies (synthetic feature values) |
| `SHA256SUMS` | checksums of the files above |

Not published: the synthetic private material (removed by cleanup), the Feast registry, and the per-phase logs.

## Limits

- **Simulated auto-unseal.** The transit "unsealer" stands in for Cloud KMS. No Google Cloud KMS, IAM,
  Workload Identity or GKE metadata server was involved. Their behaviour is NOT_VERIFIED_ON_GKE, as is
  Vault's exact behaviour when KMS is unreachable.
- kind's local-path volumes stand in for Persistent Disks. The "wake" was pod deletions, not a
  node-pool sleep.
- The Feature API read a synthetic registry and a synthetic in-namespace Redis. The Agent Platform ran in
  a rehearsal namespace without Istio ambient (ztunnel is not working on this kind cluster); its ModelConfig
  had no kagent controller reading it (BYO agents are covered by the render test).
- The Agent Gateway path is kind-only: GKE Campaign A2 does not include it (audit §15).
