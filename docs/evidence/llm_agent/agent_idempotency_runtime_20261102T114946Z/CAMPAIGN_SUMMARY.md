# Agent durable idempotency runtime closure — 2026-11-02

## Provenance

- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Source HEAD: `fb67076321735218b660a5b246040eee996beaba`
- GKE project/cluster: `ewallet-data-ai-platform-hg26` / `ewallet-dev`
- Final Helm revisions: `llm-agent=30`, `kagent=5`, `substrate=1`,
  `substrate-crds=1`

## Durable idempotency verdict

`RUNTIME_PASS`, with bounded guarantees rather than an exactly-once claim.

- A dedicated authenticated Redis StatefulSet is the shared cross-replica
  authority. It is separate from Feast and RAG data.
- Atomic transitions cover claim, started, completed replay, conflict, stale
  owner rejection and persistent `AMBIGUOUS` state.
- The trusted scope is the configured tenant plus authenticated service
  principal; forwarded identity headers do not define the authority scope.
- Redis uses a PVC, AOF, `appendfsync everysec`, 128 MiB maxmemory and
  `maxmemory-policy noeviction`.
- A Redis or malformed-reply failure is fail-closed.
- A completed result is replay-protected for 24 hours. No protection is
  claimed after that TTL.
- `appendfsync everysec` balances latency and resource cost but retains an
  acknowledged approximately one-second host-loss durability window.
- A post-start crash can be `AMBIGUOUS`; downstream exactly-once effects are
  not claimed. Operator handling is documented in
  `docs/llm_agent/coordinator_idempotency_runbook.md`.
- Redis restart and Coordinator restart both preserved the completed result.

## HPA and business runtime verdict

`RUNTIME_PASS`.

- Three HPA and three PDB resources are live.
- All three Agents run two healthy replicas at rest; bounded load proved
  2-to-3 scale-out and stabilization proved 3-to-2 scale-in.
- Cross-context/cross-replica concurrent replay produced one completed
  business execution and one set of three MCP tool calls.
- Final post-policy G3 request completed with four Feast features, KServe
  `fraud-detector/1`, three RAG citations, exactly three MCP calls and a
  successful A2A Gateway trace.
- Three Agent resources are Ready/Accepted and both RemoteMCPServer resources
  are Accepted.

## Substrate pilot verdict

`RUNTIME_PASS_FOR_BOUNDED_PILOT`, not production promotion.

- Pinned kagent 0.10.3 and Substrate 0.0.9 run in isolated `kagent-pilot` and
  `ate-system` namespaces.
- One privileged gVisor WorkerPool, one deterministic no-tool SandboxAgent,
  RustFS-backed snapshots and suspend/resume were demonstrated.
- An initial security test found that the CRD `allowedDomains` value alone did
  not block external egress: the actor reached `example.com` and received HTTP
  405. That raw evidence is intentionally preserved.
- The closure adds an enforced WorkerPool egress NetworkPolicy. It allows only
  named in-cluster DNS, model, Substrate, kagent and telemetry endpoints. The
  single IP block is the read-back kube-dns Service `/32`.
- After the fix, the real SandboxAgent returned `KAGENT_PILOT_OK`; an external
  SandboxAgent request remained blocked for 89 seconds and the internal router
  ended it with 503, with no external HTTP response. A separate worker-labelled
  pod probe to `example.com` also timed out.
- Production Secret access, Kubernetes pod listing and direct external model
  access remain denied. G3 E2E passed again after this policy was applied.

## Tests and operational closure

- Locked Coordinator/G3 group: 65/65 PASS.
- Runtime configuration and bounded-pilot group: 56/56 PASS.
- Helm lint: one chart linted, zero failures.
- `git diff --check`: PASS.
- Local available RAM at closure: approximately 21 GiB; swap usage: 0.
- No heavy build, scanner, Helm upgrade or kubectl port-forward remains.
- Original GKE Authorized Networks were restored to only
  `113.188.119.38/32`; the temporary campaign `/32` was removed.

Raw runtime evidence remains local in this directory. Only this sanitized
summary and its checksum manifest are intended for Git; raw files may contain
large operational readbacks and must be reviewed independently before any
future publication.
