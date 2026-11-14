# Agent integration (AGI) evidence runbook

Owner-run campaign that proves the integrated Multi-Agent workflow on GKE:

```text
kagent UI → A2A Gateway → fraud-coordinator ── LLM Gateway → vLLM   (one tool-selection call)
                               ├─ A2A Gateway → feature-rag-specialist → MCP Gateway → feature-rag-mcp → Feast (Fraud) · KServe · Feast (RAG)
                               └─ A2A Gateway → drift-specialist      → MCP Gateway → drift-mcp       → baseline + window artifacts → PSI
```

Status: **SOURCE_IMPLEMENTED and LOCAL_TESTED; every AGI item below is
CLOUD_PENDING.** Nothing in this runbook has been executed on GKE. Design,
contracts, local tests and the evidence-to-rubric matrix are in
[AGENT_INTEGRATION_CONTRACTS.md](../../edai2/agents/AGENT_INTEGRATION_CONTRACTS.md).

## Rules

- **Approval boundaries.** Every command block is marked:
  - `[READ-ONLY]` reads the cluster, GCS or local files only;
  - `[A1]` needs the owner's Stage 1 approval: image push, five namespaced
    inputs, the two `helm upgrade` commands;
  - `[A2]` needs the owner's Stage 5 approval: rollback and input deletion.
- **Fail closed.** Mandatory checks, gates and every mutation run through
  [`scripts/llm_agent/agi_campaign.sh`](../../../scripts/llm_agent/agi_campaign.sh)
  (`set -euo pipefail`; each check in a fresh process). It refuses to continue
  after a failed check. Specifically:
  - `upgrade` refuses without a Stage 0 `PREFLIGHT_PASS` for the same campaign,
    commit and context, and without the campaign-scoped approval string.
  - `upgrade` re-runs the gate first: render → exact object-identity diff →
    `helm upgrade --dry-run=server`. Any failure stops before `helm upgrade`.
- **Other blocks.** The remaining multi-line blocks run in a subshell,
  `( set -euo pipefail; … )`: the first failing command ends the block with a
  non-zero status and keeps your terminal. Do not continue after a non-zero block.
- **Expected negatives** (AGI-03, AGI-08) are probes that exit 0 only when they
  observe the expected rejection *and* its control. Any other failure is non-zero.
- **What this campaign touches.**
  - Nothing here wakes the cluster: waking follows [CLOUD_LAB_RUNBOOK.md](CLOUD_LAB_RUNBOOK.md)
    under its own approval.
  - Nothing writes to Redis, GCS, DataHub or the Agent Registry. The optional
    AGI-13 publish needs its own approval.
- **Redis access is read-only at the application level only.** The MCP servers
  issue Feast online reads, but the Feature Store Redis uses one `requirepass`
  user without ACLs. The derived Secrets therefore carry a credential that could
  write. Stage 5 checks that nothing changed.
- **Secrets and evidence.**
  - Never print, paste or commit a Secret value, token or connection string.
    `helm get values` output stays in `/tmp/agi-<campaign>` (mode 700), never in Git.
  - Screenshots are taken by the owner from real windows into
    `~/Pictures/Screenshots/`. File names below are conventions, not files to generate.
  - A `Ready` pod is never evidence that a tool ran, an in-process test is never
    evidence of live gateway policy, and a typed API call never replaces kagent UI chat.

## 0. Campaign identity and shell setup `[READ-ONLY]`

```bash
cd ~/Develop/ewallet-data-ai-platform
export KUBECONFIG=~/.cache/ewallet-fs-cloud/kubeconfig     # --dns-endpoint kubeconfig
export PYTHONPATH=$PWD
CAMPAIGN=agi-$(date -u +%Y%m%dT%H%M%SZ)
EV=~/ewallet-evidence/agents/integration/$CAMPAIGN
mkdir -p "$EV"
export AGI_ENV=$EV/campaign.env        # non-secret campaign file read by agi_campaign.sh
cat > "$AGI_ENV" <<EOF
CAMPAIGN=$CAMPAIGN
EV=$EV
CTX=$(kubectl config current-context)
SHA=$(git rev-parse HEAD)
FRAUD_DB=0
RAG_DB=3
BASELINE=bl-r5gb-202604.account_tx_count_1h
EOF
set -a; source "$AGI_ENV"; set +a
C=scripts/llm_agent/agi_campaign.sh
AGENT_PY=(uv run --frozen --project infra/docker/g3-byo-coordinator python)
FEAST_PY=(uv run --frozen --project infra/docker/llm-agent-tooling --extra feast python)
token() { kubectl --context "$CTX" -n ewallet-agents get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }
pf() { kubectl --context "$CTX" -n "$1" port-forward "$2" "$3" > "$EV/pf-${2//\//-}.log" 2>&1 & echo $! >> "$EV/pf.pids"; }
pf_stop() { [ -f "$EV/pf.pids" ] && { kill $(cat "$EV/pf.pids") 2>/dev/null || echo "some port-forwards had already exited"; rm -f "$EV/pf.pids"; }; }
pf ewallet-agents svc/mcp-gateway 18082:8080
pf ewallet-agents svc/a2a-gateway 18083:8083
```

A new terminal needs only `export AGI_ENV=<path>`, `set -a; source "$AGI_ENV"; set +a`
and the function lines above. Port-forwards end with `pf_stop`; restart them
after Stage 1 (upgraded pods replace the forwarded ones). The evidence folder
is per campaign and the probes are create-only, so a rerun never overwrites an
earlier run: start a new campaign instead.

| Recorded identity | Where |
|---|---|
| Campaign, commit, context, snapshot digest, Helm revisions | `campaign.env`, `stage0/PREFLIGHT_PASS` |
| Image digests | `campaign.env` (`*_IMAGE`), `stage1/images/*.json` |
| Releases, namespaces, changed objects | `stage1/llm-agent*-diff.txt`, `stage1/*-render-sha256.txt` |
| Feast projects, Redis DBs, access guarantee | `stage2/AGI-02-*-capabilities.json`, `stage0/redis-dbs-before.txt` |
| RAG publication | `ewallet_rag_hybrid_20261114t144825`, corpus `fraud-policy-v1-20261030`, registry digest in `PREFLIGHT_PASS` |
| Drift baseline and window revisions | `inputs/drift/describe.json` |
| ModelConfig name, uid, generation | `stage2/AGI-01-modelconfig.yaml` |
| Request and trace IDs | `stage2/*.json`, `stage3/*.log`, `stage4/AGI-12-*` |

## 1. Integration profile and data decision

The verification campaigns ran independently and are **not one business dataset**:

| Target | Content | Fraud workflow? |
|---|---|---|
| Redis DB 0 | Production `ewallet_fraud_online` (`canonical-100gb-v1/online-20261031-163506`) and `ewallet_rag` | Yes: the recorded transaction `dc58ff4e…` (N10/N11) was served from it. Reads only, at the application level. |
| Redis DB 2 | `fs11-r5gb-20261112`, from snapshot `feast-r5gb-20261111-214544` | Only with a rehearsal-5gb transaction; none is recorded. |
| Redis DB 3 | RAG Hybrid project `ewallet_rag_hybrid_20261114t144825` | Yes for `knowledge_search` (rows not keyed by customer). |
| Redis DB 9 | Streaming campaign `verify-fs12-20261114063323`: 26 synthetic keys | No. |

**Profile `agi-isolated`** (the commands below):

- Fraud = DB 0, pinned by `EWALLET_FRAUD_ONLINE_DB=0` and the explicit opt-in
  `EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true`, which `agi_campaign.sh` sets only when
  `FRAUD_DB=0`.
- RAG = DB 3, pinned by `EWALLET_RAG_ONLINE_DB=3`. The two databases must differ.
- Drift = artifacts from snapshot `feast-r5gb-20261111-214544`:
  - reference window 2026-04-01 → 2026-05-01;
  - current window 2026-05-01 → 2026-06-01.

The drift result is a dataset-level statement about rehearsal-5gb, not about
the canonical data in DB 0, and it does not explain the investigated
transaction. If `stage0/canonical-snapshots.txt` shows a canonical offline
snapshot, prefer it and record the decision in `stage0/decision.txt`.

## Stage 0 — Read-only preflight `[READ-ONLY]`

```bash
$C preflight              # every check prints PASS/FAIL to $EV/campaign.log; non-zero on any FAIL
$C data-state before      # DB 0-15 key counts and owners, DB 3 content digest, DB 0 reference readback
gcloud storage ls gs://ewallet-data-ai-platform-hg26-data/features/offline/snapshots/ > "$EV/stage0/canonical-snapshots.txt"
```

| Check | Fails when |
|---|---|
| P0.1 | the tracked tree is dirty or HEAD ≠ `SHA` |
| P0.2 | the kube context ≠ `CTX` |
| P0.3 | `llm-agent` or `llm-agent-mcp` is not `deployed` (revisions are recorded as rollback targets) |
| P0.4–P0.5 | the chart at `SHA` does not reproduce either live release exactly (0 objects differ) |
| P0.6 | a Deployment in `ewallet-agents` or `ewallet-mcp` is unavailable |
| P0.7 | `drift-specialist` is not `Declarative` (the expected pre-state) |
| P0.8 | Redis DB 3 does not hold 5 keys owned by the RAG Hybrid project |
| P0.9 | the drift snapshot manifest digest ≠ `3aa7e2f2…` |
| P0.10 | the RAG Hybrid registry cannot be read (its digest is recorded) |
| P0.11–P0.12 | the reference transaction cannot be read from DB 0 through `feature_lookup` |
| P0.13 | the API-server addresses cannot be read |
| P0.14–P0.15 | the drift network policy or one of the three MCP credential Secrets is missing (names only) |

Prerequisites:

- the cluster is awake;
- `uv` is installed;
- the RAG runner's local runtime exists (`infra/docker/rag-ingestion/.venv`, used by `dbs` and `digest`);
- the `mcp-gateway` port-forward is open.

**Stage 1 is blocked unless `$EV/stage0/PREFLIGHT_PASS` exists.**

## Stage 1 — Isolated integration preparation

**1.1 Images `[A1]`** (Artifact Registry write; scan gate before push):

```bash
( set -euo pipefail; source "$AGI_ENV"; test -f "$EV/stage0/PREFLIGHT_PASS"
  mkdir -p "$EV/stage1/images"
  scripts/llm_agent/build_g3_byo_coordinator.sh "$SHA" "$EV/stage1/images/coordinator.json"
  scripts/llm_agent/build_g3_byo_feature_rag_specialist.sh "$SHA" "$EV/stage1/images/feature-rag.json"
  scripts/llm_agent/build_g3_byo_drift_specialist.sh "$SHA" "$EV/stage1/images/drift.json"
  echo '{"components": ["feature-rag-mcp", "drift-mcp"]}' > "$EV/stage1/images/mcp-components.json"
  ARTIFACT_REGISTRY=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform \
    scripts/llm_agent/build_changed_images.sh "$EV/stage1/images/mcp-components.json" "$SHA" "$EV/stage1/images/mcp.json"
  for f in coordinator feature-rag drift mcp; do scripts/llm_agent/scan_and_push.sh "$EV/stage1/images/$f.json"; done
  ref() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"][sys.argv[2]]["digest_ref"])' "$@"; }
  { echo "COORDINATOR_IMAGE=$(ref "$EV/stage1/images/coordinator.json" g3-byo-coordinator)"
    echo "FEATURE_RAG_IMAGE=$(ref "$EV/stage1/images/feature-rag.json" g3-byo-feature-rag-specialist)"
    echo "DRIFT_IMAGE=$(ref "$EV/stage1/images/drift.json" g3-byo-drift-specialist)"
    echo "MCP_IMAGE=$(ref "$EV/stage1/images/mcp.json" mcp-tools)"; } >> "$AGI_ENV" )
```

`agi_campaign.sh` refuses any image that is not a digest-pinned Artifact Registry reference.

**1.2 Drift artifacts `[READ-ONLY]` for GCS, local writes only:**

```bash
( set -euo pipefail; source "$AGI_ENV"
  S5=gs://ewallet-data-ai-platform-hg26-rehearsal-5gb-data/features/offline/snapshots/rehearsal-5gb-v1/ab-20261110225456-optimized-feat-r261110225459/feast-r5gb-20261111-214544
  SNAP=~/ewallet-evidence/agents/integration/snapshot-feast-r5gb-20261111-214544
  mkdir -p "$SNAP/_control" "$EV/inputs/drift"
  gcloud storage cp "$S5/_control/export_manifest.json" "$SNAP/_control/"
  [ -d "$SNAP/feat_account_behavior" ] || gcloud storage cp -r "$S5/feat_account_behavior" "$SNAP/"
  COMMON=(--snapshot "$SNAP" --expect-manifest-sha256 3aa7e2f2b017d7375eaa506974562929c0983077edfa3825a2babb9a81ed2bca
          --source-uri "$S5" --feature account_behavior__account_tx_count_1h)
  "${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts baseline "${COMMON[@]}" \
    --reference-start 2026-04-01T00:00:00Z --reference-end 2026-05-01T00:00:00Z \
    --baseline-id-prefix bl-r5gb-202604 --bins 10 --min-current-count 100 --thresholds 0.1,0.25 \
    --out "$EV/inputs/drift/baselines.json"
  "${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts window "${COMMON[@]}" \
    --start 2026-05-01T00:00:00Z --end 2026-06-01T00:00:00Z --max-rows 20000 --out-dir "$EV/inputs/drift/window"
  "${FEAST_PY[@]}" -m ops.llm_agent.drift_artifacts describe --baselines "$EV/inputs/drift/baselines.json" \
    --window-manifest "$EV/inputs/drift/window/window-manifest.json" > "$EV/inputs/drift/describe.json"
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d["synthetic"] is False;print(d["environment"]["EWALLET_DRIFT_DATASET_REVISION"])' "$EV/inputs/drift/describe.json"
  echo "DRIFT_DESCRIBE=$EV/inputs/drift/describe.json" >> "$AGI_ENV" )
```

The thresholds `0.1,0.25` are the conventional PSI bands, an owner decision
recorded in `baselines.json`. Omit `--thresholds` for an `unclassified` baseline.

**1.3 Inputs in `ewallet-mcp` `[A1]`** (each tool prints its plan; it creates
objects only with `--approve`):

```bash
( set -euo pipefail; source "$AGI_ENV"; test -f "$EV/stage0/PREFLIGHT_PASS"
  "${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs redis-secret --context "$CTX" --source feast-online-redis \
    --db "$FRAUD_DB" --allow-db0-read --name feast-online-redis-agi-fraud --approve-sensitive-copy
  "${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs redis-secret --context "$CTX" --source feast-online-redis \
    --db "$RAG_DB" --name feast-online-redis-agi-rag --approve-sensitive-copy
  "${FEAST_PY[@]}" -m ops.llm_agent.agi_inputs drift-configmaps --context "$CTX" \
    --baselines "$EV/inputs/drift/baselines.json" --window-manifest "$EV/inputs/drift/window/window-manifest.json" )
# Read the three plans. Then repeat the same block with --approve appended to each command, and publish the registry:
( set -euo pipefail; source "$AGI_ENV"
  R=$(grep '^RAG_REGISTRY_SHA256=' "$EV/stage0/PREFLIGHT_PASS" | cut -d= -f2)
  python3 -m ops.llm_agent.g3_platform_inputs registry-configmap --context "$CTX" \
    --source gs://ewallet-data-ai-platform-hg26-data/features/registries/rag/ewallet_rag_hybrid_20261114t144825/registry.db \
    --destination-namespace ewallet-mcp --name feast-registry-rag-agi --sha256 "$R"
  kubectl --context "$CTX" -n ewallet-mcp get secret,configmap -l ewallet.io/agi-input=true > "$EV/stage1/inputs.txt"
  grep -c . "$EV/stage1/inputs.txt" )
```

The derived Secrets select a database but keep the source credential's
privileges (see Rules). The RAG registry ConfigMap is created by
`g3_platform_inputs.py` with the `ewallet.io/g3-input` label, so check it by name:
`kubectl --context "$CTX" -n ewallet-mcp get configmap feast-registry-rag-agi -o jsonpath='{.metadata.annotations.ewallet\.io/registry-sha256}'`
must print the registry digest from `PREFLIGHT_PASS`.

**1.4 MCP release** — gate `[READ-ONLY]`, then upgrade `[A1]`:

```bash
echo "APISERVER_CIDRS=$(tr -s ' \n' '\n' < "$EV/stage0/apiserver.txt" | grep . | sed 's#$#/32#' | paste -sd,)" >> "$AGI_ENV"
$C gate mcp           # render, exact identities, server-side dry-run
less "$EV/stage1/llm-agent-mcp-diff.txt"
AGI_APPROVAL=A1:$CAMPAIGN $C upgrade mcp
```

The gate passes only if the changed objects are exactly
`Deployment/ewallet-mcp/feature-rag-mcp` and `Deployment/ewallet-mcp/drift-mcp`,
with nothing added or removed. Expected content of the change:

- the new image and identity;
- the two derived Secrets and the two pinned databases with the DB 0 opt-in;
- the RAG project and registry;
- the drift environment and its two read-only mounts.

It also stops if the live release changed since Stage 0.

**1.5 Agents release** — gate `[READ-ONLY]`, then upgrade `[A1]`:

```bash
$C gate agents
less "$EV/stage1/llm-agent-diff.txt"
AGI_APPROVAL=A1:$CAMPAIGN $C upgrade agents        # refuses unless the MCP release was upgraded first
kubectl --context "$CTX" -n ewallet-agents rollout status deploy/fraud-coordinator deploy/feature-rag-specialist deploy/drift-specialist --timeout 10m
pf_stop; pf ewallet-agents svc/mcp-gateway 18082:8080; pf ewallet-agents svc/a2a-gateway 18083:8083
```

The gate passes only for exactly these objects, all in `ewallet-agents`:

- **changed:** `Agent/fraud-coordinator`, `Agent/feature-rag-specialist`, `Agent/drift-specialist`;
- **added:** `Role/ewallet-global-llm-reader`, `RoleBinding/ewallet-global-llm-reader`,
  `NetworkPolicy/allow-byo-modelconfig-to-kube-apiserver`.

If the resolver objects already exist live, the gate fails as "expected added
but not added". Stop and review: the live state differs from the recorded r42.
A BYO pod that cannot resolve the ModelConfig never turns Ready, so
`--atomic` rolls the release back.

## Stage 2 — Application verification `[READ-ONLY]`

Every probe writes create-only evidence and exits non-zero unless it observed
the expected outcome. Rubric mapping: contracts §8.

### AGI-01 — Global ModelConfig and Agent bindings

- **Proves:** one ModelConfig serves the LLM-calling agents. The Coordinator and the
  Feature-RAG BYO agents resolve it at startup; the Drift Specialist has no LLM settings.
- **Prerequisites:** Stage 1 done.
- **Commands:**
  ```bash
  ( set -euo pipefail; source "$AGI_ENV"
    kubectl --context "$CTX" -n ewallet-agents get modelconfig ewallet-global-llm -o yaml > "$EV/stage2/AGI-01-modelconfig.yaml"
    kubectl --context "$CTX" -n ewallet-agents get agents.kagent.dev -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,VERSION:.spec.version,MODELCONFIG:.spec.declarative.modelConfig > "$EV/stage2/AGI-01-agents.txt"
    for d in fraud-coordinator feature-rag-specialist; do kubectl --context "$CTX" -n ewallet-agents logs deploy/$d | grep modelconfig.resolved | tail -1; done > "$EV/stage2/AGI-01-resolved.txt"
    python3 scripts/llm_agent/verify_modelconfig_bridge.py --out "$EV/stage2/AGI-01-verify.json"
    python3 -c 'import json,sys;assert json.load(open(sys.argv[1]))["all_passed"] is True' "$EV/stage2/AGI-01-verify.json" )
  ```
- **Expected:**
  - three agents of type `BYO`;
  - the resolved `uid` and `generation` equal the CR's;
  - `all_passed: true`, mechanism `BYO resolver` for the two LLM agents;
  - no `EWALLET_LLM_*` on `drift-specialist`.
- **PASS/FAIL:** the block exits 0 and the uid/generation match. Anything else is FAIL.
- **Screenshot:** `AGI-01_global_modelconfig_bindings.png`.
- **Raw:** the four files above.
- **Failure causes:** an old image without `model_config.py`; wrong API-server CIDRs; the RoleBinding is missing.
- **Recovery:** stop; Stage 5 rollback.
- **Rubric:** row 7 (primary). It does not prove rows 3–6 (benchmark, platform).

### AGI-02 — Fraud, RAG and drift targets

- **Proves:** `feature-rag-mcp` runs the `isolated` profile with two separate,
  pinned Feast targets and states its real access guarantee; `drift-mcp` runs the
  pinned, non-synthetic artifacts.
- **Commands:** the MCP Gateway routes only `/mcp`, so the authenticated
  `/v1/capabilities` report is read through pod port-forwards:
  ```bash
  ( set -euo pipefail; source "$AGI_ENV"
    pf ewallet-mcp deploy/feature-rag-mcp 18091:8080; pf ewallet-mcp deploy/drift-mcp 18092:8080; sleep 3
    header() { token "$1" | sed 's/^/X-Ewallet-MCP-Service-Token: /'; }   # read by curl from stdin, never in argv
    header mcp-token-feature-rag-specialist-feature-rag-mcp | curl -sf -H @- http://127.0.0.1:18091/v1/capabilities > "$EV/stage2/AGI-02-feature-rag-capabilities.json"
    header mcp-token-drift-specialist-drift-mcp | curl -sf -H @- http://127.0.0.1:18092/v1/capabilities > "$EV/stage2/AGI-02-drift-capabilities.json"
    kubectl --context "$CTX" -n ewallet-mcp get secret,configmap -l ewallet.io/agi-input=true -o custom-columns=KIND:.kind,NAME:.metadata.name,DB:.metadata.annotations.ewallet\.io/redis-database,REVISION:.metadata.annotations.ewallet\.io/dataset-revision > "$EV/stage2/AGI-02-inputs.txt"
    python3 - "$EV/stage2/AGI-02-feature-rag-capabilities.json" "$EV/stage2/AGI-02-drift-capabilities.json" "$DRIFT_DESCRIBE" <<'EOF'
  import json, sys
  rag, drift, describe = (json.load(open(path)) for path in sys.argv[1:])
  targets = rag["configuration"]["feature_targets"]
  assert targets["profile"] == "isolated" and targets["fraud"]["redis_database"] == 0 and targets["rag"]["redis_database"] == 3
  assert targets["rag"]["project"] == "ewallet_rag_hybrid_20261114t144825"
  assert targets["access"]["enforced_by"] == "application"
  env, conf = describe["environment"], drift["configuration"]
  assert conf["synthetic_allowed"] is False and conf["dataset_revision"] == env["EWALLET_DRIFT_DATASET_REVISION"]
  assert conf["baselines_sha256"] == env["EWALLET_DRIFT_BASELINES_SHA256"]
  assert conf["window_manifest_sha256"] == env["EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"]
  assert all(item["status"] == "ready" for report in (rag, drift) for item in report["tools"].values())
  print("AGI-02 PASS")
  EOF
  )
  ```
- **Expected:**
  - Fraud: DB 0, `store: production`; RAG: DB 3, `store: verification`, project `ewallet_rag_hybrid_20261114t144825`;
  - `access.enforced_by: application` and `redis_credential: not restricted …`;
  - drift digests equal `describe.json`;
  - all tools `ready`.
- **PASS/FAIL:** `AGI-02 PASS` printed.
- **Screenshot:** `AGI-02_fraud_rag_drift_targets.png`.
- **Raw:** the two capability files, `AGI-02-inputs.txt`, `stage0/redis-dbs-before.txt`.
- **Failure causes:** a Secret with the wrong `db=`; the registry ConfigMap name; a digest typo.
- **Recovery:** stop; Stage 5 rollback.
- **Rubric:** row 22 (data sources of the Feature-RAG agent), row 16 (secondary). It does not prove rows 11–12 (pipelines), and it is not an infrastructure-level read-only guarantee.

### AGI-03 — MCP services, health and input validation

```bash
( set -euo pipefail; source "$AGI_ENV"
  kubectl --context "$CTX" -n ewallet-mcp get deploy,pods,svc -o wide > "$EV/stage2/AGI-03-mcp-workloads.txt"
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-drift-specialist-drift-mcp)" "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp \
    --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection --expect rejected \
    --arguments-json '{"feature_name":"account_behavior__account_tx_count_1h","baseline_id":"latest","current_window":{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}}' \
    --evidence "$EV/stage2/AGI-03-invalid-baseline.json" )
```

- **Proves:** the servers are Ready and reject invalid input in band; a mutable
  baseline ID gives `rejected` / `invalid_arguments`.
- **PASS/FAIL:** the block exits 0.
- **Screenshot:** `AGI-03_mcp_services_validation.png`.
- **Raw:** both files.
- **Rubric:** row 16 (FastAPI + Pydantic + health), row 23 (secondary: the call went through the MCP Gateway). It does not prove row 18 (rolling update, automatic fallback).

### AGI-04 — Feature lookup

```bash
( set -euo pipefail; source "$AGI_ENV"
  ARGS=$(python3 -c 'import json,sys;print(json.dumps({"transaction":json.load(open(sys.argv[1])),"feature_names":["account_behavior__account_tx_count_1h","account_behavior__account_tx_count_24h","user_behavior__user_avg_amount_30d","merchant_behavior__merchant_tx_count_24h"]}))' "$EV/inputs/transaction.json")
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)" "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp \
    --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool feature_lookup --arguments-json "$ARGS" --expect ok \
    --evidence "$EV/stage2/AGI-04-feature-lookup.json" )
```

- **Expected:** four features, each with a value and an event timestamp;
  `versions.feature_revision` equal to the release's `mcpServices.identity.featureRevision`.
- **PASS/FAIL:** exit 0 and the values equal `stage0/db0-readback-before.json`.
- **Screenshot:** `AGI-04_feature_lookup.png`.
- **Failure causes:** `missing_features` (wrong DB or registry).
- **Recovery:** stop; rollback.
- **Rubric:** row 22 (Online Feature Store), row 16 (secondary).

### AGI-05 — Fraud scoring

```bash
( set -euo pipefail; source "$AGI_ENV"
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)" "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp \
    --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool fraud_scoring --expect ok \
    --arguments-json "{\"transaction\": $(cat "$EV/inputs/transaction.json")}" --evidence "$EV/stage2/AGI-05-fraud-scoring.json" )
```

- **Expected:**
  - `fraud_probability` in [0, 1] and `fraud_prediction` 0 or 1;
  - `model_name fraud-detector`, `model_version 1`;
  - a 64-hex `model_input_schema_sha256`.
- **PASS/FAIL:** exit 0 and all fields present.
- **Screenshot:** `AGI-05_fraud_scoring_kserve.png`.
- **Failure causes:** the KServe predictor is not Ready (`kubectl -n fraud-serving get inferenceservice fraud-detector`).
- **Rubric:** row 22 (inference tool).

### AGI-06 — RAG retrieval

```bash
( set -euo pipefail; source "$AGI_ENV"
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-feature-rag-specialist-feature-rag-mcp)" "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp \
    --endpoint http://127.0.0.1:18082/feature-rag/mcp --tool knowledge_search --expect ok \
    --arguments-json '{"query":"account takeover step-up authentication","top_k":2}' --evidence "$EV/stage2/AGI-06-rag.json" )
```

- **Expected:** two citations with `corpus_revision fraud-policy-v1-20261030`,
  `embedding_revision 1110a243…` and `feature_revision fraud-policy-feast-v1`; the
  account-takeover chunk ranks first.
- **PASS/FAIL:** exit 0 and these revisions present.
- **Screenshot:** `AGI-06_rag_retrieval_hybrid_index.png`.
- **Rubric:** row 22 (RAG tool). Secondary: the first agent read of the published
  Hybrid index (rows 9–10 are already COMPLETE and unaffected).

### AGI-07 — Drift detection

```bash
( set -euo pipefail; source "$AGI_ENV"
  EWALLET_MCP_SERVICE_TOKEN="$(token mcp-token-drift-specialist-drift-mcp)" "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py mcp \
    --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection --expect ok \
    --arguments-json "{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"$BASELINE\",\"current_window\":{\"start\":\"2026-05-01T00:00:00Z\",\"end\":\"2026-05-31T00:00:00Z\"}}" \
    --evidence "$EV/stage2/AGI-07-drift.json" )
```

- **Expected:**
  - a finite `psi` equal to the sum of its bin contributions;
  - `current_count ≥ min_current_count`;
  - both `dataset_revisions` equal to `feast-r5gb-20261111-214544`;
  - the source digests equal the artifacts.
- **PASS/FAIL:** exit 0 and these identities hold.
- **Screenshot:** `AGI-07_drift_detection_psi.png`.
- **Rubric:** row 18 (secondary: an MCP tool deployed with Helm and called through the gateway). It does not prove row 18's automatic fallback, nor rows 19 and 20 (autoscaling, Sandbox).

### AGI-08 — Gateway policy (negative probes)

```bash
$C negative
```

The command runs all three probes, records each result in `campaign.log`, and
fails if any probe did not prove its mechanism:

| Probe | Mechanism under test | PASS only when |
|---|---|---|
| Feature-RAG credential → `/drift/mcp` `drift_detection` | per-server credentials | HTTP 401 at `initialize`; the same credential lists tools on `/feature-rag/mcp`; the drift credential lists `drift_detection` |
| drift-specialist's feature-rag credential → `/feature-rag/mcp` `fraud_scoring` | per-tool grants (hidden tool) | JSON-RPC `-32602 "Unknown tool"`; `fraud_scoring` absent from the tested `tools/list`; the Feature-RAG credential lists it |
| A2A Gateway with `x-kagent-host: payments-agent.ewallet-agents` | exact A2A routes | HTTP 404, while `drift-specialist.ewallet-agents` answers 200 |

Each of the following is FAIL, with the observed status recorded:

- invalid arguments, a backend failure or another JSON-RPC error;
- HTTP 5xx;
- a transport failure or a timeout;
- an "Unknown tool" for a tool that the control principal cannot see either.

The A2A control sends one real typed drift request (read-only for data).
Laptop port-forwards bypass the Ambient L4 caller check, so these probes test the
gateway routes and server credentials and grants, not the SPIFFE caller
policies. Those are shown by the policy objects (AGENTS §7).

- **Screenshot:** `AGI-08_gateway_policy_allow_deny.png` (terminal with the three PASS lines).
- **Raw:** `stage2/AGI-08-*.json`, `campaign.log`.
- **Rubric:** rows 23 and 24 (primary). Positive paths: AGI-04 to AGI-07 and AGI-09 to AGI-11.

## Stage 3 — Agent E2E in kagent UI `[READ-ONLY]` for data

```bash
pf kagent svc/kagent-ui 18300:8080
```

Open http://127.0.0.1:18300 → **Skip wizard** → Agents →
`ewallet-agents/fraud-coordinator` → Chat. After each answer, save the audit
lines:

```bash
( set -euo pipefail; source "$AGI_ENV"; ID=AGI-09   # AGI-10, AGI-11
  for d in fraud-coordinator feature-rag-specialist drift-specialist; do
    kubectl --context "$CTX" -n ewallet-agents logs deploy/$d --since 15m | grep -E '(coordinator|specialist)\.' || echo "(no $d audit lines)"
  done > "$EV/stage3/$ID-audit.log" )
```

### AGI-09 — Fraud workflow

- **Prompt:**
  `Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan.`
- **Expected:**
  - `investigate_transaction` is selected;
  - the answer shows the features, the KServe probability and version, and
    citations from DB 3;
  - contract `g3-orchestration/v1`.
- **PASS/FAIL:** completed task with every value equal to the Specialist artifact. Rejected or failed is FAIL.
- **Screenshot:** `AGI-09_kagent_fraud_workflow.png`.
- **Raw:** `stage3/AGI-09-audit.log`.
- **Rubric:**
  - row 22: an agent with the inference and RAG tools, and UI chat;
  - row 25 (secondary): kagent UI chat with the Coordinator.
  - It does not prove multi-replica or autoscaling (rows 19, 25).

### AGI-10 — Drift workflow

- **Prompt:** `Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.`
- **Expected:**
  - a section "Từ drift-specialist" with PSI, status, observation count and both revisions;
  - contract `g3-orchestration/v2`, `workflow drift`, `status complete`;
  - the audit line shows `capabilities=drift:llm` or `drift:deterministic`. Record which.
- **PASS/FAIL:** completed, `status complete`, PSI equal to AGI-07.
- **Screenshot:** `AGI-10_kagent_drift_workflow.png`.
- **Rubric:**
  - row 25: the Coordinator delegates to the second business agent;
  - row 19 (secondary): an agent using the MCP tool.
  - It does not prove KEDA autoscaling (row 19) or Sandbox (row 20).

### AGI-11 — Combined workflow

- **Prompt:** the AGI-09 prompt followed by ` Đồng thời phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z.`
- **Expected:**
  - sections "Từ feature-rag-specialist" and "Từ drift-specialist";
  - `workflow combined`, `status complete`;
  - member request IDs `<id>.fraud` and `<id>.drift`.
- **PASS/FAIL:** `status complete` with both sections. A `partial` result is a correctly
  reported failure, **not** a PASS.
- **Screenshot:** `AGI-11_kagent_combined_workflow.png`.
- **Rubric:**
  - row 25 (primary: "orchestrates the two agents"), row 24 (secondary).
  - It does not prove Coordinator scale-out (row 25's autoscaling part).

Supplementary raw JSON (typed, no LLM). It **does not replace** the UI evidence,
and its controller path `/api/a2a/<namespace>/<agent>/` has not yet been checked
on this cluster:

```bash
( set -euo pipefail; source "$AGI_ENV"
  "${AGENT_PY[@]}" scripts/llm_agent/agi_probe.py request combined --request-id "$CAMPAIGN-combined" \
    --transaction "$EV/inputs/transaction.json" --policy-query "account takeover step-up authentication" \
    --feature account_behavior__account_tx_count_1h --baseline "$BASELINE" \
    --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out "$EV/stage3/AGI-11-typed-request.json"
  "${AGENT_PY[@]}" scripts/llm_agent/g3_a2a_smoke.py --endpoint http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/ \
    --request "$EV/stage3/AGI-11-typed-request.json" --response "$EV/stage3/AGI-11-typed-task.json" \
    --timing "$EV/stage3/AGI-11-typed-timing.json" )
```

## Stage 4 — Observability and evidence `[READ-ONLY]`

### AGI-12 — End-to-end tracing

```bash
pf ewallet-agents svc/grafana 13000:3000
```

Grafana http://127.0.0.1:13000 → Explore → Tempo → TraceQL
`{ span.g3.request_id =~ "<request_id of AGI-11>.*" }`.

- **Proves:** one trace contains all of these spans:
  - `g3.coordinator.execute`;
  - `g3.coordinator.nl_intake.llm_tool_selection`, with `gen_ai.*` token attributes;
  - two `g3.coordinator.delegate` spans (`ewallet.specialist`, `ewallet.gateway=a2a-gateway`);
  - the Specialist execute spans and `g3.drift_specialist.mcp_call`;
  - `mcp.tool <tool>`, with `ewallet.backend.operation`.
- **Gateway spans:** they appear only if gateway tracing is enabled (the N11
  tracing overlay). Record whether they are present.
- **PASS/FAIL:** these spans share one trace ID.
- **Raw:**
  - export the trace JSON to `stage4/AGI-12-trace.json`;
  - save the MCP audit lines with the same `trace_id`:
    `kubectl --context "$CTX" -n ewallet-mcp logs deploy/drift-mcp --since 30m | grep tool_call_completed > "$EV/stage4/AGI-12-drift-mcp-audit.log"`.
- **Screenshot:** `AGI-12_tempo_end_to_end_trace.png`.
- **Rubric:**
  - row 49 (traces) for the new workflow;
  - row 50 partially: token attributes on one LLM span;
  - it does not replace rows 50–51's metric dashboards (already COMPLETE).

### AGI-13 — Agent Registry

```bash
( set -euo pipefail; source "$AGI_ENV"
  "${FEAST_PY[@]}" -m ops.llm_agent.registry_catalog render \
    --values-overlay infra/helm/llm-agent-platform/g3/g3.yaml --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml \
    > "$EV/stage4/AGI-13-catalog.yaml" )
pf ewallet-prompts svc/prompt-registry-agentregistry 18121:12121
```

The catalog describes the runtime the chart deploys:

- all three agents: `kagent-byo`, "BYO Agent";
- Coordinator and drift-specialist at version `1.1.0`;
- the Coordinator labelled `ewallet.io/workflows: fraud.drift`.

Each BYO agent references only the MCP server it calls.

- **Without approval:** capture the current registry UI
  (`AGI-13_agent_registry.png`). It shows the published entries, which may
  still describe the earlier runtime; compare them with the catalog file.
- **With a separate approval** (registry write):
  `"${FEAST_PY[@]}" -m ops.llm_agent.registry_catalog publish --registry http://127.0.0.1:18121 --confirm-live-write http://127.0.0.1:18121 --values-overlay infra/helm/llm-agent-platform/g3/g3.yaml --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml --evidence "$EV/stage4/AGI-13-publish.json"`.
- **Rubric:** rows 8, 21, 26.

### AGI-14 — Runtime summary

```bash
( set -euo pipefail; source "$AGI_ENV"
  kubectl --context "$CTX" -n ewallet-agents get agents.kagent.dev,deploy,pods -o wide > "$EV/stage4/AGI-14-agents.txt"
  kubectl --context "$CTX" -n ewallet-mcp get deploy,pods -o wide > "$EV/stage4/AGI-14-mcp.txt"
  for d in fraud-coordinator feature-rag-specialist drift-specialist; do
    kubectl --context "$CTX" -n ewallet-agents get deploy $d -o jsonpath='{.metadata.name}{" "}{.spec.template.spec.containers[0].image}{"\n"}'
  done > "$EV/stage4/AGI-14-images.txt"
  python3 - "$AGI_ENV" "$EV/stage4/AGI-14-images.txt" <<'EOF'
import sys
env = dict(line.strip().split("=", 1) for line in open(sys.argv[1]) if "=" in line)
running = dict(line.split() for line in open(sys.argv[2]) if line.strip())
wanted = {"fraud-coordinator": env["COORDINATOR_IMAGE"], "feature-rag-specialist": env["FEATURE_RAG_IMAGE"],
          "drift-specialist": env["DRIFT_IMAGE"]}
mismatched = sorted(name for name, image in wanted.items() if running.get(name) != image)
assert not mismatched, f"running images differ from the campaign digests: {mismatched}"
print("AGI-14 images match the campaign digests")
EOF
  helm --kube-context "$CTX" -n default history llm-agent --max 3 > "$EV/stage4/AGI-14-helm.txt"
  helm --kube-context "$CTX" -n ewallet-mcp history llm-agent-mcp --max 3 >> "$EV/stage4/AGI-14-helm.txt" )
```

Grafana queries:
`sum by (agent, mode, outcome) (increase(ewallet_agent_invocations_total[1h]))` and
`sum by (tool, outcome) (increase(ewallet_mcp_tool_calls_total[1h]))`.

- **PASS/FAIL:** the running images equal the campaign digests; the three agents
  have completed invocations; there are no unexpected restarts.
- **Screenshot:** `AGI-14_runtime_summary.png`.
- **Rubric:**
  - row 51 (agent and tool call counts for the new workflow);
  - row 47 (secondary).
  - It is **not** evidence for row 27 (Substrate WorkerPool), nor for the
    autoscaling of rows 19 and 25.

## Stage 5 — Data-state check `[READ-ONLY]`, then rollback or promotion `[A2]`

```bash
$C data-state after       # fails if DB 0-15 key counts/owners, the DB 3 content digest or the DB 0 readback changed
```

What this proves: no key was added or removed in any database. DB 3 (the RAG
verification project, 5 keys) is byte-identical (`DUMP` digest). The
reference transaction's DB 0 features are unchanged.

What it does not prove: DB 0 holds about 173k production keys, so it is checked
by counts and one bounded readback, not by a full digest.

Rollback (default unless the owner approves keeping the profile):

```bash
AGI_APPROVAL=A2:$CAMPAIGN $C rollback agents     # helm rollback to the Stage 0 revision, then 0-object diff
AGI_APPROVAL=A2:$CAMPAIGN $C rollback mcp
( set -euo pipefail; source "$AGI_ENV"   # [A2] delete the inputs only after both rollbacks: the AGI pods mount them
  kubectl --context "$CTX" -n ewallet-mcp delete secret feast-online-redis-agi-fraud feast-online-redis-agi-rag
  kubectl --context "$CTX" -n ewallet-mcp delete configmap feast-registry-rag-agi drift-baselines-agi drift-window-agi
  test -z "$(kubectl --context "$CTX" -n ewallet-mcp get secret,configmap -l ewallet.io/agi-input=true -o name)" )
pf_stop
```

The images stay in Artifact Registry (immutable, referenced by the evidence).

Promotion instead: record the decision in `stage5/decision.txt`. The five
inputs then become production objects, and the two derived Secrets rotate with
the Feature Store Secret.

## Evidence organization

```bash
( set -euo pipefail; source "$AGI_ENV"
  DEST=docs/evidence/edai2/agents/integration/$CAMPAIGN
  test ! -e "$DEST"
  mkdir -p "$DEST/screenshots"
  cp ~/Pictures/Screenshots/AGI-*.png "$DEST/screenshots/"
  rsync -a --exclude 'pf-*.log' --exclude 'campaign.env' "$EV/" "$DEST/raw/"
  grep -v -E '^(APISERVER_CIDRS)=' "$EV/campaign.env" > "$DEST/raw/campaign.env"
  if grep -rIl -E 'password=|connection_string=|redis(s)?://[^ ]*@|Bearer [A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY' "$DEST"; then
    echo "STOP: credential-like text found"; exit 1; fi
  (cd "$DEST" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS) )
```

Never copy `/tmp/agi-<campaign>` (Helm values and manifests), the snapshot copy
or any Secret. Updating AGENTS_REGISTRY_GATEWAYS.md and the rubric index is a
separate task after this campaign.
