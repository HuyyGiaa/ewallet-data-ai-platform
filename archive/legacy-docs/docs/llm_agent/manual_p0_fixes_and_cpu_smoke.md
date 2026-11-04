# Phase A–C P0 fix — kiểm thử thủ công, chưa deploy GKE

**Phạm vi:** source-only P0 mitigation. Không đổi canonical, không sửa DP2/GCS/Feast Registry, không triển khai cloud. Không ghi `VERIFIED` rubric chỉ vì test PASS.

## Tại sao chúng ta làm việc này?

- vLLM là **LLM inference server**: nhận chat messages và trả lời, hoặc trả về structured `tool_calls`. Nó *không* phải một MCP server.
- agentgateway là **LLM traffic gateway**: auth/routing/timeout/retry/fallback. kagent `ModelConfig` trỏ tới gateway, không trực tiếp vào vLLM.
- kagent **Agent** nhận tool calls, qua MCP Gateway policy-only gọi MCP tools rồi trả tool result cho model. `scripts/llm_agent/smoke_tool_call.py` là phép thử protocol trên local fixture, **chưa phải real MCP**.
- RAG có `source_checksum`, `embedding_revision`, `feature_revision` để kiểm chứng nguồn văn bản. Boundary adapter đồng nhất RAG citations vào shared MCP contract; không đọc trực tiếp `rag.Citation` rồi giả định tương thích.
- Kubernetes default-deny bảo vệ dataflow. ALLOW chỉ được render từ `networkFlows.enforced` khi cả hai đầu là workload do chart sở hữu (pod label + ServiceAccount đã biết): hiện chỉ có DNS và agentgateway→vLLM. Agent→agentgateway, native A2A, MCP, Feast/KServe, OTLP, model bootstrap và external Gateway được ghi trong `networkFlows.unresolved` (vẫn bị chặn) cho tới khi identity runtime được readback.
- Airflow nhận DAG `params` từ người trigger. Không để Jinja params vào Bash string; biến môi trường chỉ được shell expand một lần và được quote. JSON schema hạn chế endpoint, secret path và output path.

## 1. Trạng thái patch trên worktree

`phase-abc-p0-fixes.patch` (SHA256 `0a21aa8b…fcbc4`) **đã được áp dụng** trên `codex/llm-agent-rubric-20261030` (HEAD `baf2a42`) và được mở rộng bởi `docs/llm_agent/phase_abc_p0_remediation.md`. **Không áp dụng lại**: `git apply --check` sẽ fail vì nội dung đã có mặt. Không dùng `git reset`, `git clean`, `--force` hay merge.

## 2. Test offline miễn phí

```bash
python3 -m compileall -q ai_platform/llm_agent ops/llm_agent \
  data_platform/orchestration/airflow tests/llm_agent
python3 -m unittest -v tests.llm_agent.test_phase_abc_p0_fixes
python3 -m unittest -v tests.llm_agent.test_contracts tests.llm_agent.test_phase_c \
  tests.unit.test_rubric_workbook_extract tests.unit.test_rubric_evidence_matrix
python3 -m ops.llm_agent.rag_pipeline --manifest \
  data/llm_agent/rag/fraud-policy-v1/manifest.json --validate-only
```

Expected: citation tests PASS, DAG source uses environment variables; corpus validation 2 docs/4 chunks. The Helm test class **SKIPs** if Helm is not installed; SKIP is not PASS.

With Helm installed:

```bash
helm lint infra/helm/llm-agent-platform -f infra/helm/llm-agent-platform/values-gcp.yaml
helm template llm-agent infra/helm/llm-agent-platform \
  -f infra/helm/llm-agent-platform/values-gcp.yaml > /tmp/ewallet-llm-rendered.yaml
python3 -m ops.llm_agent.policy_lint /tmp/ewallet-llm-rendered.yaml
python3 -m unittest -v tests.llm_agent.test_phase_abc_p0_fixes
```

No `helm upgrade/install`, `terraform apply`, `kubectl apply` at this step.

## 3. Inspect vLLM CPU prerequisites (optional, free local test)

```bash
uname -m
lscpu | grep -E 'Model name|Flags' | head -2
docker --version
docker info >/dev/null && echo 'Docker daemon ready'
```

The pinned `vllm-openai-cpu` image may require CPU instruction sets not available on your laptop. Do not assume compatibility from available RAM alone. Qwen3-8B requires a **separate measured resource profile**, not the 8Gi memory limit for 0.6B.

Run **a single Qwen3-0.6B backend locally**, not both GKE replicas:

```bash
export EWALLET_VLLM_API_KEY='local-test-token-not-production'
IMAGE='docker.io/vllm/vllm-openai-cpu:v0.32.0-x86_64@sha256:827ef38187f9b52f2bb838695ef815ec5b65f4bf3e4c061b002b2b0a19cb0701'

docker run --rm --name ewallet-vllm-cpu-local \
  --cpus=6 --memory=12g \
  -p 127.0.0.1:8000:8000 \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  -e VLLM_CPU_NUM_OF_RESERVED_CPU=1 \
  -e VLLM_API_KEY="$EWALLET_VLLM_API_KEY" \
  -v ewallet-vllm-model-cache:/root/.cache \
  "$IMAGE" \
  --model Qwen/Qwen3-0.6B \
  --revision c1899de289a04d12100db370d81485cdf75e47ca \
  --served-model-name ewallet-fraud-llm-v1 \
  --max-model-len 2048 --max-num-seqs 2 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Do this in a **separate terminal**. Model download is required unless cached. Actual download size, startup latency and CPU instruction support are environment-dependent; if it fails, capture the first real traceback instead of retrying blindly. To stop: `Ctrl+C` in that terminal.

In a second terminal:

```bash
cd "$WT"
export EWALLET_VLLM_API_KEY='local-test-token-not-production'
curl --fail --max-time 5 http://127.0.0.1:8000/health
python3 scripts/llm_agent/smoke_tool_call.py --tool-choice required
```

Expected: `LOCAL_TOOL_PROTOCOL_PASS`, with `fixture_only=true`. This verifies only vLLM's OpenAI tool-call round trip. It does not demonstrate working MCP, Feast, KServe or the final Agent. After that, try `--tool-choice auto` to observe optional model tool selection; it can legitimately decline a tool in auto mode.

**If Docker image Entrypoint differs or the CPU image rejects the flags, inspect `docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'` and the container logs. Do not automatically restart with privilege escalation or `seccomp=unconfined`.**

## 4. Bounded Airflow test (do NOT trigger canonical Airflow 8084)

The new DAG can only be parsed with the user's correct isolated Airflow Python/PYTHONPATH. The static source test passes without importing Airflow. On Ubuntu, parse its source under the dedicated environment after verifying it does not register/trigger new DAG runs; do not edit canonical `AIRFLOW_HOME` or existing scheduler.

## 5. Before future GKE deployment

1. Confirm namespace PodSecurity policies, labels/SA identities, DNS implementation (kube-dns vs NodeLocal DNS), Istio ambient principal behavior and waypoint assumptions with **read-only** cluster inventory.
2. Resolve each `networkFlows.unresolved` entry by declaring the read-back workload identity and an enforced flow; current policy covers only DNS and agentgateway→vLLM. The current deny-all egress also blocks Hugging Face model downloads, so vLLM cannot become Ready until a verified model is staged (image/PVC) or narrow controlled egress is approved.
3. Verify actual vLLM `imageID`/model revision and agentgateway model route readback, then run unauthorized-route and real tool-calling tests.
4. Ensure the RAG embedding publication/Feast apply/materialization/readback, DataHub lineage and real Airflow DagRun remain **PENDING**, not rubric VERIFIED.
5. Request permission and billing/quota capacity review before deploying to GKE. Do not interfere with running DP2.
