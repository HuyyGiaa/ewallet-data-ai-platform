# Business agents on Substrate

These files run the Feature-RAG specialist as a `SandboxAgent` next to an identical BYO `Agent`, in a dedicated namespace, so the two can be compared. Nothing here touches production agents or the pilot, and nothing applies these files automatically.

**No BYO agent has booted in a sandbox yet.** Schema validation and local tests can't prove that; only stage S0 on the cluster can.

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace `ewallet-sandbox`, tokenless `default` account, WorkerPool `business-agents` |
| `arm-baseline.yaml.tmpl` | BYO `Agent` (Deployment on port 8080) |
| `arm-optimized.yaml.tmpl` | `SandboxAgent` (actor on port 80) |
| `s1-access.yaml` | Stage S1 only: adds flows in `ewallet-agents` |

## What kagent requires

These rules come from the kagent v0.10.3 source, and schema validation doesn't catch them. Tests in `tests/experiments/test_sandbox_experiment.py` enforce the first three.

| Rule | Handling |
|---|---|
| A BYO actor needs an explicit command | `cmd` is set to `/opt/venv/bin/python` |
| Readiness is probed on port 80 | `EWALLET_AGENT_PORT=80`, a new setting, so the image must be rebuilt |
| The command is copied verbatim, with no image entrypoint | `PATH` and `PYTHONPATH` are set explicitly |
| Each A2A call needs a `contextId` and `X-User-Id` | The client sends both |
| Each session gets its own actor, suspended after a response | The client measures new and resumed sessions separately |
| Images must be pinned by digest | The renderer refuses anything else |

## Identity

`WorkerPool.template` has no `serviceAccountName` field, so worker pods always run as their namespace's `default` account. A named service account on the pool isn't possible.

The equivalent is a dedicated namespace. `cluster.local/ns/ewallet-sandbox/sa/default` then belongs to this pool alone, it holds no RBAC, and its API token isn't mounted. `s1-access.yaml` admits that identity and nothing wider.

## MCP token

Do not reuse a production credential. Design for a dedicated one:

| Item | Value |
|---|---|
| Credential ID | `sandbox-feature-rag-v1` |
| Subject | `sandbox-feature-rag` |
| Grants | `feature_lookup`, `fraud_scoring`, `knowledge_search`; never `drift_detection` |
| Server side | One more entry in `feature-rag-mcp`'s `EWALLET_MCP_SERVICE_TOKENS` Secret, and one grant in `EWALLET_MCP_TOOL_GRANTS` |
| Client side | Secret `sandbox-mcp-token`, key `value`, in `ewallet-sandbox` |
| Revocation | Set the credential's `status` to `revoked` and restart the server |

**Why it must be dedicated:** Substrate snapshots a running actor to GCS, so a golden snapshot can hold process memory, including this token. Treat the snapshot location as holding the secret: limit bucket access, delete snapshots at cleanup, and revoke the token when the experiment ends.

Generate the value without printing it:

```bash
openssl rand -hex 32 | kubectl --context "$KUBE_CONTEXT" -n ewallet-sandbox \
  create secret generic sandbox-mcp-token --from-file=value=/dev/stdin
```

Changing the server's token table is a production change to `ewallet-mcp` and restarts its pods, so it needs its own approval. `sandbox-llm-key` needs the same treatment. S0 needs neither to be valid: any placeholder value lets the agent boot.

## Stages

Each stage stops at the first failure, and that failure is a valid result.

| Stage | Proves | Changes | Pass condition |
|---|---|---|---|
| S0 boot | The image starts in gVisor and binds port 80 | New namespace only | `SandboxAgent` Ready, golden snapshot written |
| S1 tools | MCP and LLM are reachable through mTLS | Mesh label on the namespace, `s1-access.yaml`, new token | One task returns real tool evidence |
| S2 compare | The two arms measure comparably | None beyond S1 | Two runs per arm, verdict written |

## Open questions only the cluster can answer

1. **Port 80.** The image runs as user 10001, and an unprivileged process can't normally bind below 1024. Whether Substrate honours the image's user is unknown. If S0 fails here, the fix is an image change.
2. **Mesh.** The gateways admit only mTLS identities. Whether a gVisor actor's traffic passes through ztunnel is untested, and S1 depends on it.
3. **Task store.** The controller injects `KAGENT_URL`, and the agent then authenticates with a service-account token that an actor may not have.
4. **Snapshot restore.** Open connections, such as OpenTelemetry's gRPC channel, may not survive a resume.

## Rollback

```bash
scripts/experiments/sandbox_experiment.sh baseline down /tmp/sandbox-down
kubectl --context "$KUBE_CONTEXT" delete -f infra/kubernetes/agent-sandbox/s1-access.yaml --ignore-not-found
kubectl --context "$KUBE_CONTEXT" delete -f infra/kubernetes/agent-sandbox/namespace.yaml
```

Then delete the snapshots under `gs://ate-snapshots/ewallet-sandbox/` and revoke the token.
