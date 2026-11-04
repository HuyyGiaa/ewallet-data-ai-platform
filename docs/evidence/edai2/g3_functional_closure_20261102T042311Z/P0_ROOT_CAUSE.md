# G3 functional closure: P0 root-cause checkpoint

Timestamp: `2026-11-02T04:23:11Z`
Source HEAD inspected: `8a4e6bb500905a655cd7c2089894a4cddf98fdd2`
Cloud readback: `llm-agent` revision 12, `llm-agent-mcp` revision 3,
`kagent` revision 2.

This checkpoint was written before changing runtime source. It distinguishes
observed evidence from the proposed correction.

## P0-1: transaction loss and unsolicited routing

Observed:

- A fresh Coordinator context `01a199aa-cc6a-7797-8806-f3fa02053887`
  received the complete eleven-field transaction.
- Its native kagent Agent-tool call emitted only
  `feature lookup for transaction: {transaction_id}, fraud_scoring, and drift detection`.
- The live Coordinator prompt says to preserve the transaction and select only
  requested capabilities, but the kagent Agent tool exposes a single free-form
  `request` string. The typed `AgentDelegationRequest` and
  `CoordinatorOrchestrator` source contracts are not executed on this live
  declarative path.

Root cause:

1. Mandatory transaction propagation and capability selection are currently
   delegated to the small language model rather than a validated boundary.
2. The Coordinator prompt does not define a machine-checkable input envelope,
   an exact copy rule, or an explicit capability-to-agent routing table that
   rejects unrequested capabilities.
3. Consequently `{transaction_id}` was a model-produced literal placeholder,
   and `drift detection` was a model-added capability. This was not stale pod
   state: it occurred in a new context after the Coordinator rollout.

Required correction:

- Introduce one canonical, typed business-request envelope and a deterministic
  serializer used by the reproducible A2A client.
- Version the Coordinator prompt so the complete canonical envelope is copied
  verbatim into exactly one native Agent-tool request and routing is determined
  only by the explicit capability list.
- Validate the contract locally and fail before submission when the transaction
  is incomplete, a capability is unsupported, or drift was not requested.
- Keep kagent and the A2A Gateway as the runtime/delegation path; do not add a
  second orchestration framework or a direct Specialist route.

Residual limitation to prove at runtime: kagent 0.10.3 represents a delegated
Agent as a free-form string tool. Source validation can make the ingress and
expected request deterministic, but only a real trace can prove that this
runtime/model copied it correctly. A mismatched trace remains a failure, not a
reason to silently repair data downstream.

## P0-2: Specialist context overflow

Observed:

- In context `01a1992d-2afb-704b-b464-649adafba4c9`, Feast and KServe both
  executed successfully.
- The final Specialist model call failed with `4097 > 4096` input tokens.
- A later independent Coordinator task reused the same Specialist context ID.
- The compact `feature_lookup` result alone is 4,001 characters (about 1,000
  tokens by the conservative character estimate); it contains 31 nested
  features and repeated timestamps. `fraud_scoring` adds 804 characters, in
  addition to the 1,697-character system prompt, input, tool schemas, tool
  calls, prior failures and results.
- The Coordinator Agent-tool entries do not set the CRD's supported
  `isolateSessions` field.

Root cause:

1. Native Agent-tool delegations reuse Specialist session history by default.
2. Large, valid tool results accumulate with earlier invalid-envelope attempts
   in that reused session.
3. The final synthesis call therefore crossed the real 4,096-token model
   ceiling. The model capacity itself is not the defect.

Required correction:

- Set `isolateSessions: true` on every Coordinator-to-Specialist Agent tool so
  each delegation has a fresh bounded Specialist context.
- Make the business request select only the online features required for the
  decision while preserving full-feature retrieval/scoring behavior in the
  underlying tools. Fraud scoring still uses its canonical feature contract.
- Add chart and contract regressions proving session isolation, explicit
  routing and a bounded serialized request/result budget.
- Do not change the model, context window, Feast schema, KServe model, MCP
  credentials, Gateway security or healthy direct tool behavior.

## P0-3: task left in `working`

Observed:

- The corrupted Agent-tool request entered `working` and did not reach a
  terminal response before the client/readback window ended.
- The same live stack has reached `completed` for the prior request whose
  Specialist returned a terminal overflow error.

Conclusion:

The available evidence does not prove a separate task-store defect. The
non-terminal task follows the malformed delegation and a long-running reused
Specialist session. The first correction is fresh Specialist sessions plus a
bounded request and an explicit client deadline/poll budget. A task that still
does not reach `completed`, `failed`, `canceled`, `rejected` or `unknown` within
that budget must be reported as `timeout`, never as success.

## Evidence inputs

- `runtime/coordinator-task-session-refresh-readback.json`
- `runtime/coordinator-business-response-adapter.json`
- `runtime/feature-rag-specialist-adapter-e2e.log`
- `runtime/fraud-coordinator-session-refresh.log`
- `runtime/direct-flat-mcp-sdk-result.json`
- Live readback of the three `Agent` resources, `ModelConfig`, Helm revisions,
  pods and `RemoteMCPServer/feature-rag-mcp` on 2026-11-02.
