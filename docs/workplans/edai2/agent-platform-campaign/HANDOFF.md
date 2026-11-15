# Agent platform completion: campaign handoff

- Machine-readable state: [`checkpoints.json`](checkpoints.json).
- Images: [`images.json`](images.json).
- Commands and pass rules: the
  [master runbook](../../../runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md).

## Blocker resolution (2026-11-15, BR-00 to BR-10)

| ID | Blocker | Resolution | Tests | Runtime |
|---|---|---|---|---|
| B1 | `feast 0.66.0`, CVE-2026-18948 (unsafe dill UDF deserialization), blocked `mcp-tools` | `feast[redis]==0.67.0` in `infra/docker/llm-agent-tooling` only; the lock moves `feast` alone. `feast_cross_version_check.sh`: the 0.66.0 feature repo publishes, the 0.67.0 MCP code reads 4 views, 31 features and 8 RAG fields, all equal. | Cross-version check PASS; Feast, MCP and RAG suites; lock contract | Image published, gate PASS; not deployed |
| B2 | kagent does not watch `ewallet-sandbox` | PKG-K: `controller.watchNamespaces` + `rbac.namespaces` (the kagent 0.10.3 keys) on the existing release, with the pilot post-renderer. Chart + live values + post-renderer reproduce the live release (29/29 objects). The diff adds 6 RBAC objects in `ewallet-sandbox` and changes `WATCH_NAMESPACES`. | `sandbox_watch_kind.sh`: `PACKAGES_VALIDATED`; unit tests | `APPROVAL_PENDING` |
| B3 | MCP experiment unschedulable on 4 nodes | `mcp_capacity.py` preflight. `run` stops with `CAPACITY_INSUFFICIENT` before creating anything. The optimized arm needs 2 distinct non-LLM nodes; the PKG-C plan is a 5-node wake. | Capacity and simulator tests | `APPROVAL_PENDING` (PKG-C); not proven at runtime |
| B4 | KEDA does not watch `ewallet-sandbox` | PKG-W: `values-sandbox-watch.yaml` on the existing release. Identity matches live; the diff adds 1 RoleBinding and changes `WATCH_NAMESPACE` on 3 Deployments. | `PACKAGES_VALIDATED` on kind | `APPROVAL_PENDING` (with PKG-P3 only) |
| B5 | P1–P4 prerequisites | `agi_campaign.sh` `AGI_PACKAGE` = `p1-mcp`, `s1-mcp`, `s1-agents`, `p2-agents`, `p4-agents`: exact identities, separate tokens, P2 rollback order, `IMAGE_SHA`. The fleet's Secret names now match `g3_mcp_credentials`. | Gate tests (25) | `APPROVAL_PENDING` per package |
| B6 | Guarded sleep used without its approval | `--scale-pdb-guarded` now needs `--manifest`, `--campaign` and `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>`. It scales only PDBs covered by the manifest or `--also-approve`, and resizes only once they are released. Otherwise `SLEEP_BLOCKED`. | 44 cloud_lab tests | Not run on GKE (by design) |
| B7 | Runbook out of date | Rewritten: PKG-0…P4 and PKG-C, a fresh-terminal setup, A+S kept apart from B′. A consistency test checks paths, packages, tokens, flags and namespaces. | `test_master_runbook.py` | Owner execution |

Two real defects were found and fixed along the way:

- The fleet referenced Secret names and a key that the credential tool never writes.
- A static S1 flow overlay would have deleted the 16 live cross-namespace policies,
  because Helm replaces lists.

## Approvals the owner can give, one at a time

| Package | Approves | Unlocks |
|---|---|---|
| PKG-K | `WATCH_APPROVAL=PKG-K:<context>`: kagent also watches `ewallet-sandbox` | AGS-01, 02, 04, 05, 06 (A+S, S0) |
| PKG-C | A 5-node `wake-full` for the AGR-02 window (+$0.18/h) | AGR-02 |
| PKG-P1 | `AGI_APPROVAL=P1-MCP:<campaign>` | AGR-01, AGR-03; AGI MCP side |
| PKG-S1 | Sandbox credentials, the copy of 2 client Secrets, `S1-MCP` and `S1-AGENTS`, ambient label | AGS-03; tool results in AGS-04 |
| PKG-P2 | `AGI_APPROVAL=P2-AGENTS:<campaign>` | AGI-01–14, AGR-04–07 |
| PKG-P4 | `AGI_APPROVAL=P4-AGENTS:<campaign>` | AGI-09–11-sbx |
| PKG-W + PKG-P3 | KEDA watch, then the CRD selector patch | AGS-07 (B′) |
| Guarded sleep | `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>` (+ `--also-approve ewallet-mcp/feature-rag-mcp` after P1) | Sleep when PDBs block it |

## Future report outlines (no final docs yet)

- **`docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md`:**
  - agents and runtimes;
  - Coordinator routing (fraud, drift, combined);
  - the Feature-RAG and drift Specialists;
  - the global ModelConfig;
  - MCP and A2A Gateway policies, including the sandbox routes and flow;
  - the Registry;
  - AGI-01–14.
- **`docs/edai2/agents/AGENT_RELIABILITY_AUTOSCALING.md`:**
  - MCP replicas, RollingUpdate and client failover (AGR-01–03), with the capacity
    analysis;
  - KEDA's in-flight trigger, Prometheus, scale-out and scale-in, drain (AGR-04–07);
  - limits.
- **`docs/edai2/agents/AGENT_SANDBOX_SUBSTRATE.md`:**
  - SandboxAgents and gVisor isolation;
  - the shared WorkerPool and actor assignment;
  - cold against warm starts;
  - the watch-scope packages;
  - B′ (the kind result, the GKE status);
  - the fixed pool against the KEDA-scaled pool (AGS-01–07).

## GKE runs

- **`agi-20261114t221240z`** (evidence in `gke-agi-20261114t221240z/`). Awake
  22:17–22:38Z on 4 nodes, about $0.24.
  - Recorded live facts: the kagent and KEDA watch scopes, the WorkerPool `/scale`
    without a selector, and a server-side dry-run of the CRD patch (accepted).
  - CP6 was blocked by capacity.
  - Sleep is recorded as `SLEEP_VERIFIED`, but it used `--scale-pdb-guarded` without
    that flag's separate approval (B6).
- **BR (2026-11-15).** The cluster was not woken. Only read-only Helm reads were made
  against the control plane (kagent and KEDA values, manifests and status) to build
  the exact diffs.
