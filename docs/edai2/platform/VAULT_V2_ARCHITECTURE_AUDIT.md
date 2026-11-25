# Vault V2 architecture audit: ClusterSecretStore, Cloud KMS auto-unseal, platform-wide credentials

| | |
|---|---|
| Status | **AUDIT AND PLAN ONLY.** Nothing in this document is implemented, deployed or verified on GKE. |
| Branch | `experiment/edai2-vault-clustersecretstore`, from `feature/edai2-vault-eso` at `f5ebe77b933d28377935705da83cfbdfe17c2ccd` (main `222932c2` + 15 Vault commits) |
| Date | 2026-11-25 |
| Scope | Source, Git history, committed evidence, the RecSys-MLops reference (commit `6bd6e99f`, 2026-10-05), official HashiCorp, External Secrets and Google Cloud documentation |
| Not done | No GKE wake, no live query, no GCP resource, no credential read, no implementation change |

Every statement carries one of these labels:

- **[SRC]** VERIFIED SOURCE FACT: read in this repository at the cited file and line.
- **[TEST]** VERIFIED TEST RESULT: a committed evidence run, or a test run during this audit.
- **[DOC]** stated by official vendor documentation (URL in §21).
- **[PROPOSAL]** DESIGN PROPOSAL: not implemented.
- **[ASSUMPTION]** believed true, not verified.
- **[LIVE]** REQUIRES LIVE VERIFICATION on GKE (`REQUIRES_GKE_INVENTORY` for inventory rows).

---

## 1. Executive summary

**Verdict: `VAULT_V2_ARCHITECTURE_RECOMMENDED_WITH_CHANGES`.**

The proposed V2 is sound and simpler to run than the current design, if four changes are made to the proposal as stated:

1. **One ClusterSecretStore, with an exact namespace list.** Use `ewallet-vault` with
   `spec.conditions[].namespaces: [ml-platform, ewallet-agents, ewallet-mcp]`.
   - Exclude `ewallet-sandbox`. Do not use a label selector, because anyone allowed to label a namespace could join it.
   - The Vault reader policy is limited to explicit path prefixes, not `ewallet/data/*`.
   - Per-namespace Vault-side separation is given up on purpose. It is recorded as a retired invariant (§7.4), not removed silently.
2. **Cloud KMS auto-unseal only on a fresh Vault (Scenario A).** GKE has no Vault today [SRC: `VAULT.md:14`; disks proven deleted in `gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md`, R9]. So no seal migration is needed or planned.
   - The KMS key is created once and never touched by rehearsal cleanup.
   - kind cannot reach KMS. There, auto-unseal is *simulated* with a Vault transit seal, labelled `SIMULATED_AUTO_UNSEAL`, never a KMS PASS.
3. **The first release migrates only three credentials:**
   - the Feast online Redis connection string, used by the Feature API and Fraud API;
   - `vllm-api-key`;
   - `agentgateway-client-key`.

   Everything else is preserved or deferred with a reason (§4).
4. **The MCP credential lifecycle stays as it is in V2.0 (MCP-A).** A Vault-backed immutable-release design (MCP-B) is specified for a later release, using explicit release paths, not KV version numbers (§11).

**Complexity verdict: `FEASIBLE_WITH_LIMITATIONS`.**

- What gets simpler:
  - Vault roles drop from 5 to 2: `eso-ewallet-reader` and `vault-admin`.
  - Per-namespace SecretStores, reader ServiceAccounts and CA copies go away.
  - Manual unseal after every wake goes away.
- What gets added: one KMS KeyRing and CryptoKey, one Google service account, three IAM bindings, a change to the Vault CLI's init and recovery path, and a transit-seal kind simulation.
- Limitations:
  - real KMS behaviour is GKE-only;
  - Agent Gateway rotation needs coordinated restarts;
  - MCP stays outside Vault in V2.0.

Approximate added recurring cost: **≈ US$0.06 per month per active key version**. It continues while GKE sleeps. KMS operations cost a fraction of a cent (§19).

---

## 2. Current architecture (baseline `f5ebe77b`)

| Component | Implementation | Source |
|---|---|---|
| Vault | Official chart `vault-0.34.1`, Vault 2.0.4 pinned by digest. 1 replica in Raft "HA" mode, Raft on a 5 GiB PVC, audit log on a 1 GiB PVC (`standard-rwo`). TLS listener, TLS 1.2+. UI, injector and CSI off. No PDB. | [SRC] `infra/kubernetes/vault/values-gke.yaml:1-74` |
| Seal | **Manual Shamir**, 3 shares, threshold 2. Every restart leaves Vault sealed until `ops/vault/cli.py unseal`. | [SRC] `values-gke.yaml:4-6`, `ops/vault/cli.py:145-155`, `:253-257` |
| TLS | cert-manager self-signed CA `vault-ca` (1 y), then server cert `vault-server-tls` (90 d, renewed 15 d before expiry). The SANs cover the Service and pod DNS names. | [SRC] `infra/kubernetes/vault/certificate.yaml:1-52` |
| Network | Ingress to `:8200` only from the ESO controller and the two direct-loader serving pods. No egress restriction. | [SRC] `infra/kubernetes/vault/network-policy.yaml:10-36` |
| Namespace | `ewallet-vault`, restricted Pod Security | [SRC] `infra/kubernetes/vault/namespace.yaml` |
| Bootstrap | KV v2 mount `ewallet`, file audit device, Kubernetes auth `kubernetes`, audience `vault`. 6 policies, 5 roles, all with TTL 5 m / max 15 m and `token_no_default_policy`. | [SRC] `infra/kubernetes/vault/bootstrap.json:1-61`, `ops/vault/admin.py:70-111` |
| Token lifecycle | The initial root token is retired after bootstrap. An orphan `secret-operator` token (720 h) writes values. `vault-admin` (Kubernetes login) may only run root generation, which needs 2 shares. | [SRC] `ops/vault/cli.py:1-16`, `:195-243`; `policies/root-generation.hcl`, `policies/secret-operator.hcl` |
| ESO | Chart `external-secrets 2.14.0`, ESO v2.14.0 pinned by digest. **`processClusterStore: false`**; ClusterExternalSecret and PushSecret also off. | [SRC] `infra/kubernetes/external-secrets/values.yaml:1-30` |
| Stores | Namespaced `SecretStore vault` in `ml-platform` and in `ewallet-agents` (staged). Each has its own `eso-vault-reader` SA (no automount, no RBAC), its own role and its own policy, plus a CA ConfigMap `vault-ca` per namespace. | [SRC] `external-secrets/ml-platform.yaml:11-37`, `ewallet-agents.yaml:10-36` |
| ExternalSecrets | `ml-platform/feature-api-redis` creates the new Secret `feature-api-redis-vault` (`connection_string`). `ewallet-agents/agentgateway-client-key-vault` is a shadow with no consumer. Both use `creationPolicy: Owner`, `deletionPolicy: Retain` and a 1 h refresh. | [SRC] `ml-platform.yaml:39-56`, `ewallet-agents.yaml:38-52` |
| Consumer switch | Helm overlay that sets `featureApi.redisSecret.name=feature-api-redis-vault`; the direct loader stays off | [SRC] `infra/helm/ewallet-serving/values-vault-eso.yaml:11-14` |
| Direct loader | `ai_platform/inference/vault.py`, roles `feature-api` and `fraud-api`. Superseded; nothing deployed uses it. | [SRC] `VAULT.md:169-182` |
| Evidence tools | `shadow_compare.sh`, `gke_preflight.py`, `gke_rehearsal.sh` (fail-fast phases, gates, fault injection, PV `Retain` drill, disk identity), `eso_local_validate.sh` | [SRC] `scripts/vault/` |
| Cloud lab | `ops/gcp/cloud_lab.py` has no Vault-specific logic. Guarded sleep checks PDBs (`evaluate_pdbs`, line 109). | [SRC] |

The Vault data path in Vault V1:

```text
operator --stdin--> Vault KV v2 (ewallet/) <--TLS, k8s auth (per-namespace role)-- ESO controller
                                                                                   |
                       ml-platform/SecretStore vault --> ExternalSecret --> Secret feature-api-redis-vault --> Feature API
                   ewallet-agents/SecretStore vault --> ExternalSecret --> Secret agentgateway-client-key-vault (no consumer)
```

---

## 3. Verified current-state evidence

| Claim | Status | Evidence |
|---|---|---|
| 852 tests passed, 1 skipped | **[TEST] reproduced in this audit**: `pytest tests/ops -q` at `f5ebe77b`, 852 passed, 1 skipped in 157 s (Python 3.10.20) | this audit |
| Local ESO chain, 65/65 steps | [TEST] | `docs/evidence/edai2/vault/eso_local_20261124T055021Z/results.jsonl`; summary `VAULT.md:317-341` |
| Campaign A on GKE: real PDs, GKE TokenReview, Calico NetworkPolicy, sealed restart and manual unseal, cleanup with disks proven absent, guarded sleep | [TEST] **GKE**, verdict `VAULT_GKE_REHEARSAL_COMPLETE`. 49 steps; 1 FAIL, a preflight run while cert-manager was not ready, passed on re-run | `gke_rehearsal_vault-rehearsal-20261124T173156Z/README.md` |
| Campaign A+ with the real Feature API (Helm switch, rollback, Retain drill, simulated wake), 67 steps | [TEST] **kind only** | `rehearsal_aplus_kind_20261124T191350Z/README.md` |
| Fail-fast and interrupted-recovery cleanup: 1 full A+ run (97 records) plus 5 fault points | [TEST] **kind only** | `rehearsal_aplus_faults_kind_20261125/README.md` |
| GKE A+ (real PD detach and re-attach across a sleep) | **Not executed** | `VAULT_GKE_REHEARSAL.md` §9, "GKE run (pending owner approval)" |
| Campaign B (official, real credential, owner screenshots E1–E9) | **Not executed** | `VAULT_GKE_ACCEPTANCE_CAMPAIGN.md` title "prepared, not executed" |
| Vault, ESO and stores on GKE now | **None** [SRC], as recorded after Campaign A cleanup. Current live state is [LIVE] | `VAULT.md:14`, Campaign A R9 |

kind results are never GKE results. In particular, the A+ Released-PV recovery used kind host-path volumes (`rehearsal_aplus_kind…/README.md`, Limits).

### Current-state assessment

| | |
|---|---|
| **Strengths** | No root token in use. Values only on stdin; key files mode 0600 and refused inside the repo. Fail-closed init. Exact read paths with no list or metadata. ESO tokens last 5 minutes. TLS with a pinned CA. Audit with 0 plaintext matches. Fail-fast rehearsal driver with production drift snapshots, ownership-proven cleanup and disk absence proven by HTTP 404. |
| **Operational limitations** | Manual unseal after every wake, drain or node upgrade. Every new namespace needs a SA, a role, a policy, a CA ConfigMap and a SecretStore. Recovery material is one file held by one owner. Fraud API and Feature API have two Vault paths for one credential (`policies/feature-api.hcl:2`, `policies/fraud-api.hcl:2`). Pod `Ready` does not mean unsealed (`VAULT.md:290-292`). |
| **Preserve** | The Vault chart values (except the seal), the TLS certificate chain, the KV mount `ewallet`, the audit device, the token lifecycle and `vault-admin`, `ops/vault/cli.py` safety rules, `shadow_compare.sh`, `gke_preflight.py`, `gke_rehearsal.sh` phases and gates, the fault injection, volume-state handling, `serving_post_render.py`, the Helm consumer-switch pattern, `creationPolicy: Owner` on **new** target names, and `deletionPolicy: Retain`. |
| **Must change** | The seal stanza and init (recovery keys). The ESO install (`processClusterStore: true`). The stores (one ClusterSecretStore). The reader roles and policies (one). CA distribution (one ConfigMap). The secret path layout. The negative tests that assert reader-to-reader separation. The runbooks' unseal steps. |
| **May be removed** | The direct-loader roles `feature-api` and `fraud-api`, their policies and their NetworkPolicy ingress entries (owner decision O4). The per-namespace `eso-vault-reader` SAs. `policies/eso-ml-platform.hcl` and `policies/eso-ewallet-agents.hcl`. The `unseal` step in wake procedures. |

---

## 4. Secret inventory (source-derived)

Scope and limits:

- Built from source and committed deployment records only.
- **It is not the live inventory.** Rows marked `REQUIRES_GKE_INVENTORY` need a metadata-only listing after an approved wake. Use `kubectl get secret -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,TYPE:.type,IMMUTABLE:.immutable`, which never touches data.
- No values were read. The two inline Secrets in mock manifests were classified by length and pattern only.
- Column abbreviations:
  - **Cand.**: Vault candidate.
  - **Cx/Rk**: complexity / risk.
  - **Conf.**: VERIFIED (V), INFERRED (I) or UNKNOWN (U).

### 4.1 Underlying credentials vs. Secret copies

Several Secrets are copies of one underlying value, so migrating "a Secret" is not the same as migrating "a credential":

| Underlying credential | Copies in Kubernetes (all derived from it) |
|---|---|
| **U1 Feast online Redis password** (`feature-store/redis`, `requirepass`, no ACLs) [SRC `agi/mcp.yaml:15-16`] | `ml-platform/feast-online-redis` (db 0). `ml-platform/feast-online-redis-{verify,stream,rehearsal}` (other DBs, `FEATURE_STORE_RUNBOOK.md:206-215`). `ewallet-mcp/feast-online-redis` (copied by `ops/llm_agent/g3_platform_inputs.py:13-16`). `ewallet-mcp/feast-online-redis-agi-{fraud,rag}` (`,db=N`, `ops/llm_agent/agi_inputs.py:21,153`). |
| **U2 vLLM backend key** | `ewallet-agents/vllm-api-key` |
| **U3 Gateway client key** | `ewallet-agents/agentgateway-client-key` |
| **U4 Coordinator idempotency Redis password** | `ewallet-agents/coordinator-idempotency-redis-auth` (3 keys, all derived from one password) |
| **U5 MLflow Postgres password** | `ml-platform/mlflow-postgresql`, `ml-platform/mlflow-backend-store` (URI contains it) |

The Feature API and Fraud API read the **same** Secret `ml-platform/feast-online-redis`:

- Feature API: [SRC] `infra/helm/ewallet-serving/values.yaml:31` and `templates/feature-api.yaml:101-102`.
- Fraud API: `infra/kubernetes/fraud-serving/fraud-api.yaml.tmpl:70-71`.

V2 therefore needs **one** Vault record for both, not the two paths V1 has.

### 4.2 Inventory matrix

**ML Platform**

| # | NS / Secret | Keys | Producer | Consumers | Lifecycle | Source of truth | Shared | Cand. | Decision | Cx/Rk | Migration approach | Rollback | Source | Conf. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 | `ml-platform/feast-online-redis` | `connection_string` | out of band (owner); copied from U1 | Feature API, Fraud API, `feast_online_job.sh` canonical-online | static | K8s Secret | **yes** (2 APIs + job) | YES | **MIGRATE (V2.0)** | LOW/MED | Seed `ewallet/ml-platform/feast-online-redis`; ES → new `feast-online-redis-vault`; `shadow_compare` MATCH; switch Feature API (Helm overlay) then Fraud API (kustomize patch); job keeps old name | Helm rollback / remove patch; old Secret untouched | `ewallet-serving/values.yaml:31`, `fraud-api.yaml.tmpl:70`, `scripts/mlops/feast_online_job.sh:39-46` | V |
| M2 | `feature-store/redis` | `password` | out of band | Redis StatefulSet (`REDIS_PASSWORD`, `REDISCLI_AUTH`) | static | K8s Secret | server side of U1 | CONDITIONAL | **DEFER** | MED/HIGH | Later: the same Vault record templated into the server Secret, so server and clients come from one source; rotating it restarts the Feast online store | n/a | `infra/kubernetes/feast-online/redis.yaml:56-66` | V (producer U) |
| M3 | `ml-platform/feast-online-redis-{verify,stream,rehearsal}` | `connection_string` | `FEATURE_STORE_RUNBOOK.md` §2 (copied from M1 with another DB) | Feast verification jobs | temporary | K8s Secret | U1 copy | NO | **PRESERVE** | — | Campaign-scoped; not platform credentials | — | `FEATURE_STORE_RUNBOOK.md:206-215`, `feast_online_job.sh:45-63` | V; existence `REQUIRES_GKE_INVENTORY` |
| M4 | `ml-platform/mlflow-postgresql`, `mlflow-backend-store` | `username,password` / `uri` | `scripts/mlops/deploy_mlflow.sh:47,52` | MLflow Postgres, MLflow server | static, stateful DB | K8s Secret | U5 (2 Secrets) | CONDITIONAL | **DEFER** | MED/MED | A rotation needs `ALTER USER`; not required for row 54 | — | `infra/kubernetes/mlflow/postgresql.yaml:44-50`, `deployment.yaml:36` | V |
| M5 | Vault paths `feature-api/feast-redis`, `fraud-api/feast-redis` | `connection_string` | V1 bootstrap | direct loader (unused), V1 ES | V1 | Vault (rehearsal only) | duplicate of U1 | — | **REMOVE in V2** | LOW | Replaced by one record | — | `policies/feature-api.hcl:2`, `fraud-api.hcl:2` | V |

**Agent Platform**

| # | NS / Secret | Keys | Producer | Consumers | Lifecycle | Source of truth | Shared | Cand. | Decision | Cx/Rk | Migration approach | Rollback | Source | Conf. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | `ewallet-agents/vllm-api-key` | `api-key` | `scripts/llm_agent/g1_operator.sh:176-190` (64 hex, never overwritten) | vLLM primary/fallback, vLLM candidate, agentgateway (backend credential) | static | K8s Secret | **yes**: server (vLLM) **and** client (gateway) | YES | **MIGRATE (V2.0)** | MED/MED | ES → `vllm-api-key-vault`; MATCH; one Helm upgrade sets `vllm.apiKeySecret.name` (both consumers move together) | `helm rollback llm-agent` | `templates/vllm.yaml:169-171`, `vllm-candidate.yaml:102-104`, `agentgateway.yaml:221-223`, `values.yaml:250-252` | V |
| A2 | `ewallet-agents/agentgateway-client-key` | `api-key` | `g1_operator.sh:176-190` | agentgateway (validator, `key: "$GATEWAY_CLIENT_KEY"`), ModelConfig `ewallet-global-llm` (`apiKeySecret`), from there every kagent agent and the BYO resolver | static | K8s Secret | **yes**: gateway + all agents | YES | **MIGRATE (V2.0)** | MED/HIGH | ES → `agentgateway-client-key-vault`; MATCH; **the same** Helm upgrade sets `agentgateway.clientKeySecret.name` and `modelConfig.apiKeySecret.name` | `helm rollback`; old Secret untouched | `agentgateway.yaml:46,226-228`, `templates/modelconfig.yaml:10-11`, `values.yaml:283-285,901-903` | V |
| A3 | `ewallet-agents/coordinator-idempotency-redis-auth` | `connection_url,password,users.acl` | out of band, immutable (`coordinator_idempotency_runbook.md:24`) | idempotency Redis (env + ACL file), BYO Coordinator | immutable | K8s Secret | yes (server + client, 3 keys from U4) | CONDITIONAL | **DEFER (V2.1, optional)** | MED/MED | ES with `target.template` that derives `connection_url` and `users.acl` from one `password`; new immutable name; chart values switch | Helm rollback | `templates/idempotency-redis.yaml:20-24,118-153`, `values.yaml:580-591` | V; enabled state `REQUIRES_GKE_INVENTORY` |
| A4 | `ewallet-agents/ewallet-grafana-admin` | `username,password` | out of band | Grafana (`GF_SECURITY_ADMIN_*`) | static | K8s Secret | no | CONDITIONAL | **DEFER** | LOW/LOW | Grafana keeps the admin password in its own DB after first start [ASSUMPTION], so rotating the Secret alone does not rotate the login | — | `templates/observability-stack.yaml:247-249`, `values.yaml:756` | V (producer U) |
| A5 | `ewallet-agents/ewallet-agent-tls` | TLS | cert issuance | Istio agent Gateway (`gateway.enabled: false` by default) | certificate | issuer | — | NO | **PRESERVE** | — | Certificates stay with cert-manager or the issuer | — | `templates/gateway.yaml:19-20`, `values.yaml:20` | V |
| A6 | `ewallet-agents-staging/*` (`*-staging`) | various | `ops/llm_agent/staging_credentials.py:75-135` | staging release | synthetic | K8s Secret | — | NO | **PRESERVE** | — | Synthetic staging fixtures | — | as cited | V |
| A7 | `kagent-pilot/pilot-mock-openai`, `prompt-canary/prompt-echo-key` | `OPENAI_API_KEY` | inline in manifest | mock endpoints | placeholder | Git | — | NO | **PRESERVE** | — | Placeholder strings for mock servers (classified by length and pattern only, not real keys) | — | `kagent-pilot/manifests/model-config.yaml:2-19`, `prompt-canary/canary.yaml:92-104` | V |
| A8 | `prompt-registry-db` | `password`, `AGENT_REGISTRY_DATABASE_URL` | out of band | prompt-registry Postgres, agentregistry | static | K8s Secret | yes | CONDITIONAL | **DEFER** | MED/LOW | Deployment state unknown | — | `infra/kubernetes/prompt-registry/postgres.yaml:45`, `infra/helm/agentregistry/values-ewallet-prompts.yaml:11` | V; `REQUIRES_GKE_INVENTORY` |

**MCP Platform** (details in §11)

| # | NS / Secret | Keys | Producer | Consumers | Lifecycle | Source of truth | Shared | Cand. | Decision | Cx/Rk | Migration approach | Rollback | Source | Conf. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 | `ewallet-mcp/mcp-auth-<server>-<set>` (last recorded set: `fin-20261117`) | `auth-config.json` (credential table) | `ops/llm_agent/g3_mcp_credentials.py` (`kubectl create`, immutable, plan fingerprint) | feature-rag-mcp, drift-mcp | **immutable, release-scoped** | K8s Secret (generator) | server side of every client token | CONDITIONAL | **PRESERVE (MCP-A)** | HIGH/HIGH | §11 MCP-B (V2.1) | serverSet switch | `g3_mcp_credentials.py:363-411`, `templates/mcp-services.yaml:13-15,147` | V; live set `REQUIRES_GKE_INVENTORY` (last record `lf-offline-20261117/workstream-c/projected-inventory.json`) |
| P2 | `ewallet-agents/mcp-token-kagent-controller-<server>-<release>` | `token` | same | kagent controller `RemoteMCPServer` discovery (list-only) | immutable | K8s Secret | no | CONDITIONAL | **PRESERVE** | HIGH/HIGH | MCP-B | clientRelease switch | `templates/agents.yaml:553-560` | V |
| P3 | `ewallet-agents/mcp-token-<subject>-<server>-<release>` | `token` | same | BYO agents (`authHeaderSecrets`) | immutable | K8s Secret | no | CONDITIONAL | **PRESERVE** | HIGH/HIGH | MCP-B | clientRelease switch | `templates/agents.yaml:336-337,458-459` | V; several retired (`g3/g3.yaml:1-7`) |
| P4 | `ewallet-sandbox/mcp-token-sandbox-{feature-rag-feature-rag-mcp,drift-drift-mcp}-sbx-20261115` | `token` | same | SandboxAgents | immutable | K8s Secret | no | NO (sandbox outside the store) | **PRESERVE** | — | Stays Kubernetes-only unless the owner admits the sandbox (O2) | — | `infra/kubernetes/agent-sandbox/fleet.yaml.tmpl:62,116`, `scripts/llm_agent/mcp_credential_check.sh:39-40` | V |
| P5 | `ewallet-mcp/feast-online-redis`, `feast-online-redis-agi-{fraud,rag}` | `connection_string` | `g3_platform_inputs.py`, `agi_inputs.py` (copies of U1) | feature-rag-mcp | static | K8s Secret | U1 copy | YES | **DEFER (V2.1)** | MED/MED | The same Vault record as M1, templated with `,db=N`; parity check against each existing copy | chart values switch | `agi/mcp.yaml:6-25`, `g3/g3.yaml:94` | V; live names `REQUIRES_GKE_INVENTORY` |
| P6 | `ewallet-sandbox/sandbox-llm-key`, `sandbox-mcp-token` | `value` | README procedure | experiment ARM templates | experiment | K8s Secret | — | NO | **PRESERVE** | — | Experiments only | — | `agent-sandbox/arm-baseline.yaml.tmpl:41-47`, `README.md:42-55` | V; `REQUIRES_GKE_INVENTORY` |

**Supporting services**

| # | NS / Secret | Keys | Producer | Consumers | Lifecycle | Source of truth | Shared | Cand. | Decision | Cx/Rk | Migration approach | Rollback | Source | Conf. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | `ewallet-ci/jenkins-admin` | `username,password` | out of band | Jenkins controller | static | K8s Secret | no | CONDITIONAL | **DEFER** | LOW/MED | CI is a separate trust zone; ESO into `ewallet-ci` would widen the store | — | `infra/kubernetes/jenkins/jenkins.yaml:210-213` | V |
| S2 | `ewallet-ingress/edge-basic-auth` | `htpasswd` | `scripts/edge/create_basic_auth.sh:62` | edge gateway | static | K8s Secret | no | CONDITIONAL | **DEFER** | LOW/LOW | Possible later; the hash is not a reusable secret | — | `infra/helm/ewallet-edge/values-gateway.yaml:18` | V |
| S3 | `ewallet-ingress/edge-tls` | TLS | `scripts/edge/issue_certificate.sh:63` | edge gateway | certificate | issuer | — | NO | **PRESERVE** | — | — | — | `infra/helm/ewallet-edge/values.yaml:18` | V |

**Identity and platform-managed credentials (not Vault candidates)**

| Item | Why it stays out of Vault | Source |
|---|---|---|
| GKE Workload Identity: Feature API, Fraud API, training, generator, Jenkins | short-lived Google tokens from the metadata server; no stored secret | [SRC] `infra/terraform/gcp/modules/workload_identity/main.tf:1-18`, `jenkins.yaml:35` |
| Projected SA tokens: ESO reader, `vault-admin`, kagent ↔ ate-api (`ateApiTokenAudience`) | issued per request by the API server | [SRC] `kagent-platform/kagent-values.yaml:34` |
| `vault-ca`, `vault-server-tls`, other cert-manager certificates | renewed by cert-manager | [SRC] `vault/certificate.yaml` |
| Vault recovery keys, operator token, temporary root | Vault's own administrative material, kept outside the cluster | [SRC] `ops/vault/cli.py:1-16` |
| `ateapi-stub-tls`, `mcp-exp-auth`, bench keys | test and experiment scripts | [SRC] `scripts/experiments/*.sh` |
| `bounded_cd.py` tokens | generated per CI run | [SRC] `scripts/ci/bounded_cd.py:401` |

### 4.3 Counts

| | Count |
|---|---|
| Distinct application credential groups (rows M1–M4, A1–A8, P1–P6, S1–S3; M5 excluded because it holds no credential of its own) | **21** |
| MIGRATE in V2.0 | **3**: M1, A1, A2 |
| PRESERVE | **10**: M3, A5, A6, A7, P1, P2, P3, P4, P6, S3 |
| DEFER | **8**: M2, M4, A3, A4, A8, P5, S1, S2 |
| Rows that need live verification (`REQUIRES_GKE_INVENTORY`) | **7**: M3, A3, A8, P1, P5, P6, plus the producer of M2 |

These counts cover the source and committed records only. Out-of-band Secrets created by hand on GKE and recorded nowhere cannot be counted offline.

---

## 5. Reference project: RecSys-MLops

Read at commit `6bd6e99f481f2a8d5886d6f0e8356540dc20e135` (2026-10-05). All paths in the prompt exist.

| # | Question | Finding |
|---|---|---|
| 1 | Vault deployment | Terraform `helm_release` of the official chart. Vault 2.0.3, Raft HA with `vault_replicas`, PDB `maxUnavailable: 1`, UI on, **`tlsDisable: true`, `tls_disable = 1`**, unauthenticated metrics, no audit storage (`configs/vault/values.yaml.tftpl:1-107`, `vault.tf:54-84`) |
| 2 | KMS | `google_kms_key_ring` plus `google_kms_crypto_key "vault-unseal"`, `rotation_period = 7776000s` (90 d), `prevent_destroy = true` (`vault.tf:9-28`); seal `gcpckms {project, region, key_ring, crypto_key}`, no credentials (`values.yaml.tftpl:86-91`) |
| 3 | Workload Identity | A dedicated GSA, `roles/iam.workloadIdentityUser` for `[vault/vault]`, and the KSA annotation `iam.gke.io/gcp-service-account`. Key-level `cryptoKeyEncrypterDecrypter` **plus** `cloudkms.viewer` (`vault.tf:1-52`) |
| 4 | Shared store | One `ClusterSecretStore recsys-vault`, **no `conditions`**, server `http://…:8200`, **no caProvider** (`recsys-security/templates/secretstore.yaml`, `values.yaml:1-21`) |
| 5 | ESO role and policy | Role `recsys-external-secrets` bound to the **ESO controller's own SA** `external-secrets/external-secrets`, TTL 1 h. Policy `read` on `recsys/data/*` **and `read,list` on `recsys/metadata/*`** (`ops/gcp/bootstrap_vault.sh:149-171`) |
| 6 | Fan-out | Several `externalSecrets.*` entries share one `vaultPath` in different namespaces. For example, `gateway` appears in 5 namespaces and `agent-gateway` in 3: kagent client, llm-inference server, observability probe (`recsys-security/values.yaml:25-110`) |
| 7 | Credential creation | Terraform `random_password` → `kubernetes_secret_v1` "legacy source" Secrets in `external-secrets` → copied into Vault by `bootstrap_vault.sh` (`secret_management.tf:1-92`). The passwords therefore also live in Terraform state |
| 8 | Legacy migration | `bootstrap_vault.sh:176-275` reads each legacy source Secret and writes it to KV; it keeps the existing Vault version if the source is absent |
| 9 | Terraform wait | `null_resource` local-exec: `kubectl wait --for=condition=Ready externalsecret/...` for a fixed list (`secret_management.tf:94-173`) |
| 10 | Agent Gateway | One Vault record fanned out to client, server and probe namespaces with the same value. Rotation changes all three together |
| 11 | MCP versioning | `configs/agentic/mcp-auth-versions.yaml`: revision `vN` ↔ KV version `N` ↔ Secret `<base>-vN` ↔ workload `<base>-vN` (`docs/ops/mcp-auth-secret-rotation.md`) |
| 12 | Immutable ES | Non-legacy revisions use `refreshPolicy: CreatedOnce`, `target.immutable: true`, `dataFrom.extract.version: N` (`externalsecrets.yaml:64-100`) |
| 13 | Safe rotation | **Blue/green MCP workloads**: a new token in a new KV version, a new Secret, a new Deployment, then a separate commit for `activeRevision` cutover. The CAS-checked write helper reads the token on stdin and refuses xtrace (`ops/security/rotate_mcp_auth_vault.sh`) |
| 14 | Rollback | A commit that changes only `activeRevision` back. Never roll Vault back or delete the green slot during rollback |
| 15 | Evidence | Screenshots in `docs/evidence/`: `vault_clustersecretstore_ready.png`, `vault_external_secrets_all_services_synced.png`, `vault_agent_gateway_record_metadata.png`, `vault_ha_pods_k9s.png`, and others |
| 16 | Unneeded complexity for E-Wallet | 3-replica HA; Terraform-generated passwords and legacy source Secrets; the Terraform–Jenkins sequencing; blue/green MCP workloads per token; metadata list access |

Security trade-offs observed, which must not be copied:

- plaintext HTTP to Vault;
- metadata listing for the reader, so every path is discoverable;
- the controller SA used directly as the Vault identity;
- no namespace conditions, so any namespace can use the store;
- credentials in Terraform state;
- no audit device.

### Comparison

| Area | RecSys-MLops | Current E-Wallet (V1) | Proposed E-Wallet V2 |
|---|---|---|---|
| Vault runtime | 3-replica Raft HA, UI on | 1 replica, Raft, audit PVC | **Keep V1** |
| TLS | off | cert-manager CA, TLS 1.2+ | **Keep V1**; one CA ConfigMap |
| Seal | GCP KMS | Shamir 3/2 | **GCP KMS**, recovery keys 3/2 |
| KMS identity | GSA + WI, encrypterDecrypter + viewer | — | **Adopt** (same roles, key-scoped) |
| Store | ClusterSecretStore, no conditions | 2 namespaced SecretStores | **ClusterSecretStore with exact `conditions.namespaces`** |
| ESO Vault identity | controller SA | `eso-vault-reader` per namespace | **One dedicated `external-secrets/eso-vault-reader`** (no automount, no RBAC) |
| Reader policy | `data/*` read + `metadata/*` list | exact paths per namespace | **Explicit prefixes, read only, no metadata** |
| Creation of values | Terraform random → state | operator on stdin | **Keep V1** (stdin, never in state) |
| Fan-out | one path → many namespaces | — | **Adopt** for genuinely shared values |
| MCP | KV-version-pinned immutable ES, blue/green workloads | Kubernetes-only immutable releases + transition server sets | **Keep in V2.0**; MCP-B with release paths later |
| Wait/sync gate | Terraform local-exec | `gke_rehearsal.sh` gates | **Keep V1** gates |
| IaC for Vault | Terraform | Helm + scripts | **Keep Helm + scripts**; Terraform only for the KMS key (§9.4) |

- **Adopt:** KMS seal with no JSON key, WI GSA, the key IAM pair, `prevent_destroy`, one store, fan-out, and `CreatedOnce` + `immutable` for release-scoped Secrets.
- **Adapt:** add store conditions, TLS, a dedicated reader SA and explicit prefixes.
- **Do not copy:** HTTP, metadata list, Terraform-generated passwords, HA, the controller-SA identity, and blue/green-per-token MCP.

---

## 6. SecretStore vs. ClusterSecretStore

| Criterion | A: namespaced SecretStores (V1) | B: one ClusterSecretStore | C: hybrid (shared store + sandbox store) |
|---|---|---|---|
| K8s objects per namespace | SA + SecretStore + CA ConfigMap + ESs | ESs only | B, plus a second store for the sandbox |
| Vault roles / policies for 3 namespaces | 3 / 3 | **1 / 1** | 2 / 2 |
| Adding a service | role + policy + SA + store + CA + ES | ES (+ a prefix if it is a new area) | as B |
| Authentication | per namespace SA token, audience `vault` | one SA token (`external-secrets/eso-vault-reader`), audience `vault` | as B |
| Cross-namespace | impossible by construction | an ES in an allowed namespace may request **any path the shared policy allows** | as B within the trusted set |
| RBAC dependency | creating a SecretStore in a namespace grants only that namespace's identity | **whoever can create an ExternalSecret in `ml-platform`, `ewallet-agents` or `ewallet-mcp` can read every V2 prefix** | as B |
| Security | strongest Vault-side separation | separation by Vault prefix and K8s RBAC, not by namespace | as B |
| Tests | 3 identities to test | 1 identity + conditions + prefixes | B + 1 |
| Migration | — | moderate: rewrite manifests, bootstrap, negatives | higher |
| Cleanup | per-namespace objects | one cluster-scoped object to own and delete by name | two |
| Script compatibility | current | `gke_rehearsal.sh` counts ESO cluster objects; it must expect one CSS (§14) | as B |

### Decision: `CLUSTERSECRETSTORE` [PROPOSAL]

- **Why B:** for three trusted platform namespaces run by one owner, per-namespace Vault separation protects against a mistake by an operator who can already read every Kubernetes Secret in those namespaces. That is a small gain for five objects per namespace.
- **Why the sandbox needs no second store (not C):** it receives no Vault-managed credential in V2.0. Option C would add a store with no consumer.
- **What B retires:** one security invariant, "a store in namespace X cannot read namespace Y's paths". It is replaced by the controls in §7.4.

---

## 7. Recommended Vault V2 architecture

### 7.1 Overview [PROPOSAL]

```text
                     Google Cloud KMS: keyRing ewallet-vault / key vault-unseal (asia-southeast1)
                                ^ encrypt/decrypt root key (Workload Identity, GSA ewallet-vault-unseal)
  ewallet-vault ns ┌────────────┴───────────────────────────────────────────┐
                   │ Vault 2.0.4 x1, Raft PVC + audit PVC, TLS (cert-manager)│
                   │ seal "gcpckms", KV v2 "ewallet", k8s auth, audit file   │
                   └────────────▲───────────────────────────────────────────┘
                                │ HTTPS :8200 (NetworkPolicy: ESO controller only)
  external-secrets ns  ESO controller ── login role eso-ewallet-reader as SA eso-vault-reader (aud "vault")
                                │      ClusterSecretStore ewallet-vault
                                │      conditions.namespaces = [ml-platform, ewallet-agents, ewallet-mcp]
          ┌─────────────────────┼──────────────────────────┐
   ml-platform               ewallet-agents             ewallet-mcp (V2.1)
   ES feast-online-redis     ES vllm-api-key            ES feast-online-redis-agi-* (templated)
   → feast-online-redis-vault→ vllm-api-key-vault
   → Feature API, Fraud API  ES agentgateway-client-key
                             → agentgateway-client-key-vault → agentgateway, ModelConfig → agents
   ewallet-sandbox: excluded (MCP tokens stay Kubernetes-only)
```

### 7.2 Manifests (design, not deployed)

```yaml
# infra/kubernetes/external-secrets/cluster-secret-store.yaml  [PROPOSAL]
apiVersion: v1
kind: ServiceAccount
metadata: {name: eso-vault-reader, namespace: external-secrets, labels: {app.kubernetes.io/part-of: ewallet-platform}}
automountServiceAccountToken: false        # identity only: no pod, no RBAC
---
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: {name: ewallet-vault, labels: {app.kubernetes.io/part-of: ewallet-platform}}
spec:
  conditions:
    - namespaces: [ml-platform, ewallet-agents]      # ewallet-mcp added only with P5 (V2.1)
  provider:
    vault:
      server: https://vault.ewallet-vault.svc.cluster.local:8200
      path: ewallet
      version: v2
      caProvider: {type: ConfigMap, name: vault-ca, key: ca.crt, namespace: external-secrets}
      auth:
        kubernetes:
          mountPath: kubernetes
          role: eso-ewallet-reader
          serviceAccountRef: {name: eso-vault-reader, namespace: external-secrets, audiences: [vault]}
---
# ml-platform: one record, one target, two consumers
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: feast-online-redis, namespace: ml-platform}
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: ClusterSecretStore, name: ewallet-vault}
  target: {name: feast-online-redis-vault, creationPolicy: Owner, deletionPolicy: Retain}
  data:
    - {secretKey: connection_string, remoteRef: {key: ml-platform/feast-online-redis, property: connection_string}}
```

Notes on the manifests:

- `ewallet-agents` gets two ExternalSecrets of the same shape: `vllm-api-key` → `vllm-api-key-vault` and `agentgateway-client-key` → `agentgateway-client-key-vault`, each with the key `api-key`.
- The ESO documentation for these fields:
  - `conditions` (`namespaces`, `namespaceSelector`, `namespaceRegexes`);
  - "`namespace` is mandatory for ClusterSecretStore" on `caProvider`;
  - "provide namespace in `serviceAccountRef`" for a ClusterSecretStore.

  The pages read show a "not the latest version" banner [DOC]. Field support in **v2.14.0** must be confirmed by the kind run: admission via the webhook, a denied namespace, `Ready=True` (§14, K3/K4).
- Use the exact `namespaces` list, not `namespaceSelector`: a label selector delegates the trust decision to anyone who can label namespaces.
- ESO install change: `processClusterStore: true`. `processClusterExternalSecret`, `processPushSecret` and `processClusterPushSecret` stay `false`, so ESO still never writes to Vault and nobody can fan out with a ClusterExternalSecret.
- `ClusterSecretStore` keeps `creationPolicy: Owner` on **new** target names only. Never adopt an existing Secret.

### 7.3 Vault authentication and ACL (minimum practical)

```hcl
# infra/kubernetes/vault/policies/eso-ewallet-reader.hcl  [PROPOSAL]
# The only ESO reader. Read only; no list, no metadata, no sys/. Prefixes are explicit:
# a new area (e.g. mcp-platform/, observability/) is a reviewed policy change.
path "ewallet/data/ml-platform/*"    { capabilities = ["read"] }
path "ewallet/data/agent-platform/*" { capabilities = ["read"] }
path "auth/token/lookup-self"        { capabilities = ["read"] }
path "auth/token/revoke-self"        { capabilities = ["update"] }
```

| Role | Bound SA | Policy | TTL | Purpose |
|---|---|---|---|---|
| `eso-ewallet-reader` | `external-secrets/eso-vault-reader`, audience `vault` | `eso-ewallet-reader` | 5 m / 15 m, `token_no_default_policy` | every ESO sync |
| `vault-admin` (unchanged) | `ewallet-vault/vault-admin` | `root-generation` | 15 m / 30 m | root-generation ceremony, now completed with **recovery** keys |

Other policies:

- `secret-operator` (orphan token, unchanged mechanism): paths change to `ewallet/data/{ml-platform,agent-platform}/*` with create and update, plus metadata read and list.
- Removed (owner decision O4): `eso-ml-platform`, `eso-ewallet-agents`, `feature-api`, `fraud-api`.

Result: 2 roles instead of 5, and 3 policies instead of 6 (`eso-ewallet-reader`, `secret-operator`, `root-generation`).

### 7.4 Trust model, stated plainly

- The ClusterSecretStore **condition** controls *which namespaces may use the store*.
- It does **not** control *which Vault paths* an allowed ExternalSecret may request: any ES in `ml-platform` may request `agent-platform/vllm-api-key`, and Vault will return it.

The remaining controls are:

1. Vault prefixes: nothing outside `ml-platform/` and `agent-platform/` is readable. Rehearsal paths, a future `mcp-platform/` and any admin data are not.
2. Kubernetes RBAC on `externalsecrets.external-secrets.io` in the three namespaces.
   - The repository grants no `edit`, `admin` or ESO role binding there [SRC: no `RoleBinding` to `edit`/`admin`/`external-secrets-*` under `infra/`].
   - The live RBAC is [LIVE].
   - Note that the ESO chart aggregates `external-secrets-edit` into the built-in `edit` and `admin` roles [ASSUMPTION, chart default].
3. The sandbox is excluded by the condition.
4. Every V2 negative test asserts the remaining denials (§14).

**Retired invariant:** "the ml-platform reader cannot read agent-platform paths". This was tested in V1 (`tests/ops/test_vault_eso.py::test_namespaces_cannot_read_each_others_paths`, `VAULT.md:335`). The owner accepts its removal in decision O1.

### 7.5 TLS

- Keep the cert-manager chain unchanged (`certificate.yaml`).
- `ops/vault` publishes `vault-ca` **once**, to `external-secrets`, instead of once per consumer namespace.
- The CA lasts 1 year and the server certificate 90 days with auto-renewal.
- Vault re-reads its certificate only on restart or SIGHUP [ASSUMPTION]. The runbook keeps "restart Vault after a server-certificate renewal" as a checked step. With auto-unseal this restart is free.
- After any Vault restart, ESO reconnects on its next reconcile, since the CA is unchanged. This is a test step (K16).
- Never `insecure` or `tls_disable`.

### 7.6 Ownership model

| Object | Owner | Created by | Deleted by |
|---|---|---|---|
| KMS key ring and key | owner (GCP) | one-time approved IaC step | **never by a campaign**; destroy requires O7 |
| Vault data | Vault Raft PVC (`Retain` before init) | init | never by ordinary cleanup |
| ClusterSecretStore `ewallet-vault`, reader SA, `vault-ca` ConfigMap | platform (label `app.kubernetes.io/part-of: ewallet-platform`) | `kubectl create` from the repo | by name only |
| ExternalSecret → `*-vault` target | ESO (`Owner`) | per namespace | ES delete removes the target; switch the consumer back first |
| Original Secrets (`feast-online-redis`, `vllm-api-key`, `agentgateway-client-key`) | unchanged | unchanged | **retained** until the owner closes the rollback window |

---

## 8. Google Cloud KMS auto-unseal

### 8.1 Compatibility

| Question | Answer |
|---|---|
| Does Vault 2.0.4 support `gcpckms`? | The seal is part of open-source Vault [DOC]. The reference runs it on Vault 2.0.3 [SRC ref]. For 2.0.4: [ASSUMPTION], verified at rehearsal step G-4 (Vault starts, `seal-status.type == "gcpckms"`). |
| Parameters | `project`, `region` (location of the key ring), `key_ring`, `crypto_key`; optional `credentials`. Env equivalents `GOOGLE_PROJECT`, `GOOGLE_REGION`, `VAULT_GCPCKMS_SEAL_KEY_RING`, `VAULT_GCPCKMS_SEAL_CRYPTO_KEY` [DOC] |
| Credentials without JSON keys | With `credentials` omitted, the Google SDK uses "specified credentials, environment credentials, or application default credentials in that order" [DOC]. On GKE, ADC comes from the GKE metadata server through Workload Identity. No key file. |
| Metadata server | Node pool `workload_metadata_config { mode = "GKE_METADATA" }` [SRC `infra/terraform/gcp/modules/gke/main.tf:91-93`]; cluster `workload_pool` set (`:18-20`). Live node-pool mode [LIVE]. |
| NetworkPolicy | `vault-clients` has `policyTypes: ["Ingress"]` only [SRC `network-policy.yaml:13`], so Vault egress to the metadata server and `cloudkms.googleapis.com` is not restricted. There is no default-deny egress in `ewallet-vault` in source [LIVE for any cluster-wide policy]. |
| Reaching Google APIs | Nodes are not private (no `private_cluster_config` in `gke/main.tf`), and the subnet has `private_ip_google_access = true` [SRC `network/main.tf:14`]. |
| Authorized networks | Not involved: Vault reaches KMS from the pod network, not through the control plane. **No change to master authorized networks is needed** [SRC `gke/main.tf:28-35`]. |

### 8.2 Resources [PROPOSAL]

| Resource | Value | Why |
|---|---|---|
| Key ring | `ewallet-vault`, location `asia-southeast1` (the cluster's region, zone `-b`) | Same region; key rings cannot be deleted [ASSUMPTION, GCP behaviour] |
| Crypto key | `vault-unseal`, purpose `ENCRYPT_DECRYPT`, protection `SOFTWARE`, **no scheduled rotation in V2.0** | Each rotation adds a billable active version. Old versions "must not be disabled or deleted" [DOC], so rotation only adds cost here. Manual rotation is documented, not automated (O6). |
| Destroy protection | Terraform `prevent_destroy`; default scheduled-destroy delay (30 d) kept [ASSUMPTION default] | A destroyed version means the Vault "cannot be recovered, even from backups" [DOC] |
| Google SA | `ewallet-vault-unseal@<project>.iam.gserviceaccount.com` | Same pattern as the existing per-workload GSAs (`modules/iam`, `modules/workload_identity`) |
| IAM on the **key** (not the project) | `roles/cloudkms.cryptoKeyEncrypterDecrypter` (useToEncrypt, useToDecrypt) **plus** `cloudkms.cryptoKeys.get`, via `roles/cloudkms.viewer` on the key or a custom role with exactly the 3 permissions [DOC: the seal page lists `useToEncrypt`, `useToDecrypt`, `cryptoKeys.get`; the encrypterDecrypter role does not include `get`] | least privilege |
| WI binding | `roles/iam.workloadIdentityUser` on the GSA for `serviceAccount:<project>.svc.id.goog[ewallet-vault/vault]` | `vault` is the SA the chart creates for release `vault` [ASSUMPTION chart default; confirm with `helm template`] |
| KSA annotation | `server.serviceAccount.annotations: {iam.gke.io/gcp-service-account: <GSA>}`, set with `--set` from the preflight output, not hard-coded | no project ID in Git beyond what already exists |
| Direct WIF principal (no GSA) | possible alternative [ASSUMPTION: KMS accepts `principal://…` members]; **not recommended**, for consistency with the repository's GSA pattern | — |

Not granted: `cloudkms.admin`, `cryptoKeyVersions.destroy`, any project-level KMS role, or any JSON key.

### 8.3 Vault configuration [PROPOSAL]

The change to `values-gke.yaml`, inside `server.ha.raft.config`:

```hcl
seal "gcpckms" {
  project    = "__GCP_PROJECT__"        # rendered by the install step, as with other placeholders
  region     = "asia-southeast1"
  key_ring   = "ewallet-vault"
  crypto_key = "vault-unseal"
}
```

Everything else in `values-gke.yaml` stays as it is: TLS listener, Raft, `service_registration`, no UI, no PDB, single replica.

The readiness probe can become strict:

- current: `sealedcode=204&uninitcode=204`, which is why "Pod Ready does not mean unsealed";
- V2: after init, use `uninitcode=204` only, so a **sealed** auto-unseal Vault becomes **NotReady**. That is a real, visible signal in the GKE console.
- The probe change is staged: init needs the pod reachable while uninitialised, which `uninitcode=204` keeps.

### 8.4 Recovery keys and the CLI

| Today (Shamir) | V2 (auto-unseal) |
|---|---|
| `sys/init` with `secret_shares`/`secret_threshold`; the file holds `keys_base64` and `root_token` | `sys/init` with `recovery_shares: 3`, `recovery_threshold: 2` (set explicitly: the docs disagree on defaults [DOC]); the file holds `recovery_keys_base64` and `root_token` |
| `unseal` after each restart | **not used**; `unseal` refuses when `seal-status.type != "shamir"` |
| `generate-root` feeds unseal shares | feeds **recovery** keys: same API, `sys/generate-root/update` [DOC: "these operations require recovery keys instead"] |
| shares can unseal | recovery keys **cannot** unseal: "not sufficient to unseal Vault if the auto unseal mechanism isn't working" [DOC] |

Code changes:

- `ops/vault/admin.py:60-64`: `initialise()` takes a seal type.
- `ops/vault/cli.py:100-136`: `init`.
- `cli.py:145-155`: `unseal`.
- `cli.py:234-243`: `generate-root` reads `recovery_keys_base64`.

All existing safety properties stay: exclusive 0600 file, outside the repo, fsync before success, `vault_initialised_keys_not_stored` stop.

Storage: the same file, on two offline encrypted media, as today (`VAULT.md:257-268`). The reference's KMS-encrypted copy of the init file is **not** adopted: it would make recovery depend on the same key whose loss it is meant to cover.

### 8.5 Behaviour and failure plan

| Event | Behaviour | Recovery |
|---|---|---|
| Restart, drain, node upgrade, GKE wake | Vault decrypts its root key with KMS and starts **unsealed** [DOC]. Order after a wake: gke-metadata-server, then Vault obtains a token. A first attempt before the metadata server is ready may fail and the pod restarts [ASSUMPTION] | Wait; the gate requires `sealed:false` within 5 min [LIVE] |
| KMS temporarily unavailable | A running Vault is unaffected (root key in memory) [ASSUMPTION]. A restarting Vault stays sealed: "cannot recover access … until the mechanism is available again" [DOC]. ESO syncs fail; synced Secrets remain (V1 behaviour, `VAULT.md:210-213`) | Wait for KMS; no data loss |
| IAM binding removed | As above at the next restart | Restore the binding |
| Key version disabled | Cannot decrypt, so sealed on restart [DOC: old versions must stay enabled] | Re-enable the version |
| Key version destroyed / key unusable permanently | **Unrecoverable, even from backups** [DOC] | None: prevented by `prevent_destroy`, IAM, and the rule that cleanup never touches KMS |
| Recovery keys lost | Vault still runs and auto-unseals; the root-generation ceremony becomes impossible | Before loss: `operator rekey -target=recovery` [DOC]. After loss: no admin root; routine work still uses the operator token |
| Raft PVC lost | Data lost (unchanged from V1) | `Retain` PV (A+ drill); KMS does not back data up |

### 8.6 Scenario A (fresh) and Scenario B (migration)

- **Scenario A (recommended, and the only one planned):** Vault starts uninitialised with the seal configured, then `init` with recovery keys. Both the rehearsal and production take this path, because GKE has no Vault [SRC/§3].
- **Scenario B (not planned):** the official procedure [DOC]:
  - back up first;
  - add the seal stanza;
  - restart;
  - `vault operator unseal -migrate` with the Shamir keys, which become the recovery keys;
  - for Raft, bring nodes back so the quorum holds, step down the active node and wait for "seal re-wrap completed";
  - "cannot perform the seal migration process without downtime".

  **Stop condition:** if any initialised Vault or a Raft PVC `data-vault-0` is found at G-1, stop and request a separate decision. Do not migrate.

### 8.7 How it can be tested safely

| Where | What | Label |
|---|---|---|
| Offline | `helm template` with `values-gke.yaml`: the seal stanza is present, no `credentials`, no `tls_disable`; the KSA annotation is rendered; the readiness path | unit test |
| kind | A transit seal against a throwaway dev-mode "unsealer" Vault in its own namespace. This exercises recovery-key init, auto-unseal after restart, sealed-while-unsealer-down, and generate-root with recovery keys | `SIMULATED_AUTO_UNSEAL`, **never a KMS PASS** |
| GKE rehearsal | real KMS, WI, restart, sleep/wake | `KMS_GKE_PASS` only here |
| Not tested on the production key | disable or destroy a version, revoke IAM | would endanger the key; covered by the kind simulation and the documentation |

---

## 9. Workload Identity and IAM requirements (summary)

| Principal | Grant | Scope |
|---|---|---|
| `ewallet-vault-unseal` GSA | `roles/cloudkms.cryptoKeyEncrypterDecrypter` | key `vault-unseal` |
| `ewallet-vault-unseal` GSA | `cloudkms.cryptoKeys.get` (`roles/cloudkms.viewer` or a 3-permission custom role) | key `vault-unseal` |
| KSA `ewallet-vault/vault` | `roles/iam.workloadIdentityUser` on the GSA | GSA |
| Owner | creates the key ring and key; holds `destroy` | project |
| Campaign automation (Claude) | **read-only**: `gcloud kms keys describe`, `versions list`, `get-iam-policy` | key |

### 9.4 IaC choice [PROPOSAL]

Add a small Terraform module `infra/terraform/gcp/modules/vault_kms/`:

- the key ring, the key with `prevent_destroy`, the GSA and the three bindings;
- **without `depends_on = [module.gke]`**.

`prevent_destroy` gives the key lifecycle protection that a script cannot.

The dev state has drifted from the live cluster, and a plain apply replaces the node pool [SRC `ops/gcp/r2_terraform.py:1-22`]. So:

- apply only with `-target=module.vault_kms`;
- first check the plan with a guard in the style of `ops.gcp.r2_terraform guard`: exactly these creates, nothing else.

The alternative is equivalent `gcloud` commands in a reviewed script. It is simpler to read, but it has no destroy protection. This is owner decision O5.

---

## 10. Vault KV path mapping

Mount `ewallet` (KV v2), unchanged.

| Vault path | Keys | Underlying | Generated by | Written by | Rotated by | Read by | K8s target(s) (owner ESO) | Consumers update | Old credential retired | Rollback | Release |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ml-platform/feast-online-redis` | `connection_string` | U1 | existing value (seeded from `feast-online-redis`) | owner, `cli.py put --stdin` with the operator token | owner (new KV version); the real Redis password change is a separate coordinated step | `eso-ewallet-reader` | `ml-platform/feast-online-redis-vault` | rollout restart of Feature API and Fraud API | V1 path removed; `feast-online-redis` kept until O8 | Helm rollback / remove patch | V2.0 |
| `agent-platform/vllm-api-key` | `api-key` | U2 | existing value | owner | owner | reader | `ewallet-agents/vllm-api-key-vault` | restart vLLM **and** agentgateway together (one Helm upgrade) | kept until O8 | `helm rollback llm-agent` | V2.0 |
| `agent-platform/agentgateway-client-key` | `api-key` | U3 | existing value | owner | owner, with a coordinated restart of gateway and agents | reader | `ewallet-agents/agentgateway-client-key-vault` | same Helm upgrade; kagent agents restart | kept until O8 | same | V2.0 |
| `agent-platform/coordinator-idempotency-redis` | `password` | U4 | existing | owner | maintenance window only | reader | `…-auth-vault` via `target.template` (`connection_url`, `users.acl`) | Helm values switch | — | Helm rollback | V2.1 (optional) |
| `ml-platform/feast-online-redis` (same record) | — | U1 | — | — | — | reader | `ewallet-mcp/feast-online-redis-agi-{fraud,rag}-vault` via template `…,db=N` | MCP Helm values | — | Helm rollback | V2.1 (needs `ewallet-mcp` in conditions) |
| `mcp-platform/releases/<release>/<subject>.<server>`, `mcp-platform/server-sets/<set>/<server>` | `token` / `auth-config.json` | MCP | `g3_mcp_credentials.py` | the same tool, through a Vault store, create-only (CAS 0) | new release ID, never a new version | reader (new prefix) | immutable release Secrets (§11) | existing two-step Helm switch | approval-gated cleanup | `serverSet`/`clientRelease` | MCP-B, later |

Fan-out rule: one record for one underlying value. M1's two consumers share **one** target Secret in one namespace, so no duplicate exists anywhere.

Optional template for later [PROPOSAL]: storing `endpoint` and `password` separately and templating `connection_string` would let the Redis server Secret (M2) come from the same record. V2.0 does **not** do this: the exact format of the existing string must match byte for byte (`shadow_compare.sh` MATCH), and the stored-verbatim form is already proven by A+.

---

## 11. MCP credential lifecycle

### 11.1 Existing mechanism [SRC]

- **Generator:** `ops/llm_agent/g3_mcp_credentials.py`, with `secrets.token_urlsafe(48)` and values kept in memory only.
  - Client Secrets: `mcp-token-<subject>-<server>-<release>`.
  - Server tables: `mcp-auth-<server>-<server-set>`.
  - All are `immutable: true`, carry the plan fingerprint annotation, and are created with `kubectl create`, never updated (`:1-25`, `:363-411`).
- **Repeats and failures:** an exact repeat is a metadata-only no-op; a partial or conflicting set fails closed.
- **State machine:** `LEGACY_STABLE → SERVER_TRANSITION (old+new table) → CLIENT_PROMOTED → FINAL`, with rollback in reverse order (`docs/llm_agent/g3_credential_lifecycle.md:37-51,101-110`).
- **Selection:** Helm selects tables and tokens by two non-secret identifiers, `mcpCredentialLifecycle.serverSet` and `clientRelease` (`values.yaml:811-817`; `templates/mcp-services.yaml:147`; `templates/agents.yaml:336-337,553-560`).
- **Placement:** client and discovery tokens live in `ewallet-agents`, because kagent `headersFrom` resolves there. Server tables live in `ewallet-mcp`. Sandbox tokens live in `ewallet-sandbox`.
- **Retirement:** retired subjects, `carry_releases` and `dropped_subjects` are handled by the tool; retired references are refused by `ops/llm_agent/retired_credentials.py` (`g3/g3.yaml:1-7`).
- **Last recorded live state (offline projection, not live):** server tables `fin-20261117` in `ewallet-mcp`, carrying `kagent-controller.*.r1-release1` and `sandbox-*.sbx-20261115` (`docs/evidence/edai2/agents/lf-offline-20261117/workstream-c/projected-inventory.json`) [LIVE].

### 11.2 Options

| | MCP-A: preserve | MCP-B: Vault stores immutable releases | MCP-C: redesign (reference blue/green) |
|---|---|---|---|
| What changes | nothing | the tool gains a `VaultStore` that writes `mcp-platform/...` create-only (CAS `0`). For each Secret, an ExternalSecret with `refreshPolicy: CreatedOnce`, `target.immutable: true`, `creationPolicy: Orphan` | per-token workloads, `activeRevision` commits, KV-version pins |
| Sequencing authority | tool + Helm | **tool + Helm (unchanged)** | new manifest file |
| Token identity per release | ✓ | ✓ (path = release) | per KV version |
| Transition tables | ✓ | ✓ (a table is just another create-only record) | not modelled: one token per service |
| Rollback | ✓ | ✓: the old Secrets stay (`Orphan`), and the old Vault records are never overwritten | commit revert |
| Sandbox tokens | ✓ | needs `ewallet-sandbox` in the store, or stays Kubernetes-only | same issue |
| Cost / risk | 0 | MEDIUM complexity, HIGH blast radius (agent auth) | HIGH / HIGH |

### 11.3 Recommendation: **MCP-A in V2.0; MCP-B as a separate, later release**

Why explicit **release paths** and not KV version numbers:

- One release writes many records: every client and server table.
- The release ID is already the public identity, and a KV version number would need a mapping file.
- CAS `0` gives exactly the "create, never overwrite" contract of `kubectl create`.
- Transition and final tables are distinct records, not successive versions of one.

Why `creationPolicy: Orphan` with `CreatedOnce` and `immutable`, as the ESO documentation recommends for credentials that must not be rewritten [DOC]:

- deleting an ExternalSecret must never delete an immutable release Secret that rollback depends on;
- a periodic refresh must never touch it.

Required changes for MCP-B, later:

- a `VaultStore` implementing the existing `SecretStore` protocol (`g3_mcp_credentials.py:168-176`: `metadata`, `auth_table`, `create`);
- an ES manifest renderer keyed by release;
- `ewallet/data/mcp-platform/*` added to the reader policy, and create (CAS required) for the operator;
- `cas_required=true` on `mcp-platform/` metadata;
- tests: replay is a no-op, a partial set fails closed, CAS refuses an overwrite, an ES never refreshes;
- the `mcp_credential_check.sh` and AGI-08 probes re-run unchanged.

The migration path: the next new release is prepared through Vault. Existing releases stay Kubernetes-only until retired. No live Secret is moved.

Risks if it were done now: agent authentication outages, sandbox scope, and kagent `headersFrom` behaviour with ESO-owned Secrets [LIVE]. **Not in the first acceptance campaign.**

---

## 12. Migration strategy

Order (each step has its own owner approval, and the next starts only after the previous one passes E2E):

| Step | Group | Consumers switched | E2E gate | Rollback |
|---|---|---|---|---|
| 0 | Vault (KMS) + ESO + ClusterSecretStore; shadow ESs only | none | CSS `Ready`, ES `SecretSynced`, `shadow_compare.sh` = `SHADOW_MATCH` for all 3 | delete the shadow ESs by name (targets are `-vault` names) |
| 1 | M1 to the Feature API | Helm overlay `values-vault-eso.yaml` (`redisSecret.name=feast-online-redis-vault`) | Swagger or real lookup `BUSINESS_LOOKUP_PASS` (`VAULT_GKE_ACCEPTANCE_CAMPAIGN.md` §4) | `helm rollback ewallet-serving` |
| 2 | M1 to the Fraud API | kustomize patch `fraud-api-eso.patch.yaml` (new) | scoring request 200 | remove the patch |
| 3 | A1 + A2 together | `infra/helm/llm-agent-platform/values-vault-eso.yaml` (new): `vllm.apiKeySecret.name`, `agentgateway.clientKeySecret.name`, `modelConfig.apiKeySecret.name` in **one** `helm upgrade --reuse-values` | gateway 200 with the key, 401 without or with a wrong key; kagent UI chat through the gateway; BYO resolver start | `helm rollback llm-agent` |
| 4 | V2.1: P5, A3 | — | — | — |
| 5 | Remove old Secrets | — | owner closes the rollback window (O8) | n/a |

The values switch is safe only because MATCH proves the values are identical. Pods restarted by the rollout receive the same credential, so no consumer pair can disagree during the rollout.

---

## 13. Rotation and rollback

| Credential | Rotation | Notes |
|---|---|---|
| M1 (synthetic or rehearsal) | `put` → KV v2 version N+1 → `force-sync` annotation → rollout restart of both APIs (as A+ `lifecycle`) | Changing the **real** Redis password also needs M2 and a Redis restart, so it is not part of V2.0 |
| A1 `vllm-api-key` | new version → force-sync → restart vLLM and agentgateway | brief 401/502 window between the two restarts; acceptable in the rehearsal, owner-timed in production |
| A2 `agentgateway-client-key` | new version → force-sync → restart agentgateway **and** every agent that reads the ModelConfig | the gateway accepts one key (`agentgateway.yaml:46`), so there is no dual-key window without a chart change. Production rotation is **deferred**; rehearsed on kind |
| MCP | unchanged: a new release ID through the existing tool | never a refreshed ES |

Rollback layers:

1. Helm or kustomize consumer rollback to the original Secret.
2. Vault: read the previous version (metadata kept) and `put` it as the new current version. Never delete versions.
3. KMS: none needed. The key is never rotated or disabled by a campaign.

---

## 14. E2E test matrix

Columns: **A+** = covered in the existing Campaign A+; **V2** = what V2 needs; **kind** / **GKE** = where it runs; **Prod** = production-only.

### 14.1 Existing tests: keep, modify, replace or remove

| Test (tests/ops/…) | A+ | V2 | Action |
|---|---|---|---|
| `test_vault_eso::test_store_authenticates_with_its_own_identity_over_verified_https` | ✓ | ✓ | **modify**: ClusterSecretStore, CA `namespace`, SA `namespace` |
| `::test_policy_reads_exactly_the_mapped_paths` | ✓ | ✓ | **modify**: explicit prefixes; assert no `metadata`, `list`, `sys/`, `*` outside the prefixes |
| `::test_namespaces_cannot_read_each_others_paths` | ✓ | — | **replace** with `test_only_listed_namespaces_may_use_the_store` + `test_reader_cannot_read_outside_its_prefixes`; retired invariant documented (§7.4, O1) |
| `::test_targets_are_new_secrets_never_existing_ones` | ✓ | ✓ | keep (new names) |
| `::test_eso_installation_processes_only_namespaced_stores_and_pins_images` | ✓ | ✓ | **modify**: `processClusterStore: true`, the other three still false |
| `::test_no_rbac_or_workload_uses_the_reader_identity` | ✓ | ✓ | keep (reader now in `external-secrets`) |
| `::test_vault_stays_internal` | ✓ | ✓ | keep |
| `::test_overlay_switches_the_feature_api_…` | ✓ | ✓ | modify the name; **add** the llm-agent and Fraud overlays |
| `::test_negative_*` (3) | ✓ | ✓ | **modify**: add sandbox-denied and out-of-prefix cases |
| `::test_business_lookup_never_passes_on_…` | ✓ | ✓ | keep |
| `test_vault_tools::test_workload_role_is_scoped_…`, `::test_workload_policy_grants_one_read_only_path` | ✓ | — | **remove** with the direct-loader roles (O4), or keep if O4 = keep |
| `test_vault_tools::` init tests (4) | ✓ | ✓ | **modify**: recovery shares and threshold, the `recovery_keys_base64` field |
| `::test_fewer_shares_than_the_threshold_…`, `::test_an_authorized_ceremony_…` | ✓ | ✓ | modify: recovery keys |
| `::test_gke_values_never_use_dev_mode_or_plain_http` | ✓ | ✓ | keep; **add** seal assertions |
| `::test_single_replica_vault_has_no_disruption_budget` | ✓ | ✓ | keep |
| all other `test_vault_tools` token-lifecycle tests | ✓ | ✓ | keep |
| `test_vault_gke_preflight::*` (11) | ✓ | ✓ | keep; **add** KMS read-only gates |
| `test_vault_shadow_compare::*` (3) | ✓ | ✓ | keep |
| `test_vault_rehearsal::*` (39) | ✓ | ✓ | keep the safety tests; **modify** the role and bootstrap tests for a rehearsal CSS `ewallet-vault-rehearsal` (conditions `[vault-rehearsal, vault-rehearsal-b]`, path `rehearsal/*`); the cleanup tests add "never touches KMS" |

### 14.2 V2 coverage

| Test | A+ | V2 required | kind | GKE | Prod |
|---|---|---|---|---|---|
| Offline: Helm render, seal stanza, no credentials file, TLS, KSA annotation, readiness path | partial | ✓ | — | — | — |
| Offline: CSS schema + conditions; ES → CSS refs; key mappings; ownership labels | — | ✓ | — | — | — |
| Offline: bootstrap has 2 roles, 3 policies; prefixes | — | ✓ | — | — | — |
| Offline: migration and rollback preflight, cleanup inventory (one cluster object) | partial | ✓ | — | — | — |
| K1 Vault deploy, TLS, CA chain | ✓ | ✓ | ✓ | ✓ | — |
| K2 init with recovery keys; **auto-unsealed** without `unseal` | — | ✓ | simulated (transit) | **real KMS** | — |
| K3 ESO + CSS `Ready=True` | — | ✓ | ✓ | ✓ | — |
| K4 multi-namespace sync (2 namespaces, 3 ES) | partial | ✓ | ✓ | ✓ (rehearsal namespaces) | ✓ |
| K5 synthetic Redis to the real Feature API lookup | ✓ | ✓ | ✓ | ✓ | ✓ |
| K6 synthetic gateway key to the real agentgateway: 200 / 401 wrong / 401 missing | — | ✓ | ✓ (llm-agent chart, mock or CPU vLLM) | — (limitation) | ✓ step 3 |
| K7 vLLM backend key via the gateway | — | ✓ | ✓ | — | ✓ |
| K8 Coordinator Redis (template) | — | V2.1 | ✓ | — | — |
| K9 negatives: sandbox namespace denied by conditions; out-of-prefix path `permission denied`; wrong SA; wrong audience; untrusted CA; non-ESO pod blocked by NetworkPolicy | ✓ (V1 set) | ✓ | ✓ | ✓ | ✓ (subset) |
| K10 shadow MATCH before every switch | ✓ | ✓ | ✓ | ✓ | ✓ |
| K11 rotation (KV N+1 → sync → restart → 200) | ✓ | ✓ | ✓ | ✓ | — |
| K12 consumer rollback and re-switch | ✓ | ✓ | ✓ | ✓ | ✓ |
| K13 Vault restart → **unsealed automatically**, ESO resumes | sealed variant | ✓ | simulated | ✓ | — |
| K14 unsealer (KMS stand-in) down → Vault stays sealed, Secrets retained, consumers serve | — | ✓ | simulated | — (never on the real key) | — |
| K15 generate-root with 2 recovery keys; 1 key cannot finish | Shamir variant | ✓ | simulated | optional | — |
| K16 PV `Retain` drill, interrupted-recovery cleanup, fault injection | ✓ | ✓ | ✓ | ✓ (Retain only) | — |
| K17 guarded sleep/wake: same disks (IDs), auto-unsealed, data intact | — | ✓ | pod restart only | **✓** | — |
| K18 cleanup ownership, disks proven absent, **KMS key untouched** (describe before = after) | ✓ (no KMS) | ✓ | ✓ | ✓ | — |
| K19 production snapshot unchanged | ✓ | ✓ | ✓ | ✓ | — |

---

## 15. GKE rehearsal strategy (Campaign A2, synthetic)

Prerequisites, each a separate owner approval:

- (O5) the KMS resources exist;
- the read-only preflight reports `key ENABLED`, `primary ENABLED`, the 3 bindings present, and no Vault object or PVC;
- the newest recovery manifest is used for the wake.

| Block | Steps | Stop condition |
|---|---|---|
| G-0 pre-wake (read-only) | clean HEAD; pinned context; KMS describe snapshot; 0 nodes | any FAIL |
| G-1 wake + baseline | `cloud_lab.py wake-full` (owner); preflight retried until `cert-manager-ready`; production snapshot | existing Vault or `data-vault-0` → **STOP (Scenario B)** |
| G-2 Vault | install with seal and KSA annotation; PVs `Retain`; `initialized:false` | pod not Ready after 10 min |
| G-3 init | recovery 3/2 into the private file; **no unseal**; `seal-status.type=gcpckms`, `sealed=false` | sealed after init |
| G-4 bootstrap + ESO + CSS | rehearsal CSS, 2 rehearsal namespaces, synthetic records | CSS not Ready |
| G-5 consumers | A+ Feature API release, switch, rollback, re-switch | any lookup ≠ `BASELINE_OK` |
| G-6 negatives | K9 set | any unexpected cause |
| G-7 lifecycle | rotation; `vault-0` deleted → auto-unsealed ≤ 5 min; ESO resumes | still sealed |
| G-8 sleep/wake | guarded sleep (owner, `--also-approve` as required); wake; same PD names; auto-unsealed; KV versions intact; lookup 200 | different disk, or sealed |
| G-9 cleanup | A+ cleanup; disks 404; **KMS describe equal to G-0** | any KMS difference |
| G-10 sleep | fresh manifest; `verify --expect sleep` | — |

Estimated cost: Campaign A took ≈33 min awake (≈ US$0.55 including the cluster fee). With two wakes, A2 will take about 1.5–2 h awake, so **≈ US$1.5–2.5** [ASSUMPTION, from the `cloud_lab.py cost-status` rates used in that README], plus the KMS key-version fee.

Network: the cloud-lab kubeconfig uses the DNS control-plane endpoint (`VAULT_ESO_RUNBOOK.md:24-25`). If the owner's network changes, **do not modify authorized networks**. Use the DNS endpoint, or stop and report.

Not covered by the GKE rehearsal (stated limitation): Agent Gateway runtime authentication. It is covered on kind (K6/K7) and in production step 3.

---

## 16. Evidence plan (row 54)

Owner screenshots only, captured during the official campaign; none is generated.

- E1–E9 of `VAULT_GKE_ACCEPTANCE_CAMPAIGN.md` §7 stay, with these replacements:
  - E3 (policies) shows the **2 roles**;
  - E5a shows the **ClusterSecretStore `Ready=True`** and ESs in two namespaces.
- New figures:

| ID | Figure | Shows | Never visible |
|---|---|---|---|
| K-E1 | Cloud KMS key `vault-unseal` page | protection SOFTWARE, primary version ENABLED, location | — |
| K-E2 | Key permissions (IAM) | the GSA with encrypterDecrypter + get; no other principal with use | — |
| K-E3 | `vault status` / `cli.py status` after `kubectl delete pod vault-0` | `Seal Type gcpckms`, `Sealed false`, `Recovery Seal true` | recovery keys, tokens |
| K-E4 | Vault workload Ready after a wake, with no unseal command in the shell history shown | auto-unseal across a sleep | — |
| K-E5 | `kubectl get externalsecret -A` | `SecretSynced` in `ml-platform` and `ewallet-agents` | Secret data |
| K-E6 | Agent Gateway 200 / 401 (curl with `-H "Authorization: Bearer $KEY"` hidden) | consumer authentication | the key |
| K-E7 | Sandbox ES denied (ESO condition message) | namespace restriction | — |

Rules (unchanged from V1):

- metadata, status, hash-prefix or equality verdicts only;
- kind evidence is labelled kind;
- `SIMULATED_AUTO_UNSEAL` never appears as KMS evidence.

Documentation: after acceptance, update `docs/edai2/platform/VAULT.md` and the existing runbooks in place. This audit stays the decision record. No new small files.

---

## 17. Risks and limitations

| Area | Risk | Mitigation |
|---|---|---|
| Offline (unverifiable here) | ESO v2.14 field support for `conditions` and the CSS `caProvider.namespace`; ESO honouring `remoteRef.version` for Vault KV v2 (the reference relies on it; ESO pages do not state it [DOC]) | kind K3, K4, K9; MCP-B only after a kind version-pin test |
| Offline | Vault 2.0.4 `gcpckms` startup behaviour before the metadata server is ready | GKE G-3, G-7, G-8 |
| GKE-only | real KMS, WI token exchange, auto-unseal across a PD re-attach | Campaign A2 |
| Production | wrong consumer pair during the Agent Gateway switch | one Helm upgrade for all three references; MATCH first; `helm rollback` |
| Production | an over-broad reader: a mis-written ES in a trusted namespace reads another area's credential | explicit prefixes, RBAC review [LIVE], retired-invariant record |
| Production | KMS key destroyed or disabled → total loss | `prevent_destroy`; no destroy permission for automation; cleanup never touches KMS; recovery file offline |
| Production | single owner holds all recovery keys | unchanged from V1 (`VAULT.md:265-268`), a people decision |
| Production | Grafana and Redis server passwords are not actually rotated by V2.0 | stated (A4, M2 DEFER) |
| Cost | the key-version fee continues while asleep | ≈ US$0.06/month (§19) |

---

## 18. Implementation roadmap [PROPOSAL, not started]

| Phase | Changes (exact files) | Reuse | Tests | PASS | Rollback | Approval |
|---|---|---|---|---|---|---|
| **P1 Offline config** | **Modify:** `infra/kubernetes/vault/values-gke.yaml` (seal, KSA annotation placeholder, staged readiness), `values-local.yaml` (transit seal for kind), `bootstrap.json` (2 roles), `policies/secret-operator.hcl` (new prefixes), `network-policy.yaml` (ESO only, if O4), `external-secrets/values.yaml` (`processClusterStore: true`), `external-secrets/ml-platform.yaml` and `ewallet-agents.yaml` (ES only → CSS), `negative-tests.yaml`, `infra/helm/ewallet-serving/values-vault-eso.yaml` (target name). **Create:** `external-secrets/cluster-secret-store.yaml`, `policies/eso-ewallet-reader.hcl`, `infra/helm/llm-agent-platform/values-vault-eso.yaml`, `infra/kubernetes/fraud-serving/fraud-api-eso.patch.yaml`. **Remove:** `policies/eso-ml-platform.hcl`, `eso-ewallet-agents.hcl`, and, if O4, `feature-api.hcl`, `fraud-api.hcl` | V1 values, cert chain, overlay pattern | §14.1 changes; new render tests | `pytest tests/ops` green; every security invariant either kept or listed as retired | `git revert` | none (repo only) |
| **P2 CLI** | `ops/vault/admin.py` (`initialise` with recovery fields, `seal_status` type), `ops/vault/cli.py` (init, unseal refusal, generate-root with recovery keys, `status` shows seal type) | all file-safety code | init, ceremony, refusal tests with recovery keys | tests green; no key or token printed | revert | none |
| **P3 kind E2E** | `scripts/vault/eso_local_validate.sh` (unsealer Vault, CSS, the two agent keys, sandbox negative), `scripts/vault/gke_rehearsal.sh` (CSS phases, auto-unseal assertions, KMS-untouched check stub) | the whole A+ driver | K1–K16 on kind | full run 0 FAIL, published as `rehearsal_v2_kind_<ts>` labelled kind / `SIMULATED_AUTO_UNSEAL` | cleanup phase | none (local; check `free -g` first) |
| **P4 KMS IaC + preflight** | **Create:** `infra/terraform/gcp/modules/vault_kms/{main,variables,outputs}.tf`, wired in `environments/dev/main.tf` without a gke dependency; a plan guard (extend `ops/gcp/r2_terraform.py` or a sibling). **Modify:** `scripts/vault/gke_preflight.py` (read-only KMS and WI gates) | R2 guard pattern | guard rejects any extra resource; preflight tests | plan guard PASS on a scratch plan | `terraform state rm` (never destroy) | **O5: create the GCP resources** |
| **P5 GKE Campaign A2** | none (run only) | cloud_lab guarded sleep | §15 | `KMS_GKE_PASS` + A2 complete | A+ cleanup | **wake, each block, sleep** |
| **P6 Campaign B2 (production)** | update `VAULT_ESO_RUNBOOK.md`, `VAULT_GKE_ACCEPTANCE_CAMPAIGN.md` | Campaign B plan, E1–E9 | steps 0–3 of §12 | row 54 evidence | per-step rollback | **each consumer switch** |
| **P7 docs** | `docs/edai2/platform/VAULT.md` (replace the Shamir and namespaced-store sections), evidence index | — | link check | — | — | — |
| later | V2.1 (P5, A3), MCP-B | — | — | — | — | separate |

Dependencies:

- P1 → P2 → P3, which must pass before any GKE step.
- P4 is independent of P3 but needed before P5.
- P5 → P6 → P7.

---

## 19. Cost implications

| Item | Estimate | Basis |
|---|---|---|
| KMS active key version | ≈ **US$0.06 per month** per active SOFTWARE version (US$0.000082192 per hour, prorated) | Google Cloud KMS pricing page, via search summary (§21). The page itself did not render in this audit: confirm before approval |
| KMS operations | US$0.03 per 10,000. Vault calls KMS at init, at each start and on seal re-wrap: well under 1,000 a month even with daily wakes, so < US$0.01 | same; the operation count is an [ASSUMPTION] |
| While GKE sleeps | the key-version fee continues; operations stop | the key is independent of the cluster |
| Rotation | +US$0.06/month per additional version kept enabled | hence no scheduled rotation (O6) |
| Disabled versions | likely still billed until destroyed [ASSUMPTION] | confirm on the pricing page |
| GKE | no new nodes, CPU or disks compared with V1: Vault ≈100m and 256Mi, ESO ≈40m, 6 GiB PD (`VAULT.md:306-307`) | [SRC] |
| Manual Shamir (V1) | US$0, plus owner time for an unseal on every wake | — |
| Campaign A2 | ≈ US$1.5–2.5 one-off | §15 |

---

## 20. Decisions and owner approvals

| # | Decision | Recommendation |
|---|---|---|
| D1 Store | `SECRETSTORE` / **`CLUSTERSECRETSTORE`** / `HYBRID` | **CLUSTERSECRETSTORE** `ewallet-vault`, exact namespace list, sandbox excluded |
| D2 Unseal | `MANUAL_SHAMIR` / **`GCP_KMS_AUTO_UNSEAL`** | **GCP_KMS_AUTO_UNSEAL**, fresh init only, recovery keys 3/2 |
| D3 Runtime | | **Keep a single replica**, Raft, audit PVC, no PDB. HA would triple cost for a sleeping lab, and auto-unseal removes the main availability pain |
| D4 Auth | | 1 ESO reader role (dedicated SA, explicit prefixes, read only) + `vault-admin` + the orphan operator token |
| D5 Coverage V2.0 | | M1 (Feature API + Fraud API), A1, A2 |
| D6 MCP | preserve / integrate / defer | **Preserve in V2.0** (MCP-A); MCP-B with release paths later |
| D7 Migration order | | Feature API → Fraud API → vLLM key + gateway client key (one upgrade) |
| D8 Testing | | keep the A+ driver and safety tests; modify 9 tests, replace 1, remove 2 (if O4) (§14.1) |
| D9 Cost | | ≈ US$0.06/month + ≈ US$2 one-off rehearsal |
| D10 Complexity | | **`FEASIBLE_WITH_LIMITATIONS`** |

Owner approvals required:

| | Approval |
|---|---|
| O1 | Accept the retired invariant "per-namespace Vault separation among trusted namespaces" |
| O2 | Keep `ewallet-sandbox` (and `ewallet-ci`, `ewallet-ingress`) outside the store |
| O3 | Approve the V2.0 coverage (D5) and the deferrals in §4 |
| O4 | Remove the direct-loader roles, policies and NetworkPolicy entries (`ai_platform/inference/vault.py` stays as code) |
| O5 | Create the KMS key ring, key, GSA and bindings (Terraform targeted or gcloud) |
| O6 | No scheduled key rotation in V2.0 |
| O7 | Any KMS key-version disable or destroy: never part of a campaign |
| O8 | When to delete the original Kubernetes Secrets after acceptance |
| O9 | GKE wake, each A2 block, guarded sleep (`--also-approve` targets) |
| O10 | Each production consumer switch (§12 steps 1–3) |

---

## 21. Sources

Repository files are cited inline. External sources:

- HashiCorp Vault, GCP Cloud KMS seal: https://developer.hashicorp.com/vault/docs/configuration/seal/gcpckms
- HashiCorp Vault, Seal/Unseal concepts (recovery keys, seal migration): https://developer.hashicorp.com/vault/docs/concepts/seal
- HashiCorp Vault, `operator init`: https://developer.hashicorp.com/vault/docs/commands/operator/init
- External Secrets, ClusterSecretStore: https://external-secrets.io/latest/api/clustersecretstore/
- External Secrets, HashiCorp Vault provider: https://external-secrets.io/latest/provider/hashicorp-vault/
- External Secrets, ExternalSecret and templating: https://external-secrets.io/latest/api/externalsecret/, https://external-secrets.io/latest/guides/templating/
- Google Cloud KMS, roles and permissions: https://docs.cloud.google.com/kms/docs/reference/permissions-and-roles
- Google Cloud KMS pricing: https://cloud.google.com/kms/pricing (figures in §19 must be confirmed on this page)
- Reference: https://github.com/itsmekhoathekid/RecSys-MLops at `6bd6e99f`

---

## 22. Implementation record and deviations (P1–P3, appended after implementation)

Sections 1–21 above are the original decision record (commit `0d22f643`) and are not rewritten.
P1 (`e8c31dce`), P2 (`46bef874`) and P3 implemented them on this branch, offline only. Where the
implementation differs from the text above, or a stated assumption turned out differently on kind,
it is listed here.

| # | Audit text | Implemented / observed | Why |
|---|---|---|---|
| D-1 | §7.3, O4: remove the direct-loader roles if safe | **Removed.** The roles `feature-api` and `fraud-api`, their policies and their NetworkPolicy entries are gone. `ai_platform/inference/vault.py`, `featureApi.vault` and `fraud-api-vault.patch.yaml` stay, marked "V1, not supported by the V2 bootstrap" | The offline dependency audit found no deploy script or default that uses them |
| D-2 | §8.5 [ASSUMPTION]: with the seal unavailable, a restarting Vault "stays sealed" | **Observed (kind, transit seal):** Vault never opens its listener. After ≈110 s it exits with `error parsing Seal configuration: context deadline exceeded`, and kubelet restarts it with back-off. Once the seal is back, the next start unseals by itself; no pod delete and no unseal are needed. Synced Secrets and consumers are unaffected, and consumers can even start | The behaviour was learned offline. For `gcpckms` it is expected to be similar, but that stays NOT_VERIFIED_ON_GKE. The runbook and the driver check exactly this behaviour |
| D-3 | §15: a rehearsal store with paths under `rehearsal/*` | The rehearsal store `ewallet-vault-rehearsal` uses role `eso-vault-rehearsal`, bound to the **official** reader identity. Its exact paths are `ml-platform/rehearsal-redis`, `agent-platform/rehearsal-vllm-api-key` and `agent-platform/rehearsal-agentgateway-client-key` | The unchanged `secret-operator` token can seed them with the official workflow, and the narrower policy still cannot read the real records (tested) |
| D-4 | §14.1: negative causes read from the ESO log per ExternalSecret | For a ClusterSecretStore, ESO validates the store by logging in itself. A login failure (wrong audience, untrusted CA) is logged under the **store's** name; the ExternalSecret only reports "could not get secret data from provider" | The driver checks three things: the ExternalSecret is refused, the store is NotReady, and the store's log lines carry the cause. In the Campaign B template the test store and its ExternalSecret share one name, so the documented grep still finds the cause. A side effect: `Ready=True` on a store proves a real login |
| D-5 | §14.1: modify the A+ seed assertion | The Feast seed Job reads the ESO-managed Secret, not the original one | After the real Redis rotation (K11) the original credential is retired. The "registry contains no password" check now covers both the original and the current password |
| D-6 | §18 P3: extend `eso_local_validate.sh` | `eso_local_validate.sh` and the direct-loader `local_validate.sh` were **removed**. `gke_rehearsal.sh` in kind mode is the V2 end-to-end driver | Both validators hard-wired the V1 objects (namespaced stores, direct-loader roles). Their evidence runs and Git history remain |
| D-7 | §18 P4: KMS preflight gates | Not implemented (P4). On GKE the driver's `vault` phase refuses until they exist | P1–P3 are offline only |
| D-8 | (new) | `scripts/vault/publish_ca.sh` publishes the one CA ConfigMap and refuses to overwrite one it did not publish. `scripts/vault/rehearsal/kind_cert_manager.sh` installs and removes cert-manager v1.16.2 for kind runs | Needed by §7.5 and by the kind campaign; kept as committed scripts, not ad-hoc commands |
| D-9 | §14.2 K6/K7: the gateway on kind, the backend "mock or CPU vLLM" | The **real vLLM CPU image** with the staged Qwen3-0.6B model, plus the real agentgateway, through the real chart and the official overlay | The image and model were already on the kind node, so no mock was needed and the backend's own `--api-key` check is exercised |

---

## 23. P4-A: Cloud KMS as code, plan guard and preflight (appended; offline only)

| | State |
|---|---|
| IMPLEMENTED SOURCE CODE | `infra/terraform/gcp/modules/vault_kms/`; `module.vault_kms` in `environments/dev` (`enable_vault_kms`, default `false`); `ops/gcp/vault_kms_terraform.py`; `scripts/vault/kms_preflight.py`; `scripts/vault/kms_offline_plan.sh`; the driver's GKE KMS gate |
| OFFLINE TESTED | `terraform fmt` and backend-disabled `validate`; a **real `terraform plan`** of `-target=module.vault_kms` against an empty LOCAL state with the network black-holed (7 creates, nothing else); the guard on that plan and on its mutations; the preflight against a fake `gcloud` |
| FUTURE CLOUD VERIFICATION | the real-backend plan and guard; apply; `kms_preflight.py --mode cloud`; Vault auto-unseal through KMS on GKE (Campaign A2) |
| OWNER APPROVAL REQUIRED | every step of P4-B (VAULT_ESO_RUNBOOK.md step 0) and Campaign A2 |

**What the module creates** (exactly the guard's allowlist): the Cloud KMS API, key ring `ewallet-vault`
(`asia-southeast1`), key `vault-unseal` (ENCRYPT_DECRYPT, SOFTWARE, no rotation, 30-day destroy delay),
GSA `ewallet-vault-unseal`, and three non-authoritative IAM members:

- `cryptoKeyEncrypterDecrypter` on the key;
- `cloudkms.viewer` on the key;
- `iam.workloadIdentityUser` on the GSA for `ewallet-vault/vault`.

Effective permissions of the GSA, all on that one key:

- `useToEncrypt` and `useToDecrypt`;
- `cryptoKeys.get`, together with metadata reads (get/list) of the key and its versions, which come with
  `cloudkms.viewer`.

That is slightly broader than the three permissions the seal needs: still no key material, use only, no
change, disable or destroy. A three-permission custom role would be exact, at the cost of one more resource.

**Decisions and deviations:**

| # | Decision | Why |
|---|---|---|
| P4-1 | The Cloud KMS API is owned by `module.vault_kms`, not added to `module.project_services` | A plan targeted at the module then stays inside it. The project-services set is not touched (tested: one owner) |
| P4-2 | `count` on every resource, gated by `enable_vault_kms = false` | The repository's cost-gate convention; an ordinary plan creates nothing |
| P4-3 | `prevent_destroy` on the key ring, key and GSA, **and** `deletion_policy = "PREVENT"` on the key and GSA (provider 7.46.1) | `prevent_destroy` is configuration-level; `PREVENT` is enforced from state. Neither stops an out-of-band console, `gcloud` or API action: IAM must |
| P4-4 | The Workload Identity principal is built from `var.project_id` and the fixed KSA, not from GKE outputs | No dependency on `module.gke` or `module.workload_identity`. The offline targeted plan proves the closure is the module alone |
| P4-5 | The guard accepts an unknown scope attribute (`key_ring`, `crypto_key_id`, `service_account_id`) only when the plan's configuration derives it from the approved resource | These IDs are unknown before apply. Every role and member is known at plan time and must match exactly |
| P4-6 | On GKE the driver's `vault` phase proceeds only after this run recorded `kms-preflight` PASS | Replaces P2's unconditional refusal; Campaign A2 still needs its own approvals |

**Drift that remains** (not resolved; unverified until P4-B step 1):

- The dev state records an earlier node pool.
- A full plan proposes replacing it.
- With `enable_gke = false` (the default) a full plan would even plan to **destroy** the cluster (inferred from the count gate in `modules/gke`).

Only a targeted plan that the guard passes may be applied. The guard rejects every update, delete, replace,
import or forget outside the seven creates. A synthetic or local-state plan proves the guard and the module
closure, never the content of the real plan.
