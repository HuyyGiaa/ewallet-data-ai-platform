# agentregistry 0.4.0 security assessment (prompt management, row 35)

Scope: release `prompt-registry` r1 in `ewallet-prompts` on GKE `ewallet-dev`, as deployed 2026-11-03. Bounded assessment, no IAM change, no registry replacement.

Evidence: `docs/evidence/edai2/cicd_runtime_20261103/registry-security/`.

## 1. Findings

| # | Area | Observation (GKE, unless noted) | Severity |
|---|---|---|---|
| F1 | Authentication | The server's complete configuration surface (env keys in the pinned binary `arctl-server@sha256:a2f69bd3…`) is `DATABASE_URL`, `SERVER_ADDRESS`, `MCP_PORT`, `AGENT_GATEWAY_PORT`, `PLATFORM_MODE`, `MCP_REGISTRY_COMPAT_*`, `CONTROLLER_*`, `LOG_LEVEL`, `VERBOSE`, `RUNTIME_DIR`, `SKIP_MIGRATIONS`. **No authentication setting exists.** `/openapi.json` declares no security scheme. | High (by design of 0.4.0) |
| F2 | Write surface | 12 unauthenticated REST writes, including `POST/DELETE /v0/apply`, `DELETE /v0/prompts/{name}/{tag}`, `PUT/DELETE /v0/deployments/{name}`, `PUT/DELETE /v0/runtimes/{name}`. `POST /v0/apply?dryRun=true` with altered v2 content returned 200 and persisted nothing. | High |
| F3 | MCP endpoint `:31313` | 18 tools, all read-only (`get_*`, `list_*`, `registry_health`, `registry_version`). | Low |
| F4 | Kubernetes identity | The registry pod has `automountServiceAccountToken: false` and no Role or ClusterRole binding, so `PLATFORM_MODE=kubernetes` deploy endpoints have no credentials to act with. | Low |
| F5 | Network | `default-deny` plus same-namespace and DNS. From `ewallet-cicd`: registry 12121, MCP 31313 and Postgres 5432 are **BLOCKED**. Positive control inside `ewallet-prompts`: all REACHABLE. | Medium (in-namespace lateral reach) |
| F6 | Port-forward reach | Of 156 ServiceAccounts, only `ewallet-ci/jenkins` could `create pods/portforward` in `ewallet-prompts` (batch B). Human cluster admins can too. Jenkins does not publish prompts. **Closed by M1 (applied 2026-11-03T14:12:29Z):** Jenkins port-forward is now Forbidden; only cluster admins remain. | Medium → Low |
| F7 | Integrity root | Git `prompts/manifest.json` pins every version's sha256. `ops/llm_agent/prompt_registry.py` refuses unknown, unpublished and tampered versions. Fallback is explicit, recorded and content-identical. | Control |
| F8 | Detective control | `prompt_registry.py verify-all` checks all 12 pinned versions against the registry (read-only). | Control |
| F9 | **Incident (executor error)** | During this assessment, one probe line sent `POST /v0/apply?dryRun=false` with a forged bearer token. It was accepted and **changed registry `drift-specialist-system/v2`** to v2 plus a probe line (sha `d0174b5e…`). `verify-all` reported the MISMATCH and the resolver refused v2. Production was unaffected: r32 reads the Git ConfigMap (`0c0798e1…`). The registry was restored from Git: republish 17/17, `verify-all` 12/12 MATCH (`incident-unintended-write.txt`). Full record: `docs/edai2/prompt_registry_incident_f9_20261103.md`. | Demonstrates F1/F2 live |

## 2. Threat scenarios

| Scenario | Outcome with the current controls |
|---|---|
| Poisoning (overwrite a pinned version) | The resolver rejects on sha256. The ConfigMap, kagent config-hash and the agent are unchanged. **Prevented** (kind canary and F9 on GKE). |
| Replay or downgrade (old text under the current tag) | Same sha256 rejection. The selected version comes from reviewed Git/Helm values, not from the registry. **Prevented.** |
| New unreviewed version (tag not in the manifest) | Refused before any call. **Prevented.** |
| Deletion of a prompt | Resolver reports "not published", fails closed, ConfigMap unchanged. Only *future* updates are blocked. **Availability impact only.** |
| Registry or Postgres outage | Fails closed; optional fallback uses identical Git bytes; the running agent keeps its last ConfigMap. **Availability of updates only.** |
| Catalog vandalism (agents, MCP servers) | No consumer reads these at runtime. Rebuilt from Git by `registry_catalog publish --all-prompt-versions`. **Low.** |
| Lateral movement from a compromised in-namespace pod | Only the registry and its Postgres run there; pods cannot be created by Jenkins. **Low.** |

**Conclusion:** registry writes are unauthenticated, but they **cannot change an agent's effective prompt**, because integrity is anchored in the reviewed Git manifest and enforced fail-closed by the resolver. The residual risk is availability and catalog integrity, limited to principals that can port-forward (`ewallet-ci/jenkins` and cluster admins).

## 3. Minimal remediation (no new IAM, no registry replacement)

Status after the owner decision of 2026-11-03:
- **M1 APPLIED and verified** (`m1-apply/`).
- **M2 design and local test only**, not applied (`prompt_registry_m2_networkpolicy_review_20261103.md`).
- **M3 IMPLEMENTED** as a mandatory fail-closed gate in the tooling (incident record §5, C3).

| # | Change | Effect | Rollback |
|---|---|---|---|
| M1 | Remove `pods/portforward` from Role `ewallet-prompts/jenkins-prompt-registry` (batch B) | The only non-human writer path disappears; registry writes need cluster-admin. Jenkins does not publish prompts today. | Re-apply the batch B Role |
| M2 | Replace `same-namespace-and-dns` in `ewallet-prompts` with pod-specific rules: Postgres ingress only from the registry pod on 5432; registry egress only to Postgres 5432 and DNS; **no in-cluster ingress** to the registry (operator access is via port-forward, which NetworkPolicy does not mediate) | Closes F5 lateral reach | Re-apply the current policy |
| M3 | Make `verify-all` a mandatory gate: before and after any r33 or prompt change, plus a documented periodic operator check | Detects tampering (as in F9) before any resolver use | n/a |

Not proposed now:
- **Authenticated writes:** not available in agentregistry 0.4.0. They would need a newer registry release or an authenticating proxy (a new component); both need a separate decision.
- **Langfuse:** a separate decision.
