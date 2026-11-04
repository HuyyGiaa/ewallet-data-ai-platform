# G3 MCP credential lifecycle

Status: **SOURCE/STATIC ONLY**. This procedure does not prove GKE recovery.

## Ownership boundary

Helm owns workload and credential **references**. It never owns or renders the
credential values. `ops.llm_agent.g3_mcp_credentials` creates immutable Secret
objects outside the release. It never updates or deletes an existing Secret.
The operator must therefore prepare a release before Helm references it and
must retain every prior Secret until rollback and the approval-gated cleanup
have both closed.

Credential release and server set are separate identifiers:

- `clientRelease` selects the per-subject header Secrets;
- `serverSet` selects the server authentication table;
- a transition server set contains the previous and new release credentials;
- a final server set contains only credentials from the promoted release.

The release identifier is public identity, not token entropy. Tokens use the
Python `secrets` generator and never enter Helm values, rendered YAML, logs or
Git. A non-secret SHA-256 fingerprint binds Secret metadata to the release,
namespace, grants and transition source.

R1A namespace isolation separates credential placement without changing the
credential subjects or grants:

- client and discovery-token Secrets remain in `ewallet-agents`, where official
  kagent 0.10.3 resolves `Agent` and `RemoteMCPServer` `headersFrom` values;
- server authentication tables are created in `ewallet-mcp` and mounted only by
  the isolated MCP server Deployments;
- the first transition reads its legacy server table from `ewallet-agents` in
  memory. Later transition/final operations read their predecessor from
  `ewallet-mcp`.

## State machine

| State | Server set | Client release | Valid callers |
|---|---|---|---|
| `LEGACY_STABLE` | `legacy` | `legacy` | Existing credentials |
| `PREPARED` | no Helm change | no Helm change | Existing credentials |
| `SERVER_TRANSITION` | `r1-transition` | `legacy` | Existing and R1; clients still use existing |
| `CLIENT_PROMOTED` | `r1-transition` | `r1` | Existing and R1 |
| `R1_FINAL` | `r1-final` | `r1` | R1 only |
| `ROLLED_BACK` | `legacy` | `legacy` | Existing credentials |

Preparing an exact complete release again is a metadata-only no-op. A partial
set or fingerprint mismatch fails closed. Recovery never overwrites the
partial set automatically; a human must review its non-secret metadata before
authorizing cleanup and a new release identifier.

## Static preparation and rehearsal

Use an owner-approved lowercase DNS label no longer than 24 characters. The
following first command is non-mutating and prints names/fingerprint only:

```bash
python -m ops.llm_agent.g3_mcp_credentials \
  --context "$KUBE_CONTEXT" \
  --values infra/helm/llm-agent-platform/g3/g3.yaml \
  --release r1-release1 \
  --server-set r1-transition \
  --mode transition \
  --previous-server-set legacy \
  --client-namespace ewallet-agents \
  --server-namespace ewallet-mcp \
  --previous-server-namespace ewallet-agents \
  --dry-run
```

Removing `--dry-run` is a Kubernetes Secret mutation and requires separate R1A-S2
approval. It reads previous authentication tables only in process memory,
validates their subjects/status/token bounds and creates new immutable Secrets
with `kubectl create`. It does not print their values.

Render the two rollout phases separately:

```bash
# Server first: old callers remain compatible throughout the rolling update.
helm template llm-agent infra/helm/llm-agent-platform \
  -f infra/helm/llm-agent-platform/values-gke-cpu.yaml \
  -f infra/helm/llm-agent-platform/values-gke-cpu-g1.yaml \
  -f infra/helm/llm-agent-platform/g3/g3.yaml \
  --set mcpCredentialLifecycle.serverSet=r1-transition \
  --set mcpCredentialLifecycle.clientRelease=legacy

# Promote callers only after every MCP pod uses the transition table.
helm template llm-agent infra/helm/llm-agent-platform \
  -f infra/helm/llm-agent-platform/values-gke-cpu.yaml \
  -f infra/helm/llm-agent-platform/values-gke-cpu-g1.yaml \
  -f infra/helm/llm-agent-platform/g3/g3.yaml \
  --set mcpCredentialLifecycle.serverSet=r1-transition \
  --set mcpCredentialLifecycle.clientRelease=r1-release1
```

Image identity flags required by the chart are omitted from this illustration;
the R1 operator must supply the independently verified immutable digest and
matching Git identity.

## Rollback contract

Before client promotion, rollback restores `serverSet=legacy` and leaves
`clientRelease=legacy`. After promotion, rollback first restores client
references to `legacy`, verifies RemoteMCPServer discovery, then restores the
legacy server set. The original Secrets remain immutable and available, so
rollback never regenerates a deleted token.

No NetworkPolicy, ServiceAccount, CRD, IAM binding or cluster-wide object is
part of a credential phase change.

## Finalization and cleanup

R1A-S1 does not automatically enter `R1_FINAL`. After the promoted release
passes positive, negative, discovery-only and business E2E checks, a separately
approved operation may prepare `r1-final` from `r1-transition` using the same
release identifier and `--previous-server-namespace ewallet-mcp`. The tool
copies only the R1 entries in memory into a new immutable auth table; it does
not regenerate client tokens.

Deletion of `legacy` or `r1-transition` Secrets is not automated. It requires
an explicit owner approval after the rollback window closes, with a retained
inventory of names, fingerprints, rollout timestamps and test evidence. Never
put Secret payloads in that inventory.
