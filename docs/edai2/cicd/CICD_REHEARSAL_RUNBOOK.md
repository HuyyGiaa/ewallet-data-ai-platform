# CI/CD rebuild: release units, local verification and the owner rehearsal

This is the technical runbook for the rebuilt Jenkins CI/CD (branch `feature/edai2-cicd-rebuild`).
It covers rubric rows 33–36. The audit that motivated each change is
[`docs/workplans/edai2/CICD_P0_AUDIT.md`](../../workplans/edai2/CICD_P0_AUDIT.md); finding IDs
(`CICD-P0-Fnn`) refer to it.

**Status:**
- **Implemented and tested offline.** Nothing below has run on Jenkins or GKE.
- **Every runtime result is pending.** The owner rehearsal in §5 produces the evidence for each row.
- **Historical results are not evidence for this implementation.** These are RAG CD build #8 and the
  N9 CI builds.

## 1. How a release works now

```text
Jenkins job (one per release group)
  -> Checkout and provenance       HEAD must equal EXPECTED_GIT_SHA; build URL and triggering user recorded
  -> Validate catalog, scope       components.json + release-units.json; merge-base change detection
  -> Component CI                  uv lock --check, uv sync --locked, catalog tests -> ci-verdict.json
                                   PASS | FAILED | NOT_APPLICABLE (a release refuses anything but PASS)
  -- DEPLOY_APPROVED + input --
  -> Verify release branch         origin/release/<group> == EXPECTED_GIT_SHA
  -> Plan release units            release_request -> release_policy images (which images the units need)
  -> Build or reuse images         Cloud Build per image, or reuse <name>:<sha> if its provenance labels match;
                                   digest read back from Artifact Registry
  -> Scan images                   Trivy (pinned) per digest via Cloud Build; PASS | VULNERABLE | SCANNER_ERROR
  -> Rehearse release units        release_policy build -> deploy_release.sh (release_dispatch.py) under Lease locks
  -- PROMOTE_TO_PRODUCTION + second input by a different person --
  -> Prepare operator handoff      production plan + release_handoff.py build -> archived handoff/;
                                   console: RELEASE_STATUS READY_FOR_OPERATOR_PROMOTION, RELEASE_HANDOFF sha256=...
                                   (Jenkins never deploys production; it holds no production identity)

Operator workstation (approved identity, §8)
  -> Verify handoff                release_handoff.verify: console digest, artifacts, SHA chain, target, approvals
  -> Production transaction        production_release.py, units in plan order (§7), lock checkpoint + journal entry
                                   before every mutation: sandbox-candidate (create, verify, always delete) ->
                                   helm-image-promotion -> fleet-image-promotion (guarded JSON patch) ->
                                   runtime_verify (fail-closed reconciliation) -> reverse rollback on failure
  -> Registry reconciliation       separately approved publication, then registry_release.py reconcile
```

**CI components vs release units.**
- A *component* (`jenkins/catalog/components.json`) only tests and builds: it owns source paths,
  locked Python projects, test commands and the images built from it.
- A *release unit* (`jenkins/catalog/release-units.json`, validated by `scripts/ci/release_units.py`)
  is a deployment. It has a typed `kind`, a `stage` (`rehearsal` or `production`), consumed images,
  `depends_on` (deployed earlier in the same release), `prerequisites` (verified, never redeployed),
  a lock, and the verifications it must pass.
- The dispatcher runs a closed set of executors chosen by kind. No plan can name a script.

| Group / Jenkins job | Rehearsal units, in order | Production units | Rubric row |
|---|---|---|---|
| `release/rag-pipeline` / `edai2-cd-rag-pipeline` | `rag-pipeline` | — | 33 |
| `release/mcp-agents` / `edai2-cd-mcp-agents` | `prompt-registry-gate`, `mcp-servers`, `agent-fleet` | `mcp-servers-production` | 34 |
| `release/agent-fleet` / `edai2-cd-agent-fleet` | `mcp-servers`, `agent-fleet` | `agent-fleet-candidate`, `agent-fleet-production` | 34 |
| `release/coordinator` / `edai2-cd-coordinator` | `mcp-servers`, `agent-fleet`, `coordinator` | `agent-fleet-candidate`, `mcp-servers-production`, `agent-fleet-production`, `coordinator-production` (full stack of one SHA) | 35 |
| `release/streaming` / `edai2-cd-streaming` | `streaming-block-a` | — | 36 |
| `release/prompt-management` / `edai2-cd-prompt-management` | `prompt-registry-gate` | — | 34 |

## 2. What each unit proves, and what it does not

| Unit | Runs | Proves | Does not prove |
|---|---|---|---|
| `rag-pipeline` | Unchanged handler `scripts/ci/deploy/rag_pipeline.sh`. Suspended CronJob `ewallet-cicd/rag-ingestion` at the new digest; one smoke Job against an ephemeral Redis; contract `jenkins/cd/rag-pipeline.contract.json` | The released image reproduces the production RAG publication (corpus, chunks, embeddings, publication identity) | That the production Airflow DAG `rag_fraud_policy_ingestion` ran (its tasks are `BashOperator`s on the Airflow host, not this image) |
| `prompt-registry-gate` | `scripts/ci/registry_gate.py`: Git-pinned prompt bytes vs `prompts/manifest.json`, then `ops/llm_agent/prompt_registry.py verify-all` | agentregistry holds every pinned prompt version byte-for-byte | That a running agent reads it. The verdict says `runtime_consumption: NOT_PROVEN` (§6) |
| `mcp-servers` | `mcp-cd-feature-rag` and `mcp-cd-drift`, each with its production factory and real auth stack; smoke checks readiness, refusal of an anonymous call, and that the full tool set is listed | Both servers start from the released image with production authentication, and each lists every tool it should | Business answers (Feast, KServe, Redis and drift data are unreachable here) |
| `agent-fleet` | Both Specialists started with their SandboxAgent actor contract (cmd, args, port 80, `PATH`/`PYTHONPATH`, typed-only flags; `test_cd_rehearsals.py` fails on drift) | Both actors boot from the released image as Substrate would start them, serve their Agent Card and reject a malformed task | Substrate WorkerPool, gVisor, suspend/resume, kagent sandbox route (verified live after promotion by `platform-status`) |
| `coordinator` | Coordinator with its idempotency Redis and the production Sandbox-only routing configuration | It boots with that configuration, serves its card and rejects a malformed task | The fraud, drift and combined workflows (`runtime_verify` after promotion) |
| `streaming-block-a` | Image smoke (`jenkins/cd/streaming.contract.json`), then the frozen operator `scripts/streaming/block_a_cloud.sh` for one `verify-cicd-<sha12>-<build>` campaign | Redpanda → Flink job → sink Jobs wrote the Offline store (campaign GCS prefix) and the `rehearsal-stream` Online store for **this** run, with Offline/Online parity and the release SHA in the report | A stateful upgrade of a long-running production job (none exists; checkpoints are ephemeral, §6) |
| `agent-fleet-candidate` | `scripts/ci/sandbox_candidate.py` (§7): `feature-rag-specialist-cicd` and `drift-specialist-cicd` from the frozen fleet template on the shared WorkerPool; Ready at the current generation, gVisor ActorTemplate with a golden actor; typed direct A2A requests with pinned answers; deleted by uid | The released image runs as a real SandboxAgent actor (Substrate, gVisor, MCP Gateway, real credentials) and returns the known answers | The A2A Gateway and Coordinator path (their sandbox routes are fixed to the production names); that comes after promotion |
| `mcp-servers-production`, `coordinator-production` | `scripts/ci/promote.py`: drift = 0, render = deployed, image-only diff, no no-op; `helm upgrade --atomic`; readback | The release runs the promoted digest | Alone, nothing about the workflows; see `runtime_verify` |
| `agent-fleet-production` | `scripts/ci/fleet_promote.py`: JSON patch of `spec.byo.deployment.image` with uid and image `test` operations; wait Ready, new generation, golden actor on the new image | Both production SandboxAgents run the promoted digest; ownership and every other field unchanged | — |
| `runtime_verify` (after the last promotion) | `agent_replay.sh status`, `e2e fraud\|drift\|combined`, `negative`, with the target guard (`AGI_ENV` `CTX` = production context) and the reconciliation of Helm revisions and running images | The production stack runs the plan digests and the business workflows pass on it | Agent Registry freshness (§7, registry follow-up) |

## 3. Local verification (offline, no cloud)

Run from the repository root. `uv` comes from the fintech conda environment.

```bash
export PATH=~/miniconda3/envs/fintech/bin:$PATH
python3 -m scripts.ci.catalog validate                       # components, images, release units, groups
uv run --locked --project infra/docker/llm-agent-tooling --python 3.12 --group test \
  python -m pytest -q tests/ci                               # all CI/CD tests (fakes only)
# Real boots of the rehearsal configurations (no cluster): MCP factories need the feast extra,
# the Specialists need the g3-byo-coordinator environment.
UV_PROJECT_ENVIRONMENT=/tmp/v-agent uv sync --locked --project infra/docker/g3-byo-coordinator
CD_REHEARSAL_AGENT_PYTHON=/tmp/v-agent/bin/python uv run --locked --project infra/docker/llm-agent-tooling \
  --extra feast --extra rag-query --group test --python 3.12 python -m pytest -q tests/ci/test_cd_rehearsals.py
# Dry-run of a release chain (prints the commands each unit would run; nothing is applied)
python3 -m scripts.ci.release_dispatch --plan <release-plan.json>
# Dry-run of the streaming campaign through the frozen operator (renders the stack and Jobs)
FEAST_SNAPSHOT_ID=<id> FEATURE_RUN_ID=<id> uv run --locked --project infra/docker/streaming-sinks \
  python -m scripts.ci.streaming_verify --git-sha "$(git rev-parse HEAD)" --evidence-dir /tmp/sv \
  --kube-context <ctx> --image flink-block-a=<digest-ref> --image streaming-sinks=<digest-ref>
```

## 4. Prerequisites the owner must approve (cloud mutations, not done in this batch)

| # | Prerequisite | Why | Where |
|---|---|---|---|
| P1 | Rebuild the Jenkins source snapshot at the reviewed SHA and redeploy the controller | Jobs read an immutable Git bundle; the new Job DSL adds `edai2-cd-coordinator`, `edai2-cd-agent-fleet`, `disableConcurrentBuilds()` and the new parameters | `scripts/ci/render_jenkins_runtime.py`, `infra/kubernetes/jenkins/jenkins.yaml` |
| P2 | Apply the updated Role `jenkins-bounded-workloads` (adds `leases` get/create/delete in `ewallet-cicd`) | Release locks are Leases | `infra/kubernetes/jenkins/rbac/batch-b-isolated.yaml` |
| P3 | Grant the Jenkins GSA `roles/artifactregistry.reader` on the release repository, or run with `REGISTRY_LOOKUP=off` | Image reuse and digest readback read the registry. `off` always builds, does no readback, and records `SKIPPED_BY_PARAMETER`. A rerun of the same SHA then fails, because tags are immutable | Terraform `infra/terraform/gcp/modules/cicd_build` |
| P4 | Cloud Build can run the scan step (runner SA already reads the repository; the scan downloads the Trivy DB) | `Scan images` stage | `scripts/ci/image_scan.py` |
| P5 | Jenkins environment `PROMPT_REGISTRY_URL` reachable from the controller (`ewallet-prompts`) | `prompt-registry-gate`; an empty URL fails the gate | Jenkins global env / job env |
| P6 | Streaming campaign identity: rights in `ml-platform` (Jobs, Deployments, Services, ConfigMaps, `pods/exec` on the Flink JobManager) and `feature-store` (`pods/exec` on `statefulset/redis` for the claim), plus `FEAST_SNAPSHOT_ID` and `FEATURE_RUN_ID` | The frozen operator works in `ml-platform` and claims the `rehearsal-stream` Redis database. Jenkins has none of these rights today | Owner decision D6. Alternative: run `streaming_verify.py --execute` from the operator workstation with the same plan |
| P7 | Production deployer identity (the operator's kube context) and a second Jenkins user for approvals | `PROMOTE_TO_PRODUCTION` records `PRODUCTION_IDENTITY`, the target context name and an approver distinct from the trigger in the handoff; the operator runs it (§8) | Owner decision D1/D2. Jenkins itself never receives production credentials |
| P8 | Runtime verification needs `AGI_ENV` (its `CTX` must equal the production context), the cluster and `agent_replay.sh pf` port-forwards | `runtime_verify.py` reuses the owner-replay helper; the dispatcher refuses a production plan before any lock when the target does not match | `docs/edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md` |
| P9 | Agent release identity `ewallet-cicd-agent-release` and its Roles (SandboxAgent create, patch on the two production names, delete on the two candidate names; WorkerPool/ActorTemplate read; two SA and two Secret names get; MCP Deployments get; kagent port-forward; Leases get/create/update/delete, `update` for renewal) | Candidate and fleet units, under the production approval | `infra/kubernetes/jenkins/rbac/agent-cicd-production.proposed.yaml` (inert) |
| P10 | Substrate may write golden snapshots under `gs://ate-snapshots/ewallet-sandbox/cicd-<sha12>/<execution>/`, and the objects are removed afterwards (exact prefix only) | The candidate's ActorTemplate takes a golden snapshot there | Read-only check of the Substrate identity's bucket rights before the run; cleanup of the prefix (§7) |
| P11 | Credential release `sbx-20261115` still current (`jenkins/cd/sandbox-candidate.contract.json`) | The candidate reuses the production MCP client Secrets; a rotated release refuses with `MISSING_CREDENTIALS` | Update the contract after a rotation |
| P12 | Agent Registry publication after a promotion that changed the Coordinator or fleet image | Registry entries compare image-digest labels; publication overwrites the record at the unchanged tag; CI/CD never publishes | The verdict's `registry.procedure`: capture, dry-run, approved publish, readback, `registry_release.py reconcile` (§8) |
| P13 | Worker nodes can pull from `ewallet-platform/g3-byo-coordinator` (today the fleet runs `g3-byo-feature-rag-specialist`) | The fleet promotion switches repository (identical filesystem layers, §9) | Read-only check of the node pull identity's reader role on the repository |

## 5. Owner rehearsal sequence

**Common parameters for every release job:**
- `EXPECTED_GIT_SHA`: the reviewed SHA.
- `RELEASE_BRANCH=release/<group>`, pushed at that SHA.
- `TARGET_BRANCH`: the merge base used for change detection.
- `DEPLOY_APPROVED=true`, `ENVIRONMENT=gke-cpu`, `KUBE_CONTEXT=in-cluster`.
- `REGISTRY_LOOKUP=required`, or `off` (P3).

**Check before starting:**
- `git rev-parse origin/release/<group>` equals the SHA.
- The Jenkins source snapshot was built from it.

**Run order:**

1. **Row 33.** Run `edai2-cd-rag-pipeline` and approve the input.
   - **Expect:** CI PASS with test counts; Cloud Build digest `rag-ingestion@sha256:…`; scan PASS;
     `rag-pipeline` verdict PASS; the CronJob in `ewallet-cicd` at the digest; no smoke Job, Redis or Pod
     left behind.
2. **Row 34.** Run `edai2-cd-mcp-agents`.
   - **Expect, in this order:**
     - `prompt-registry-gate` PASS;
     - `mcp-servers` PASS, with `feature-rag-mcp` and `drift-mcp` both `tools_missing: []`;
     - `agent-fleet` PASS, with both Specialists `card_status: 200` and the malformed task rejected.
   - **Optionally (P7):** `PROMOTE_TO_PRODUCTION=true` promotes `llm-agent-mcp`, then `platform-status`.
3. **Row 35.** Run `edai2-cd-coordinator`.
   - **Expect:** `mcp-servers` → `agent-fleet` → `coordinator` PASS.
   - **With P7–P13 (production, §7–§8):** Jenkins ends `READY_FOR_OPERATOR_PROMOTION`; the operator
     verifies the handoff and runs the transaction: `agent-fleet-candidate` PASS (both candidates Ready
     with a golden actor, both known answers, both deleted, `production_unchanged: true`) →
     `mcp-servers-production` → `agent-fleet-production` → `coordinator-production` → `status`
     (reconciled; any accepted exception listed), `e2e fraud`, `e2e drift`, `e2e combined`, `negative`
     PASS → `DEPLOYED_PENDING_REGISTRY` → P12 → `registry_release.py reconcile` → `RELEASE_COMPLETE`.
4. **Row 36.** Run `edai2-cd-streaming` (P6).
   - **Expect:**
     - image smoke PASS;
     - campaign `verify-cicd-<sha12>-<build>`: render → preflight `claim=CLAIMED` → registry apply → deploy
       → run → collect `verification=PASS`;
     - verdict checks: report run id = namespace, release SHA, offline rows > 0, parity comparisons > 0
       with 0 mismatches, Flink job not failed;
     - cleanup exit 0.

**Evidence to capture** (screenshots by the owner from the real run):
- Jenkins Stage View and console for each job.
- Archived `ci-verdict.json`, `image-manifest.json`, `image-scan.json`, `release-plan.json`,
  per-unit `verdict.json` and `release-verdict.json`.
- Row 34/35 with promotion: `helm history` showing the new revision, and the runtime verification
  verdicts.
- Row 36: the consistency report and the Flink job overview.

## 6. Failure handling, rollback, cleanup, known gaps

**Failure handling and rollback:**
- **Rehearsal failure.** The failing unit restores its previous workload, or removes a first
  deployment. Later units do not run. Locks are released, and the release failure stays the reported
  error.
- **Promotion failure** (details and states in §8).
  - `helm --atomic` restores a failed upgrade; a failed, timed-out or lost upgrade is still inspected
    (`UPGRADE_ATTEMPTED`) and rolled back if the revision moved.
  - A failure after an upgrade (readback, a later unit, runtime verification, a verifier crash) rolls
    every possibly changed release and SandboxAgent back, in reverse order.
- **Streaming.** `cleanup --execute` always runs once the campaign has started. Two kinds of data
  remain by design: the campaign's GCS prefixes and the claimed `rehearsal-stream` Redis database.

**Known gaps** (frozen platform or owner decision; not fixable in CI/CD alone):
- **Prompt runtime consumption (row 34).**
  - The BYO Coordinator sends the system prompt fixed in `ai_platform/llm_agent/nl_intake.py`.
  - The Specialists are typed-only.
  - The chart's prompt ConfigMaps are not mounted by BYO agents.
  - CI/CD can prove registry consistency, not consumption. Closing this needs an application change
    (decision D4).
- **Isolated Agent staging (Option A).** Not possible without frozen-code or production changes:
  the Specialists hard-code the MCP Gateway host, the Coordinator hard-codes and validates the A2A
  Gateway host, the A2A Gateway sandbox routes and the MCP Gateway authorization are production Helm
  values, and the MCP token tables are production server configuration. The candidate SandboxAgents
  (§7) are the real-runtime substitute; the Coordinator path is verified only after promotion.
- **Frozen verdict pins.** `replay_verdict.status` pins the Helm revisions of the last owner campaign.
  `runtime_verify` reconciles a failing `Helm <release>` row only to the revision the transaction
  recorded; every other failing row still fails.
- **Streaming stateful upgrade.** The frozen operator submits the job with `file://` checkpoints and
  no savepoint. A stop-with-savepoint upgrade would need changes to the frozen operator.
- **Jenkins validation.** It covers Groovy syntax (Groovy 4 parse) and text contracts. CPS
  transformation, sandbox approvals and Job DSL API resolution are verified only by a real
  controller.

## 7. Agent Platform release (Batch 2A, offline-implemented)

**Strategy (Option B+).** The candidate runs on the existing runtime, controlled and additive; the
production fleet is promoted only after the candidate passes.
- **Candidate.** Two release-scoped SandboxAgents, `feature-rag-specialist-cicd` and
  `drift-specialist-cicd`, in `ewallet-sandbox` on the shared WorkerPool `business-agents`.
  - Rendered from the frozen fleet template.
  - Only four things differ: name, labels (release SHA, execution id), image digest, snapshot prefix
    `cicd-<sha12>/<execution>/`. `contract_differences` proves nothing else changed (security
    context, network allow-list, ServiceAccount, credentials, WorkerPool reference included).
  - This is NOT isolated staging: the candidates share the production WorkerPool, ServiceAccounts
    and MCP credentials.
  - Every run has a fresh execution id `<yyyymmdd>t<hhmmss>-<6 hex>`: the candidate label, the exact
    snapshot prefix, every A2A request id (`cicd-<execution>-<agent>`) and the evidence directory carry
    it, so a retry of the same SHA never reuses an earlier golden snapshot, idempotency entry or answer.
  - They reuse the production ServiceAccounts and MCP client Secrets.
  - They are verified through the kagent route `/api/a2a-sandboxes/ewallet-sandbox/<candidate>/` and
    then deleted.
- **Approval.** Candidates are objects in a production namespace, so they run only inside the
  approved production stage, first in the plan. One `PROMOTION_APPROVED=yes` covers the candidate,
  the fleet patch and Helm.
- **Fleet promotion.** A guarded JSON patch of `spec.byo.deployment.image` on the two production
  SandboxAgents. No Helm migration. Ownership, labels, the WorkerPool, credentials and every other
  field are untouched.

**Where it runs.** On the operator workstation, from the Jenkins handoff (§8). The dispatcher opens
`kubectl --context <ctx> proxy` once; the locks, the SandboxAgent units and Helm all use that one
identity (P9). The E2E port-forward to the kagent controller is owned by the process: kubectl picks
the local port and the dispatcher reads it from that child's own `Forwarding from` line, so a
pre-existing listener is never mistaken for the controller; the child is terminated and reaped.

**Candidate refusal and failure codes.** Each code names the check that stopped the run.

| Code | Stage | Cause |
|---|---|---|
| `WRONG_TARGET` | Preflight | Not a production stage, or a target namespace other than `ewallet-sandbox` |
| `MISSING_CRD` | Preflight | A CRD is not served |
| `MISSING_WORKER_POOL` | Preflight | The WorkerPool is missing |
| `WORKER_POOL_NOT_READY` | Preflight | The WorkerPool is not gVisor, has 0 workers, or is scaling |
| `MISSING_MCP` | Preflight | `feature-rag-mcp` or `drift-mcp` is not ready |
| `MISSING_PRODUCTION_FLEET` | Preflight | A production SandboxAgent is missing |
| `MISSING_CREDENTIALS` | Preflight | A ServiceAccount or MCP Secret is missing (checked by metadata only) |
| `STALE_CANDIDATE` | Preflight | A candidate is left over from an earlier run (never adopted or overwritten) |
| `IDENTITY_MISMATCH` | Preflight, and again after Ready | Plan SHA ≠ manifest SHA or revision, or the digest/label differs |
| `NOT_ACCEPTED` | Run | kagent rejected the candidate |
| `NOT_READY` | Run | Not Ready at the current generation within 900 s |
| `ACTOR_STARTUP_FAILED` | Run | No gVisor ActorTemplate with a golden actor on the image |
| `E2E_FAILED` | Run | By gate: `TRANSPORT` (`TRANSPORT_ERROR`), `PROTOCOL` (`A2A_TIMEOUT`, `FAILED_TASK`), `SCHEMA` (`INVALID_VERDICT`), `BUSINESS` (`PARTIAL`, `BUSINESS_MISMATCH`, incl. a stale request id), `REGRESSION` (`KNOWN_ANSWER_MISMATCH`) |
| `CANDIDATE_ERROR` | Run | An API error mid-run (for example the second create failed); cleanup still runs |
| `CLEANUP_INCOMPLETE` | Cleanup | Something this execution created cannot be proven gone after 300 s; the verdict lists each kind, name and uid (critical, dominates the status) |
| `PRODUCTION_CHANGED` | Cleanup | A production SandboxAgent's uid, generation or image changed during the run |
| `PRODUCTION_STATE_UNKNOWN` | Cleanup | The production SandboxAgents could not be re-read |

Cleanup runs once anything was created, while mutation authority is proven, and only for what this
execution created: each object is re-read and deleted by uid only if it still has that uid and this
execution's label (a recreated object is `NOT_OURS_LEFT_UNTOUCHED`). A create whose response was lost
is resolved by that label. Cleanup is idempotent (gone counts as deleted) and retries API errors until
its deadline.

Mutation authority. Every candidate create and every cleanup DELETE (retries included) is preceded by
the release checkpoint (Lease ownership and deployment identity):
- an ordinary failure (not Ready, actor startup, E2E, unexpected response) with verified ownership is
  cleaned up as above;
- once a checkpoint fails (`LOCK_LOST`, `LOCK_OWNERSHIP_UNKNOWN`, `IDENTITY_UNVERIFIED`), no further
  Kubernetes mutation is issued by this run, cleanup included, even if a later check would pass. If it
  fails between two cleanup DELETEs, the next DELETE is not sent. The candidates left are recorded with
  their exact identity (`candidate-cleanup-skipped` in the journal and `cleanup_incomplete` in the
  verdict: execution id, release SHA, name, namespace, uid, the `ewallet.io/cicd-execution` label, the
  last known state, `cleanup_status` `SKIPPED_OWNERSHIP_UNPROVEN` or `DELETE_REQUESTED`, the reason).
  The ownership error stays the primary failure and the release ends `RECOVERY_REQUIRED` without a
  rollback.

**Known answers** (`jenkins/cd/sandbox-candidate.contract.json`). The source is the
p2a-closure replays (same transaction and drift window).
- Feature-RAG: `status ok`, this `request_id`, features, citations, `unavailable []`, `fraud_probability`
  0.0639895560144887, `prompt_revision` `feature-rag-specialist/byo-v1`.
- Drift: `psi` 0.0004795543627041439, `prompt_revision` `drift-specialist/byo-v1`.
- The artifact must be a `g3-orchestration/v1` SpecialistEvidence object with no unknown fields.
- Tolerance: relative 1e-9. The values are float64 results of a deterministic computation on fixed
  inputs and reproduced bit-for-bit on 2026-10-31, 2026-11-02 and 2026-11-16; 1e-9 absorbs only
  platform floating-point noise and is far below business meaning. The candidate runs before any
  production mutation, so a mismatch stops the release and never triggers a rollback.

**Rollback order on any failure.** The Coordinator (`helm rollback`), then the fleet (patch back,
guarded by the new image), then MCP. A rollback error makes the verdict `ROLLBACK_FAILED` and is
listed; it is never hidden.

**Runtime reconciliation.** After the last promotion, `expected_runtime` in the release verdict
records:
- the Helm baseline of both releases plus the revisions this run produced;
- the plan digest each workload must run.

`runtime_verify` accepts a failing frozen `Helm <release>` row only at exactly that revision. It adds
`IDENTITY <ns>/<kind>/<name>` rows read from the same status snapshot.

**Cleanup after a run.**
- The candidates and their ActorTemplates are removed by the run.
- Golden-snapshot objects under exactly `gs://ate-snapshots/ewallet-sandbox/cicd-<sha12>/<execution>/`
  (the verdict's `snapshot_prefix`, also in the journal) may remain. Removing them is an owner cleanup
  of that exact prefix only (P10), never a parent or fleet campaign prefix.
- After `CLEANUP_INCOMPLETE` or `STALE_CANDIDATE`, follow `recovery-plan` (§8): delete the named
  candidate by uid only if it still carries that uid and execution label, and confirm that its
  ActorTemplate is gone before rerunning.

**Side effects the candidate has in production.**
- Two additional golden actors on the shared WorkerPool while it runs.
- One A2A session actor per request.
- Read-only MCP tool calls under the production client subjects. Audit rows carry
  `request_id cicd-<execution>-<agent>`.

**Prompt Management.** `PROMPT_RUNTIME_CONSUMPTION_BLOCKED_BY_FREEZE`.
- The Specialists report a code-pinned `PROMPT_REVISION`; the Coordinator prompt is fixed in
  `nl_intake.py`.
- The candidate checks the reported revision as identity evidence.
- The registry gate proves registry consistency only.

**Agent Registry provenance.** It reuses `ops/llm_agent/registry_catalog.py`; lifecycle in §8.
- Readback is not a rollback gate: its image-digest labels go stale on any promotion.
- CI/CD never publishes.

## 8. Release lifecycle, handoff, recovery and locks (Batch 2A.1)

**Release states** (`verdict` of the production `release-verdict.json`, and the journal's `state`):

| State | Meaning | Dispatcher exit |
|---|---|---|
| `CHECKED_NOT_EXECUTED` | No approval or no `--execute`; every unit checked, nothing changed | 1 |
| `FAILED_NOTHING_CHANGED` | Failed before any production change (candidate cleaned up, or an attempt that provably changed nothing) | 1 |
| `FAILED_ROLLED_BACK` | Failed after changes; every one rolled back and verified (`deployed` / Ready) | 1 |
| `ROLLBACK_FAILED` | A rollback failed, did not become healthy, or met a concurrent change it must not overwrite | 4 (critical) |
| `RECOVERY_REQUIRED` | Lock ownership lost or unverifiable, deployment identity changed, candidate cleanup incomplete, or a Helm release in an ambiguous state (`STATE_UNKNOWN`) | 4 (critical) |
| `DEPLOYED_PENDING_REGISTRY` | Runtime verified; the Agent Registry still describes the previous images | 3 |
| `RELEASE_COMPLETE` | Runtime verified and nothing registered changed, or `registry_release.py reconcile` passed | 0 |

`REGISTRY_RECONCILIATION_FAILED` is the outcome of a reconciliation that did not pass; the runtime
stays as verified (it is healthy and the publication has its own approval).

**Fail-closed rules in the transaction** (`scripts/ci/production_release.py`):
- `--execute` without a runtime verifier is refused before any mutation; a verifier that raises, or
  returns anything but `True`, is a failed verification and enters the rollback path; the original
  failure stays the reported cause.
- Every mutation is preceded by a lock checkpoint and a durable journal entry (atomic rewrite with
  fsync): Helm records `UPGRADE_ATTEMPTED` with the previous revision before `helm upgrade`; the fleet
  records `PATCH_ATTEMPTED` with uid, previous and target image before the patch.
- Rollback acts on attempts, not only confirmed promotions. One Helm policy,
  `promote.rollback_decision`, decides both the automated rollback and the operator recovery plan,
  from the latest Helm revision and status and the deployed manifest fingerprint against the
  pre-mutation snapshot (a SHA-256 of the canonical manifest and the image list; values and Secrets
  are never stored):

  | Decision | Condition | Action |
  |---|---|---|
  | `NOT_CHANGED` | latest = recorded, `deployed`, manifest SHA-256 and images = recorded | none |
  | `RESTORED` | latest = recorded+1 or +2 (`helm --atomic`, or a finished rollback), `deployed`, manifest and images = recorded | none |
  | `ROLLBACK` | latest = recorded+1, `deployed` or `failed`, manifest readable and different, not after a rollback | `helm rollback <release> <recorded>`, then observe again: only `RESTORED` completes it, else `ROLLBACK_FAILED`; Helm's exit code alone never does |
  | `STATE_UNKNOWN` | anything else: `pending-*`/other status, older revision, more than recorded+2 or +2 not restored (concurrent modification), unreadable history or manifest, missing fingerprint, same revision not `deployed` or with another manifest, a rollback that did not restore | no automated or recommended rollback; manual inspection and a separately authorized decision; `RECOVERY_REQUIRED` |

  A promotion never starts on a release whose latest revision is not `deployed`.
- A fleet attempt with the same uid and the target image is
  patched back; with the previous image it is `NOT_CHANGED`; another uid or image is a concurrent change
  that is never overwritten (`ROLLBACK_FAILED`).
- Helm mutations run with a 25-minute process timeout (above Helm's own `--timeout 15m`); a timeout is
  an ambiguous mutation, inspected before any rollback decision.

**Runtime verdict.** `runtime_verify.reconcile_status` accepts a failing frozen row only when it is a
`Helm <release>` revision row whose frozen detail and the snapshot's Helm history both show exactly the
revision this transaction recorded, `deployed`, with the frozen helper's own failure exit (1). Each
accepted exception is listed in `accepted_exceptions` and the check is `PASS_WITH_ACCEPTED_EXCEPTIONS`.
Everything else fails: other failing rows, a missing, malformed or inconsistent verdict, an exit code
other than 0/1, a verdict with no runtime row besides Helm, an empty or wrong identity expectation, a
verdict the command did not (re)write. Business E2E verdicts are never reconciled.

**Jenkins → operator handoff** (`scripts/ci/release_handoff.py`).
- Jenkins (`Prepare operator handoff`) builds the production plan and `handoff/` with `handoff.json`:
  job, build number, URL, tag; Git SHA and release branch; units with dependencies, namespaces and
  digests; target context, production identity, triggering user and production approver; runtime
  checks and the SHA-256 of the contracts; source snapshot identity; recovery entry point; SHA-256 of
  the plan, image manifest, request, CI verdict, scan verdict, Jenkins build record and source metadata.
  The console prints `RELEASE_HANDOFF sha256=<digest>` and `RELEASE_STATUS READY_FOR_OPERATOR_PROMOTION`.
- Trust boundary: SHA-256 is integrity, not a signature. The anchor is the digest read from the
  authenticated Jenkins console (or the archived fingerprint). A compromised controller is out of scope
  and covered by the separate production approval and the operator's review of the plan.
- The operator verifies before anything: the digest, every artifact, the SHA chain (reviewed SHA =
  Jenkins checkout = source snapshot = manifest revision = plan = operator HEAD), the plan against the
  catalog and release units, the target context and identity, approver ≠ trigger, the unit graph (kind,
  dependencies, namespace, digests) = the plan's, and contracts = the operator checkout. Any mismatch
  refuses before any lock.
- The artifacts are checked for meaning, not only checksums (Jenkins refuses to announce a handoff
  that would fail these): `source-metadata.txt` is mandatory (source SHA, the release branch ref at that
  SHA, a well-formed bundle digest); `ci-verdict.json` must be `PASS` for this SHA (`git_sha`, now
  recorded by `run_ci.py`), with test cases executed, no failed command and every changed released
  component tested (`NOT_APPLICABLE`, zero tests or another SHA are refused); `image-scan.json` must
  be `PASS` from the pinned scanner for this SHA with every deployed image scanned at exactly the
  manifest digest (`digest_ref`, now recorded by `image_scan.py`).

```bash
git checkout <git_sha>                                   # operator HEAD must equal the handoff SHA
IDENTITY=system:serviceaccount:ewallet-ci:ewallet-cicd-agent-release   # exactly the handoff's production_identity
export AGI_ENV=<campaign env whose CTX is the production context> REPLAY_DIR=<one dir>
scripts/llm_agent/agent_replay.sh pf
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.release_handoff verify \
  --dir <archived handoff/> --handoff-sha256 <digest from the console> --production-context <ctx> \
  --production-identity "$IDENTITY"
PROMOTION_APPROVED=yes scripts/ci/deploy_release.sh --plan <handoff>/release-plan.json --execute \
  --production-context <ctx> --production-identity "$IDENTITY" \
  --handoff <handoff> --handoff-sha256 <digest> --evidence-dir <operator evidence root>
# after DEPLOYED_PENDING_REGISTRY: follow release-verdict.json registry.procedure, then
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.registry_release reconcile \
  --release-verdict <evidence>/<execution>/release-verdict.json --before <REPLAY_DIR>/registry/readback-before.json \
  --after <REPLAY_DIR>/registry/readback.json --output <evidence>/<execution>/registry-verdict.json
```

**Agent Registry reconciliation** (`scripts/ci/registry_release.py`). Publication overwrites the record
at an unchanged tag (agentregistry `/v0/apply` upserts), so the procedure captures the previous records
first (strict readback), dry-runs, publishes under its own approval, reads back, and reconciles.
`RELEASE_COMPLETE` needs the capture with every changed Agent registered, a strict readback (verdict
PASS, every entry `matches`, rendered at the release SHA) and the changed Agents published at the
release SHA; with the runtime identity rows this proves the registry digests are the plan digests.
Records are never deleted to make a readback pass; a failed publication is restored by re-publishing
the captured previous catalog (previous live values and fleet image are in the journal).

**Recovery after an interruption.** The journal is `<evidence>/<execution>/transaction-journal.json`.

```bash
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.production_release recovery-plan \
  --journal <evidence>/<execution>/transaction-journal.json      # read-only; exit 1 when recovery is required
```

Recovery planning is read-only: it never contacts a cluster. For each unverified Helm attempt it needs
the journal's `helm-upgrade-attempted` event, written durably before `helm upgrade` runs: release,
namespace, previous revision and status, previous manifest SHA-256, previous image references, target
images. Then, optionally with read-only observations:

```bash
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.production_release observe-helm \
  --release <release> --namespace <ns> --context <ctx> [--after-rollback] > observed.json   # helm history + manifest
uv run --locked --project infra/docker/llm-agent-tooling python -m scripts.ci.production_release recovery-plan \
  --journal <journal> --observed observed.json
```

It lists, deterministically, and every mutating action carries the prerequisites (re-establish the
approved identity with `kubectl auth whoami`, take the release lock):
- `lock-and-identity-check` first, when ownership or identity was unproven;
- per Helm attempt: `helm-observe-then-decide` (no observation: the read-only commands and the rules),
  or the decision of `promote.rollback_decision`: `none-helm-unchanged`, `none-helm-restored`,
  `helm-guarded-rollback` (the command plus the post-rollback observation that must be `RESTORED`) or
  `helm-manual-inspection` (`STATE_UNKNOWN`, also for a journal without the fingerprint; never a
  rollback recommendation);
- `sandboxagent-restore-image` (uid, previous and target image; only if uid and target image still
  match) for each unverified fleet attempt;
- `candidate-recovery` for each candidate not proven deleted, with its identity and cleanup status: read
  it, act only if it still has that uid and execution label and its state is understood, then delete
  with a uid precondition (never by name alone);
- `gcs-remove-exact-prefix` for the execution's snapshot prefix; `verify-runtime` for any unfinished or
  failed state; `registry-publication` for `DEPLOYED_PENDING_REGISTRY`.

**Deployment identity** (`scripts/ci/deploy_identity.py`). Two conditions, both before any lock or
mutation, and identity is re-proven at every checkpoint:
- A, approved: the handoff's `production_identity` is an exact Kubernetes principal
  (`system:serviceaccount:<ns>:<name>` or a user e-mail) and the operator's `--production-identity`
  equals it (required, never optional).
- B, authenticated: the explicit context authenticates as that principal, by the read-only
  `SelfSubjectReview` (`authentication.k8s.io/v1`, `v1beta1` only if `v1` is not served) on two views:
  the `kubectl proxy` client that also takes the locks and changes the SandboxAgents, and a fresh
  `kubectl --context <ctx> auth whoami`, which re-reads the kubeconfig the way every Helm call does
  (a context edited or switched after preflight is caught at the next checkpoint).
- No fallback: no Jenkins token, no current context, no default credentials, no `--api` override in
  production. Any failure refuses before mutation; during a release it stops it as
  `RECOVERY_REQUIRED` (`IDENTITY_UNVERIFIED`) without an automatic rollback.

**Locks.** Ownership has three states, and only the first authorizes a mutation (a rollback included):

| State | When | Effect |
|---|---|---|
| verified | Lease read, holder and resourceVersion are this holder's, not expired, renewal confirmed | continue |
| `LOCK_LOST` | Lease gone, held by someone else, rewritten since this holder wrote it, expired, or a 409 on renewal | stop, no rollback, `RECOVERY_REQUIRED` |
| `LOCK_OWNERSHIP_UNKNOWN` | any Lease API failure (HTTP 5xx/4xx, timeout, refused connection) or a renewal whose response was lost | stop, no rollback, `RECOVERY_REQUIRED` |

An API outage is not proof that the lock was released. Transport failures of the API client are
`KubeTransportError` (unknown outcome); programming errors are never turned into a lock state. The
journal records `ownership-unproven` and `recovery-plan` starts with `lock-and-identity-check`. A
stale holder never deletes a lock another holder acquired; overlapping releases are refused at
acquisition (create-only Lease).

**Checkpoints and lease lifetime.** The Lease lasts 7,200 s and is renewed (with the identity check)
before and after every mutation, before each verification command and before any rollback. The
longest interval between two checkpoints is bounded:

| Step between checkpoints | Bound |
|---|---|
| `helm upgrade` / `helm rollback` (`--wait --timeout 15m`, process timeout) | 1,500 s |
| SandboxAgent patch + readiness / golden actor wait (per agent) | 900 s |
| Candidate create + readiness (checkpoint before create and before E2E) | 900 s + API time |
| Candidate E2E (2 tasks) + cleanup | 2 × 240 s + 300 s |
| One `agent_replay.sh` verification command (now bounded) | 1,200 s |
| Kubernetes API call | 60 s |

So no step can outlive the lease (largest 1,500 s < 7,200 s). If a step still exceeds it (a hung
call, a suspended laptop), the next checkpoint finds the Lease expired (`LOCK_LOST`) and nothing
further runs. No background heartbeat is used: it could not recover from a partition and would race
the checkpoints.

## 9. Artifact identity evidence (offline, 2026-11-18)

- The fleet image is `g3-byo-coordinator` from `infra/docker/g3-byo-coordinator/Dockerfile`, which
  copies `ai_platform/llm_agent` (both Specialist modules) into `/workspace` and the locked venv into
  `/opt/venv`, exactly the actor's `PYTHONPATH` and `cmd` (`test_agent_cicd.py` checks this contract).
- Locally cached images built from the same SHA (`21994c03`, `40027507`) in the
  `g3-byo-coordinator`, `g3-byo-feature-rag-specialist` and `g3-byo-drift-specialist` repositories have
  identical filesystem layers; only the image config (labels, timestamps) differs, so the digests differ.
  The SandboxAgent overrides `cmd`/`args`, so the default `CMD` is irrelevant.
- A local build at the release SHA imported both modules with the actor environment
  (`/opt/venv/bin/python`, `PYTHONPATH=/workspace`, no network), running as uid 10001.
- Not provable offline (P13): the worker nodes' pull rights on the `g3-byo-coordinator` repository.

## 10. Batch 2B prerequisites and known runtime limitations (after Batch 2A.2)

- Identity: the approved principal must exist with the P9 Roles (Leases include `update`), and the
  operator's kubeconfig context must authenticate as it (`kubectl --context <ctx> auth whoami`; the
  cluster must serve `SelfSubjectReview`, Kubernetes ≥ 1.27). Not testable offline.
- Jenkins: the controller must run a snapshot whose `source-metadata.txt` is mounted at
  `/opt/ewallet-source` (the production handoff refuses without it), and the CI and scan stages of the
  same build must produce the verdicts with `git_sha` and `digest_ref` (this branch's scripts do).
- Helm: an ambiguous release state (pending, unreadable history) is left to the operator by design;
  `helm rollback` from a pending state is never automated.
- Agent Registry: publication stays a separately approved owner step (P12); records are overwritten at
  the unchanged tag, so the capture before publishing is mandatory.
- Prompt Management: `PROMPT_RUNTIME_CONSUMPTION_BLOCKED_BY_FREEZE` (frozen application).
- Nothing here has run on Jenkins or GKE; every runtime result comes from the Batch 2B rehearsal.
