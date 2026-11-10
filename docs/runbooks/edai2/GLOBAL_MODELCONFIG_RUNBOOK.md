# Global ModelConfig: owner runbook

Design and results: [LLM_PLATFORM §5](../../edai2/llm/LLM_PLATFORM.md#5-global-modelconfig-and-byo-resolver); the earlier detailed report is archived as [GLOBAL_MODELCONFIG.md](../../../archive/legacy-docs/docs/edai2/llm/GLOBAL_MODELCONFIG.md).
Screenshots are captured by the owner into
`docs/evidence/edai2/screenshots/llm/modelconfig/<n>_<name>.png`; nothing in this runbook
generates them.

Every command runs from the repository root on the GKE context. Steps A and D are
read-only. Steps B and C change production and need the owner's approval.

```bash
export KUBECONFIG=<kubeconfig with the --dns-endpoint context>
CTX=$(kubectl config current-context)
NS=ewallet-agents
EV=docs/evidence/edai2/llm/global_modelconfig_$(date -u +%Y%m%d)/owner
mkdir -p $EV
```

## A. Preflight (read-only)

```bash
helm -n default history llm-agent --max 3              # expect the deployed revision (42 on 2026-11-08)
helm -n default get values llm-agent -o yaml > /tmp/llm-agent-values.yaml   # private file, do not commit
helm -n default get manifest llm-agent > /tmp/llm-agent-live.yaml
kubectl -n $NS get agents.kagent.dev,modelconfig
kubectl -n $NS get pods -o custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount > $EV/pods-before.txt

# The chart reproduces the deployed release with the resolver off: expect 0 differing objects.
helm template llm-agent infra/helm/llm-agent-platform -n default -f /tmp/llm-agent-values.yaml > /tmp/off.yaml
python3 scripts/llm_agent/helm_object_diff.py /tmp/llm-agent-live.yaml /tmp/off.yaml --expect-objects 0

# API server addresses for the egress policy: Service ClusterIP and endpoints.
kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}{"\n"}'
kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[*].endpoints[*].addresses[*]}{"\n"}'
```

## B. Promotion step 1: images only (approval required)

Build both BYO images from the exact commit, scan and push them, then let
`release_state.py` prove that only image identity changes. With `byoResolver` off the new
code takes the same env path as today.

```bash
SHA=$(git rev-parse HEAD)          # clean tracked tree required by the build scripts
scripts/llm_agent/build_g3_byo_coordinator.sh $SHA /tmp/coordinator.json
scripts/llm_agent/build_g3_byo_feature_rag_specialist.sh $SHA /tmp/feature-rag.json
scripts/llm_agent/scan_and_push.sh /tmp/coordinator.json
scripts/llm_agent/scan_and_push.sh /tmp/feature-rag.json
C=$(python3 -c 'import json;print(json.load(open("/tmp/coordinator.json"))["images"]["g3-byo-coordinator"]["digest_ref"])')
R=$(python3 -c 'import json;print(json.load(open("/tmp/feature-rag.json"))["images"]["g3-byo-feature-rag-specialist"]["digest_ref"])')

python3 scripts/ci/release_state.py values --release llm-agent --git-sha $SHA \
  --image coordinator=$C --image feature-rag-specialist=$R --output /tmp/promotion.yaml
python3 scripts/ci/release_state.py check --release llm-agent --context $CTX --promotion /tmp/promotion.yaml
PROMOTION_APPROVED=yes python3 scripts/ci/release_state.py promote --release llm-agent \
  --context $CTX --promotion /tmp/promotion.yaml --execute
helm -n default history llm-agent --max 1     # note this revision: it is the rollback target of step C
```

## C. Promotion step 2: enable the resolver (approval required)

```bash
helm -n default get values llm-agent -o yaml > /tmp/llm-agent-values.yaml    # now with the new digests
cat > /tmp/byo-resolver.yaml <<'EOF'
modelConfig:
  byoResolver:
    enabled: true
    apiServer:
      cidrs: [<ClusterIP>/32, <endpoint IP>/32]     # from step A (2026-11-08: 34.118.224.1, 10.20.0.8)
EOF
helm -n default get manifest llm-agent > /tmp/llm-agent-live.yaml
helm template llm-agent infra/helm/llm-agent-platform -n default \
  -f /tmp/llm-agent-values.yaml -f /tmp/byo-resolver.yaml > /tmp/on.yaml
# Expect exactly 5: Agents fraud-coordinator and feature-rag-specialist changed;
# Role, RoleBinding ewallet-global-llm-reader and NetworkPolicy allow-byo-modelconfig-to-kube-apiserver added.
python3 scripts/llm_agent/helm_object_diff.py /tmp/llm-agent-live.yaml /tmp/on.yaml --expect-objects 5 | tee $EV/helm-diff.txt
helm upgrade llm-agent infra/helm/llm-agent-platform -n default \
  -f /tmp/llm-agent-values.yaml -f /tmp/byo-resolver.yaml --dry-run=server > /dev/null
helm upgrade llm-agent infra/helm/llm-agent-platform -n default \
  -f /tmp/llm-agent-values.yaml -f /tmp/byo-resolver.yaml --atomic --timeout 10m
kubectl -n $NS rollout status deploy/fraud-coordinator deploy/feature-rag-specialist --timeout 10m
```

A pod that cannot resolve the CR never turns Ready, so the rollout stops with the old pods
serving and `--atomic` rolls back. Manual rollback:

```bash
helm -n default rollback llm-agent <revision from the end of step B> --wait
```

## D. Evidence capture (read-only, after step C)

| # | Shows | Command |
|---|---|---|
| 1 | live ModelConfig | `kubectl -n $NS get modelconfig ewallet-global-llm -o yaml \| tee $EV/1-modelconfig.yaml` |
| 2 | Declarative drift reference | `kubectl -n $NS get agent drift-specialist -o jsonpath='{.spec.type}{" modelConfig="}{.spec.declarative.modelConfig}{"\n"}' \| tee $EV/2-drift.txt` |
| 3 | BYO configuration source and RBAC | `kubectl -n $NS get deploy fraud-coordinator -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"="}{.value}{"\n"}{end}' \| grep -E 'LLM\|KUBE' \| tee $EV/3-byo-env.txt`; `kubectl -n $NS get role,rolebinding ewallet-global-llm-reader -o yaml \| tee -a $EV/3-rbac.yaml`; `for c in "get modelconfigs.kagent.dev/ewallet-global-llm" "list modelconfigs.kagent.dev" "get secrets"; do echo "$c: $(kubectl auth can-i $c -n $NS --as=system:serviceaccount:$NS:fraud-coordinator)"; done \| tee $EV/3-can-i.txt` |
| 4 | BYO runtime resolution | `for d in fraud-coordinator feature-rag-specialist; do kubectl -n $NS logs deploy/$d \| grep modelconfig.resolved; done \| tee $EV/4-resolved.txt` (uid must equal step 1's `metadata.uid`), then `python3 scripts/llm_agent/verify_modelconfig_bridge.py --out $EV/4-verify.json` (expect `"all_passed": true`, mechanism `BYO resolver` for both BYO agents) |
| 5 | real Coordinator request through agentgateway | kagent UI → `ewallet-agents/fraud-coordinator` → paste prompt `business-00` from `configs/experiments/fixtures/agent-ab-suite.jsonl`; or Grafana/Tempo trace of the turn showing the agentgateway span |
| 6 | valid tool call | the same kagent turn shows `investigate_transaction` with the transaction id from the prompt; agentgateway access log: `kubectl -n $NS logs deploy/agentgateway --since 10m \| grep ewallet-fraud-llm \| tail -3 \| tee $EV/6-gateway.txt` |
| 7 | no regression | `helm -n default history llm-agent --max 3 \| tee $EV/7-history.txt`; `kubectl -n $NS get agents.kagent.dev,pods \| tee -a $EV/7-health.txt`; drift agent chat in the kagent UI still answers with model `ewallet-fraud-llm` |

## E. Optional: re-run the pre-promotion checks

```bash
# Isolated canary in kagent-pilot (creates and deletes its own objects only).
LLM_AGENT_VALUES=/tmp/llm-agent-values.yaml API_SERVER_CIDRS=<ClusterIP>/32,<endpoint IP>/32 \
  infra/k8s/modelconfig-canary/run_canary.sh up $EV/canary
infra/k8s/modelconfig-canary/run_canary.sh cases $EV/canary/cases
infra/k8s/modelconfig-canary/run_canary.sh down

# Live CR -> resolver -> Coordinator planner -> agentgateway -> CPU vLLM, from the laptop.
kubectl -n $NS port-forward svc/agentgateway 18300:3000 &
T=$(mktemp -d); gke-gcloud-auth-plugin | python3 -c "import json,sys;open(sys.argv[1],'w').write(json.load(sys.stdin)['status']['token'])" $T/token
cp /etc/ssl/certs/ca-certificates.crt $T/ca.crt
EWALLET_LLM_API_KEY="$(kubectl -n $NS get secret agentgateway-client-key -o jsonpath='{.data.api-key}' | base64 -d | tr -d '\n')" \
  python3 scripts/llm_agent/modelconfig_runtime_check.py --gateway-url http://127.0.0.1:18300 \
  --kube-api-host "$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | sed 's#https://##')" \
  --token-dir $T --out $EV/runtime
rm -rf $T; kill %1
```

The runtime check needs the agent Python environment (`infra/docker/g3-byo-coordinator`
lockfile) and sends five requests through the production gateway's token budget.
