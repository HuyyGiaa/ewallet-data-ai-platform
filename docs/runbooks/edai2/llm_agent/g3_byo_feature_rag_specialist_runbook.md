# G3 BYO Feature-RAG Specialist deployment and rollback runbook

This runbook changes only `Agent/ewallet-agents/feature-rag-specialist` from
the kagent 0.10.3 Declarative runtime to the deterministic BYO runtime. It
preserves the Agent name, ServiceAccount, resource contract, node placement,
A2A/MCP gateways, Coordinator, Drift Specialist and all security policies.

## Mandatory preflight

1. Work only in the dedicated `codex/llm-g2-llmd-routing` worktree and record
   the full committed source SHA.
2. Read back the current `llm-agent`, `llm-agent-mcp` and `kagent` revisions;
   export the current values and manifest as the exact rollback baseline.
3. Confirm Coordinator, Drift Specialist, A2A Gateway, MCP Gateway, Feast,
   KServe and RAG are healthy. Do not replace healthy components.
4. Confirm the existing Specialist ServiceAccount and immutable credential
   Secret exist. Never print or decode the Secret value.
5. Confirm node capacity for the unchanged 100m/256Mi request and 500m/512Mi
   limit. Do not add a node and do not run build and scan concurrently.

## Locked source and image

```bash
UV=/home/huyy-giaa/miniconda3/envs/fintech/bin/uv
"$UV" lock --project infra/docker/g3-byo-coordinator --check
"$UV" sync --project infra/docker/g3-byo-coordinator --locked
"$UV" run --project infra/docker/g3-byo-coordinator --locked \
  --with pytest==9.0.2 pytest -q \
  tests/llm_agent/test_g3_byo_feature_rag_specialist.py \
  tests/llm_agent/test_g3_byo_coordinator.py \
  tests/llm_agent/test_g3_programmatic_coordinator.py
python -m pytest -q \
  tests/llm_agent/test_agents_chart.py::BYOFeatureRagSpecialistManifestTests
git diff --check
```

Build, import-smoke, scan and push sequentially from a clean committed tree:

```bash
SHA=$(git rev-parse HEAD)
OUT=/tmp/g3-byo-feature-rag-specialist-$SHA/image-digests.json
scripts/llm_agent/build_g3_byo_feature_rag_specialist.sh "$SHA" "$OUT"
SCAN_ONLY=1 scripts/llm_agent/scan_and_push.sh "$OUT"
scripts/llm_agent/scan_and_push.sh "$OUT"
```

Verify the remote digest independently and deploy only the digest reference.

## Semantic diff and controlled rollout

Render from the actual live values. Compare all resources by
`(kind, namespace, name)`. The only permitted Helm source change is
`Agent/ewallet-agents/feature-rag-specialist`; its directly controller-owned
Deployment and Service may roll. No Gateway, policy, Secret, Coordinator,
Drift, MCP, MLOps, IAM/RBAC or node mutation is permitted.

```bash
helm get values llm-agent -n default -o yaml > /tmp/llm-agent-baseline-values.yaml
helm get manifest llm-agent -n default > /tmp/llm-agent-baseline-manifest.yaml
helm upgrade llm-agent infra/helm/llm-agent-platform -n default \
  -f /tmp/llm-agent-baseline-values.yaml \
  --set agents.specialists.feature-rag-specialist.byo.enabled=true \
  --set-string agents.specialists.feature-rag-specialist.byo.image="$DIGEST_REF" \
  --wait --timeout 10m
```

Read back Agent conditions, generated Deployment/Service, remote image ID,
ServiceAccount, Agent Card, pod readiness/restarts and all existing healthy
components. Then use an explicit localhost port-forward and run:

```bash
python -m scripts.llm_agent.g3_byo_specialist_smoke \
  --endpoint http://127.0.0.1:18084/ \
  --request "$EVIDENCE/specialist-request.json" \
  --response "$EVIDENCE/specialist-response.json" \
  --summary "$EVIDENCE/specialist-summary.json"
```

Run the full Coordinator path only after direct Specialist protocol and
security-negative checks pass. Evidence must be redacted and must distinguish
HTTP readiness from actual `tools/call` business completion.

## Rollback

On deployment or security regression, roll back only `llm-agent` to the
revision recorded during preflight:

```bash
helm rollback llm-agent "$ROLLBACK_REVISION" -n default --wait --timeout 10m
kubectl -n ewallet-agents rollout status deployment/feature-rag-specialist --timeout=10m
kubectl -n ewallet-agents get agent feature-rag-specialist -o yaml
```

Reverify the working Coordinator, Gateways, MCP authentication and MLOps.
Never delete controller resources manually or rotate credentials as rollback.
