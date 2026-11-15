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
