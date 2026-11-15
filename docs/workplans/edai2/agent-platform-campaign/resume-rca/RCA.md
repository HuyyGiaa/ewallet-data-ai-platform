# Session actor resume failure: root cause

Verdicts:

- `ROOT_CAUSE_CONFIRMED`: from the pinned source, the GKE raw timeline and a deterministic
  reproduction on unmodified kagent v0.10.3.
- `FIX_IMPLEMENTED` and `UNIT_TESTED`: the client pacing in `ops/experiments/sandbox_sessions.py`.
- Upstream patch: proposed and tested in isolation, not deployed.
- `GKE_TESTED` (campaign `agi-20261115T084916Z`, 2026-11-15): the race reproduced (A), and the
  client-side pacing passed on GKE (B2). This verifies the workaround, not a kagent fix: the
  unmodified kagent controller is still vulnerable to unpaced multi-turn requests (section 7).
- New, separate finding: concurrent first-turn restores can exceed kagent's 30 s
  `--substrate-call-timeout` and leave an actor stuck `RESUMING` (section 7.3).
- **`RUNTIME_STABILITY_GKE_PASS`** (campaign `rts-20261115T114235Z`, section 8). The runtime is
  fixed in our pinned builds of kagent v0.10.3 and Substrate v0.0.9 (patches in
  `infra/kubernetes/kagent-pilot/patches/`). Upstream is unchanged. The client pacing (section
  6) is no longer needed.

## 1. What fails

A follow-up A2A message on the same `contextId`, sent immediately after the previous turn
returns, waits 120 s and fails:
`substrate session actor … not reachable via atenet-router: context deadline exceeded`.

## 2. Raw timeline (`gke-agi-20261115T064905Z/ags/`, UTC)

| Time | Component | Event | Observed state | Source |
|---|---|---|---|---|
| 07:14:33 | client | turn 1 sent, contextId `…-drift-specialist-c0-098c10` | — | `AGS-04-sessions.txt` |
| 07:14:36 | Substrate | actor `asr-7ed6e5037bd81306a0bb7c53` created from golden, on worker `…hsnv2` | Resuming@hsnv2 | `AGS-04-status.jsonl` |
| 07:15:03 | client | turn 1 ends (`failed`, `mcp_transport_error`: no MCP Gateway in S0) | — | `AGS-04-sessions.txt` |
| 07:15:03 | client | **turn 2 sent at once**, same contextId | — | `AGS-04-sessions.txt` |
| 07:15:04 | Substrate | turn 1's asynchronous suspend completes | **Suspended**, no pod | `AGS-04-status.jsonl` |
| 07:15:04–07:17:03 | Substrate | **no Resuming**: ResumeActor is never issued | Suspended for 2 min | `AGS-04-status.jsonl` (2 s samples) |
| 07:17:03 | kagent | turn 2 fails, `rpc_error` after 120 s | — | `AGS-04-sessions.txt`, `resume-probe.txt` |

The first divergence is at 07:15:03–07:15:04. The second request arrived while the first
turn's suspend was still running. After that, nothing resumed the actor.

Not available from that run: kagent and atelet debug logs for the individual request.
The next GKE run collects them (section 6).

## 3. Code path (kagent v0.10.3)

1. `go/core/internal/a2a/substrate_sandbox_transport.go`, `RoundTrip`:
   - calls `EnsureSessionActor`, then proxies the request;
   - wraps the response body in `suspendSessionActorOnClose`, so closing the body starts
     `scheduleSuspendSession`;
   - that is a **goroutine** calling `SuspendSessionActor`, so the suspend is asynchronous.
2. `go/core/pkg/sandboxbackend/substrate/agent_actor.go`, `EnsureSessionActor`:
   - calls `ResumeActor` only for SUSPENDED, UNSPECIFIED, PAUSED and PAUSING;
   - treats **RUNNING, RESUMING and SUSPENDING** as "need nothing".
3. `actor_reachability.go`, `waitForActorReachableViaAtenet`:
   - polls for RUNNING plus `GET /health` through atenet-router for 2 minutes;
   - has no path that resumes an actor that became SUSPENDED in the meantime.

A follow-up during the suspend window therefore sees SUSPENDING, skips the resume, and waits
for a RUNNING state that never comes.

The GKE message lacks `actor status …` because the final `GetActor` call uses the expired
wait context and fails, so the generic branch is taken.

The ID derivation (`SandboxAgentSessionActorID`) is deterministic and was ruled out: the
Python port matches 12 of 14 actor IDs that Substrate reported, and the error's actor ID.

## 4. Hypotheses

| | Hypothesis | Verdict | Why |
|---|---|---|---|
| H1 | Client session reuse | **Trigger.** The client is valid A2A: same contextId, new message and task IDs. It sent the follow-up inside the suspend window. | timeline, reproduction |
| H2 | Snapshot restore failure | Rejected | Restore was never attempted: the actor stayed Suspended and no ResumeActor was issued. Golden restores worked for every first message. |
| H3 | Stale router state | Rejected | The router is never reached: the wait needs RUNNING first. |
| H4 | Actor process dead after resume | Rejected | There was no resume. Separately, Substrate checkpoints with `runsc checkpoint` (a freeze), with no signal to the process (`cmd/ateom-gvisor/main.go`, `CheckpointWorkload`). |
| H5 | Network path | Rejected for this failure | The same path serves every first message. |
| H6 | Upstream defect | **Root cause.** kagent `EnsureSessionActor` does not handle an in-flight suspend. | reproduction below |

## 5. Proof

`upstream-test/` holds tests run in `golang:1.26` against the full v0.10.3 tag. They drive the
real `EnsureSessionActor` and `SuspendSessionActor` against a scripted ate-api fake.

- **Unmodified (`run-unpatched.txt`):** with turn 2 sent 50 ms after the suspend starts,
  `ResumeActor` is called 0 times, the actor ends SUSPENDED, and the error is
  `not reachable via atenet-router`. The paced control resumes. All 53 upstream tests pass.
- **Patched (`kagent-0.10.3-resume-during-suspend.patch`, `run-patched.txt`):**
  `settleSuspendingActor` waits, bounded to 1 minute, while the actor is SUSPENDING, then
  resumes. The race now resumes once, 3 cycles resume 3 times, and a stuck suspend fails
  in bounded time. 56 tests pass.

## 6. Fix and limits

- **In this repository (Option 1):** before each follow-up, `sandbox_sessions.py` waits,
  bounded by `--settle-limit` (60 s), until `/api/substrate/status` shows the session's
  actor `Suspended`. `EnsureSessionActor` then takes its working ResumeActor path. If the
  actor does not settle, that context sends nothing further, rather than walking into the
  race. `--no-settle` reproduces the failure for an A/B check.
- **Production Coordinator (P4):** sends exactly one message per member context (no
  retries), so it cannot hit this race. Multi-turn chats (kagent UI) can, when the next
  message arrives within about 2 s.
- **Upstream patch:** proposed only. Running it needs a rebuilt kagent controller image, a
  kagent upgrade, and separate owner approval.
  - The patch closes the SUSPENDING window.
  - A follow-up that arrives while the actor is still RUNNING can still be suspended
    mid-turn by the previous turn's goroutine. A complete upstream fix serializes turns
    per session in the transport.
- **Not tested:** kind, because the full Substrate runtime with gVisor exceeds the laptop
  budget. GKE: see section 7.

## 7. GKE A/B result (campaign `agi-20261115T084916Z`, 2026-11-15)

Profile `agent-sandbox`, 3 CPU nodes, awake 08:51–09:27 UTC. S0 was used: two
SandboxAgents, WorkerPool `business-agents` with two gVisor workers, images from
`images.json`, placeholder MCP credentials. Evidence: `../gke-agi-20261115T084916Z/`.

Under S0 every turn ends `failed` with `mcp_transport_error` (no MCP Gateway). That is
the expected business result and not a success. The verdicts below cover only the actor
lifecycle.

### 7.1 Experiment A: `--no-settle`, drift, 1 context × 2 messages: race reproduced

| Time (UTC) | Event | Source |
|---|---|---|
| 08:57:24 | turn 1: actor `asr-54ccd1cf380fd9292532b24c` Resuming on worker `…tgtpp` | `ags/RESUME-AB-lifecycle.txt` |
| 08:57:48 | golden restore done (18.4 s), task ends `failed` (S0), checkpoint | `logs/worker-tgtpp-rpc.txt` |
| 08:57:49 | turn 2 sent at once; actor Suspended; **no further Resuming** | `ags/RESUME-AB-lifecycle.txt` |
| ≈08:59:50 | turn 2 `rpc_error` after 120.5 s | `ags/RESUME-A.txt` |

This is the same signature as section 2.

### 7.2 Experiment B2: paced, drift, 1 context × 4 messages: PASS

The actor was `asr-62efd060a1d79057ff6018c4` on worker `…qjxtl`. The ID was derived by
`session_actor_id()` and matched the ID in the worker's RPC log.

| Message | `settled_before_send` (wait) | `RestoreWorkload` err (elapsed) | Task | Checkpoint |
|---|---|---|---|---|
| m0 (new, from golden) | — | none (19.2 s) | `failed` (S0), 25.4 s | ok |
| m1 (resume 1) | true (1.42 s) | none (18.0 s) | `failed` (S0), 21.9 s | ok |
| m2 (resume 2) | true (1.41 s) | none (18.0 s) | `failed` (S0), 21.6 s | ok |
| m3 (resume 3) | true (1.43 s) | none (19.1 s) | `failed` (S0), 24.7 s | ok |

Sources: `ags/RESUME-B2.txt`, `ags/RESUME-B2-worker-rpcs.txt`, `ags/RESUME-B2-lifecycle.txt`.

- **Same Actor ID through 3 resumes.** The actor went Suspended → Resuming → Running
  each time.
  - The 1 s sampler caught two of the `Running` windows. The other windows lasted under
    a second; the worker's RPC log has all four restores.
- **Reachable after each resume.** The agent logged `specialist.task_started` for m0–m3,
  so atenet-router delivered every message.
- **No resume timed out.** There was no `rpc_error`.
- **One actor for this context.** No second actor appeared.
- **Worker sharing holds (B3).** One feature-rag context (2 messages, paced) then ran on
  the same worker `…qjxtl`. It restored and checkpointed `asr-eb6d0b7374ab36d200a56d79`
  without errors, so one worker served both agents.
  - Golden actors were excluded from these counts.
  - Sources: `ags/RESUME-B3-*.txt`.

### 7.3 Experiment B as first specified: both agents, 2 contexts × 4 messages: failed, not the race

1. **08:59:59.** Two first turns (c0, one per agent) started at the same moment. Both
   workers were on the same node, `…x6xq`.
2. **About 12 s of setup.** Restore setup took about 12 s (runsc create at 09:00:11),
   against about 6 s when only one restore ran.
3. **The 30 s deadline.** The restores started at 08:59:59 and were cancelled at 09:00:29.7:
   `while waiting for container readyz: readyz cancelled … context canceled`. That is
   30 s after the start, which matches kagent's
   `--substrate-call-timeout` default of 30 s. That timeout is the per-RPC limit for ate-api
   calls (`go/core/pkg/app/app.go:235`), here the blocking `ResumeActor`.
   - Evidence: `logs/worker-restore-rpcs.txt`.
4. **Actors left `RESUMING`.** Substrate kept both actors `STATUS_RESUMING` and still
   assigned to their workers, although the workloads went on to start (uvicorn up at
   09:00:36). Evidence: `ags/RESUME-B-stuck-actors.txt`.
5. **Every later request rejected.** With both workers held, every later request got
   `substrate worker pool has no free workers` within about 0.5 s
   (`ags/RESUME-diag-rpc.txt`).
   - That includes the c1 follow-ups, which were correctly paced:
     `settled_before_send=true`.
   - The c0 follow-ups were withheld by the driver (`not_sent`, status Resuming).

Classification:

- **This is a snapshot restore failure.** The restore was cancelled at the kagent RPC
  deadline under concurrent restores on one node. The result was a worker assignment
  failure.
- **It is not the suspend/resume race.** No resume ran inside a suspend window. The
  Actor IDs were correct, and the router was never reached.

Recovery (no actor was deleted and no shared controller was changed):

- Each stuck actor's worker pod was restarted. These were campaign workers, owned by
  the WorkerPool ReplicaSet.
- Substrate then marked the actor `SUSPENDED` and the worker was free
  (`ags/RESUME-worker-restart.txt`).
- The bounded B2 and B3 runs above followed.

Headroom: a single restore takes about 24–25 s, from the request to the end of the restore
(A: 08:57:24 to 08:57:48; B2 m0: 25.4 s). Against the 30 s deadline that leaves about 5 s.

Remediation, not deployed (each needs an owner decision):

1. Raise kagent's `--substrate-call-timeout`. This is a kagent controller argument: a
   shared-controller change.
2. Spread the WorkerPool's workers across nodes with pod anti-affinity. This is a
   campaign manifest change.
3. Stagger first turns in clients. Coordinator P4 sends one message per member, but in
   parallel.
4. Upstream Substrate: roll a cancelled restore back to `SUSPENDED` instead of leaving
   it `RESUMING` with its worker held.

### 7.4 What remains

- **The upstream race is not fixed.** The kagent 0.10.3 controller on GKE is unmodified,
  so an unpaced follow-up within about 2 s of the previous turn still fails after 120 s.
  The client pacing is verified; the patch in this directory is proposed only.
- **Finalizer cleanup is blocked by stuck actors.**
  - While `asr-0b5fef2d…` was `RESUMING`, kagent's SandboxAgent finalizer looped on
    `suspend actor …: InvalidArgument` (`logs/kagent-finalizer.txt`).
  - Restarting that actor's worker unblocked it.
- **Residual actors**, recorded read-only, none deleted:
  - before cleanup: `ags/RESUME-residual-actors-before-cleanup.txt`;
  - after cleanup: `ags/RESUME-residual-actors-after-cleanup.txt` (`ewallet-sandbox`)
    and `ags/RESUME-golden-after-cleanup.txt`.
  - Deletion needs `RESIDUE_APPROVAL`.

## 8. Runtime fix (PKG-T) and GKE verification (`rts-20261115T114235Z`, 2026-11-15)

Evidence: `../gke-rts-20261115T114235Z/`. Procedure: runbook section 2.3c.

Four things are kept apart below:

- **Upstream**: kagent v0.10.3 (tag `8878c39`) and Substrate v0.0.9 (`136dbb16`). Unchanged.
- **Our modifications**: two patches, recorded with their SHA-256 in `runtime-fix.json`.
- **The deployed runtime**: the PKG-T images and settings.
- **The GKE observations**.

### 8.1 Root causes

| | Defect | Facts (source and GKE) | Layer fixed |
|---|---|---|---|
| F1 | Multi-turn race | kagent suspends in a detached goroutine after each turn. `EnsureSessionActor` treats SUSPENDING and RESUMING as "nothing to do". A follow-up waited 120 s for RUNNING (sections 2–5). | kagent transport and backend |
| F2 | 30 s restore deadline | kagent `--substrate-call-timeout` defaults to 30 s and bounds the blocking `ResumeActor`. Two co-located restores took about 30 s. | kagent setting |
| F3 | Stuck RESUMING, worker leaked | ate-api ran the resume workflow on the caller's context. When the caller gave up after `AssignWorker`, no rollback ran: the actor stayed RESUMING with its worker claimed (`workflow_resume.go`). ateom's failed `RestoreWorkload` cleaned up only the network, so the sandbox kept running (GKE: the app booted 7 s after the cancel). | ate-api and ateom |
| F4 | Placement | The installed WorkerPool CRD (structural) has only `nodeAffinity`, `nodeSelector`, `priorityClassName`, `resources` and `tolerations` under `spec.template`. Pod anti-affinity and topology spread cannot be expressed. | measured, no constraint (8.5) |
| F5 | `httpx`/`pydantic` missing | Both are declared and locked in `infra/docker/llm-agent-tooling`. The tests were run with the conda interpreter instead of `uv run --project …`. The `uv` contract test also scanned an ignored worktree's venvs. | test contract |
| F6 | atelet OOM (found on GKE) | `memorypullcache` holds the flattened image (203 MiB for the business agents) in memory, and `io.ReadAll` needs about 2× that. The 512Mi limit from our own post-renderer OOM-killed atelet three times. Resumes failed with `Unavailable`/`EOF`. | Substrate config |
| F7 | `no free workers` between sessions (found on GKE) | A post-turn suspend holds its worker for 1–2 s. With 2 workers and 2 busy agents, a new session failed immediately; upstream deliberately does not retry. | kagent |

### 8.2 Fixes

**kagent v0.10.3 patch** (`kagent-0.10.3-session-turns.patch`, sha256 `ea032b19…`):

- `pkg/sandboxbackend/substrate/session_turns.go`: per-session turns, keyed by atespace and
  actor ID.
  - A turn owns its actor from before `EnsureSessionActor` until its post-turn suspend has
    finished. If another turn is already waiting, the RUNNING actor is handed over instead.
  - Turns of different sessions never wait for each other.
  - The queue is bounded to 8 waiters and the wait to 10 min, also cut by the request
    context.
  - The suspend, with retries on `Aborted`, is bounded to 2 min. The settle after a failed
    turn is bounded to 6 min, longer than ate-api's 5 min workflow deadline.
  - Idle entries are deleted. If every waiter of a handed-over actor gives up, that actor is
    suspended (reclaim).
- `agent_actor.go`, `bringUp`:
  - SUSPENDING → finish or join the suspend, then resume.
  - RESUMING → `ResumeActor` again; ate-api's workflow is re-entrant.
  - `Aborted` → settle.
  - `no free workers` → wait up to 20 s for a worker (F7), then fail with `ErrNoFreeWorkers`.
  - Session deletion takes the turn.
- `internal/a2a/substrate_sandbox_transport.go`: every request runs inside a turn. The turn
  ends when the response body is closed, or as a backstop when the request context ends.

**Substrate v0.0.9 patch** (`substrate-0.0.9-resume-recovery.patch`, sha256 `0291af52…`):

- ate-api `workflow.go`: the workflow context is detached from caller cancellation. It is
  bounded by `--actor-workflow-deadline` (5 min) and by lock loss.
- ate-api `workflow_resume.go`: any non-crash failure of the restore step (atelet dial,
  workload spec, Run/Restore) rolls the actor back to SUSPENDED and frees its worker.
  - The worker write is version-checked, and only a worker still assigned to this actor is
    freed.
  - The rollback is skipped if the lock was lost, and for PAUSED actors.
  - Crashes keep the upstream semantics (CRASHED, no rollback).
  - Two upstream tests that pinned the old RESUMING contract are updated.
- ateom `sandbox_teardown.go`:
  - A failed or cancelled activation force-deletes the containers it created.
  - A success that its caller never receives is torn down.
  - A stale recorded sandbox is removed before the next activation, with a PID fallback that
    signals only processes whose command line is `runsc`.
  - Every teardown is bounded to 30 s.

**Configuration:**

- `SUBSTRATE_CALL_TIMEOUT=90s`: kagent controller env, set through the chart's
  `controller.env`.
- atelet memory `512Mi:1536Mi`: Substrate post-renderer `--atelet-memory`, F6.
- Campaign WorkerPool `ateomImage`: the patched ateom. `kagent-pilot` keeps the release image.

### 8.3 Timeout hierarchy

From outside in:

| Layer | Bound |
|---|---|
| Coordinator member call | 480 s |
| A2A Gateway sandbox route | 150 s |
| kagent A2A client | none |
| Turn wait | ≤ 10 min, cut by the request |
| `ResumeActor` | **90 s** (was 30) |
| Reachability wait | 120 s |
| Specialist business turn | drift 60 s, feature-rag 120 s |
| ate-api workflow (detached) | 300 s |

Measured on GKE:

- ateom restore alone: 16–18 s.
- atelet restore total: 26.3–27.8 s with two concurrent restores on one node.
- End to end: 28.6–29.5 s on one node, 20–24 s on separate nodes, 36.7 s worst on a
  cold-cache first pull.

Why 90 s:

- It is about 3× the worst co-located restore and 2.4× the cold-cache case.
- It stays below the reachability wait and the 150 s gateway.
- A call that does run out no longer strands anything: the workflow finishes detached, and
  the failed turn suspends the actor.

Limitation: a 90 s resume plus a 120 s feature-rag business turn can exceed the gateway's
150 s. That budget predates PKG-T and is outside this change.

### 8.4 Local tests

- **kagent patch:**
  - 14 transport tests drive the real RoundTrip and backend against a blocking ate-api fake
    with per-actor locks, client deadlines and failure injection. They cover follow-ups
    during RUNNING, SUSPENDING and SUSPENDED, concurrent same-context requests, independent
    sessions, cancellation while waiting and during execution, suspend and resume failure,
    a resume outliving the caller's deadline, 10 rapid cycles, a 120-request stress run and
    the capacity wait.
  - Coordinator tests cover the queue bound, idle cleanup, both reclaim orderings and the
    capacity bound.
  - Everything runs under `-race`.
  - Against unmodified v0.10.3, 9 of the 14 fail
    (`gke-rts-…/build/upstream-kagent-0.10.3-turn-tests.txt`).
- **Substrate patch:**
  - 7 failure-injection tests run on the upstream envtest, miniredis and fake-atelet
    harness: caller deadline mid-restore, deadline then failure, capacity returning, a
    concurrent retry during a detached restore, a dial failure, delete after failure, and
    crash semantics.
  - Against unmodified v0.0.9, 6 fail, all with the GKE symptoms; the crash control passes
    on both (`upstream-substrate-0.0.9-recovery-tests.txt`).
  - 5 ateom teardown tests run as root with a fake `runsc`.
- **Image build:** it runs the suites of every touched module: 8 kagent packages
  (`-short -race`) and 24 Substrate packages (`-race`).
- **Python:** `uv run --locked --project infra/docker/llm-agent-tooling --group test python
  -m pytest -q tests/ci tests/experiments tests/ops/test_cloud_lab.py
  tests/ops/test_cloud_lab_guarded_sleep.py tests/ops/test_r2_terraform.py
  tests/unit/test_uv_dependency_contract.py` passes.
  - Exception: one pre-existing, unrelated failure,
    `test_vllm_bench_session.py::test_only_bench_objects_are_changed`, which fails on HEAD
    too.
  - `uv lock --check` passes for every project.

### 8.5 GKE results

The run used the `agent-sandbox` profile on 3 nodes: awake 11:43–13:52 UTC, sleep
`SLEEP_VERIFIED`. During that window the laptop running the campaign lost power from about
12:23 to 13:04 UTC; the cluster state survived.

| Phase | Result | Evidence (`ags/`) |
|---|---|---|
| G2 single session, paced | First run: 2 of 3 resumes failed with atelet OOM (F6). After the fix: 4 turns, 3 genuine resumes on actor `asr-57cf1a04…`, no `rpc_error`, no atelet restart. | `T-G2-run1-atelet-oom.txt`, `T-G2.txt` |
| G3 rapid unpaced | 16 follow-ups (14 arriving while the previous suspend was in flight) and 8 burst messages, all served; one actor per context. 2 of 4 new sessions failed with `no free workers` (F7, before the capacity fix). | `T-G3.txt` |
| G4 concurrent first turns | 3 rounds × both Specialists on separate nodes: 6 of 6 served, ateom 16–18 s. | `T-G4*.txt` |
| G4b co-located | Both workers pinned to one node (nodeAffinity, then removed): 6 of 6 served, end to end 28.6–29.5 s, atelet total 26.3–27.8 s. | `T-G4b*.txt` |
| G5 forced cancel | Client gave up after 5 s of the first restore. The restore finished detached (17.5 s), kagent's failed turn suspended the actor, and the worker was free with no pod restarted. The same context was then served twice on the same actor. | `T-G5*.txt` |
| G6 bounded load | 2 agents × 3 contexts × 3 unpaced: 18 of 18 served. ate-api refused `no free workers` twice and the capacity wait absorbed both. Peaks: node 1.9 of 3.92 CPU, atelet 490 Mi. | `T-G6*.txt` |
| G7 controller restart | (a) Restart between turns: the next turns were served on the same actor. (b) Pod deleted mid-turn: the client got `RemoteDisconnected`. The restore finished 14 s later, and the terminating controller suspended the actor within its 30 s grace. | `T-G7*.txt` |

Totals over the measured runs:

- 67 turns; 65 had no `rpc_error`, and the 2 that did are the pre-fix F7 cases in G3.
- 28 + 8 messages arrived while an actor was SUSPENDING.
- No context had more than one actor, and no actor was left in a transitional state.
- 5 workers served both agents (sharing).
- Source: `T-report.json`.

**Placement (F4).** Measured CPU requests were 87%, 97% and 53% per node. The pinned pod
could not schedule on the 87% node (`Insufficient cpu`). Co-located workers met the restore
budget, at about 4 s more ateom time. Hard anti-affinity is not expressible in the CRD and is
not needed for correctness. Default scheduling stays.

### 8.6 Limitations and rollback

- **The pinned fork, not upstream, is fixed.** An unmodified kagent v0.10.3 or Substrate
  v0.0.9 still has F1–F3.
- **Single controller process.** Turn state is in memory and assumes one kagent controller
  replica, the chart value `controller.replicas: 1`. A SIGKILL or crash during a turn whose
  restore outlasts the 30 s grace leaves the actor RUNNING without an owner until that
  session's next turn. A start-up reconciliation would close this; it is not implemented.
- **ate-api crash.** If ate-api dies mid-workflow, an abandoned RESUMING actor remains. The
  next turn's re-entrant `ResumeActor` recovers it.
- **Unrebuilt images.** atelet, atecontroller and atenet still run the release images, which
  fail the Trivy gate (Go stdlib and module CVEs). They were not rebuilt because their source
  did not change.
- **Log retention.** ate-api and kagent logs from before the restarts were not retained; the
  sampler's 1 s `ListActors` load also rotates them. The per-cycle evidence is the driver
  rows, the sampler and the worker and atelet logs.
- **Rollback.** `runtime_fix.sh <kagent|substrate> rollback` returns to the recorded
  original revisions: kagent 6 (image `b2572a59…`) and Substrate 3 (ate-api `3c50661b…`,
  atelet 128Mi/512Mi). After rollback the identity gate compares against the original
  render.
