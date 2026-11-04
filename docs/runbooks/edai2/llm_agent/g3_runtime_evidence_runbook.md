# G3 runtime and evidence runbook

This runbook reproduces the read-only G3 checks performed on 2026-11-02. It
does not rebuild images, upgrade Helm releases, rotate Secrets, scale workloads
or write application data.

## Provenance and prerequisites

- Repository worktree: `.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Deployed MCP source: `642553f40cf395fed06eba52d04f6fa0db3fec94`
- Deployed MCP tree: `71447aab9060a8eb8837c291b872da715e88a486`
- MCP image: `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/mcp-tools@sha256:b80c07c76e4d240328804218c46ff66ecc7e4ba45694072f94774c57548fa058`
- GCP project: `ewallet-data-ai-platform-hg26`
- Context: `gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev`
- Locked Python environment: `infra/docker/llm-agent-tooling/.venv`

Before running a check, verify the checkout and local headroom:

```bash
cd ~/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2
git status --short --branch
git show --no-patch --format=fuller 642553f40cf395fed06eba52d04f6fa0db3fec94
free -h
```

The current deployment used Helm revisions `llm-agent=12`,
`llm-agent-mcp=3`, and `kagent=2`. Read them back instead of assuming they are
still current.

## Read-only platform health

```bash
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
run_id="manual-$(date -u +%Y%m%dT%H%M%SZ)"
evidence="docs/evidence/edai2/agents/$run_id/runtime"
scripts/llm_agent/g3_runtime_readback.sh "$evidence"
```

Success means all three Helm releases are deployed, the two isolated MCP
Deployments and the Agent/Gateway Deployments are Ready, Redis is Ready, the
KServe `InferenceService` is Ready, and the two `RemoteMCPServer` resources are
Accepted. The script records SHA-256 hashes and does not read Secret payloads.

## MCP → Feast/KServe or RAG readback

Start a local port-forward:

```bash
kubectl --context "$KUBE_CONTEXT" -n ewallet-agents \
  port-forward svc/mcp-gateway 18082:8080
```

In another terminal, place the Feature-RAG Specialist credential in memory.
Do not print it or save it to evidence:

```bash
export EWALLET_MCP_SERVICE_TOKEN="$(
  kubectl --context "$KUBE_CONTEXT" -n ewallet-agents get secret \
    mcp-token-feature-rag-specialist-feature-rag-mcp-r1-release1 \
    -o jsonpath='{.data.token}' | base64 -d
)"

infra/docker/llm-agent-tooling/.venv/bin/python \
  scripts/llm_agent/g3_mcp_smoke.py \
  --tool knowledge_search \
  --arguments-json '{"query":"account takeover controls","top_k":2}' \
  --evidence "$evidence/knowledge-search.json"
unset EWALLET_MCP_SERVICE_TOKEN
```

The expected result is `is_error=false`, server `feature-rag-mcp`, protocol
`2025-06-18`, two real citations from corpus
`fraud-policy-v1-20261030`, and no fallback. Use the canonical transaction
fixture in this campaign's runtime evidence to repeat `feature_lookup` and
`fraud_scoring`. A scoring PASS must contain 31 historical features, model
`fraud-detector/1`, schema hash
`03c0ea040c59dd5bafd7e649a7cc8067b93f1971f8b0d3dfb2d3571268c43176`,
and a numeric probability and prediction.

## Coordinator A2A request

Start the Coordinator port-forward:

```bash
kubectl --context "$KUBE_CONTEXT" -n ewallet-agents \
  port-forward svc/fraud-coordinator 18083:8080
```

Build the A2A payload from the reviewed typed business request, then run the
bounded client:

```bash
python3 scripts/llm_agent/g3_build_business_request.py \
  --business-request docs/evidence/edai2/g3_functional_closure_20261102T042311Z/business-request.json \
  --output "$evidence/coordinator-request.json"

python3 scripts/llm_agent/g3_a2a_smoke.py \
  --request "$evidence/coordinator-request.json" \
  --response "$evidence/coordinator-response.json" \
  --timing "$evidence/coordinator-timing.json" \
  --poll-timeout 180
```

The client rejects non-canonical JSON, missing transaction fields, unrequested
drift and unbounded feature projections before sending. It polls a returned
task only to a bounded deadline and exits successfully only for terminal state
`completed`; `working` at the deadline is recorded as `timeout`.

HTTP 200 and `completed` only prove transport/task lifecycle success. Inspect
the final artifact and history.
A business PASS also requires a Coordinator function call to the
Feature-RAG Specialist, an A2A Gateway trace, successful `feature_lookup` and
`fraud_scoring` calls through the MCP Gateway, and grounded Feast/KServe data
in the final response. Keep `partial`, `unavailable`, context-limit errors and
missing fields as failures; do not rewrite them as PASS.

## Tests and CI scope

```bash
infra/docker/llm-agent-tooling/.venv/bin/python -m unittest \
  tests.llm_agent.test_phase_d_tools \
  tests.llm_agent.test_phase_d_security \
  tests.llm_agent.test_mcp_sdk_conformance \
  tests.llm_agent.test_mcp_production_authenticator \
  tests.llm_agent.test_phase_d_chart \
  tests.llm_agent.test_agents_chart \
  tests.llm_agent.test_g3_orchestration \
  tests.llm_agent.test_g3_runtime_config -v

python3 -m unittest tests.ci.test_jenkins_contract -v
python3 -m scripts.ci.catalog validate
```

The 2026-11-02 execution produced 163/163 G3 tests and 7/7 Jenkins contract
tests. `coverage`, `hypothesis`, `mutmut`, and `locust` are not present in the
locked tooling environment, so their rubric gates remain unverified. Do not
install them with an uncontrolled `pip install`; add them through the owning
`pyproject.toml` and `uv.lock` in a separate reviewed change.

## Genuine UI evidence

1. GKE Console → Kubernetes Engine → Workloads. Filter namespaces
   `ewallet-agents` and `ewallet-mcp`. Capture the Ready workload list with the
   project and timestamp visible.
2. Run `kubectl -n kagent port-forward svc/kagent-ui 18085:8080`, open
   `http://127.0.0.1:18085`, skip the first-run wizard, and capture the three
   Agent cards plus the Coordinator chat/tool result.
3. Airflow at `http://127.0.0.1:8084` currently has no successful
   `rag_fraud_policy` DagRun. Do not stage a success screenshot.
4. DataHub, Jenkins, Grafana, Tempo and Agent Registry have no live runtime in
   this GKE checkpoint. Their UI evidence is blocked, not pending capture.

The campaign collected genuine GKE and kagent UI views through the authorized
browser, but the browser automation interface could not persist the image
bytes into the repository. Those rubric records therefore remain
`SCREENSHOT_PENDING`; the raw API/log evidence is retained.

## Safe cleanup and rollback

Stop only the local port-forward processes created for evidence capture with
`Ctrl-C`. Leave the GKE workloads running for demonstration. No rollback was
needed. If the MCP release later regresses, the campaign's pre-upgrade target
is `llm-agent-mcp` revision 2; a rollback is a separate cloud mutation and
requires a fresh state readback and owner authorization.
