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
  -> Production transaction        production_release.py, units in plan order (§7):
                                   sandbox-candidate (create, verify, always delete) -> helm-image-promotion
                                   (release_state checks, helm upgrade --atomic) -> fleet-image-promotion
                                   (guarded JSON patch) -> runtime_verify (reconciled) -> reverse rollback on failure
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
| P7 | Production deployer identity and a second Jenkins user for approvals | `PROMOTE_TO_PRODUCTION` needs `PRODUCTION_IDENTITY`, `PRODUCTION_KUBE_CONTEXT` and an approver distinct from the trigger | Owner decision D1/D2. Without them, promote with `scripts/ci/release_dispatch.py --plan <production plan> --execute --production-context <ctx>` from the operator workstation |
| P8 | Runtime verification needs `AGI_ENV` (its `CTX` must equal the production context), the cluster and `agent_replay.sh pf` port-forwards | `runtime_verify.py` reuses the owner-replay helper; the dispatcher refuses a production plan before any lock when the target does not match | `docs/edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md` |
| P9 | Agent release identity `ewallet-cicd-agent-release` and its Roles (SandboxAgent create, patch on the two production names, delete on the two candidate names; WorkerPool/ActorTemplate read; two SA and two Secret names get; MCP Deployments get; kagent port-forward; Leases) | Candidate and fleet units, under the production approval | `infra/kubernetes/jenkins/rbac/agent-cicd-production.proposed.yaml` (inert) |
| P10 | Substrate may write golden snapshots under `gs://ate-snapshots/ewallet-sandbox/cicd-<sha12>/`, and the objects are removed afterwards | The candidate's ActorTemplate takes a golden snapshot there | Read-only check of the Substrate identity's bucket rights before the run; cleanup of the prefix (§7) |
| P11 | Credential release `sbx-20261115` still current (`jenkins/cd/sandbox-candidate.contract.json`) | The candidate reuses the production MCP client Secrets; a rotated release refuses with `MISSING_CREDENTIALS` | Update the contract after a rotation |
| P12 | Agent Registry publication after a promotion that changed the Coordinator or fleet image | Registry entries compare image-digest labels; CI/CD never publishes | `ops/llm_agent/registry_catalog.py publish` (dry-run, then `--confirm-live-write`), then `agent_replay.sh registry` |

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
   - **With P7–P11 (production, §7):** from the operator workstation with the plan Jenkins archived:
     `agent-fleet-candidate` PASS (both candidates Ready with a golden actor, both known answers, both
     deleted, `production_unchanged: true`) → `mcp-servers-production` → `agent-fleet-production` →
     `coordinator-production` → `status` (reconciled), `e2e fraud`, `e2e drift`, `e2e combined`, `negative`
     all PASS. Then P12 if the verdict's `registry.status` is `PUBLICATION_REQUIRES_APPROVAL`.
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
- **Promotion failure.**
  - `helm --atomic` restores a failed upgrade.
  - A failure after an upgrade (readback, a later unit, runtime verification) rolls every upgraded
    release back to its recorded revision, in reverse order.
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
  - Only four things differ: name, labels, image digest, snapshot prefix `cicd-<sha12>`.
    `contract_differences` proves nothing else changed.
  - They reuse the production ServiceAccounts and MCP client Secrets.
  - They are verified through the kagent route `/api/a2a-sandboxes/ewallet-sandbox/<candidate>/` and
    then deleted.
- **Approval.** Candidates are objects in a production namespace, so they run only inside the
  approved production stage, first in the plan. One `PROMOTION_APPROVED=yes` covers the candidate,
  the fleet patch and Helm.
- **Fleet promotion.** A guarded JSON patch of `spec.byo.deployment.image` on the two production
  SandboxAgents. No Helm migration. Ownership, labels, the WorkerPool, credentials and every other
  field are untouched.

**Where it runs.** Jenkins has no production identity. Its production stage is refused before any
lock (no `AGI_ENV`). The operator runs the archived, re-verified plan:

```bash
export AGI_ENV=<campaign env whose CTX is the production context> REPLAY_DIR=<one dir>
scripts/llm_agent/agent_replay.sh pf
PROMOTION_APPROVED=yes scripts/ci/deploy_release.sh --plan <release-plan.json> --execute --production-context <ctx>
```

The dispatcher opens `kubectl --context <ctx> proxy` once. The locks, the SandboxAgent units and
Helm all use that one identity (P9).

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
| `E2E_FAILED` | Run | `A2A_TIMEOUT`, `FAILED_TASK`, `INVALID_VERDICT`, `PARTIAL`, `KNOWN_ANSWER_MISMATCH` or `TRANSPORT_ERROR` |
| `CLEANUP_FAILED` | Cleanup | A candidate or its ActorTemplate is still present after 300 s |
| `PRODUCTION_CHANGED` | Cleanup | A production SandboxAgent's uid, generation or image changed during the run |

Cleanup always runs once anything was created.

**Known answers** (`jenkins/cd/sandbox-candidate.contract.json`). The source is the
p2a-closure replays (same transaction and drift window).
- Feature-RAG: `status ok`, this `request_id`, features, citations, `unavailable []`, `fraud_probability`
  0.0639895560144887, `prompt_revision` `feature-rag-specialist/byo-v1`.
- Drift: `psi` 0.0004795543627041439, `prompt_revision` `drift-specialist/byo-v1`.
- The artifact must be a `g3-orchestration/v1` SpecialistEvidence object with no unknown fields.

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
- Golden-snapshot objects under `gs://ate-snapshots/ewallet-sandbox/cicd-<sha12>/` may remain.
  Removing them is an owner cleanup step (P10).
- After `CLEANUP_FAILED`, delete the named candidate by uid and confirm that its ActorTemplate is
  gone before rerunning (otherwise `STALE_CANDIDATE`).

**Side effects the candidate has in production.**
- Two additional golden actors on the shared WorkerPool while it runs.
- One A2A session actor per request.
- Read-only MCP tool calls under the production client subjects. Audit rows carry
  `request_id cicd-<sha12>-<agent>`.

**Prompt Management.** `PROMPT_RUNTIME_CONSUMPTION_BLOCKED_BY_FREEZE`.
- The Specialists report a code-pinned `PROMPT_REVISION`; the Coordinator prompt is fixed in
  `nl_intake.py`.
- The candidate checks the reported revision as identity evidence.
- The registry gate proves registry consistency only.

**Agent Registry provenance.** It reuses `ops/llm_agent/registry_catalog.py`.
- Readback is not a rollback gate: its image-digest labels go stale on any promotion.
- The verdict's `registry` block lists the publication commands (dry-run, then
  `--confirm-live-write`) and the readback for P12. CI/CD never publishes.
