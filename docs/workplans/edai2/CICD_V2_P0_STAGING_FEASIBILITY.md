# CI/CD V2-P0 — Audit kiến trúc, khả thi Staging độc lập và thiết kế Migration (EDAI-2)

| Mục | Giá trị |
|---|---|
| Trạng thái | `V2_P0_ARCHITECTURE_DECISION_READY` |
| Ngày | 2026-11-18 |
| Nhánh | `feature/edai2-cicd-v2` (tạo từ `3754233e`, HEAD rebuild đã xác minh sau Batch 2B-1) |
| Chính sách | Behavioral Freeze (thay cho Full Code Freeze) |
| Phạm vi | Offline-only: đọc repository, render Helm offline, tài liệu. Không có thao tác cloud, không thay đổi ứng dụng/hạ tầng. |
| Bằng chứng V1 | Giữ nguyên. Báo cáo này không sửa và không diễn giải lại kết quả lịch sử: V1 **chưa** chứng minh triển khai đầy đủ Agent qua CD. |

Nhãn bằng chứng dùng trong toàn bộ báo cáo:
`SOURCE_VERIFIED` (đọc mã nguồn/manifest có dòng tham chiếu), `OFFLINE_TESTED` (đã chạy offline trong V2-P0),
`HISTORICAL_RUNTIME_EVIDENCE` (evidence runtime đã lưu từ campaign trước), `DOCUMENTATION_SUPPORTED`,
`REQUIRES_RUNTIME_VERIFICATION`, `UNKNOWN`.

---

## A. Executive Decision

**Verdict: `CONDITIONALLY_FEASIBLE`.**

Một môi trường staging độc lập cho Agent stack (Coordinator BYO + 2 Specialist SandboxAgent trên Substrate WorkerPool +
MCP/A2A/LLM gateway) là khả thi **mà không cần thay đổi hành vi nghiệp vụ**, với các điều kiện:

1. **Một refactor nhỏ về phân giải endpoint gateway** (được Behavioral Freeze cho phép): 5 hằng số host đang hard-code
   `ewallet-agents` trong mã Coordinator/Specialist/MCP client/ModelConfig phải chọn được namespace từ một **allowlist
   biên dịch sẵn**, mặc định giữ nguyên production (mục C, I/V2-P1). Không có refactor này, Agent ở staging hoặc bị
   từ chối khởi động (validator fail-closed), hoặc gọi vào gateway production. `SOURCE_VERIFIED`
2. **Staging có WorkerPool riêng trong namespace sandbox riêng**: `SandboxAgent.spec.substrate.workerPoolRef` không
   có trường namespace, nên không thể dùng chung pool `ewallet-sandbox/business-agents`. `SOURCE_VERIFIED`
3. **kagent controller phải watch namespace staging**: nâng cấp values `watchNamespaces`/`rbac.namespaces` làm restart
   controller dùng chung → ảnh hưởng production ngắn → cần owner approval; phương án thay thế là dùng `kagent-pilot`
   (đã được watch). `SOURCE_VERIFIED`
4. **Phụ thuộc dữ liệu cô lập**: Feast online Redis, Redis idempotency, KServe fraud-detector, LLM API key/budget phải là
   tài nguyên staging; chart hiện tại khi render staging vẫn sinh NetworkPolicy **trong namespace production**
   `feature-store` và `fraud-serving`. `OFFLINE_TESTED`
5. Mọi kết luận về runtime (dung lượng RustFS snapshot, khả năng 2 pool chạy đồng thời với proxy vá, ambient enrolment
   của namespace mới, chi phí thực) là `REQUIRES_RUNTIME_VERIFICATION` theo danh sách ở mục K.

**Kiến trúc ưu tiên:** **Architecture A′ — Dedicated staging namespaces trên cùng cụm GKE**, chỉ dùng chung control plane
Substrate (`ate-system`) và kagent controller; mọi data plane (gateway, Coordinator, MCP server, WorkerPool, Redis,
snapshot prefix, prompt registry read path) là của staging. Lộ trình: **Level A** (Agent thật + phụ thuộc tổng hợp /
staging-seeded) làm cổng V2 bắt buộc; **Level B** (E2E nghiệp vụ cô lập hoàn toàn, có KServe staging cùng artifact
model) là mục tiêu V2-P4. Option B+ (canary production có kiểm soát) **được giữ lại** làm bước promotion sau staging,
không bị xóa trong pha này.

**Go/no-go V2-P1: GO có điều kiện** — V2-P1 chỉ gồm thay đổi offline (refactor endpoint + chart staging values + test
regression); không cần cloud. Xem mục M.

---

## B. Current Architecture

### B.1 Luồng runtime production (SOURCE_VERIFIED, HISTORICAL_RUNTIME_EVIDENCE)

```
                       ewallet-agents (ambient mesh)
 client ─► agentgateway (LLM, :3000) ◄── NL intake (byo_coordinator, LLM tool call)
           a2a-gateway (:8083) ◄──────── fraud-coordinator (BYO, typed + NL)
              │  /api/a2a-sandboxes/ewallet-sandbox/<agent>/      │ Redis idempotency (secret URL)
              ▼                                                    ▼
 kagent controller (ns kagent) ──► ewallet-sandbox: WorkerPool business-agents (gVisor, ateom vá)
                                     SandboxAgent feature-rag-specialist / drift-specialist
                                       │ egress proxy 169.254.17.1:3128 → identity ns/ewallet-sandbox/sa/default
                                       ▼
                                  mcp-gateway (:8080) /feature-rag/mcp, /drift/mcp  (AuthorizationPolicy S1)
                                       │
                    feature-rag-mcp ──► feature-store/feast-online-redis (DB 0 fraud, DB 3 rag)
                                    └─► fraud-serving/fraud-detector-predictor (KServe v1)
                    drift-mcp ───────► baselines/window manifest mount (artifact, SHA-pinned)
 ate-system: ate-api-server, ate-controller, atenet-router, dns, rustfs (bucket ate-snapshots), valkey-cluster ×6
```

### B.2 Tham chiếu mã nguồn chính

| Thành phần | Nguồn | Dòng | Ghi chú | Nhãn |
|---|---|---|---|---|
| A2A gateway endpoint | `ai_platform/llm_agent/programmatic_coordinator.py` | 63 | `APPROVED_A2A_GATEWAY = "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/"` | SOURCE_VERIFIED |
| `GatewayPolicy` | cùng file | 230–303 | `__post_init__` bắt buộc scheme/host/port/path; sandbox ns từ `EWALLET_SANDBOX_NAMESPACE` (cấm `ewallet-agents`); URL `{endpoint}api/a2a-sandboxes/{ns}/{agent}/`; contextId = request_id | SOURCE_VERIFIED |
| `x-kagent-host` | cùng file | 289 | hard-code `f"{request.target_agent}.ewallet-agents"` (đường non-sandbox) | SOURCE_VERIFIED |
| `GatewayPolicy.from_env` | cùng file | 296 | **không** đọc endpoint từ env; gọi từ `byo_coordinator.py` | SOURCE_VERIFIED |
| Default policy | cùng file | 445 | `self._gateway = gateway or GatewayPolicy()` | SOURCE_VERIFIED |
| Workflows | `ai_platform/llm_agent/byo_coordinator.py` | 99 | `EWALLET_COORDINATOR_WORKFLOWS` (`fraud` / `fraud,drift`) | SOURCE_VERIFIED |
| Idempotency | `byo_coordinator.py` 69; `redis_idempotency.idempotency_store_from_env` | — | `EWALLET_IDEMPOTENCY_REDIS_URL`, `_TENANT`, `_PRINCIPAL` bắt buộc | SOURCE_VERIFIED |
| NL intake prompt | `ai_platform/llm_agent/nl_intake.py` | 66, 259 | `SYSTEM_PROMPT` hằng số trong mã; `MAX_PROMPT_CHARS = 2000` | SOURCE_VERIFIED |
| LLM host | `ai_platform/llm_agent/model_config.py` | 40–41, 85 | `APPROVED_LLM_HOST = "agentgateway.ewallet-agents.svc.cluster.local"`, port 3000, `validate_base_url` strict | SOURCE_VERIFIED |
| MCP client host | `ai_platform/llm_agent/byo_mcp.py` | 28–29, 219 | `MCP_GATEWAY_HOST = "mcp-gateway.ewallet-agents.svc"`, 8080; `validate_gateway_route` strict; token `EWALLET_MCP_SERVICE_TOKEN` | SOURCE_VERIFIED |
| Feature-RAG Specialist | `ai_platform/llm_agent/byo_feature_rag_specialist.py` | 79, 85 | `APPROVED_MCP_GATEWAY=…/feature-rag/mcp`; tool {feature_lookup, fraud_scoring, knowledge_search}; `PROMPT_REVISION="feature-rag-specialist/byo-v1"` | SOURCE_VERIFIED |
| Drift Specialist | `ai_platform/llm_agent/byo_drift_specialist.py` | 76, 82 | `…/drift/mcp`; tool {drift_detection}; deterministic, không LLM | SOURCE_VERIFIED |
| Chart namespace | `infra/helm/llm-agent-platform/` | 72 lần `.Values.namespace` | hầu hết đã tham số hóa | SOURCE_VERIFIED |
| OTEL literal | `templates/agents.yaml` | 151 | `otel-collector.ewallet-agents.svc.cluster.local:4317` hard-code | SOURCE_VERIFIED |
| RemoteMCPServer URL | `values.yaml` 404 → `templates/agents.yaml` 489 | — | `agents.mcpGatewayUrl` | SOURCE_VERIFIED |
| Idempotency tenant | `values.yaml` | 590–591 | `tenant: ewallet-agents`, `principal: cluster.local/ns/kagent/sa/kagent-controller` | SOURCE_VERIFIED |
| A2A sandbox routes | `values.yaml` 668; `templates/policy-gateways.yaml` | 207–225 | chart `fail` nếu `sandboxRoutes.namespace` = agents ns; route `/api/a2a-sandboxes/<ns>/<agent>/` | SOURCE_VERIFIED |
| crossNamespace | `values.yaml` | 605 | `crossNamespace.flows: []` mặc định; flows feature-rag sinh NetworkPolicy ở ns đích | SOURCE_VERIFIED |
| WorkerPool | `infra/kubernetes/agent-sandbox/namespace.yaml` | — | `business-agents`, ateom vá `0.0.9-ewallet.0291af52.d8d1db43@sha256:68a6f1a8…`, gVisor, 500m/1Gi req, KEDA min 2 max 3 | SOURCE_VERIFIED |
| S1 access | `infra/kubernetes/agent-sandbox/s1-access.yaml` | — | raw manifest ngoài chart: NetworkPolicy + AuthorizationPolicy cho `ns/ewallet-sandbox/sa/default` | SOURCE_VERIFIED |
| Fleet template | `infra/kubernetes/agent-sandbox/fleet.yaml.tmpl` | — | `workerPoolRef{apiGroup,kind,name}`, snapshot `gs://ate-snapshots/ewallet-sandbox/<campaign>/<agent>/`, allowedDomains, proxy | SOURCE_VERIFIED |
| Prompt Registry | `ops/llm_agent/prompt_registry.py`; `infra/helm/llm-agent-platform/prompts/manifest.json` | — | agentregistry v0.4.0 `/v0/prompts/<agent>-system/<ver>`, SHA-256 fail-closed; ops tool, không Agent nào gọi lúc runtime | SOURCE_VERIFIED |

### B.3 V1 CI/CD (SOURCE_VERIFIED, HISTORICAL_RUNTIME_EVIDENCE)

- Jenkins (một executor, PVC `jenkins-home` 32Gi), Job DSL trong `infra/kubernetes/jenkins/jenkins.yaml`, shared pipeline
  `jenkins/pipeline/componentPipeline.groovy` qua `Jenkinsfile.llm-agent`.
- 35 module trong `scripts/ci` (8.063 dòng), catalog `jenkins/catalog/{components,release-units,snapshot-provenance}.json`,
  8 contract trong `jenkins/cd/`.
- Bounded rehearsal trong `ewallet-cicd` (quota 2 CPU / 6Gi / 6 Job), Cloud Build + Artifact Registry, Trivy gate
  (`scripts/ci/image_scan.py`, exception `jenkins/security/vulnerability-exceptions.json`).
- Batch 2B-1 (2026-11-18, `d84ea4e9`): Prompt Management #3, MCP #2, Coordinator #1, Agent Fleet #1 SUCCESS; RAG #10
  VULNERABLE (11 finding); CI llm-agent 1.529 test PASS. Production không đổi (llm-agent rev 59, llm-agent-mcp rev 19).
  Evidence ngoài repo: `~/ewallet-evidence/cicd/b2b1-20261118T051531Z`. `HISTORICAL_RUNTIME_EVIDENCE`
- Giới hạn V1 (giữ nguyên trong lịch sử): rehearsal Agent là smoke Job/Deployment trong `ewallet-cicd`, **không** dựng
  SandboxAgent trên WorkerPool, không chạy luồng A2A → Specialist → MCP thật. Option B+ (`sandbox_candidate.py`,
  `fleet_promote.py`) là canary trong namespace production.

---

## C. Production Coupling

Kết quả render offline (`OFFLINE_TESTED`): args của `tests/llm_agent/test_agi_chart` (AGENTS + AGI_AGENTS + COMMON)
+ `-f agi/specialists-sandbox-only.yaml --set a2aGateway.sandboxRoutes.agents={feature-rag-specialist,drift-specialist}
--set agents.coordinator.byo.sandboxSpecialists={feature-rag-specialist,drift-specialist}`.

| Render | rc | Object | Phân bố namespace |
|---|---|---|---|
| production | 0 | 106 | 104 `ewallet-agents`, 1 `feature-store`, 1 `fraud-serving` |
| staging (`namespace.name=ewallet-agents-staging`, `sandboxRoutes.namespace=ewallet-sandbox-staging`) | 0 | 106 | 104 `ewallet-agents-staging`, **1 `feature-store`, 1 `fraud-serving`** |

Lần render đầu thất bại vì thiếu `agents.coordinator.byo.sandboxSpecialists` (chart fail-closed đúng thiết kế).

### C.1 Ma trận coupling

| # | Coupling | Vị trí | Loại | Hậu quả nếu không xử lý | Cách khử | Nhãn |
|---|---|---|---|---|---|---|
| C1 | A2A host hard-code | `programmatic_coordinator.py:63`, `GatewayPolicy.__post_init__` | mã | Coordinator staging gọi a2a-gateway production hoặc fail validation | allowlist gateway namespace (V2-P1) | SOURCE_VERIFIED |
| C2 | `x-kagent-host` suffix `.ewallet-agents` | `programmatic_coordinator.py:289` | mã | route non-sandbox trỏ vào agent production | dùng cùng namespace đã chọn | SOURCE_VERIFIED |
| C3 | MCP host | `byo_mcp.py:28` | mã | MCP client staging bị từ chối hoặc gọi mcp-gateway production | allowlist | SOURCE_VERIFIED |
| C4 | LLM host | `model_config.py:40` | mã | NL intake staging dùng agentgateway production (chung key/budget) | allowlist | SOURCE_VERIFIED |
| C5 | Specialist MCP endpoint | `byo_feature_rag_specialist.py:79`, `byo_drift_specialist.py:76` | mã | Specialist staging đọc qua gateway production | allowlist | SOURCE_VERIFIED |
| C6 | OTEL endpoint literal | `templates/agents.yaml:151` | chart | trace staging trộn vào collector production | thêm value `observability.otlpTracesEndpoint` (default giữ literal) | OFFLINE_TESTED |
| C7 | Idempotency tenant | `values.yaml:590` | value | staging dùng chung keyspace tenant `ewallet-agents` nếu chung Redis | override + Redis staging riêng | OFFLINE_TESTED |
| C8 | `RemoteMCPServer` URL | `values.yaml:404` | value | trỏ mcp-gateway production | override `agents.mcpGatewayUrl` | OFFLINE_TESTED |
| C9 | crossNamespace NetworkPolicy | flows feature-rag → `feature-store`, `fraud-serving` | chart | **staging tạo object trong namespace production** | staging values `crossNamespace.flows: []` + Redis/KServe staging | OFFLINE_TESTED |
| C10 | Feast production DB | `EWALLET_FRAUD_ONLINE_DB=0`, `EWALLET_RAG_ONLINE_DB=3`, `EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true`; secret `feast-online-redis` (`g3/g3.yaml` 94–110) | value/secret | staging đọc dữ liệu production | Redis staging seeded, `ALLOW_PRODUCTION_DB=false` | SOURCE_VERIFIED |
| C11 | KServe production | `http://fraud-detector-predictor.fraud-serving.svc.cluster.local` (`g3/g3.yaml` 204–209) | value | staging tạo tải lên model production | Level A: KServe staging cùng artifact; tạm thời cấm | SOURCE_VERIFIED |
| C12 | S1 access ngoài chart | `agent-sandbox/s1-access.yaml` | raw manifest | staging cần policy cho `ns/<staging-sandbox>/sa/default` trên gateway staging | đưa vào chart staging, không sửa bản production | SOURCE_VERIFIED |
| C13 | kagent watch scope | `kagent-pilot/values/kagent-sandbox-watch-overlay.yaml` | Helm values controller dùng chung | SandboxAgent staging không reconcile | owner chọn L1 | SOURCE_VERIFIED |
| C14 | KEDA watch scope | `infra/helm/keda/values-sandbox-watch.yaml` | Helm values | không có KEDA cho staging | staging dùng replicas cố định, không cần KEDA | SOURCE_VERIFIED |
| C15 | Snapshot store dùng chung | `kagent-pilot/values/substrate.yaml` (RustFS `ate-snapshots`, 256Mi trong values pilot) | hạ tầng chung | cô lập chỉ bằng prefix; dung lượng có thể cạn | prefix `ewallet-sandbox-staging/<run>/`, dọn sau run | SOURCE_VERIFIED / dung lượng REQUIRES_RUNTIME_VERIFICATION |
| C16 | Prompt Registry ns | `ewallet-prompts`, port 12121→8080 | dịch vụ chung, chỉ đọc | staging đọc cùng nội dung (mong muốn) | read-only, SHA pin; không publish từ staging | SOURCE_VERIFIED |

Kết luận C: **không có coupling nào bắt buộc phải sửa logic nghiệp vụ**. C1–C5 là phân giải endpoint (được phép),
C6–C12 là chart/values, C13–C16 là hạ tầng dùng chung có kiểm soát.

---

## D. Substrate / kagent Feasibility (Q1–Q7)

| Q | Câu hỏi | Kết luận | Bằng chứng | Nhãn |
|---|---|---|---|---|
| Q1 | Nhiều WorkerPool trên một Substrate? | **Có.** `WorkerPool` là CRD Namespaced; một ate-api-server Syncer đã quản lý đồng thời worker của `ewallet-sandbox/business-agents` và `kagent-pilot/kagent-pilot` | `infra/kubernetes/substrate-scale/upstream/ate.dev_workerpools.v0.0.9.yaml` (scope Namespaced); `docs/evidence/edai2/agents/p2a-closure-20261116T151631Z/baseline/ateapi-syncer-at-wake.txt` | SOURCE_VERIFIED + HISTORICAL_RUNTIME_EVIDENCE |
| Q2 | SandboxAgent ở namespace khác? | **Có, nhưng pool phải cùng namespace.** `workerPoolRef` chỉ có {apiGroup, kind, name} — không có `namespace`. `kagent-pilot` đã chạy `pilot-sandbox-agent` Ready | `infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json`; `docs/evidence/edai2/screenshots/runtime/raw/03_sandbox_substrate.txt` (2026-11-04) | SOURCE_VERIFIED + HISTORICAL_RUNTIME_EVIDENCE |
| Q3 | Dùng chung controller? | **Có cho Substrate `ate-system`** (không cần đổi gì). **kagent: phải mở rộng `watchNamespaces` + `rbac.namespaces`**, làm restart controller dùng chung (gián đoạn reconcile production ngắn). Thay thế: dùng `kagent-pilot` đã được watch và có env-source access | `kagent-sandbox-watch-overlay.yaml`; `scripts/llm_agent/sandbox_watch.sh`; `kagent-platform/kagent-values.yaml:12` | SOURCE_VERIFIED; thời gian gián đoạn REQUIRES_RUNTIME_VERIFICATION |
| Q4 | Snapshot cô lập? | **Chỉ cô lập theo prefix** trong RustFS in-cluster dùng chung (các URL `gs://ate-snapshots/...` được Substrate phục vụ bởi RustFS, **không phải GCS**). Snapshot có thể chứa bộ nhớ tiến trình gồm token → staging phải dùng token staging riêng | `kagent-pilot/values/substrate.yaml`; `agent-sandbox/README` | SOURCE_VERIFIED; dung lượng còn trống REQUIRES_RUNTIME_VERIFICATION |
| Q5 | Scaling? | Pool staging dùng `replicas` cố định (1–2), **không KEDA** (KEDA không watch ns mới và không cần). Có `nodeSelector`/`tolerations` trong template để đặt lên cpu-pool | CRD WorkerPool template fields; `values-sandbox-watch.yaml` | SOURCE_VERIFIED |
| Q6 | Proxy egress + mesh identity? | **Tái dùng được.** Proxy vá nằm trong `ateomImage` theo từng pool → pool staging dùng cùng digest. Proxy re-originate từ worker pod nên ztunnel gán `cluster.local/ns/<staging-sandbox>/sa/default`. Namespace staging cần label `istio.io/dataplane-mode: ambient` và AuthorizationPolicy riêng | `kagent-pilot/patches/substrate-0.0.9-sandbox-egress-proxy.patch` (`cmd/ateom-gvisor/egress_proxy.go`); `kagent-platform/namespace-g3.yaml` | SOURCE_VERIFIED; enrolment thực REQUIRES_RUNTIME_VERIFICATION |
| Q7 | Wake/sleep? | Staging phải **không có PDB** và phải teardown trước sleep. Dùng resize-only wake (không unpause KEDA). Mọi `cloud_lab wake-*` khác unpause KEDA → sleep kế tiếp cần approval guarded/KEDA | Batch 2B-1 runbook; memory dự án | HISTORICAL_RUNTIME_EVIDENCE |

Rủi ro Q: WorkerPool template **không có `serviceAccountName`** → worker chạy bằng `default` SA của namespace, tức
**namespace chính là identity**. Hệ quả tích cực: staging namespace riêng ⇒ identity riêng, AuthorizationPolicy
production không admit staging. Hệ quả cần chú ý: không được đặt pool staging trong `ewallet-sandbox`
(sẽ thừa hưởng quyền production). `SOURCE_VERIFIED`

### D.1 Lựa chọn namespace sandbox staging

| Phương án | Ưu | Nhược | Đề xuất |
|---|---|---|---|
| L1-a: ns mới `ewallet-sandbox-staging` | identity sạch, tên rõ, PSA/quota riêng | cần nâng values kagent (restart controller dùng chung) | **Ưu tiên** (owner approval) |
| L1-b: tái dùng `kagent-pilot` | đã watch, có env-source access, không restart | ns pilot có pool release-ateom và lịch sử; identity `ns/kagent-pilot/sa/default` dùng chung với pilot | fallback nếu không duyệt restart |
| L1-c: đặt trong `ewallet-sandbox` | không đổi gì | **thừa hưởng quyền production** — vi phạm cô lập | **Loại** |

---

## E. Data and Security Isolation

| Thành phần | Production | Staging Level A | Staging Level B | Cơ chế cô lập | Nhãn |
|---|---|---|---|---|---|
| Namespace agents | `ewallet-agents` | `ewallet-agents-staging` | như A | namespace, quota, PSA | OFFLINE_TESTED (render) |
| Namespace sandbox | `ewallet-sandbox` | `ewallet-sandbox-staging` (hoặc `kagent-pilot`) | như A | namespace = identity | SOURCE_VERIFIED |
| WorkerPool | `business-agents` (KEDA 2–3) | `staging-agents`, replicas 1–2, không KEDA, cùng `ateomImage` digest | như A | CRD namespaced | SOURCE_VERIFIED |
| a2a/mcp/agentgateway | ns production | bản sao trong ns staging (chart render) | như A | allowlist + AuthorizationPolicy | REQUIRES_RUNTIME_VERIFICATION |
| Redis idempotency | secret production | Redis/Valkey ephemeral trong ns staging, tenant `ewallet-agents-staging` | như A | instance riêng + tenant riêng | SOURCE_VERIFIED |
| Feast online (fraud/rag) | `feature-store` DB 0 / 3 | Redis staging seed fixture tổng hợp, `ALLOW_PRODUCTION_DB=false` | snapshot đã ẩn danh, đã freeze, nạp vào Redis staging | instance riêng; không NetworkPolicy vào `feature-store` | SOURCE_VERIFIED |
| KServe fraud-detector | `fraud-serving` v1 | KServe/predictor staging cùng artifact (hoặc chưa bật fraud_scoring E2E — xem I) | InferenceService staging cùng model URI + digest | ns riêng; cấm flow vào `fraud-serving` | REQUIRES_RUNTIME_VERIFICATION |
| Drift baselines | mount artifact SHA-pinned | cùng artifact, mount trong drift-mcp staging | như A | file-based, deterministic | SOURCE_VERIFIED |
| LLM provider | agentgateway + key production | agentgateway staging + key/budget staging riêng (owner) | như A | secret riêng, rate limit | UNKNOWN (owner) |
| MCP token | `mcp-token-sandbox-<subject>-<server>-<release>` | token staging riêng, release id staging | như A | secret riêng; snapshot có thể chứa token | SOURCE_VERIFIED |
| Snapshot | RustFS `ate-snapshots/ewallet-sandbox/...` | prefix `ewallet-sandbox-staging/<run>/`, xoá sau run | như A | prefix-only | SOURCE_VERIFIED |
| Prompt Registry | `ewallet-prompts` | đọc-only cùng registry, SHA pin | như A | không quyền publish | SOURCE_VERIFIED |
| OTEL | collector production | collector staging hoặc tắt | như A | value mới (C6) | SOURCE_VERIFIED |
| GCS / production Redis | — | **cấm** | **cấm** | RBAC SA staging không có quyền | SOURCE_VERIFIED (policy) |

Ràng buộc bảo mật giữ nguyên: không bypass proxy, không hạ mTLS, không bỏ validator, không thêm LLM vào Drift Specialist,
không ghi production Feature Store.

---

## F. Prompt Management Decision (Prompt Registry hiện có vs Langfuse)

**Quyết định: giữ Prompt Registry hiện có (agentregistry v0.4.0 + Git manifest SHA-256). Langfuse: `DEFER` — không đưa
vào V2.** `SOURCE_VERIFIED` cho hiện trạng; `DOCUMENTATION_SUPPORTED` cho đánh giá Langfuse.

| # | Câu hỏi | Prompt Registry hiện có | Langfuse | Kết luận |
|---|---|---|---|---|
| 1 | Prompt nào thực sự chạy lúc runtime? | `fraud-coordinator` v5 (declarative router, 853 B) qua `systemMessageFrom` ConfigMap; NL intake dùng `SYSTEM_PROMPT` **hằng số trong mã** (`nl_intake.py:66`); Specialist deterministic (`PROMPT_REVISION` chỉ là nhãn) | — | Bề mặt prompt rất nhỏ |
| 2 | Ai đọc prompt? | `ops/llm_agent/prompt_registry.py` (ops/CI), không Agent nào gọi lúc runtime | SDK runtime trong Agent | Langfuse thêm dependency runtime |
| 3 | Versioning | manifest v1–v5 / v1–v2 trong Git | label/version server-side | Git đã đủ, có review |
| 4 | Toàn vẹn | SHA-256 fail-closed với Git manifest | hash không mặc định gắn Git | Registry tốt hơn cho freeze |
| 5 | Rollback | đổi version trong values + verify gate | đổi label | tương đương |
| 6 | Promotion giữa môi trường | cùng registry, đọc-only từ staging; `gate pre/post` | project/env riêng | Registry đủ |
| 7 | Tracing/observability prompt | OTEL hiện có | trace UI mạnh | lợi ích có thật nhưng ngoài phạm vi V2 |
| 8 | Evaluation | regression gate deterministic (mục I) | dataset/eval UI | eval LLM chỉ áp dụng NL intake |
| 9 | Dữ liệu/PII | trong cụm | Cloud: egress dữ liệu prompt/trace ra ngoài — **cấm** gửi giao dịch/PII; self-host: Postgres + ClickHouse + Redis + S3 (nặng với ngân sách RAM/chi phí) | rủi ro cao hơn |
| 10 | Chi phí vận hành | đã chạy, Batch 2B-1 Prompt Management #3 SUCCESS | tài khoản mới hoặc stack self-host | Registry rẻ hơn |

Đề xuất bổ sung (V2-P2, cần owner duyệt vì chạm mã NL intake): đăng ký `fraud-coordinator-intake` có **nội dung
byte-identical** với `SYSTEM_PROMPT` hiện tại vào manifest, Coordinator resolve lúc khởi động với SHA pin và fallback
về hằng số khi SHA khớp — hành vi model giữ nguyên (prompt identity gate, mục I). Điều kiện xét lại Langfuse: khi có
nhiều prompt LLM thay đổi thường xuyên và nhu cầu eval dataset; chỉ self-host trong cụm hoặc Cloud với dữ liệu tổng hợp.

---

## G. CI/CD V1 Reuse Matrix

Bằng chứng runtime: "2B-1" = Batch 2B-1 tại `d84ea4e9` (2026-11-18); "N10" = N10-E2 (2026-11-04). Không có mục nào
`REMOVE` trong V2: bằng chứng lịch sử và Option B+ được giữ.

| File | Trách nhiệm | Runtime evidence | Caller | Vai trò V2 | Phân loại | Thay đổi | Rủi ro | Điều kiện gỡ bỏ |
|---|---|---|---|---|---|---|---|---|
| `scripts/ci/run_ci.py`, `catalog.py`, `change_detection.py`, `provision_tools.py`, `junit_unittest.py`, `evidence.py` | CI scope + test + evidence | 2B-1: 1.529 test PASS | `componentPipeline.groovy` | giữ nguyên CI | KEEP_AS_IS | — | thấp | — |
| `scripts/ci/cloud_build.py`, `image_scan.py`, `source_snapshot.py`, `render_jenkins_runtime.py` | build, scan, snapshot, render runtime | 2B-1: 5 build, 9 scan | pipeline | dùng cho image staging | KEEP_AS_IS (image_scan: KEEP_AND_ADAPT ở mục exception) | hợp nhất nguồn exception (RAG `trivyignore` vs `jenkins/security/vulnerability-exceptions.json`) | trung bình: lệch exception | — |
| `release_lock.py`, `k8s_api.py`, `registry_gate.py` | khóa release, client K8s stdlib, prompt gate | 2B-1 | bounded_cd, promote | dùng lại | KEEP_AS_IS | — | thấp | — |
| `release_request.py`, `release_policy.py`, `release_units.py`, `release_dispatch.py` | plan + policy + unit | 2B-1 | pipeline | thêm stage `staging` giữa rehearsal và production | KEEP_AND_ADAPT | stage enum + policy "production yêu cầu staging verdict PASS cùng digest" | trung bình | — |
| `bounded_cd.py`, `cd_smoke.py` | rehearsal trong `ewallet-cicd` | 2B-1 (fix race `d84ea4e9`) | handler CD | giữ cho component không phải Agent; thêm target staging | KEEP_AND_ADAPT | target namespace staging + teardown | trung bình | — |
| `sandbox_agents.py`, `specialist_e2e.py`, `deploy_identity.py` | render SandboxAgent, E2E Specialist, identity | offline + N10 | agent-fleet contract | nòng cốt staging Level A | KEEP_AND_ADAPT | nhận namespace/pool staging, snapshot prefix staging | trung bình | — |
| `sandbox_candidate.py`, `fleet_promote.py` | Option B+ canary production | chưa chạy production qua CD | sandbox-candidate contract | promotion sau staging | KEEP_AND_ADAPT → DEPRECATE_AFTER_V2_ACCEPTANCE (chỉ nhánh "canary thay staging") | gate: chỉ chạy khi staging PASS | cao (chạm production) | staging Level A PASS ×2 + owner approval |
| `promote.py`, `production_release.py`, `runtime_verify.py`, `release_state.py`, `release_handoff.py`, `registry_release.py` | đường production | offline-tested; production chưa chạy qua CD | release jobs | giữ, thêm điều kiện staging | KEEP_AND_ADAPT | handoff chứa staging verdict sha256 | cao | — |
| `rbac_matrix.py`, `jenkins/cd/production-rbac-matrix.json` | ma trận RBAC | offline | tests | thêm SA staging | KEEP_AND_ADAPT | hàng staging | thấp | — |
| `verify_rag_smoke.py`, `jenkins/cd/rag-pipeline.contract.json`, `cicd-runtime/rag/rag-ingestion.yaml` | RAG CD | N10 #8 PASS; 2B-1 #10 VULNERABLE | RAG job | giữ; sửa image (row 33) | KEEP_AND_ADAPT | không đổi CD; sửa Dockerfile runtime ở V2-P5 | trung bình | — |
| `streaming_identity.py`, `streaming_verify.py`, `streaming-block-a.profile.json` | Block A streaming (row 36) | offline; chưa chạy Jenkins | streaming job | handler thật | KEEP_AND_ADAPT | chờ identity M6/P6 | cao (dữ liệu) | — |
| `jenkins/cd/streaming.contract.json`, `cicd-runtime/streaming/streaming-cd.yaml` | smoke Job (không Kafka) | N10 smoke | streaming job | thay bằng Block A | DEPRECATE_AFTER_V2_ACCEPTANCE | — | thấp | Block A Jenkins PASS |
| `jenkins/cd/agent-fleet.contract.json`, `coordinator.contract.json`, `mcp-servers.contract.json` | contract CD Agent | 2B-1 SUCCESS (smoke) | pipeline | target staging | KEEP_AND_ADAPT | thêm `staging` profile | trung bình | — |
| `infra/kubernetes/cicd-runtime/agents/{agent-fleet-cd,coordinator-cd}.yaml`, `mcp/mcp-cd.yaml` | runtime smoke trong `ewallet-cicd` | 2B-1 | bounded_cd | được staging thay thế cho Agent | DEPRECATE_AFTER_V2_ACCEPTANCE | — | thấp | staging Level A PASS |
| `jenkins/pipeline/componentPipeline.groovy`, `Jenkinsfile.llm-agent`, `jenkins/catalog/*.json` | pipeline + catalog | 2B-1 | Job DSL | thêm stage Staging | KEEP_AND_ADAPT | stage mới, không bỏ Trivy/test | trung bình | — |
| `Jenkinsfile.rag` | file cũ không phải pipeline đã chạy (P0 audit F10) | — | tài liệu | — | DEPRECATE_AFTER_V2_ACCEPTANCE | sửa link tài liệu | thấp | tài liệu trỏ đúng `Jenkinsfile.llm-agent` |
| `infra/kubernetes/jenkins/jenkins.yaml`, `rbac/batch-b-isolated.yaml` | controller + RBAC rehearsal (applied) | 2B-1 | — | giữ | KEEP_AS_IS | — | thấp | — |
| `infra/kubernetes/jenkins/prompt-registry-access.proposed.yaml` | NetworkPolicy tới registry 8080 | áp dụng 2B-1 | — | giữ | KEEP_AS_IS | — | thấp | — |
| `rbac/agent-cicd-production.proposed.yaml`, `batch-d*`, `production-rollout-rbac.proposed.yaml` | RBAC production đề xuất | inert | tests `test_inert_proposals.py` | thêm biến thể staging, production giữ inert | KEEP_AND_ADAPT | file staging RBAC mới (proposed) | trung bình | — |
| `rbac/streaming-cd-identity.proposed.yaml` | identity streaming | inert | streaming_identity | giữ | KEEP_AS_IS | — | — | — |

---

## H. Target Architecture

### H.1 Decision matrix (A / B / C)

Thang 1–5 (5 tốt nhất). Trọng số theo ưu tiên owner: an toàn production và tính chứng minh trước, chi phí sau.

| Tiêu chí (trọng số) | A: Dedicated staging (ns riêng, data plane riêng) | B: Partially isolated (Agent staging, dùng chung gateway/data production read-only) | C: Controlled production canary (Option B+) |
|---|---|---|---|
| An toàn production (5) | 4 — chỉ chia sẻ controller | 2 — chia gateway, Redis, KServe | 2 — chạy trong production |
| Chứng minh hành vi Agent thật (5) | 5 — A2A→Specialist→MCP thật | 4 | 5 |
| Cô lập dữ liệu (4) | 5 | 2 — đọc DB 0 production | 1 |
| Độ phức tạp thay đổi (3) | 3 — refactor endpoint + chart | 4 — ít đổi nhưng phải vá policy production | 4 — đã có mã |
| Chi phí (2) | 3 — +1 node trong run | 4 | 5 |
| Tác động lên controller dùng chung (3) | 3 — restart kagent một lần (hoặc dùng `kagent-pilot`) | 3 | 4 |
| Khả năng rollback (3) | 5 — xoá namespace | 3 | 2 |
| **Tổng có trọng số** (Σ trọng số = 25) | **4.16** | 3.04 | 3.16 |

Kết luận: **A (dạng A′)** là kiến trúc staging; **C giữ làm bước promotion sau staging**, không thay thế staging;
B bị loại vì cần sửa AuthorizationPolicy/NetworkPolicy production và đọc dữ liệu production.

### H.2 Level A vs Level B

| Khía cạnh | Level A — Agent thật + phụ thuộc tổng hợp | Level B — E2E nghiệp vụ cô lập hoàn toàn |
|---|---|---|
| Agent/gateway/SandboxAgent | thật (image digest release) | thật |
| Feast | Redis staging seed fixture tổng hợp | snapshot đã freeze, ẩn danh |
| KServe | predictor staging cùng artifact model (hoặc fraud_scoring E2E chỉ đối chiếu contract nếu chưa có predictor — owner) | InferenceService staging cùng model URI + digest |
| LLM (NL intake) | key staging, ngân sách thấp, prompt SHA pin | như A |
| Chứng minh | routing, authz, identity, SpecialistEvidence contract, idempotency, prompt identity | thêm parity điểm fraud / drift với baseline production |
| Rủi ro | thấp | trung bình (sao chép dữ liệu, chi phí KServe) |
| Đề xuất | **cổng V2 bắt buộc (V2-P3)** | V2-P4 |

### H.3 Sơ đồ đích

```
 Jenkins (ewallet-jenkins)
   CI ─► Cloud Build ─► Trivy gate ─► Rehearsal (ewallet-cicd, giữ nguyên)
        ─► STAGING (mới)  ─► verdict sha256 ─► Handoff ─► [owner] Option B+ canary ─► Production

 STAGING (cùng cụm, ns riêng, không PDB, không KEDA, teardown sau run)
 ┌ ewallet-agents-staging (ambient) ─────────────────────────────────────────┐
 │ agentgateway-stg  a2a-gateway-stg  mcp-gateway-stg  fraud-coordinator-stg │
 │ feature-rag-mcp-stg ─► redis-stg (Feast fixture)   drift-mcp-stg (artifact)│
 │                    └─► fraud-predictor-stg (Level A/B)                    │
 │ redis-idem-stg (tenant ewallet-agents-staging)    otel: tắt/collector-stg │
 └───────────────────────────────────────────────────────────────────────────┘
 ┌ ewallet-sandbox-staging (ambient, PSA privileged, default SA tokenless) ──┐
 │ WorkerPool staging-agents (ateom vá, cùng digest, replicas 1–2)           │
 │ SandboxAgent feature-rag-specialist, drift-specialist (snapshot prefix stg)│
 └───────────────────────────────────────────────────────────────────────────┘
 Dùng chung (chỉ control plane): ate-system (Substrate), kagent controller, ewallet-prompts (read-only)
 Cấm: object trong ewallet-agents / ewallet-sandbox / feature-store / fraud-serving; production Redis/GCS
```

---

## I. Implementation Plan (V2-P1 … V2-P5)

### V2-P1 — Phân giải endpoint theo allowlist + chart staging (offline)

| File | Thay đổi |
|---|---|
| `ai_platform/llm_agent/gateway_namespace.py` (mới) | `APPROVED_GATEWAY_NAMESPACES = frozenset({"ewallet-agents", "ewallet-agents-staging"})`; `gateway_namespace(env)` đọc `EWALLET_GATEWAY_NAMESPACE`, mặc định `ewallet-agents`, giá trị ngoài allowlist → `ValueError` (fail-closed) |
| `ai_platform/llm_agent/programmatic_coordinator.py` | `APPROVED_A2A_GATEWAY` và suffix `x-kagent-host` (dòng 63, 289) tính từ namespace đã chọn; `GatewayPolicy.from_env` (296) truyền namespace; `__post_init__` vẫn kiểm tra exact host/port/path |
| `ai_platform/llm_agent/byo_mcp.py` | `MCP_GATEWAY_HOST` (28) theo namespace; `validate_gateway_route` giữ strict |
| `ai_platform/llm_agent/model_config.py` | `APPROVED_LLM_HOST` (40) theo namespace; `validate_base_url` giữ strict |
| `byo_feature_rag_specialist.py:79`, `byo_drift_specialist.py:76` | `APPROVED_MCP_GATEWAY` theo namespace |
| `infra/helm/llm-agent-platform/templates/agents.yaml:151` | value `observability.otlpTracesEndpoint`, default = literal hiện tại |
| `templates/agents.yaml` | env `EWALLET_GATEWAY_NAMESPACE` = `.Values.namespace.name` (chỉ khi khác default → production render không đổi) |
| `infra/helm/llm-agent-platform/staging/values-staging.yaml` (mới) | namespace, sandboxRoutes, `mcpGatewayUrl`, idempotency tenant/principal, `crossNamespace.flows: []`, Feast/KServe endpoint staging, `ALLOW_PRODUCTION_DB=false`, S1-equivalent AuthorizationPolicy cho `ns/ewallet-sandbox-staging/sa/default` |
| `infra/kubernetes/agent-sandbox-staging/` (mới, proposed) | namespace (ambient label, PSA, quota có tính 3 ConfigMap Istio), WorkerPool `staging-agents`, không PDB |

Regression test (bắt buộc PASS, offline):
- `tests/llm_agent/test_gateway_namespace.py`: default = production; giá trị lạ/rỗng/`ewallet-sandbox` bị từ chối; mọi host sinh ra khớp exact cho cả hai namespace.
- **Production Helm render equivalence**: render production trước/sau V2-P1 bằng args `test_agi_chart` → diff byte-identical (106 object).
- Staging render: 0 object ngoài `ewallet-agents-staging`; grep 0 chuỗi `ewallet-agents.svc` / `feature-store` / `fraud-serving`.
- Toàn bộ suite hiện có: `tests/llm_agent`, `tests/ci` (gồm `test_controller_stdlib.py`), catalog llm-agent 1.529 test.

### V2-P2 — Prompt identity + Prompt Registry cho staging (offline)
- `infra/helm/llm-agent-platform/prompts/manifest.json` + `prompts/fraud-coordinator-intake/v1.md` byte-identical với `nl_intake.SYSTEM_PROMPT` (owner duyệt).
- `ops/llm_agent/prompt_registry.py`: lệnh `verify` cho staging (read-only).
- Test: SHA của file prompt == SHA của `SYSTEM_PROMPT`; Coordinator từ chối khởi động khi SHA lệch.

### V2-P3 — Staging Level A trong CD (cần cloud, owner approval)
- `scripts/ci/staging_deploy.py` (mới) hoặc mở rộng `bounded_cd.py`: namespace guard (`*-staging` only), render, apply, wait, run gates, collect, teardown luôn chạy.
- `jenkins/cd/staging.contract.json`, `jenkins/catalog/release-units.json` (stage `staging`), `release_policy.py` (production cần staging verdict cùng digest), `componentPipeline.groovy` (stage Staging).
- RBAC proposed: `infra/kubernetes/jenkins/rbac/staging-cd.proposed.yaml` (Role chỉ trong 2 ns staging).
- Test: `tests/ci/test_staging_deploy.py` (guard từ chối ns production, teardown khi lỗi), `test_release_policy.py` (thiếu staging verdict → refuse).

### V2-P4 — Level B
- Feast snapshot đã ẩn danh + KServe staging cùng model URI/digest; parity score vs baseline.

### V2-P5 — RAG row 33 và Streaming row 36
- RAG: xem J.3; Streaming: handler Block A qua `streaming_verify.py` sau khi identity M6/P6 được duyệt.

### I.1 Behavioural regression gates (áp dụng V2-P1 offline, V2-P3 runtime)

| Gate | Kiểm tra | Tiêu chí PASS |
|---|---|---|
| Fraud | cùng request fixture → `fraud_scoring` qua MCP staging | score/decision bằng baseline (Level A: predictor cùng artifact; Level B: parity tập freeze) |
| Drift | `drift_detection` với baselines/window SHA-pinned | output byte-equal với baseline offline; Specialist không gọi LLM |
| Combined workflows | `EWALLET_COORDINATOR_WORKFLOWS=fraud,drift` | thứ tự gọi Specialist + kết quả hợp nhất như production test |
| MCP authorization | token sai/thiếu, tool ngoài allowlist, route sai | 401/403; tool set đúng {feature_lookup, fraud_scoring, knowledge_search} / {drift_detection} |
| A2A route authorization | gọi route sandbox production từ staging; identity khác | bị từ chối; chỉ `ns/ewallet-sandbox-staging/sa/default` được admit |
| SpecialistEvidence | schema contract | validator hiện có PASS, không trường mới |
| Prompt identity | SHA prompt trong registry/ConfigMap/hằng số | khớp manifest |
| Redis idempotency | replay cùng request_id, lease/TTL | một lần thực thi; khóa nằm dưới tenant staging |
| Snapshot isolation | liệt kê prefix sau run | chỉ prefix staging; dọn sạch sau teardown |
| Production Helm render equivalence | render production trước/sau | byte-identical |

---

## J. Costs and Risks

### J.1 Mô hình chi phí

| Hạng mục | Giá trị | Nhãn |
|---|---|---|
| Cụm thức (5 × e2-standard-4) | ≈ $1.0/h tổng, ≈ $0.83/h node | HISTORICAL_RUNTIME_EVIDENCE (2B-1) |
| Batch 2B-1 | 190 phút, ≈ $3.2 | HISTORICAL_RUNTIME_EVIDENCE |
| Staging tăng thêm (requests ước tính) | WorkerPool 2 × 500m/1Gi + 3 gateway + Coordinator + 2 MCP + 2 Redis ≈ 2.5–3.5 vCPU / 5–7 GiB → khả năng +1 node ≈ $0.17/h | estimate, REQUIRES_RUNTIME_VERIFICATION |
| Một run staging Level A | ≈ 60–90 phút trên cụm đã thức → +$0.2–0.4 ngoài chi phí cụm | estimate |
| Level B (KServe staging) | thêm predictor ≈ 1 vCPU/2 GiB | estimate |
| LLM | vài chục request NL intake / run | UNKNOWN (giá provider) |
| Langfuse | $0 (không áp dụng) | — |

### J.2 Rủi ro

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Restart kagent controller khi mở rộng watch | trung bình | owner window; hoặc L1-b `kagent-pilot` |
| Refactor endpoint làm đổi hành vi production | trung bình | default production + render equivalence + test exact host |
| RustFS snapshot đầy (256Mi trong values pilot) | trung bình | đo dung lượng trước (K), dọn prefix |
| Staging để lại tài nguyên khiến sleep bị chặn | trung bình | không PDB, teardown luôn chạy, kiểm tra trước sleep |
| Jenkins một executor / PVC | thấp | 32Gi đã đủ cho 2B-1 (16G dùng) |
| Token trong snapshot | trung bình | token staging riêng, revoke sau run |
| Quota namespace mới bị 3 ConfigMap Istio ăn | thấp | tính vào quota |

### J.3 RAG row 33 (`HISTORICAL_RUNTIME_EVIDENCE` + `SOURCE_VERIFIED`)
- Build #10 VULNERABLE, 11 finding. Base `python:3.10.19-slim-trixie@sha256:ed9e9e63…` mang pip 23.0.1, setuptools 79.0.1,
  wheel 0.45.1 → nguồn của finding wheel/setuptools/jaraco.context/msgpack; các gói này **không** có trong `uv.lock`.
- Lock: feast 0.66.0 (CRITICAL), urllib3 2.8.0, filelock 4.1.0, fsspec 2025.12.0, setuptools 81.0.0; Python 3.10 ghim theo Airflow 3.3.1 / Feast 0.66.
- Lệch exception: `infra/docker/rag-ingestion/trivyignore` có CVE-2026-112940 (filelock, hết hạn 2027-01-31) nhưng gate
  Jenkins dùng `jenkins/security/vulnerability-exceptions.json` (rỗng).
- Khắc phục đề xuất (V2-P5, không làm trong V2-P0): gỡ pip/setuptools/wheel khỏi runtime stage; bump urllib3/fsspec;
  feast CRITICAL cần kiểm tra tương thích Python 3.10/Airflow (rủi ro); hợp nhất một nguồn exception có review + hạn.
  **Không tắt Trivy. Không khẳng định hết CVE khi chưa rescan.**

### J.4 Streaming row 36
- Hiện trạng: contract V1 chỉ chạy smoke Job (không Kafka/Flink). `scripts/ci/streaming_verify.py` đã có driver Block A
  gọi operator fail-closed `scripts/streaming/block_a_cloud.sh` (namespace `verify-*`, Redis DB không-production, registry
  create-only, cleanup luôn chạy), nhưng chưa từng chạy qua Jenkins; identity streaming (`streaming-cd-identity.proposed.yaml`) còn inert. `SOURCE_VERIFIED`
- Không chạy Streaming trong V2-P0. Staging Agent không phụ thuộc streaming; row 36 đi riêng ở V2-P5.

---

## K. Runtime Verification Prerequisites (chỉ thực hiện khi owner cho phép cloud)

| # | Kiểm tra | Lệnh/thủ tục (read-only trừ khi ghi chú) | Tiêu chí |
|---|---|---|---|
| K1 | Dung lượng RustFS snapshot | `kubectl -n ate-system get pvc` + kích thước bucket `ate-snapshots` | còn ≥ 2× kích thước snapshot 2 Specialist |
| K2 | kagent watch hiện tại | `helm -n kagent get values kagent` | khớp overlay trong repo |
| K3 | Thời gian gián đoạn khi nâng watch | thực hiện trong owner window, đo thời gian controller Ready | < 2 phút, SandboxAgent production vẫn Ready |
| K4 | Pool thứ hai với ateom vá | apply WorkerPool staging (ghi) | worker Ready, Syncer liệt kê cả 2 pool |
| K5 | Ambient enrolment ns staging | `istioctl ztunnel-config workloads` | worker có identity `ns/ewallet-sandbox-staging/sa/default` |
| K6 | Authz phủ định | gọi mcp-gateway **production** từ worker staging | bị từ chối |
| K7 | Không object ngoài staging | `kubectl get all,networkpolicy,authorizationpolicy -A -l <campaign>` | chỉ 2 ns staging |
| K8 | Production không đổi | `helm history` llm-agent (59), llm-agent-mcp (19); replica/KEDA state như trước | không đổi |
| K9 | Node headroom | `kubectl describe nodes` requests | đủ cho ước tính J.1 |
| K10 | Teardown | xoá 2 ns staging, prefix snapshot | 0 tài nguyên còn lại; sleep thường không bị chặn |

---

## L. Decisions Requiring Owner Approval

| # | Quyết định | Đề xuất |
|---|---|---|
| L1 | Namespace sandbox staging: ns mới (restart kagent) hay `kagent-pilot` | ns mới `ewallet-sandbox-staging` trong owner window |
| L2 | Refactor phân giải endpoint (C1–C5) dưới Behavioral Freeze | duyệt, kèm gate render equivalence |
| L3 | Level A dùng predictor staging cùng artifact hay hoãn fraud_scoring E2E tới Level B | predictor staging cùng artifact |
| L4 | LLM key/budget staging riêng | key riêng, hạn mức thấp |
| L5 | Prompt `fraud-coordinator-intake` byte-identical (V2-P2) | duyệt |
| L6 | Langfuse | DEFER |
| L7 | Cửa sổ cloud cho V2-P3 (wake resize-only, chi phí ước tính ≤ $5) | khi V2-P1/P2 PASS offline |
| L8 | RAG remediation (feast CRITICAL) | bump + kiểm tra tương thích riêng ở V2-P5 |
| L9 | Option B+ chỉ sau staging PASS | duyệt |

---

## M. Final Recommendation

**GO cho V2-P1 (offline)** với phạm vi: module allowlist gateway namespace, cập nhật 5 hằng số endpoint, value OTEL,
`values-staging.yaml`, manifest namespace/WorkerPool staging dạng proposed, và các regression test ở I/I.1 — trong đó
production Helm render phải byte-identical. **Không** bao gồm cloud, Jenkins live, kagent upgrade hay deploy.

V2-P3 (staging runtime) chỉ bắt đầu khi: V2-P1 + V2-P2 PASS offline, owner quyết định L1–L4 và L7, và các kiểm tra K1–K2
read-only xác nhận giả định. Báo cáo này không tuyên bố staging đã sẵn sàng dựa trên render Helm; render chỉ chứng minh
coupling ở mức manifest.

Trạng thái: `V2_P0_ARCHITECTURE_DECISION_READY`.
