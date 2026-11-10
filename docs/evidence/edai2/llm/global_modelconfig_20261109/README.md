# Global ModelConfig: BYO resolver evidence

Authentic outputs of the runs on GKE `ewallet-dev` between 2026-11-08 17:50Z and 18:30Z
(2026-11-09 local time), from branch `feature/global-modelconfig-integration`. Report:
[GLOBAL_MODELCONFIG.md](../../../../edai2/llm/GLOBAL_MODELCONFIG.md). No screenshot here was
generated; owner screenshots go to `docs/evidence/edai2/screenshots/llm/modelconfig/`.
No credential is stored: the canary Secrets held random placeholder keys and were deleted;
the gateway client key was passed through the environment only (checked: it occurs in no
file here).

| Path | What | How it was produced |
|---|---|---|
| `compat/kagent-api.txt` | kagent 0.10.3 Agent CRD fields (declarative has `modelConfig`, byo only `deployment`), live Agents, live ModelConfig | `helm list`, `kubectl get crd agents.kagent.dev`, `kubectl get modelconfig` |
| `canary/00-production-modelconfig.json` | the live production CR copied into the canary | `kubectl -n ewallet-agents get modelconfig ewallet-global-llm -o json` |
| `canary/canary-manifest.yaml` | canary objects rendered from the chart | `infra/k8s/modelconfig-canary/run_canary.sh up` |
| `canary/01-modelconfig.yaml` | canary CR with kagent's real `Accepted` status | same |
| `canary/02-resolved-*.log` | startup resolution in the Coordinator and Feature-RAG ServiceAccounts; denied-egress probe | `kubectl logs` |
| `canary/03-unbound-forbidden.log` | ServiceAccount without RoleBinding: HTTP 403, fail closed | `kubectl logs --previous` |
| `canary/04-canary-objects.txt`, `05-kagent-rendered-deployment.yaml` | kagent-rendered Deployments with the projected token volume | `kubectl get` |
| `canary/06-rbac-can-i.txt` | RBAC matrix for three ServiceAccounts | `kubectl auth can-i --as` |
| `canary/cases/10-16` | model change (before/after restart), unauthorized endpoint, Secret mismatch, deleted CR, restore | `run_canary.sh cases` |
| `canary/99-teardown.txt` | every canary object deleted | `run_canary.sh down` |
| `runtime/summary.json`, `cases.jsonl`, `resolver-stderr.log` | live production CR → resolver → Coordinator planner → agentgateway → CPU vLLM, 5 suite cases | `scripts/llm_agent/modelconfig_runtime_check.py` |
| `production/before.txt`, `after.txt` | `llm-agent` revision and agent pod restarts before and after: unchanged | `helm history`, `kubectl get pods` |
| `production/drift-native-reference.txt` | drift-specialist `Declarative`, `modelConfig=ewallet-global-llm` | `kubectl get agent` |
| `production/verify-current-production.json` | current production: drift native, BYO still on the bridge | `scripts/llm_agent/verify_modelconfig_bridge.py` |
| `production/render-off-identical.txt` | chart with the resolver off = deployed r42 manifest | `helm template` vs `helm get manifest` |
| `production/helm-diff-resolver-on.txt` | the exact production change (5 objects) | object diff of the two renders |
| `production/server-dry-run.txt` | API server accepts the changed Agents and new RBAC/NetworkPolicy (nothing persisted) | `kubectl apply --server-side --dry-run=server` |
