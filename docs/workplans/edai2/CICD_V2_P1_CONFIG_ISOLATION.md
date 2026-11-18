# CI/CD V2-P1 — Gateway configuration và cô lập staging (offline)

| Mục | Giá trị |
|---|---|
| Trạng thái | `V2_P1_OFFLINE_ACCEPTED` (phạm vi tối giản theo owner update) |
| Ngày | 2026-11-18 |
| Nhánh | `feature/edai2-cicd-v2` |
| Phạm vi | Offline. Không GKE, Jenkins, Cloud Build, IAM, credential, Langfuse hay thay đổi production. |

## A. Đã làm gì

V2-P1 có ba mục tiêu:

1. **Gateway endpoint cấu hình được, nhưng chỉ cho production và staging.**
   - Vấn đề: 5 host Gateway (A2A, MCP, LLM, Agent Card, `x-kagent-host`) bị hard-code `ewallet-agents`. Agent chạy ở staging sẽ hoặc bị validator từ chối, hoặc gọi thẳng vào production.
   - Cách làm: module mới [gateway_environment.py](../../../ai_platform/llm_agent/gateway_environment.py) đọc **một** biến `EWALLET_GATEWAY_NAMESPACE`. Biến này chỉ nhận đúng một trong hai giá trị cố định `ewallet-agents` hoặc `ewallet-agents-staging`; mọi địa chỉ đều được suy ra từ namespace đó. Biến không đặt thì giữ đúng hành vi cũ (production).
   - Nếu không có: muốn có staging thì phải cho nhập URL tùy ý, tức là bỏ lớp kiểm tra host chính xác hiện có.
2. **Profile Helm staging không thể vô tình sửa production.**
   - Vấn đề: V2-P0 render staging vẫn tạo NetworkPolicy trong `feature-store` và `fraud-serving`, vẫn trỏ MCP/OTEL/Redis tenant về production.
   - Cách làm:
     - [values-staging.yaml](../../../infra/helm/llm-agent-platform/values-staging.yaml) là overlay trên chart defaults.
     - [staging-guard.yaml](../../../infra/helm/llm-agent-platform/templates/staging-guard.yaml) là template không sinh object; nó `fail` render khi profile staging trỏ vào namespace production.
   - Nếu không có: chỉ một lần quên `--set` là `helm upgrade` staging đã ghi vào namespace production.
3. **Giữ nguyên hành vi production.**
   - Render Helm production phải byte-identical so với baseline ([render_equivalence.py](../../../scripts/llm_agent/render_equivalence.py)).
   - Các regression suite hiện có không được có test nào từ PASS chuyển sang FAIL.

## B. Git

- **Phát hiện khi kiểm tra baseline:**
  - Commit V2-P0 `b5dac2e1` đã nằm nhầm trên nhánh local `feature/edai2-agent-platform-integration`. Lý do: nhánh này được checkout giữa phiên lúc 17:22. Do đó lần push trước chỉ đẩy `feature/edai2-cicd-v2` ở `3754233e`, và GitHub không có báo cáo V2-P0.
  - Đã cherry-pick sang `feature/edai2-cicd-v2` thành `0fff5625` và push. Không reset, không force.
  - `b5dac2e1` vẫn nằm local trên nhánh kia, chưa push. Owner quyết định có giữ hay không.
- **Baseline V2-P1:** `0fff5625`. Commit cuối: xem lịch sử nhánh.

## C. Gateway

| Gateway | Production (mặc định) | Staging (`EWALLET_GATEWAY_NAMESPACE=ewallet-agents-staging`) |
|---|---|---|
| A2A | `http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/` | `http://a2a-gateway.ewallet-agents-staging.svc.cluster.local:8083/` |
| MCP | `http://mcp-gateway.ewallet-agents.svc:8080/<server>/mcp` | `http://mcp-gateway.ewallet-agents-staging.svc:8080/<server>/mcp` |
| LLM | `http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1` | `http://agentgateway.ewallet-agents-staging.svc.cluster.local:3000/v1` |
| Sandbox route | `/api/a2a-sandboxes/ewallet-sandbox/<agent>/` | `/api/a2a-sandboxes/ewallet-sandbox-staging/<agent>/` |
| `x-kagent-host`, Agent Card | `<agent>.ewallet-agents` | `<agent>.ewallet-agents-staging` |

**Quy tắc kiểm tra:**
- Namespace phải nằm trong allowlist, so khớp chính xác (không trim, không phân biệt chữ hoa thường).
- URL phải đúng `http://<host>:<port><path>`. Bị từ chối nếu có: credential, query, fragment, `;params`, `%`-encoding, alias như `.cluster.local` hay host viết hoa, port sai, path sai, host ngoài cụm, hoặc gọi thẳng MCP server.
- Validator vẫn so khớp host chính xác, không đổi sang so suffix.

**Fail-closed:**
- Chart staging render thêm `EWALLET_RUNTIME_NAMESPACE`, lấy từ namespace của pod qua downward API.
- Pod chạy trong namespace `*-staging` mà không chọn staging Gateway thì lỗi ngay khi khởi động, không âm thầm rơi về production.
- Chọn staging trong namespace production cũng lỗi.
- Gateway staging không phản hồi thì trả lỗi, không thử lại sang production.

**File sửa:**
- `programmatic_coordinator.py` (GatewayPolicy)
- `byo_mcp.py`
- `byo_feature_rag_specialist.py`, `byo_drift_specialist.py` (MCP client, Agent Card)
- `byo_coordinator.py` (Agent Card)
- `model_config.py`, `nl_intake.py` (LLM route)

Hằng số production cũ (`APPROVED_A2A_GATEWAY`, `MCP_GATEWAY_HOST`, `APPROVED_LLM_*`, `APPROVED_MCP_GATEWAY`, `APP_URL`) vẫn còn và giữ nguyên giá trị.

`model_config.py` vẫn load độc lập được cho canary `infra/k8s/modelconfig-canary`; khi load độc lập nó chỉ chấp nhận production.

## D. Tương thích production

Render 8 profile production nguồn bằng cùng Helm v3.22.0, cùng thứ tự values và cùng `--set`, tại `0fff5625` và sau thay đổi:

| Profile | SHA-256 (baseline = sau) | Object |
|---|---|---|
| chart-defaults | `b996ab5c…31471` | 29 |
| values-gcp | `2023faeb…6eea` | 31 |
| values-dev | `b996ab5c…31471` | 29 |
| values-local | `d30546e4…4d99` | 30 |
| gke-cpu-agents | `25f1660c…10d6` | 145 |
| agi-agents | `fb56a065…451ab1e` | 148 |
| agi-agents-sandbox (gần live nhất) | `45075499…61b94d0` | 95 |
| agi-mcp | `7984eece…d413db` | 22 |

**Kết quả: `PRODUCTION_RENDER_BYTE_IDENTICAL`.**

Giới hạn: values live là private (`AGI_PRIVATE`), nên kết quả này chứng minh chart cho cùng output với cùng input nguồn, chưa chứng minh trạng thái release live.

Lệnh tái lập:

```bash
python3 scripts/llm_agent/render_equivalence.py render --ref 0fff5625 --out A
python3 scripts/llm_agent/render_equivalence.py render --out B
python3 scripts/llm_agent/render_equivalence.py compare A B
```

Thay đổi chart giữ nguyên byte production nhờ ba điểm:
- OTEL endpoint đổi từ literal sang `otel-collector.<namespace>`; namespace production vẫn ra đúng chuỗi cũ.
- `agents.byoTelemetry.traces.enabled` mặc định là `true`.
- Env staging chỉ render khi namespace khác `ewallet-agents`.

## E. Staging Helm inventory

Release `llm-agent-staging` có 53 object, **tất cả** trong `ewallet-agents-staging`. Không có object cluster-scoped, không có object trong namespace production.

| Kind | Số |
|---|---|
| NetworkPolicy | 20 |
| AuthorizationPolicy | 7 |
| ConfigMap | 6 |
| ServiceAccount | 6 |
| Service | 5 |
| Deployment (a2a-gateway, mcp-gateway, feature-rag-mcp, drift-mcp) | 4 |
| RemoteMCPServer | 2 |
| StatefulSet (idempotency Redis riêng) | 1 |
| Agent (Coordinator) | 1 |
| ModelConfig | 1 |

- Không có PDB, HPA, KEDA, vLLM, agentgateway, OTEL collector, hay observability/ML-platform policy.
- Peer cross-namespace duy nhất là kagent controller (control plane dùng chung); policy chỉ nằm trong namespace staging.
- Tenant idempotency là `ewallet-agents-staging`. Secret mang hậu tố `-staging`, chỉ là tham chiếu (tạo ngoài Helm).
- Feast và KServe trỏ về đích staging, `EWALLET_FRAUD_ALLOW_PRODUCTION_DB=false`.
- Level A tắt NL intake (không LLM) và tắt tracing; cả hai được ghi rõ trong values.

**Isolation verdict: ISOLATED (offline).** Kiểm tra hai lớp:
1. Guard ở thời điểm render.
2. Test duyệt từng object đã render, kiểm cả nơi object nằm lẫn nơi nó trỏ tới. Chỉ `metadata.namespace` staging là chưa đủ.

## F. Kết quả test

| Lệnh (uv env khóa) | Kết quả |
|---|---|
| `tests/llm_agent/test_v2p1_gateway_environment.py` (g3-byo-coordinator) | 8 passed, 24 subtests |
| `tests/llm_agent/test_v2p1_staging_chart.py` với `EWALLET_RENDER_BASELINE_REF=0fff5625` (llm-agent-tooling) | 5 passed, 27 subtests |
| Toàn bộ `tests/llm_agent tests/ops tests/ci`, so từng test với baseline, ở cả hai env | **0 regression**. tooling312: baseline 1834 passed → 1838; coord312: 917 → 925. Các fail/error còn lại đã có từ baseline: mỗi file chỉ chạy được ở env riêng của nó. |
| `helm lint` (mặc định và staging) | 0 chart failed |

Mutation check:
- Tắt luật fail-closed thì 2 test fail.
- Đổi so khớp host chính xác thành so suffix thì 3 test fail.

Guard có 13 case từ chối, mỗi case fail tại `staging-guard.yaml` đúng lý do.

**10 behavioural gate:**

| Gate | Bằng chứng |
|---|---|
| 1–3. Fraud / Drift / Combined | Suite hiện có (`test_g3_programmatic_coordinator`, `test_agi_drift_specialist`, `test_agi_coordinator_workflows`, `test_agi_end_to_end`…): 0 regression |
| 4. MCP authz | Suite hiện có (`test_mcp_production_authenticator`, `test_c5_mcp_failover`) cộng test cross-route và bypass mới |
| 5. A2A authz | Allowlist Specialist không đổi; test staging/production cross-namespace mới |
| 6. SpecialistEvidence | `test_agi_contracts`, `test_contracts`: 0 regression; schema không đổi |
| 7. Prompt identity | `SYSTEM_PROMPT` SHA-256 `cbb0c51b…44380` được ghim; không đổi prompt, không Langfuse |
| 8. Redis idempotency | `test_g3_durable_idempotency`: 0 regression; tenant staging bắt buộc (guard + test) |
| 9. Snapshot isolation | **Hoãn sang V2-P3**, cùng manifest WorkerPool/SandboxAgent staging |
| 10. Helm equivalence | Byte-identical (mục D) |

## G. Bảo mật: điều còn phải xác minh runtime

- Pod BYO thực sự nhận `EWALLET_RUNTIME_NAMESPACE` qua downward API trong kagent 0.10.3.
- Ambient mesh identity của namespace staging.
- AuthorizationPolicy cho worker sandbox staging.

## H. Chưa làm (để các phase sau)

- **WorkerPool, SandboxAgent, namespace và quota staging; snapshot prefix.** Để V2-P3. Ràng buộc đã biết: `workerPoolRef` không có namespace, nên pool phải cùng namespace.
- **Upgrade watch scope của kagent controller dùng chung.** Thuộc V2-P3A và cần owner quyết định.
- **Dung lượng RustFS `ate-snapshots`.** Production dùng chung, chỉ 256Mi trong values pilot.
- **Tạo credential staging; Feast Redis và KServe predictor staging.** Network flow tới chúng cũng chưa khai báo.
- **Không thay đổi Jenkins/catalog.** Hai test mới chưa nằm trong `jenkins/catalog/components.json`; thêm ở phase pipeline.
- **Langfuse / Prompt Provider.** Để V2-P2.

## I. Phase tiếp

V2-P2 (Managed Prompt Runtime) có thể bắt đầu vì không phụ thuộc gì vào staging runtime. Tuy vậy V2-P3 **chưa** sẵn sàng deploy. Lý do: kết quả ở đây chỉ là offline, và còn thiếu các mục ở H, gồm kagent watch scope, manifest sandbox staging, credential và dữ liệu staging.
