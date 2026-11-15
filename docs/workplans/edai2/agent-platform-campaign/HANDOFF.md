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

## Stage PKG-K and Sandbox S0 (`agi-20261115T054518Z`, evidence in `gke-agi-20261115T054518Z/`)

- **Wake:** `wake-experiment --profile agent-sandbox --nodes 3`. The cluster went 0 → 3 → 0
  nodes and was awake from 05:47 to 06:23Z, about $0.30.
  - The profile held 34 workloads outside it at 0 (listed in `wake/wake.txt`).
  - The five PDB-guarded Deployments stayed at 0.
  - Running outside the profile, because they are not in the 2026-11-06 manifest:
    `ewallet-llm-gpu` ×4 and `keda` ×3. `ml-platform/feature-api` also ran, held at 1 by
    its ScaledObject.
- **PKG-K: `GKE_TESTED`, kept.**
  - Revision 5 → 6.
  - Identity PASS; exact diff (+6 RBAC, `WATCH_NAMESPACES`); server-side dry-run PASS.
  - After the upgrade, the live manifest equals the reviewed candidate.
  - The controller logs `["ewallet-agents","kagent-pilot","ewallet-sandbox"]`.
  - The pilot SandboxAgent is still Ready.
- **S0: `BLOCKED`.**
  - Observed working:
    - 2 warm gVisor workers, Ready, on 2 nodes;
    - both SandboxAgents `Accepted`;
    - both ActorTemplates created, with `sandboxClass gvisor` and the shared worker
      selector;
    - Substrate assigned one golden actor to each worker.
  - The golden snapshots never formed. atelet pulls the actor image itself and
    anonymously (`--gcp-auth-for-image-pulls=false`), so Artifact Registry answered
    `DENIED: Unauthenticated request`. The pilot works only because its image is public.
- **AGS-01:** PARTIAL.
- **AGS-02:** observed read-only. Workers run `ateom-gvisor`; `ewallet-sandbox/default`
  mounts no token and has discovery-only RBAC; `allowedDomains` are set. The egress
  denial was not exercised.
- **AGS-04:** BLOCKED. Without a golden snapshot, no session can create an actor.
- **Cleanup:**
  - Fleet, placeholder Secrets, ActorTemplates and the WorkerPool were removed.
  - The namespace and the PKG-K RBAC were kept.
  - Two Suspended golden-actor records remain in ate-api, with no pod and no snapshot.
- **Sleep:** `SAFE_TO_SLEEP`, then a plain `sleep`: `SLEEP_VERIFIED`.

### New owner decision: PKG-R (Artifact Registry pull for Substrate)

1. `substrate` release (`ate-system`): `atelet.gcpAuthForImagePulls=true`
   (`infra/kubernetes/kagent-pilot/values/substrate-ar-pull-overlay.yaml`). The atelet
   DaemonSet rolls.
2. IAM: `roles/artifactregistry.reader` on repository `ewallet-platform` for
   `principal://iam.googleapis.com/projects/198818809719/locations/global/workloadIdentityPools/ewallet-data-ai-platform-hg26.svc.id.goog/subject/ns/ate-system/sa/atelet`.

Offline, the overlay changes only atelet's `args[0]`. A live identity gate for the
`substrate` release, in the style of `sandbox_watch.sh`, is still to be written before
PKG-R runs.

### Screenshot views for the owner's rerun (after PKG-R)

```bash
kubectl -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide            # AGS-01_shared_workerpool.png
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents \
  -o custom-columns=POD:.metadata.name,SA:.spec.serviceAccountName,IMAGE:.spec.containers[0].image   # AGS-02_sandbox_isolation.png
kubectl auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl"     # AGS-04_actor_worker_assignment.png
curl -s "http://127.0.0.1:18383/api/substrate/status?namespace=ewallet-sandbox" | python3 -m json.tool
```

## PKG-R readiness (2026-11-15, no cloud mutation; evidence in `pkg-r-readiness-20261115/`: substrate-gates.txt, substrate-diff.json, iam-plan.txt)

- **Authentication path, read from the live configuration and the pinned source:**
  - atelet runs as `ate-system/atelet`, without `hostNetwork` and without a GSA annotation.
  - The cluster pool is `ewallet-data-ai-platform-hg26.svc.id.goog` and `cpu-pool` uses
    `GKE_METADATA`.
  - The flag makes atelet call `googauth.DefaultTokenSource` once at start-up. That
    resolves to the federated principal of the ServiceAccount, and a failure is fatal.
- **PKG-R Helm** (`scripts/llm_agent/substrate_pull_auth.sh`), run read-only against release
  `substrate` revision 2:
  - The lookup-render identity check PASSES. CA, TLS and session pools are read back;
    nothing is suppressed.
  - The diff is DaemonSet `atelet` `args[0]` only.
  - The server-side dry-run PASSES.
- **PKG-R-IAM** (`scripts/llm_agent/substrate_pull_iam.sh`): grants
  `roles/artifactregistry.reader` on repository `ewallet-platform` to the principal above.
  Currently absent. It adds or removes exactly one pair and checks that every other
  binding is unchanged.
- **Residue** (`scripts/llm_agent/substrate_golden_residue.sh`, `golden-residue.json`): the
  cause is Substrate 0.0.9 not deleting golden actors. `kubectl-ate` builds from the
  pinned source (`scripts/llm_agent/build_kubectl_ate.sh`, sha256 `f65ab2f7…`). Listing
  needs a running ate-api.
- **Next campaign:** the sequence is in section 2.3a of the master runbook.

Approvals needed, one at a time:

1. `IAM_APPROVAL=PKG-R-IAM:ewallet-data-ai-platform-hg26`.
2. `PKG_R_APPROVAL=PKG-R:<context>`.
3. The `agent-sandbox` wake on 3 nodes.
4. Optionally `RESIDUE_APPROVAL=GOLDEN-RESIDUE:<context>`.
