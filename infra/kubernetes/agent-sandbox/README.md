# Business agents on Substrate

These manifests run the production BYO agents as `SandboxAgent` objects in `kagent-pilot`, on the existing gVisor `WorkerPool`. Nothing here is applied by Jenkins or Helm, and production agents stay as they are.

kagent 0.10.3 accepts `type: BYO` on `SandboxAgent`, but it documents the sandbox path mainly for declarative agents. **Whether a BYO agent boots there is unproven**, so the work is staged and each stage stops at the first failure.

| Stage | Proves | Needs | Pass condition |
|---|---|---|---|
| S0 boot | The agent starts in gVisor | `feature-rag-specialist.yaml`, two Secrets | `SandboxAgent` Ready, agent card served |
| S1 tools | It reaches MCP and the LLM | `worker-egress.yaml` | One task returns real tool results |
| S2 A2A | The coordinator calls it | `fraud-coordinator.yaml`, Redis URL Secret, a new A2A route | One end-to-end task completes |

## Secrets

Create these in `kagent-pilot`, each with key `value`: `sandbox-llm-key`, `sandbox-mcp-token`, and for S2 `sandbox-redis-url`.

Issue a new MCP token for the sandbox rather than copying the production one. A copy would put a production credential in a second namespace.

## Known risks

- Worker pods run as SA `default`, and the WorkerPool API cannot change it, so `worker-egress.yaml` admits that identity.
- The worker has a limit of 1.5 CPU and 2 GiB and is shared by every agent placed on it. S2 may need a second WorkerPool.
- The sandbox agent uses the pinned live image. A rebuilt image needs its digest updated here.

## Rollback

```bash
kubectl --context "$KUBE_CONTEXT" delete -f infra/kubernetes/agent-sandbox/
```

This removes only the objects in this directory. The pilot agent and production agents are untouched.
