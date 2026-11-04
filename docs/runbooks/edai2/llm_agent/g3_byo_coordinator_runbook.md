# G3 BYO Coordinator deployment and rollback runbook

This runbook changes only `Agent/ewallet-agents/fraud-coordinator` from
`Declarative` to the kagent 0.10.3 `BYO` type. It preserves the Agent name,
namespace, ServiceAccount, resource contract, node placement, A2A/MCP gateways,
Specialists and all policies.

## Preconditions

1. Work only from `codex/llm-g2-llmd-routing` in the dedicated worktree.
2. Verify the full source SHA and a clean tracked tree.
3. Verify `llm-agent` revision 15 remains deployed. If it is not, recalculate
   the rollback target and semantic diff before continuing.
4. Verify `llm-agent-mcp` revision 3 and `kagent` revision 2 remain healthy.
5. Verify Specialists, A2A Gateway, MCP Gateway, Feast and KServe are healthy,
   and that no critical Coordinator task is active.
6. Confirm the cluster has capacity for the unchanged Coordinator requests and
   limits. Do not add a node.

## Locked source verification

```bash
UV=/home/huyy-giaa/miniconda3/envs/fintech/bin/uv
"$UV" --project infra/docker/g3-byo-coordinator lock --check
"$UV" --project infra/docker/g3-byo-coordinator sync --locked
infra/docker/g3-byo-coordinator/.venv/bin/python -m unittest \
  tests.llm_agent.test_g3_byo_coordinator -v
infra/docker/llm-agent-tooling/.venv/bin/python -m unittest \
  tests.llm_agent.test_agents_chart.BYOCoordinatorManifestTests -v
git diff --check
```

## Build, scan and push

Use a full committed SHA. The scan must pass before any push.

```bash
SHA=$(git rev-parse HEAD)
OUT=/tmp/g3-byo-coordinator-$SHA/image-digests.json
scripts/llm_agent/build_g3_byo_coordinator.sh "$SHA" "$OUT"
SCAN_ONLY=1 scripts/llm_agent/scan_and_push.sh "$OUT"
scripts/llm_agent/scan_and_push.sh "$OUT"
```

Read `digest_ref` from the JSON and independently verify it with Artifact
Registry. Never deploy the mutable tag.

## Render and deploy

Save revision 15 values and manifest first. Render both the declarative and
BYO variants and compare resources by `(kind, namespace, name)`. The only Helm
source resource permitted to differ is
`Agent/ewallet-agents/fraud-coordinator`.

```bash
helm get values llm-agent -n default -o yaml > /tmp/llm-agent-r15-values.yaml
helm get manifest llm-agent -n default > /tmp/llm-agent-r15-manifest.yaml
helm upgrade llm-agent infra/helm/llm-agent-platform -n default \
  -f /tmp/llm-agent-r15-values.yaml \
  --set agents.coordinator.byo.enabled=true \
  --set-string agents.coordinator.byo.image="$DIGEST_REF" \
  --wait --timeout 10m
```

Verify the Agent conditions, controller-generated Deployment and Service,
image ID, ServiceAccount, port 8080, Agent Card, task persistence, Gateway
traffic, negative authorization checks and backend health. Run the existing
local-only `scripts/llm_agent/g3_a2a_smoke.py` through an explicit port-forward.

## Rollback

If a deployment or security gate fails, restore only the verified previous
release and re-check the generated workload and security controls:

```bash
helm rollback llm-agent 15 -n default --wait --timeout 10m
kubectl -n ewallet-agents rollout status deployment/fraud-coordinator --timeout=10m
kubectl -n ewallet-agents get agent fraud-coordinator -o yaml
```

Do not delete controller-generated resources manually. Do not change kagent,
MCP, IAM/RBAC, Secrets, nodes, Redis, GCS or model-serving releases.
