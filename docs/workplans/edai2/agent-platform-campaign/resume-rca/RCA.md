# Session actor resume failure: root cause

Verdicts:

- `ROOT_CAUSE_CONFIRMED`: from the pinned source, the GKE raw timeline and a deterministic
  reproduction on unmodified kagent v0.10.3.
- `FIX_IMPLEMENTED` and `UNIT_TESTED`: the client pacing in `ops/experiments/sandbox_sessions.py`.
- Upstream patch: proposed and tested in isolation, not deployed.
- `GKE_TESTED`: not yet; this is `GKE_APPROVAL_PENDING`.

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
  budget; and GKE (approval pending). The GKE A/B run in the master runbook (section 2.3b)
  is the runtime proof.
