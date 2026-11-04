# Bounded kagent SandboxAgent / Agent Substrate pilot

This package is an isolated feasibility pilot for kagent `0.10.3` and Agent
Substrate `0.0.9`. Its source and bounded GKE runtime were validated on
2026-11-02. It is not part of the production G3 request path.
It uses one deterministic in-cluster mock model, zero MCP tools, zero production
credentials and exact resource limits. The privileged worker is confined to
the dedicated `kagent-pilot` namespace, so the production `kagent` namespace
keeps its baseline Pod Security policy. The mock Service is `ClusterIP`; the
SandboxAgent may reach only `mock-openai.kagent-pilot.svc.cluster.local`.
The `allowedDomains` CRD field is descriptive in this pinned pairing and is
not treated as the security boundary. A default-deny egress `NetworkPolicy`
on the WorkerPool pod is the enforced boundary. It permits only the mock model
and the named in-cluster DNS, Substrate, kagent and telemetry endpoints needed
by the pilot. The only IP block is the read-back GKE `kube-dns` Service `/32`;
the value must be re-read before applying to another cluster. There is no
public IP or broad CIDR rule.

## Reproducible render

```bash
render_dir="$(mktemp -d /tmp/kagent-pilot-render.XXXXXX)"
infra/kubernetes/kagent-pilot/scripts/render.sh "${render_dir}"
cat "${render_dir}/approval-gate.json"
```

The script verifies all four pinned OCI chart digests, renders the charts,
validates the pilot custom resources against the rendered CRD schemas and
generates a deterministic approval inventory. `approval-gate.json` is expected
to report `BLOCKED_PENDING_OWNER_APPROVAL`; that classification is a successful
safety check, not permission to apply the manifests.

## Approval gate

The owner approved this exact pinned pilot for the 2026-11-02 campaign. Any
future installation still requires fresh approval because the render adds
12 CRDs, five ClusterRoles, five ClusterRoleBindings, a cluster-scoped
`SandboxConfig`, a `ValidatingAdmissionPolicy` and its binding. It also adds the
`atelet` DaemonSet in `ate-system`, which is privileged and mounts a host path.
These are materially broader than the namespace-scoped production G3 runtime
and require separate, explicit owner approval after a fresh semantic diff.

Any future approved pilot must remain isolated from production Secrets, MCP
tools, model endpoints and business data. Before application, recheck available
node headroom, GKE version compatibility, Pod Security admission, storage
classes, every RBAC rule, the exact host path, all image digests and the
rollback plan. Positive tests must prove a deterministic sandbox response and
snapshot/suspend/resume. Negative tests must prove denied external network
from an actual sandbox actor,
denied production Secret access, denied Kubernetes API access and resource
limit enforcement.

## Cleanup and rollback boundary

The pilot uses `kagent-pilot` and `ate-system`, while the existing `kagent`
release remains production-owned. Cleanup is not a blind `helm uninstall`:
capture evidence, rollback `kagent` to the read-back baseline revision, remove
namespaced pilot resources, then remove only cluster-scoped objects whose
ownership and absence of other consumers were revalidated. Persistent volumes
and CRDs must never be deleted automatically. Production Secrets, G3 Agents
and MCP resources are out of scope.
