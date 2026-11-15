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

## Stage PKG-R and Sandbox S0 (`agi-20261115T064905Z`, evidence in `gke-agi-20261115T064905Z/`)

- **PKG-R-IAM: kept.** One `roles/artifactregistry.reader` binding on `ewallet-platform`
  for `ns/ate-system/sa/atelet`. The only other binding (Cloud Build writer) is unchanged.
- **PKG-R: `substrate` 2 → 3, kept.**
  - Identity PASS (lookup render); diff atelet `args[0]` only; server-side dry-run PASS.
  - After the upgrade, the live release equals the reviewed candidate.
  - 3/3 atelets rolled out with 0 restarts. A failed token request at start-up would
    have been fatal, so this shows Workload Identity works. The pilot is still Ready.
- **Private actor image pulls: PASS.** Both ActorTemplates reached Ready with golden
  snapshots at 07:08Z. There were 0 pull, DENIED or Unauthenticated errors. The 32
  transient `Unavailable` dial errors (07:00–07:04) came while atelets and workers
  restarted.
- **AGS-01:** both SandboxAgents `Accepted` and `Ready`, both templates Ready on
  `business-agents`, 2 warm gVisor workers.
- **AGS-02:** partial. gVisor; `default` SA with no token and discovery-only RBAC;
  `allowedDomains` set. There is no NetworkPolicy in `ewallet-sandbox` and no egress probe
  was run, so network isolation is not verified.
- **AGS-04: `SHARED_WORKERS_OBSERVED`, golden actors excluded.**
  - 14 session actors on 2 workers. Each worker hosted 3 distinct session actors of both
    agents, at most 1 at a time.
  - Suspension was observed.
  - **Resumption failed:** `session actor … not reachable via atenet-router: context deadline
    exceeded` after 120 s, on every resume.
  - Business tool calls failed with `mcp_transport_error`: the MCP Gateway is held at 0
    by the profile, and S1 is not deployed.
- **Defect found and fixed:** kagent serves SandboxAgents at
  `/api/a2a-sandboxes/<ns>/<name>`, and `/api/a2a/` returned 404. The Coordinator, the
  Gateway routes, the runbook and the tests are fixed.
  - `ai_platform` changed, so **the four images must be rebuilt (PKG-0) before P1, P2
    or P4**.
- **Residue (not deleted, `golden-residue.json`):** 3 golden actors in `ate-golden`
  (2 old, 1 new) and 4 session actors in `ewallet-sandbox`. All Suspended, none with a pod.
- **Cloud:** 0 → 3 → 0 nodes, about $0.50; plain sleep, `SLEEP_VERIFIED`.

### Owner screenshot views (rerun with the same sequence, runbook section 2.3a)

```bash
scripts/llm_agent/substrate_pull_iam.sh plan                                          # IAM binding
kubectl -n ate-system get ds atelet -o jsonpath='{.spec.template.spec.containers[0].args[0]}'; kubectl -n ate-system get pods -l app=atelet
kubectl -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide            # AGS-01_shared_workerpool.png
kubectl -n ewallet-sandbox get actortemplate -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.status.goldenSnapshot}{"\n"}{end}'
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o custom-columns=POD:.metadata.name,SA:.spec.serviceAccountName,IMAGE:.spec.containers[0].image   # AGS-02
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl"     # AGS-04_actor_worker_assignment.png
```

## Resume root cause (2026-11-15, no cloud action; `resume-rca/RCA.md`)

- **Root cause (`ROOT_CAUSE_CONFIRMED`):** kagent 0.10.3 suspends a session actor
  asynchronously after each turn. `EnsureSessionActor` skips `ResumeActor` for a SUSPENDING
  actor. A follow-up sent within that window therefore waits 120 s and fails with
  `not reachable via atenet-router`.
  - GKE timeline: turn 2 was sent at 07:15:03, the actor was Suspended at 07:15:04, and it
    was never Resuming.
  - Reproduced deterministically on unmodified v0.10.3.
- **Fix in this repository (`FIX_IMPLEMENTED`, `UNIT_TESTED`):** `sandbox_sessions.py` waits,
  bounded, until the session's actor is `Suspended` before each follow-up. `--no-settle`
  reproduces the failure.
  - The production Coordinator sends one message per member context, so it is not affected.
- **Upstream patch (proposed):** `resume-rca/kagent-0.10.3-resume-during-suspend.patch`. All
  56 package tests pass. Not deployed: that needs a kagent image and upgrade approval.
- **Images:** rebuilt from `f2583641` for the `/api/a2a-sandboxes` fix. Trivy PASS 4/4,
  pushed, not deployed (`images.json`).
- **GKE A/B:** done on 2026-11-15; see the next section.

## Resume A/B on GKE (`agi-20261115T084916Z`, evidence in `gke-agi-20261115T084916Z/`)

- **Run:** `agent-sandbox` profile, 3 nodes, awake 08:51–09:27 UTC.
- **Sleep:** normal sleep, `SLEEP_VERIFIED` (cpu-pool 0 VMs). Cost about $0.40 at list
  price.
- **A, `--no-settle`:** the race reproduced. The follow-up got `rpc_error` after 120.5 s,
  and the actor stayed Suspended.
- **B2, paced (drift, 1 × 4): `RESUME_GKE_TESTED_PASS`.**
  - Actor `asr-62efd060a1d79057ff6018c4` resumed 3 times on the same ID.
  - Every restore was clean and every message reached the agent; no `rpc_error`.
  - This is a lifecycle result only: under S0 each task ended `failed`
    (`mcp_transport_error`).
- **B3:** the same worker then served feature-rag, so one worker served both agents
  (sharing holds).
- **B as first specified (both agents at once): failed, not the race.**
  - Two concurrent first-turn restores on one node were cancelled at kagent's 30 s
    `--substrate-call-timeout`. The actors were left `RESUMING`, both workers were held,
    and every later request got `no free workers`.
  - Recovery: the campaign worker pods were restarted; nothing was deleted.
- **Not fixed upstream:** the unmodified kagent controller is still vulnerable to unpaced
  multi-turn requests. Only the client-side pacing is verified.
- **Open, each needing an owner decision:**
  1. Raise `--substrate-call-timeout`, a shared kagent controller change. A single restore
     takes about 24–25 s, leaving about 5 s of headroom.
  2. Add worker anti-affinity to the WorkerPool.
  3. Upgrade kagent with the proposed patch.
  4. Delete the residual actors (`RESIDUE_APPROVAL`); they are recorded read-only.
  - Details: `resume-rca/RCA.md` section 7.

## Runtime stability PKG-T (`rts-20261115T114235Z`, evidence in `gke-rts-20261115T114235Z/`)

- **Verdict: `RUNTIME_STABILITY_GKE_PASS`.** The fix is in our pinned builds of kagent
  v0.10.3 and Substrate v0.0.9; upstream is unchanged. See `resume-rca/RCA.md` section 8.
- **Live (kept):**
  - kagent revision 8: patched controller (session turns and capacity wait),
    `SUBSTRATE_CALL_TIMEOUT=90s`.
  - Substrate revision 5: patched ate-api (detached workflows and restore rollback), atelet
    memory 512Mi:1536Mi.
  - The campaign WorkerPool uses the patched ateom; `kagent-pilot` keeps the release image.
  - Rollback: `runtime_fix.sh <target> rollback` returns kagent to revision 6 and Substrate
    to revision 3.
- **Found and fixed on GKE:**
  - F6: atelet was OOM-killed by its in-memory pull cache.
  - F7: `no free workers` while another session's suspend was releasing its worker.
- **Remaining:**
  - Single kagent replica.
  - A controller crash mid-turn beyond the 30 s grace can leave a RUNNING actor until that
    session's next turn.
  - The atelet, atecontroller and atenet release images still fail the Trivy gate.
  - The gateway's 150 s budget against a 90 s resume plus a 120 s business turn.
- **Next session:** PKG-P1, S1, P2, P4 and B′, on this runtime.
