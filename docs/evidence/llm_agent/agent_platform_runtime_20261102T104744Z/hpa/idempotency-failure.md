# Multi-replica idempotency gate failure

Two A2A `message/send` requests used the exact same validated payload and
business `request_id=agent-platform-hpa-20261102-001` while revision 25 was at
three Ready replicas per Agent.

Observed:

- First request: HTTP 200, completed in 0.971 s.
- Repeated request: HTTP 200, completed in 0.407 s.
- The second invocation created a new A2A task and executed the Feature-RAG MCP
  sequence again instead of returning the prior business result without side
  effects.
- Coordinator logs show different generated task IDs for the two invocations.
- Feature-RAG logs show a second MCP session and repeated tool calls.

Root cause traced from source and runtime evidence:

- `ProgrammaticCoordinator._completed` is process-local memory.
- Its key is `(session_id, request_id)`.
- `CoordinatorExecutor` passes the A2A SDK-generated `context_id` as
  `session_id`.
- Independent `message/send` calls without a supplied context receive different
  context IDs, so the same business request ID misses the cache even on the
  same replica.
- Across replicas, the process-local cache cannot provide a shared idempotency
  boundary.
- The controller-backed `KAgentTaskStore` persists generated A2A task IDs but
  does not supply a durable business-request idempotency record.

Action:

- Classified as a critical multi-replica consistency failure.
- Rolled Helm release `llm-agent` from revision 25 back to revision 24, creating
  deployed rollback revision 26.
- Substrate deployment was not started, as required by the owner directive.

No business-logic fix was attempted because the approved mutation scope was
HPA/PDB-only and explicitly preserved production Agent business logic.
