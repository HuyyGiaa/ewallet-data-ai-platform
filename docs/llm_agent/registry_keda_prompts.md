# Agent Registry, prompt management and KEDA

## Agent Registry (rubric rows 8, 21, 26, 41)

The rubric links https://aregistry.ai/ (agentregistry). Pinned version:
`infra/helm/agentregistry/versions.env`, 0.4.0. The OCI chart digest is
recorded, and the tarball sha256 equals the GitHub release asset.

- **Catalog only.** The agents are deployed by `llm-agent-platform` as kagent
  SandboxAgents. The registry records what is deployed; it does not deploy.
  The chart's default ClusterRole can write Secrets and Deployments in every
  namespace and manage kagent Agents. It is disabled (`rbac.enabled: false`,
  no token automount) in `infra/helm/agentregistry/values.yaml`.
- `ops/llm_agent/registry_catalog.py` derives `ar.dev/v1alpha1` documents
  from the chart sources:
  - 3 `Prompt` (`<agent>-system`, tag = prompt version, exact text, sha256);
  - 2 `MCPServer` (remote Streamable HTTP URL on the MCP Gateway);
  - 3 `Agent` (tag = agent version, title = business role, `source.protocol:
    A2A`, `instructions` → its Prompt tag, MCP server refs, git SHA and
    rubric rows as labels, `compatibleHarnesses: [kagent]`).
- `publish` posts to `/v0/apply`, then reads back
  `/v0/{agents,prompts,mcpservers}/<name>/<tag>` and compares tags and prompt
  content. It only accepts a local port-forward URL.

**Local runtime evidence (2026-10-31).**
`scripts/llm_agent/registry_local_smoke.sh` ran agentregistry v0.4.0 (server
image digest recorded) with postgres:18 in Docker. The API was bound to
127.0.0.1, with a throwaway password.
- The first run was rejected by the server:
  `instructions require compatibleHarnesses`. The static schema could not show
  that.
- After the fix, all 8 entries were `created`, and every readback matched,
  including prompt content.
- `/v0/agents` lists the three agents at 1.0.0.

Evidence: `docs/evidence/llm_agent/source_review/gateways/registry_local_smoke.json`.

Not done:
- UI screenshot (row 8 and row 41 proof). The UI is served on the same port.
  A screenshot needs a browser session or the in-cluster install.
- In-cluster install behind the authenticated route (row 41 and row 42).

## Prompt management (row 34)

- Source of truth: `infra/helm/llm-agent-platform/prompts/<agent>/<version>.md`
  with a sha256 manifest.
- Deployed: `<agent>-prompt-<version>` ConfigMap via `systemMessageFrom`. The
  render fails if a prompt changes without a version bump.
- Managed and versioned in agentregistry (`Prompt`, tag = version). This is the
  rubric-linked tool, so Langfuse was not added as a second store. Row 34 asks
  for "a tool such as Langfuse"; whether agentregistry Prompts satisfy the
  instructor is a stated acceptance risk.
- Every agent answer carries `prompt_revision`. The SandboxAgent annotation
  and the registry label carry the sha256.

## KEDA (rows 19, 25, 27)

- KEDA 2.21.0 pinned (`infra/helm/keda/versions.env`, chart tarball sha256),
  with operator values limited to `ewallet-agents`.
- One `ScaledObject` per agent targets its Substrate **WorkerPool**
  (`ate.dev/v1alpha1`). The WorkerPool has a scale subresource; SandboxAgent
  and Agent do not (checked against the CRD schemas).
- The WorkerPool has no label selector, so CPU and memory triggers cannot
  work. Triggers used:
  - a warm floor `minReplicaCount: 1`, which is the warm-up/Fleet behaviour of
    row 27;
  - a `cron` business-hours window (2 workers);
  - an optional `prometheus` trigger on A2A request rate. Its query must be
    read back from a2a-gateway metrics first; the chart refuses an empty
    query.
- `fallback.replicas` equals the warm floor.
- Rendered ScaledObjects are validated against the ScaledObject CRD schema
  extracted from the pinned chart.

Not done (runtime): KEDA install; scaling a WorkerPool through `/scale`;
warm vs. cold first-request latency. Runbook R5.
