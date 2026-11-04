# Runtime attempt 3 — bounded correction limit reached

Helm revision: 15

Request `g3-functional-closure-003` reached task
`01a19a8a-78e8-77d5-8a9a-9e12442c2783`, context
`01a19a8a-78e8-7891-8251-30e2e39175f6`, and terminal state
`completed` in 133.123 seconds.

Business result: **FAIL**.

The canonical two-line request preserved the complete validated transaction
and did not contain an unresolved placeholder. The Coordinator nevertheless
returned an ordinary text object of the following shape instead of emitting a
native Agent-tool function call:

```json
{"name":"feature-rag-specialist","arguments":{"request":{"contract_version":"g3-business-request/v1","request_id":"g3-functional-closure-003","target_agent":"feature-rag-specialist"}}}
```

The actual live executor logs expose the registered remote Agent tool as
`ewallet_agents__NS__feature_rag_specialist`, while the model printed the
human-facing Agent name. No A2A Gateway request, Specialist execution, or MCP
business call occurred. The terminal protocol state therefore does not count
as business success.

The bounded campaign's two correction attempts (revisions 14 and 15) are now
exhausted. No additional prompt-only cloud rollout is performed. The remaining
Coordinator blocker is the live model/runtime's failure to select the native
Agent function despite a valid typed request and explicit routing prompt. The
typed request, fail-closed client validation, bounded polling, isolated
Specialist sessions, and existing direct MCP/Feast/KServe/RAG functionality
remain preserved for a subsequent targeted runtime-contract correction.
