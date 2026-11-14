# Agent integration (AGI) evidence runbook

Owner-run campaign that proves the integrated Multi-Agent workflow on GKE:

```text
kagent UI → A2A Gateway → fraud-coordinator ── LLM Gateway → vLLM   (one tool-selection call)
                               ├─ A2A Gateway → feature-rag-specialist → MCP Gateway → feature-rag-mcp → Feast (Fraud) · KServe · Feast (RAG)
                               └─ A2A Gateway → drift-specialist      → MCP Gateway → drift-mcp       → baseline + window artifacts → PSI
```

Status when this runbook was written: **SOURCE_IMPLEMENTED and LOCAL_TESTED; every
AGI item below is CLOUD_PENDING.** Nothing in this runbook has been executed on
GKE. The design, contracts and local test results are in
[AGENT_INTEGRATION_CONTRACTS.md](../../edai2/agents/AGENT_INTEGRATION_CONTRACTS.md).

Rules for the whole campaign:

- Stages 0, 2, 3 and 4 are read-only or use only the agents' own read paths.
  Stage 1 changes two Helm releases and creates five namespaced inputs: it
  needs the owner's explicit approval (**A1**). Stage 5 rollback or promotion is
  a second decision (**A2**).
- Nothing here wakes the cluster. Waking is the separate, approved procedure in
  [CLOUD_LAB_RUNBOOK.md](CLOUD_LAB_RUNBOOK.md).
- Nothing writes to Redis (any database), to GCS, to DataHub or to the Agent
  Registry, except the optional registry publish of AGI-13, which needs its own
  approval.
- Never print, paste or commit a Secret value, a token or a connection string.
  `helm get values` output stays in `/tmp` and is never committed.
- Screenshots are taken by the owner from real windows into
  `~/Pictures/Screenshots/`. File names below are conventions, not files to
  generate. A Kubernetes `Ready` state is never evidence that a business tool ran.
- If any PASS rule fails, stop at that step. Do not continue to the next stage.

## 0. Campaign identity

Run everything from the repository root on the GKE context.

```bash
cd ~/Develop/ewallet-data-ai-platform
export KUBECONFIG=~/.cache/ewallet-fs-cloud/kubeconfig     # --dns-endpoint kubeconfig
CTX=$(kubectl config current-context)
CAMPAIGN=agi-$(date -u +%Y%m%dT%H%M%SZ)
EV=~/ewallet-evidence/agents/integration/$CAMPAIGN
mkdir -p $EV/{stage0,stage1,stage2,stage3,stage4,stage5,inputs}
SHA=$(git rev-parse HEAD)
git status --porcelain --untracked-files=no | tee $EV/stage0/git-status.txt   # must be empty
printf '%s\n' "$CAMPAIGN" > $EV/campaign_id.txt
printf '%s\n' "$SHA" > $EV/git_sha.txt
printf '%s\n' "$CTX" > $EV/kube_context.txt
# Python runtimes (uv-locked): agents/MCP client tools and Feast/pyarrow tools.
export PYTHONPATH=$PWD   # scripts/llm_agent/*.py import ai_platform from the repository
AGENT_PY=(uv run --frozen --project infra/docker/g3-byo-coordinator python)
FEAST_PY=(uv run --frozen --project infra/docker/llm-agent-tooling --extra feast python)
```

The campaign identity recorded in the evidence folder:

| Item | Where it is recorded |
|---|---|
| Git SHA, Kubernetes context, campaign ID | `git_sha.txt`, `kube_context.txt`, `campaign_id.txt` |
| Helm chart and overlays | `infra/helm/llm-agent-platform` at `$SHA`; `agi/agents.yaml`, `agi/mcp.yaml`; Helm revisions in `stage1/helm-*.txt` |
| Image digests | `stage1/images/*.json` (from the build and push scripts) |
| Namespaces | `ewallet-agents` (agents, gateways), `ewallet-mcp` (MCP servers), `kagent`, `feature-store` |
| Feast projects and Redis DBs | `stage2/AGI-02-*.json` (`/v1/capabilities` configuration) and `stage0/redis-dbs-before.txt` |
| RAG publication revision | `ewallet_rag_hybrid_20261114t144825`, corpus `fraud-policy-v1-20261030`, identity `b85ae796…` (RAG campaign `hybrid-20261114t151101`) |
| Drift baseline and window revisions | `inputs/drift/describe.json` |
| ModelConfig name and generation | `stage2/AGI-01-modelconfig.yaml` |
| Request IDs and trace IDs | `stage2/*`, `stage3/AGI-*-task.json`, `stage4/AGI-12-*` |

## 1. Integration profile and data decision

The verification campaigns were run independently and are **not one business
dataset**:

| Target | Content | Can the Fraud workflow use it? |
|---|---|---|
| Redis DB 0 | Production `ewallet_fraud_online` (`canonical-100gb-v1/online-20261031-163506`) and production `ewallet_rag` | Yes. The recorded reference transaction `dc58ff4e…` (N10/N11 evidence) was served from it. The MCP server only reads. |
| Redis DB 2 | `fs11-r5gb-20261112`, materialized from snapshot `feast-r5gb-20261111-214544` (rehearsal-5gb) | Only with a transaction whose four entity keys come from the rehearsal-5gb data; none is recorded. |
| Redis DB 3 | RAG Hybrid project `ewallet_rag_hybrid_20261114t144825`, same corpus revision and embedding model as the MCP image | Yes for `knowledge_search`; RAG rows are not keyed by customer. |
| Redis DB 9 | Streaming campaign `verify-fs12-20261114063323`: 26 synthetic keys derived from the campaign namespace | No. Its keys are not customers of any dataset. |

**Recommended profile (`agi-isolated`)**, used by the commands below:

- Fraud features: DB 0, **read-only**, with the recorded reference transaction.
  `feature-rag-mcp` reads it through a separate Secret that names `db=0`
  explicitly, pinned by `EWALLET_FRAUD_ONLINE_DB=0`.
- RAG: DB 3, project `ewallet_rag_hybrid_20261114t144825`, pinned by
  `EWALLET_RAG_ONLINE_DB=3`. This is the first time the agents read the newly
  published Hybrid index instead of production `ewallet_rag`.
- Drift: a baseline (reference window 2026-04-01 → 2026-05-01) and a current
  window (2026-05-01 → 2026-06-01) extracted from the immutable offline snapshot
  `feast-r5gb-20261111-214544` (manifest SHA-256 `3aa7e2f2…`).

The drift result therefore describes the **rehearsal-5gb dataset**, not the
canonical dataset behind DB 0. It is a dataset-level statement and does not
explain the investigated transaction. If Stage 0 (P0.6) finds an offline
snapshot of the canonical dataset, use it instead and record the choice.

Alternative profile: Fraud features from DB 2 with a transaction taken from the
rehearsal-5gb Gold `fact_transactions` table for 2026-05-31 18:00–23:00. Use it
only if such a transaction is selected and checked first with P0.8.

## Stage 0 — Read-only preflight

| Step | Command | PASS rule |
|---|---|---|
| P0.1 cluster awake | `python3 ops/gcp/cloud_lab.py status \| tee $EV/stage0/cloud-lab-status.txt` | cpu-pool has nodes and they are Ready. If asleep: stop and follow CLOUD_LAB_RUNBOOK (separate approval). |
| P0.2 releases | `helm -n default history llm-agent --max 3 \| tee $EV/stage0/helm-llm-agent.txt`; `helm -n ewallet-mcp history llm-agent-mcp --max 3 \| tee $EV/stage0/helm-llm-agent-mcp.txt` | Both last revisions are `deployed`. Note both revision numbers: they are the rollback targets. |
| P0.3 live values (private) | `helm -n default get values llm-agent -o yaml > /tmp/agi-agents-live-values.yaml`; `helm -n ewallet-mcp get values llm-agent-mcp -o yaml > /tmp/agi-mcp-live-values.yaml`; `helm -n default get manifest llm-agent > /tmp/agi-agents-live.yaml`; `helm -n ewallet-mcp get manifest llm-agent-mcp > /tmp/agi-mcp-live.yaml` | Files written. Do not commit them. |
| P0.4 chart reproduces live | `helm template llm-agent infra/helm/llm-agent-platform -n default -f /tmp/agi-agents-live-values.yaml > /tmp/agi-agents-off.yaml && python3 scripts/llm_agent/helm_object_diff.py /tmp/agi-agents-live.yaml /tmp/agi-agents-off.yaml --expect-objects 0 \| tee $EV/stage0/agents-reproduce.txt`, and the same for `llm-agent-mcp` with `-n ewallet-mcp` | 0 differing objects for both. Otherwise the source does not reproduce production: stop. |
| P0.5 workloads | `kubectl -n ewallet-agents get agents.kagent.dev,modelconfig,remotemcpservers,pods -o wide \| tee $EV/stage0/agents.txt`; `kubectl -n ewallet-mcp get pods -o wide \| tee $EV/stage0/mcp.txt` | All pods Running/Ready; `drift-specialist` is `Declarative`. |
| P0.6 Redis databases | `RAG_TARGET=gke RAG_KUBE_CONTEXT=$CTX scripts/llm_agent/rag_runtime_run.sh dbs \| tee $EV/stage0/redis-dbs-before.txt` | DB 0 non-empty; DB 3 has 5 keys owned by `ewallet_rag_hybrid_20261114t144825`. Needs the RAG runner's local runtime (`infra/docker/rag-ingestion/.venv`). |
| P0.7 drift snapshot | `S5=gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/offline/snapshots/rehearsal-5gb-v1/ab-20261110225456-optimized-feat-r261110225459/feast-r5gb-20261111-214544`; `gcloud storage cat $S5/_control/export_manifest.json \| sha256sum \| tee $EV/stage0/snapshot-manifest-sha256.txt`; `gcloud storage ls gs://ewallet-data-ai-platform-hg26-data/features/offline/snapshots/ \| tee $EV/stage0/canonical-snapshots.txt` | Digest `3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca`. Record whether a canonical snapshot exists (section 1). |
| P0.8 RAG registry | `R=gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/ewallet_rag_hybrid_20261114t144825/registry.db`; `gcloud storage ls -l $R \| tee $EV/stage0/rag-registry.txt`; `gcloud storage cat $R \| sha256sum \| tee -a $EV/stage0/rag-registry.txt` | 2,275 bytes (RAG-09). Keep the digest for Stage 1. |
| P0.9 Fraud target pre-check | see below | `verdict: PASS` and 4 features with timestamps. |
| P0.10 API server CIDRs | `kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}{"\n"}' \| tee $EV/stage0/apiserver.txt`; `kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[*].endpoints[*].addresses[*]}{"\n"}' \| tee -a $EV/stage0/apiserver.txt` | One ClusterIP and the endpoint IPs (2026-11-08: `34.118.224.1`, `10.20.0.8`). |
| P0.11 drift-specialist network | `kubectl -n ewallet-agents get networkpolicy -o name \| grep drift-specialist \| tee $EV/stage0/drift-netpol.txt` | Egress to `mcp-gateway` exists (`drift-specialist-to-mcp-gateway`) and ingress from the A2A Gateway. If BYO audit logs are on, egress to `otel-collector:4317` must exist as it does for `feature-rag-specialist`. |

P0.9 (read-only, production MCP path as deployed now):

```bash
kubectl -n ewallet-agents port-forward svc/mcp-gateway 18082:8080 > $EV/stage0/pf-mcp.log 2>&1 &
PF_MCP=$!
python3 -c "import json;print(json.dumps(json.load(open('docs/evidence/llm_agent/agent_platform_runtime_20261102T104744Z/rollback/business-input.json'))['transaction']))" > $EV/inputs/transaction.json
export EWALLET_MCP_SERVICE_TOKEN="$(kubectl -n ewallet-agents get secret mcp-token-feature-rag-specialist-feature-rag-mcp -o jsonpath='{.data.token}' | base64 -d)"
"${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/feature-rag/mcp \
  --tool feature_lookup --expect ok --evidence $EV/stage0/fraud-target-precheck.json \
  --arguments-json "$(python3 -c "import json;print(json.dumps({'transaction':json.load(open('$EV/inputs/transaction.json')),'feature_names':['account_behavior__account_tx_count_1h','account_behavior__account_tx_count_24h','user_behavior__user_avg_amount_30d','merchant_behavior__merchant_tx_count_24h']}))")"
unset EWALLET_MCP_SERVICE_TOKEN
```

Leave the port-forward open for later stages, or stop it with `kill $PF_MCP`.

## Stage 1 — Isolated integration preparation (approval A1)

A1 covers exactly: four images, two Secrets and three ConfigMaps in
`ewallet-mcp`, one `helm upgrade` of `llm-agent-mcp`, one `helm upgrade` of
`llm-agent`. Nothing else.

**1.1 Images** (clean tree at `$SHA`; scan gate before push):

```bash
mkdir -p $EV/stage1/images
scripts/llm_agent/build_g3_byo_coordinator.sh $SHA $EV/stage1/images/coordinator.json
scripts/llm_agent/build_g3_byo_feature_rag_specialist.sh $SHA $EV/stage1/images/feature-rag.json
scripts/llm_agent/build_g3_byo_drift_specialist.sh $SHA $EV/stage1/images/drift.json
echo '{"components": ["feature-rag-mcp", "drift-mcp"]}' > /tmp/agi-mcp-components.json
ARTIFACT_REGISTRY=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform \
  scripts/llm_agent/build_changed_images.sh /tmp/agi-mcp-components.json $SHA $EV/stage1/images/mcp.json
for f in coordinator feature-rag drift mcp; do scripts/llm_agent/scan_and_push.sh $EV/stage1/images/$f.json; done
ref() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"][sys.argv[2]]["digest_ref"])' "$@"; }
C=$(ref $EV/stage1/images/coordinator.json g3-byo-coordinator)
R=$(ref $EV/stage1/images/feature-rag.json g3-byo-feature-rag-specialist)
D=$(ref $EV/stage1/images/drift.json g3-byo-drift-specialist)
M=$(ref $EV/stage1/images/mcp.json mcp-tools)
printf 'coordinator=%s\nfeature-rag=%s\ndrift=%s\nmcp=%s\n' "$C" "$R" "$D" "$M" | tee $EV/stage1/images/digests.txt
```

PASS: four `…@sha256:` references; every Trivy gate passed.

**1.2 Drift artifacts** (GCS read-only; written on the laptop):

```bash
SNAP=~/ewallet-evidence/agents/integration/snapshot-feast-r5gb-20261111-214544
mkdir -p $SNAP/_control
gcloud storage cp $S5/_control/export_manifest.json $SNAP/_control/
gcloud storage cp -r $S5/feat_account_behavior $SNAP/
"${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts baseline --snapshot $SNAP \
  --expect-manifest-sha256 3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca \
  --source-uri $S5 --feature account_behavior__account_tx_count_1h \
  --reference-start 2026-04-01T00:00:00Z --reference-end 2026-05-01T00:00:00Z \
  --baseline-id-prefix bl-r5gb-202604 --bins 10 --min-current-count 100 --thresholds 0.1,0.25 \
  --out $EV/inputs/drift/baselines.json
"${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts window --snapshot $SNAP \
  --expect-manifest-sha256 3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca \
  --source-uri $S5 --feature account_behavior__account_tx_count_1h \
  --start 2026-05-01T00:00:00Z --end 2026-06-01T00:00:00Z --max-rows 20000 --out-dir $EV/inputs/drift/window
"${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts describe --baselines $EV/inputs/drift/baselines.json \
  --window-manifest $EV/inputs/drift/window/window-manifest.json | tee $EV/inputs/drift/describe.json
BASELINE=bl-r5gb-202604.account_tx_count_1h
```

The PSI thresholds `0.1,0.25` are the conventional PSI bands; they are an
owner decision recorded in `baselines.json`. Omit `--thresholds` for an
`unclassified` baseline. PASS: `describe.json` shows `"synthetic": false`,
dataset revision `feast-r5gb-20261111-214544` and the baseline ID above.

**1.3 Inputs in `ewallet-mcp`** (plan first, then `--approve`):

```bash
"${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs redis-secret --context $CTX --source feast-online-redis \
  --db 0 --allow-db0-read --name feast-online-redis-agi-fraud --approve-sensitive-copy
"${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs redis-secret --context $CTX --source feast-online-redis \
  --db 3 --name feast-online-redis-agi-rag --approve-sensitive-copy
"${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs drift-configmaps --context $CTX \
  --baselines $EV/inputs/drift/baselines.json --window-manifest $EV/inputs/drift/window/window-manifest.json
# Read the three plans, then repeat each command with --approve, and publish the RAG registry:
python3 -m ops.llm_agent.g3_platform_inputs registry-configmap --context $CTX --source $R \
  --destination-namespace ewallet-mcp --name feast-registry-rag-agi --sha256 <digest from P0.8>
kubectl -n ewallet-mcp get secret,configmap -l ewallet.io/agi-input=true | tee $EV/stage1/inputs.txt
```

**1.4 MCP release** (`llm-agent-mcp`, first):

```bash
DRIFT_ENV=$EV/inputs/drift/describe.json
pin() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["environment"][sys.argv[2]])' $DRIFT_ENV "$1"; }
MCP_SETS=(-f /tmp/agi-mcp-live-values.yaml -f infra/helm/llm-agent-platform/agi/mcp.yaml
  --set mcpServices.image=$M --set mcpServices.identity.imageDigest=${M#*@}
  --set mcpServices.identity.gitSha=$SHA --set mcpServices.identity.configRevision=$CAMPAIGN
  --set-string mcpServices.services.feature-rag-mcp.env.EWALLET_FRAUD_ONLINE_DB=0
  --set-string mcpServices.services.feature-rag-mcp.env.EWALLET_RAG_ONLINE_DB=3
  --set mcpServices.services.drift-mcp.env.EWALLET_DRIFT_BASELINES_SHA256=$(pin EWALLET_DRIFT_BASELINES_SHA256)
  --set mcpServices.services.drift-mcp.env.EWALLET_DRIFT_WINDOW_MANIFEST_SHA256=$(pin EWALLET_DRIFT_WINDOW_MANIFEST_SHA256)
  --set mcpServices.services.drift-mcp.env.EWALLET_DRIFT_DATASET_REVISION=$(pin EWALLET_DRIFT_DATASET_REVISION))
helm template llm-agent-mcp infra/helm/llm-agent-platform -n ewallet-mcp "${MCP_SETS[@]}" > /tmp/agi-mcp-on.yaml
python3 scripts/llm_agent/helm_object_diff.py /tmp/agi-mcp-live.yaml /tmp/agi-mcp-on.yaml --expect-objects 2 | tee $EV/stage1/helm-mcp-diff.txt
helm upgrade llm-agent-mcp infra/helm/llm-agent-platform -n ewallet-mcp "${MCP_SETS[@]}" --dry-run=server > /dev/null
helm upgrade llm-agent-mcp infra/helm/llm-agent-platform -n ewallet-mcp "${MCP_SETS[@]}" --atomic --timeout 10m
kubectl -n ewallet-mcp rollout status deploy/feature-rag-mcp deploy/drift-mcp --timeout 10m
helm -n ewallet-mcp history llm-agent-mcp --max 2 | tee $EV/stage1/helm-mcp-after.txt
```

PASS: the diff names exactly `Deployment feature-rag-mcp` and `Deployment drift-mcp`
(image, identity, the two Secrets, the two pinned DBs, the RAG project and
registry, the drift environment and two read-only mounts). Anything else: stop.

**1.5 Agents release** (`llm-agent`):

```bash
AGENT_SETS=(-f /tmp/agi-agents-live-values.yaml -f infra/helm/llm-agent-platform/agi/agents.yaml
  --set agents.coordinator.byo.image=$C --set agents.specialists.feature-rag-specialist.byo.image=$R
  --set agents.specialists.drift-specialist.byo.image=$D
  --set 'modelConfig.byoResolver.apiServer.cidrs={<ClusterIP>/32,<endpoint IP>/32}')   # from P0.10
helm template llm-agent infra/helm/llm-agent-platform -n default "${AGENT_SETS[@]}" > /tmp/agi-agents-on.yaml
python3 scripts/llm_agent/helm_object_diff.py /tmp/agi-agents-live.yaml /tmp/agi-agents-on.yaml --expect-objects 6 | tee $EV/stage1/helm-agents-diff.txt
helm upgrade llm-agent infra/helm/llm-agent-platform -n default "${AGENT_SETS[@]}" --dry-run=server > /dev/null
helm upgrade llm-agent infra/helm/llm-agent-platform -n default "${AGENT_SETS[@]}" --atomic --timeout 10m
kubectl -n ewallet-agents rollout status deploy/fraud-coordinator deploy/feature-rag-specialist deploy/drift-specialist --timeout 10m
helm -n default history llm-agent --max 2 | tee $EV/stage1/helm-agents-after.txt
```

PASS: six objects: Agents `fraud-coordinator`, `feature-rag-specialist`,
`drift-specialist` changed; Role, RoleBinding `ewallet-global-llm-reader` and
NetworkPolicy `allow-byo-modelconfig-to-kube-apiserver` added (the same five as
the ModelConfig promotion plus the drift Agent). A BYO pod that cannot resolve
the ModelConfig never turns Ready, so `--atomic` rolls back.

## Stage 2 — Application verification

Port-forwards used from here on (open each once):

```bash
kubectl -n ewallet-agents port-forward svc/mcp-gateway 18082:8080 > $EV/stage2/pf-mcp.log 2>&1 &
kubectl -n ewallet-agents port-forward svc/a2a-gateway 18083:8083 > $EV/stage2/pf-a2a.log 2>&1 &
token() { kubectl -n ewallet-agents get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }
```

### AGI-01 — Global ModelConfig and Agent bindings

1. **Proves:** one ModelConfig serves every LLM-calling agent; the Coordinator and
   Feature-RAG BYO agents resolve it at startup; the Drift Specialist makes no LLM call.
2. **Prerequisites:** Stage 1 done.
3. **Commands:**
   ```bash
   kubectl -n ewallet-agents get modelconfig ewallet-global-llm -o yaml | tee $EV/stage2/AGI-01-modelconfig.yaml
   kubectl -n ewallet-agents get agents.kagent.dev -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,VERSION:.spec.version,MODELCONFIG:.spec.declarative.modelConfig | tee $EV/stage2/AGI-01-agents.txt
   for d in fraud-coordinator feature-rag-specialist; do kubectl -n ewallet-agents logs deploy/$d | grep modelconfig.resolved | tail -1; done | tee $EV/stage2/AGI-01-resolved.txt
   python3 scripts/llm_agent/verify_modelconfig_bridge.py --out $EV/stage2/AGI-01-verify.json
   ```
4. **Expected:** three agents `BYO`; the resolved `uid` and `generation` equal the
   CR's; `verify_modelconfig_bridge.py` reports `all_passed: true` with mechanism
   `BYO resolver` for the two LLM agents and no LLM variables on `drift-specialist`.
5. **PASS/FAIL:** PASS only if all of the above hold; one mismatch is FAIL.
6. **Screenshot:** `AGI-01_global_modelconfig_bindings.png` (terminal with the three outputs).
7. **Raw:** `AGI-01-modelconfig.yaml`, `AGI-01-agents.txt`, `AGI-01-resolved.txt`, `AGI-01-verify.json`.
8. **Failure causes:** old image without `model_config.py`; wrong API-server CIDRs; RoleBinding missing.
9. **Recovery:** stop; `helm -n default rollback llm-agent <P0.2 revision> --wait`.
10. **Rubric:** row 7 (Global ModelConfig), rows 3–6 (LLM route), row 25.

### AGI-02 — Fraud and RAG integration targets

1. **Proves:** `feature-rag-mcp` runs the `isolated` profile with two independent,
   pinned Feast targets, and `drift-mcp` runs the pinned artifacts.
2. **Prerequisites:** Stage 1 done; the two pod port-forwards below.
3. **Commands:** the MCP Gateway routes only the `/mcp` endpoints, so the
   authenticated `/v1/capabilities` report is read from each server through its
   own port-forward:
   ```bash
   kubectl -n ewallet-mcp port-forward deploy/feature-rag-mcp 18091:8080 > $EV/stage2/pf-frag.log 2>&1 &
   kubectl -n ewallet-mcp port-forward deploy/drift-mcp 18092:8080 > $EV/stage2/pf-drift.log 2>&1 &
   curl -s -H "X-Ewallet-MCP-Service-Token: $(token mcp-token-feature-rag-specialist-feature-rag-mcp)" http://127.0.0.1:18091/v1/capabilities | tee $EV/stage2/AGI-02-feature-rag-capabilities.json
   curl -s -H "X-Ewallet-MCP-Service-Token: $(token mcp-token-drift-specialist-drift-mcp)" http://127.0.0.1:18092/v1/capabilities | tee $EV/stage2/AGI-02-drift-capabilities.json
   kubectl -n ewallet-mcp get secret,configmap -l ewallet.io/agi-input=true -o custom-columns=KIND:.kind,NAME:.metadata.name,DB:.metadata.annotations.ewallet\.io/redis-database,REVISION:.metadata.annotations.ewallet\.io/dataset-revision | tee $EV/stage2/AGI-02-inputs.txt
   ```
4. **Expected:** `configuration.feature_targets.profile = isolated`, Fraud
   `redis_database 0` (`read-only (production store)`), RAG `redis_database 3`,
   project `ewallet_rag_hybrid_20261114t144825`; `rag.corpus_revision
   fraud-policy-v1-20261030`; drift `dataset_revision feast-r5gb-20261111-214544`,
   `synthetic_allowed false`, both digests equal `describe.json`; all tools `ready`.
5. **PASS/FAIL:** every value equal; any `not_ready` tool is FAIL.
6. **Screenshot:** `AGI-02_fraud_rag_drift_targets.png`.
7. **Raw:** the two capability JSON files, `AGI-02-inputs.txt`, `stage0/redis-dbs-before.txt`.
8. **Failure causes:** connection Secret without the expected `db=`, registry ConfigMap name, digest typo.
9. **Recovery:** stop; roll back `llm-agent-mcp` (Stage 5).
10. **Rubric:** rows 11–12 (Feature Store targets), rows 9–10 (RAG publication), row 21.

### AGI-03 — MCP services, health and validation

1. **Proves:** both MCP servers are Ready and reject invalid input with a typed error.
2. **Prerequisites:** port-forward 18082.
3. **Commands:**
   ```bash
   kubectl -n ewallet-mcp get deploy,pods,svc -o wide | tee $EV/stage2/AGI-03-mcp-workloads.txt
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-drift-specialist-drift-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection \
     --arguments-json '{"feature_name":"account_behavior__account_tx_count_1h","baseline_id":"latest","current_window":{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}}' \
     --expect rejected --evidence $EV/stage2/AGI-03-invalid-baseline.json
   unset EWALLET_MCP_SERVICE_TOKEN
   ```
4. **Expected:** Deployments available; the probe returns `outcome: rejected` with
   error code `invalid_arguments` (a mutable baseline ID is refused by validation).
5. **PASS/FAIL:** probe `verdict: PASS`.
6. **Screenshot:** `AGI-03_mcp_services_validation.png`.
7. **Raw:** `AGI-03-mcp-workloads.txt`, `AGI-03-invalid-baseline.json`.
8. **Failure causes:** pod not Ready (authentication table missing), gateway route missing.
9. **Recovery:** read `kubectl -n ewallet-mcp logs deploy/drift-mcp`; stop on a Secret problem.
10. **Rubric:** rows 16 (MCP tools), 23 (MCP Gateway).

### AGI-04 — Feature lookup

1. **Proves:** `feature_lookup` returns the configured features with types and timestamps through the MCP Gateway.
2. **Prerequisites:** `inputs/transaction.json` (P0.9).
3. **Command:** the P0.9 command with `--evidence $EV/stage2/AGI-04-feature-lookup.json`.
4. **Expected:** four features, each `{value, event_timestamp}`; `versions.feature_revision` equal to the release's
   `mcpServices.identity.featureRevision` (`canonical-100gb-v1/online-20261031-163506` in `g3/g3.yaml`).
5. **PASS/FAIL:** `verdict: PASS` and no `null` timestamp for the count features.
6. **Screenshot:** `AGI-04_feature_lookup.png`.
7. **Raw:** `AGI-04-feature-lookup.json`.
8. **Failure causes:** `missing_features` (wrong DB or registry), Redis timeout.
9. **Recovery:** compare with P0.9; if P0.9 passed and this fails, the AGI Fraud Secret is wrong: roll back.
10. **Rubric:** rows 17, 22.

### AGI-05 — Fraud scoring

1. **Proves:** `fraud_scoring` assembles the canonical feature vector and gets a KServe V2 prediction with model identity.
2. **Command:**
   ```bash
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool fraud_scoring \
     --arguments-json "{\"transaction\": $(cat $EV/inputs/transaction.json)}" --expect ok --evidence $EV/stage2/AGI-05-fraud-scoring.json
   unset EWALLET_MCP_SERVICE_TOKEN
   ```
3. **Expected:** `fraud_probability` in [0, 1], `fraud_prediction` 0 or 1,
   `model_name fraud-detector`, `model_version 1`, a 64-hex `model_input_schema_sha256`.
4. **PASS/FAIL:** `verdict: PASS` and all fields present. 5. **Screenshot:** `AGI-05_fraud_scoring_kserve.png`.
6. **Raw:** `AGI-05-fraud-scoring.json`. 7. **Failure causes:** KServe predictor not Ready, egress policy.
8. **Recovery:** `kubectl -n fraud-serving get inferenceservice fraud-detector`; stop if not Ready.
9. **Prerequisites:** AGI-04 PASS. 10. **Rubric:** rows 18, 22.

### AGI-06 — RAG retrieval

1. **Proves:** `knowledge_search` reads the Hybrid index in DB 3 and cites chunks of the intended corpus.
2. **Command:**
   ```bash
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool knowledge_search \
     --arguments-json '{"query":"account takeover step-up authentication","top_k":2}' --expect ok --evidence $EV/stage2/AGI-06-rag.json
   unset EWALLET_MCP_SERVICE_TOKEN
   ```
3. **Expected:** two citations with `document_id`, `chunk_id`, `source_checksum`,
   `corpus_revision fraud-policy-v1-20261030`, `embedding_revision 1110a243…`,
   `feature_revision fraud-policy-feast-v1`; the account-takeover chunk ranks first.
4. **PASS/FAIL:** `verdict: PASS` and the revisions above. 5. **Screenshot:** `AGI-06_rag_retrieval_hybrid_index.png`.
6. **Raw:** `AGI-06-rag.json`. 7. **Failure causes:** RAG Secret not `db=3`, registry ConfigMap of another project.
8. **Recovery:** check AGI-02; roll back the MCP release. 9. **Prerequisites:** AGI-02 PASS. 10. **Rubric:** rows 9, 10, 22.

### AGI-07 — Drift detection

1. **Proves:** PSI is computed by `drift-mcp` from the versioned baseline and window artifacts.
2. **Command:**
   ```bash
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-drift-specialist-drift-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection \
     --arguments-json "{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"$BASELINE\",\"current_window\":{\"start\":\"2026-05-01T00:00:00Z\",\"end\":\"2026-05-31T00:00:00Z\"}}" \
     --expect ok --evidence $EV/stage2/AGI-07-drift.json
   unset EWALLET_MCP_SERVICE_TOKEN
   ```
3. **Expected:** `drift_status` one of `no_drift`/`warning`/`drift`, a finite `psi`
   equal to the sum of bin contributions, `current_count ≥ min_current_count`,
   `dataset_revisions` both `feast-r5gb-20261111-214544`, `baseline_source.sha256`
   and `current_source.sha256` equal to the artifacts.
4. **PASS/FAIL:** `verdict: PASS` and the identities above. 5. **Screenshot:** `AGI-07_drift_detection_psi.png`.
6. **Raw:** `AGI-07-drift.json`, `inputs/drift/describe.json`. 7. **Failure causes:** digest pins wrong, window outside coverage.
8. **Recovery:** compare with `describe.json`; roll back the MCP release. 9. **Prerequisites:** AGI-02 PASS. 10. **Rubric:** rows 19–21.

### AGI-08 — Gateway policy (positive and negative)

1. **Proves:** authorized calls pass; an unauthorized principal and an unrouted agent are denied.
2. **Commands:**
   ```bash
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection \
     --arguments-json "{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"$BASELINE\",\"current_window\":{\"start\":\"2026-05-01T00:00:00Z\",\"end\":\"2026-05-31T00:00:00Z\"}}" \
     --expect denied --evidence $EV/stage2/AGI-08-mcp-wrong-principal.json
   export EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-drift-specialist-drift-mcp)"
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool fraud_scoring \
     --arguments-json "{\"transaction\": $(cat $EV/inputs/transaction.json)}" --expect denied --evidence $EV/stage2/AGI-08-mcp-ungranted-tool.json
   unset EWALLET_MCP_SERVICE_TOKEN
   "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py request drift --request-id $CAMPAIGN-a2a-neg \
     --feature account_behavior__account_tx_count_1h --baseline $BASELINE \
     --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out $EV/stage2/AGI-08-a2a-request.json
   "${AGENT_PY[@]}" scripts/llm_agent/g3_a2a_smoke.py --endpoint http://127.0.0.1:18083/ --kagent-host payments-agent.ewallet-agents \
     --request $EV/stage2/AGI-08-a2a-request.json --response $EV/stage2/AGI-08-a2a-unrouted.json --timing $EV/stage2/AGI-08-a2a-unrouted-timing.json
   kubectl -n ewallet-agents logs deploy/mcp-gateway --since 15m | tail -20 > $EV/stage2/AGI-08-mcp-gateway.log
   ```
3. **Expected:** both MCP probes `verdict: PASS` (`outcome: denied`, `Unknown tool`);
   the A2A probe exits non-zero with HTTP 404 (no route for an undeclared host).
   The positive cases are AGI-04 to AGI-07 and AGI-09 to AGI-11.
4. **PASS/FAIL:** all three negatives denied. 5. **Screenshot:** `AGI-08_gateway_policy_allow_deny.png`.
6. **Raw:** the three probe files and the gateway log. 7. **Failure causes:** a grant broader than reviewed (stop and report).
8. **Recovery:** none needed for a read-only probe. 9. **Prerequisites:** port-forwards 18082, 18083. 10. **Rubric:** rows 23, 24.

## Stage 3 — Agent E2E in kagent UI

```bash
kubectl -n kagent port-forward svc/kagent-ui 18300:8080 > $EV/stage3/pf-ui.log 2>&1 &
```

Open http://127.0.0.1:18300 → **Skip wizard** → Agents → `ewallet-agents/fraud-coordinator` → Chat.
After each answer, save the Coordinator's audit lines:
`kubectl -n ewallet-agents logs deploy/fraud-coordinator --since 10m | grep -E 'coordinator\.(llm_tool_selected|task_completed|task_failed|intake_rejected)' | tee $EV/stage3/<ID>-coordinator.log`
and the Specialists' lines with `deploy/feature-rag-specialist` or `deploy/drift-specialist`.

### AGI-09 — Fraud workflow

- **Prompt:** the N11 analyst question for the reference transaction:
  `Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan.`
- **Proves:** Coordinator → A2A Gateway → Feature-RAG Specialist → MCP Gateway → Feast, KServe and the Hybrid RAG index.
- **Expected:** a status message naming `investigate_transaction`; the answer lists
  the four features, the KServe probability and model version, and citations
  from DB 3; artifact `validated-fraud-investigation`, `contract_version g3-orchestration/v1`.
- **PASS/FAIL:** completed task, every number equal to the Specialist artifact; a failed or rejected task is FAIL.
- **Screenshot:** `AGI-09_kagent_fraud_workflow.png`. **Raw:** `stage3/AGI-09-*.log`.
- **Failure causes:** LLM timeout (CPU vLLM), ungrounded arguments. **Recovery:** retry once; then stop.
- **Rubric:** rows 22, 25.

### AGI-10 — Drift workflow

- **Prompt:** `Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.`
- **Proves:** Coordinator → A2A Gateway → Drift Specialist → MCP Gateway → drift-mcp.
- **Expected:** answer section "Từ drift-specialist" with PSI, status, observation
  count, both dataset revisions; artifact `validated-drift-investigation`,
  `contract_version g3-orchestration/v2`; the audit line shows
  `capabilities=drift:llm` or `drift:deterministic`.
- **PASS/FAIL:** completed, PSI equal to AGI-07 for the same window. Record which selection source was used.
- **Screenshot:** `AGI-10_kagent_drift_workflow.png`. **Raw:** `stage3/AGI-10-*.log`.
- **Failure causes:** baseline ID typo (rejected), drift Specialist not Ready. **Recovery:** check AGI-07; stop.
- **Rubric:** rows 19–21, 25.

### AGI-11 — Combined workflow

- **Prompt:** the AGI-09 prompt followed by ` Đồng thời phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.`
- **Proves:** both Specialists run for one request and the answer attributes each result to its Specialist.
- **Expected:** sections "Từ feature-rag-specialist" and "Từ drift-specialist";
  `workflow combined`, `status complete`; `specialists` object with both member
  request IDs `<id>.fraud` and `<id>.drift`.
- **PASS/FAIL:** both sections present with validated values. `partial` with an
  explicit `unavailable` entry is a correct failure mode but not a PASS.
- **Screenshot:** `AGI-11_kagent_combined_workflow.png`. **Raw:** `stage3/AGI-11-*.log`.
- **Failure causes:** one Specialist down (partial). **Recovery:** fix and retry with a new chat.
- **Rubric:** rows 24, 25.

Typed fallback (no LLM), the same routes through the A2A Gateway's
kagent entrypoint, for the raw JSON evidence:

```bash
"${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py request combined --request-id $CAMPAIGN-combined \
  --transaction $EV/inputs/transaction.json --policy-query "account takeover step-up authentication" \
  --feature account_behavior__account_tx_count_1h --baseline $BASELINE \
  --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out $EV/stage3/AGI-11-typed-request.json
"${AGENT_PY[@]}" scripts/llm_agent/g3_a2a_smoke.py --endpoint http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/ \
  --request $EV/stage3/AGI-11-typed-request.json --response $EV/stage3/AGI-11-typed-task.json --timing $EV/stage3/AGI-11-typed-timing.json
```

## Stage 4 — Observability and evidence

### AGI-12 — End-to-end tracing

```bash
kubectl -n ewallet-agents port-forward svc/grafana 13000:3000 > $EV/stage4/pf-grafana.log 2>&1 &
```

Grafana http://127.0.0.1:13000 → Explore → Tempo → TraceQL
`{ span.g3.request_id =~ "<request_id from AGI-11>.*" }`.

- **Proves:** one trace links `g3.coordinator.execute` →
  `g3.coordinator.nl_intake.llm_tool_selection` (LLM Gateway span) →
  two `g3.coordinator.delegate` spans (`ewallet.specialist`, `ewallet.gateway=a2a-gateway`)
  → `g3.drift_specialist.execute` / `g3.feature_rag_specialist.execute` →
  `g3.drift_specialist.mcp_call` (`mcp.tool.name`, `ewallet.gateway=mcp-gateway`) →
  `mcp.tool drift_detection` (`ewallet.backend.operation`).
- **PASS/FAIL:** the spans above share one trace ID; MCP spans of both servers present.
  Gateway spans appear only if gateway tracing is on (N11 tracing overlay).
- **Screenshot:** `AGI-12_tempo_end_to_end_trace.png`. **Raw:** export the trace JSON
  (Tempo → Export) to `stage4/AGI-12-trace.json`; MCP audit lines with the same
  `trace_id`: `kubectl -n ewallet-mcp logs deploy/drift-mcp --since 30m | grep tool_call_completed > $EV/stage4/AGI-12-drift-mcp-audit.log`.
- **Failure causes:** collector not receiving BYO spans (network policy). **Recovery:** read-only; record the gap.
- **Rubric:** rows 46–51 (observability), 24, 25.

### AGI-13 — Agent Registry

```bash
"${FEAST_PY[@]}" -m ops.llm_agent.registry_catalog render --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml > $EV/stage4/AGI-13-catalog.yaml
kubectl -n ewallet-prompts port-forward svc/prompt-registry-agentregistry 18121:12121 > $EV/stage4/pf-registry.log 2>&1 &
```

- Without approval: open http://127.0.0.1:18121 and capture the registered
  agents as they are (`AGI-13_agent_registry.png`); compare with the catalog file.
- With a separate approval (registry write):
  `"${FEAST_PY[@]}" -m ops.llm_agent.registry_catalog publish --registry http://127.0.0.1:18121 --confirm-live-write http://127.0.0.1:18121 --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml --evidence $EV/stage4/AGI-13-publish.json`
  (verdict `PASS`: `fraud-coordinator` and `drift-specialist` at `1.1.0`, runtime label `kagent-byo`).
- **Rubric:** rows 8, 26.

### AGI-14 — Runtime summary

```bash
kubectl -n ewallet-agents get agents.kagent.dev,deploy,pods -o wide | tee $EV/stage4/AGI-14-agents.txt
kubectl -n ewallet-mcp get deploy,pods -o wide | tee $EV/stage4/AGI-14-mcp.txt
for d in fraud-coordinator feature-rag-specialist drift-specialist; do
  kubectl -n ewallet-agents get deploy $d -o jsonpath='{.metadata.name}{" "}{.spec.template.spec.containers[0].image}{"\n"}'
done | tee $EV/stage4/AGI-14-images.txt
helm -n default history llm-agent --max 3 | tee -a $EV/stage4/AGI-14-helm.txt
helm -n ewallet-mcp history llm-agent-mcp --max 3 | tee -a $EV/stage4/AGI-14-helm.txt
```

Grafana: `sum by (agent, mode, outcome) (increase(ewallet_agent_invocations_total[1h]))`
and `sum by (tool, outcome) (increase(ewallet_mcp_tool_calls_total[1h]))`.
PASS: images equal `stage1/images/digests.txt`; completed invocations for the three
agents; no unexpected restarts. Screenshot `AGI-14_runtime_summary.png`. Rubric rows 25–27.

## Stage 5 — Cleanup or promotion (approval A2)

```bash
RAG_TARGET=gke RAG_KUBE_CONTEXT=$CTX scripts/llm_agent/rag_runtime_run.sh dbs | tee $EV/stage5/redis-dbs-after.txt
diff $EV/stage0/redis-dbs-before.txt $EV/stage5/redis-dbs-after.txt && echo "Redis key counts unchanged" | tee $EV/stage5/redis-diff.txt
```

PASS: every database has the same key count and owner as before (the campaign only reads).

Rollback (default unless the owner approves keeping the profile):

```bash
helm -n default rollback llm-agent <P0.2 llm-agent revision> --wait
helm -n ewallet-mcp rollback llm-agent-mcp <P0.2 llm-agent-mcp revision> --wait
kubectl -n ewallet-agents rollout status deploy/fraud-coordinator deploy/feature-rag-specialist --timeout 10m
kubectl -n ewallet-mcp rollout status deploy/feature-rag-mcp deploy/drift-mcp --timeout 10m
kubectl -n ewallet-mcp delete secret feast-online-redis-agi-fraud feast-online-redis-agi-rag
kubectl -n ewallet-mcp delete configmap feast-registry-rag-agi drift-baselines-agi drift-window-agi
helm template llm-agent infra/helm/llm-agent-platform -n default -f /tmp/agi-agents-live-values.yaml > /tmp/agi-agents-off.yaml
helm -n default get manifest llm-agent > /tmp/agi-agents-after.yaml
python3 scripts/llm_agent/helm_object_diff.py /tmp/agi-agents-after.yaml /tmp/agi-agents-off.yaml --expect-objects 0 | tee $EV/stage5/rollback-agents.txt
```

Delete the inputs only after the rollback: the AGI pods mount them. The images
stay in Artifact Registry (immutable, referenced by the evidence).

Promotion instead of rollback: keep both releases, record the decision in
`stage5/decision.txt`, and treat the five inputs as production objects
(rotation of the two derived Secrets follows the Feature Store Secret).

## Evidence organization

After review, copy the genuine files only:

```bash
DEST=docs/evidence/edai2/agents/integration/$CAMPAIGN
mkdir -p $DEST/screenshots
cp ~/Pictures/Screenshots/AGI-*.png $DEST/screenshots/
rsync -a --exclude 'pf-*.log' $EV/ $DEST/raw/
(cd $DEST && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
grep -rIl -e 'password=' -e 'connection_string' $DEST && echo "STOP: credential-like text" || echo "no credential-like text"
```

Do not copy `/tmp/agi-*-live-values.yaml`, the snapshot copy or any Secret.
The final documentation update (AGENTS_REGISTRY_GATEWAYS.md, the rubric index)
is a separate task after this campaign.
