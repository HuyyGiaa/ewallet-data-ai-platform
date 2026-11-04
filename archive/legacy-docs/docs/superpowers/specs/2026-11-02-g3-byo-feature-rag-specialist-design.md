# G3 BYO Feature-RAG Specialist Design

## Intent

Khôi phục đường business G3 đã được phê duyệt bằng cách thay riêng
`feature-rag-specialist` declarative Agent không ổn định bằng một kagent 0.10.3
BYO Agent xác định, trong khi giữ nguyên BYO Coordinator, A2A Gateway, MCP
Gateway, credential, Feast, KServe, RAG và security policies hiện hữu.

## Goals

- Nhận đúng canonical two-line A2A business request và giữ nguyên transaction ID.
- Gọi đúng các tool được request, theo thứ tự request, qua MCP Gateway bằng MCP
  SDK Streamable HTTP đã pin.
- Kiểm tra `tools/list`, từng `tools/call`, response identity, KServe result và
  RAG citations trước khi trả một terminal A2A artifact tương thích với
  `SpecialistEvidence` hiện hữu.
- Fail closed khi input, authentication, tool inventory, tool result hoặc
  provenance sai; không thực thi pseudo-tool text.
- Dùng lại task persistence, projected controller token, health/readiness,
  telemetry và bounded lifecycle pattern của BYO Coordinator.

## Non-goals và anti-goals

- Không sửa Coordinator để gọi MCP hoặc diễn giải model text.
- Không gọi Feast, KServe hay RAG trực tiếp.
- Không đổi model/parser, Gateway, NetworkPolicy, AuthorizationPolicy,
  IAM/RBAC, Secret, node hoặc G1/G2 serving.
- Không biến direct MCP smoke thành bằng chứng full E2E.
- Không tạo framework orchestration thứ hai và không mở route không được bảo vệ.

## Constraints

- Worktree `codex/llm-g2-llmd-routing`; không merge/push canonical/main.
- Agent identity phải tiếp tục là `feature-rag-specialist` trong
  `ewallet-agents`, ServiceAccount giữ nguyên.
- A2A chạy port 8080 theo official A2A SDK/kagent 0.10.3 BYO contract.
- MCP endpoint duy nhất:
  `http://mcp-gateway.ewallet-agents.svc:8080/feature-rag/mcp`.
- Credential lấy từ Secret hiện hữu
  `mcp-token-feature-rag-specialist-feature-rag-mcp-r1-release1`, key `token`,
  qua `env.valueFrom.secretKeyRef`; không log giá trị.
- Image phải private, digest-pinned, scan trước rollout; build/scan tuần tự do
  swap local đã đầy.

## Approaches considered

1. **Prompt-only declarative Agent:** ít source change nhưng runtime đã chứng
   minh model phát pseudo-tool text khi request đầy đủ; không đáng tin cậy và
   prompt-only upgrades đã hết giá trị chẩn đoán. Loại.
2. **Coordinator thực thi pseudo-tool:** có thể làm request chạy nhưng phá
   separation of duties, Gateway boundary và fail-closed contract. Loại.
3. **Programmatic BYO Specialist:** dùng official A2A/MCP SDK, giữ first-class
   kagent Agent và mọi identity/policy hiện hữu. Đây là phương án owner đã phê
   duyệt có điều kiện và là phương án được chọn.

## Architecture

`FeatureRagSpecialistExecutor` parse request bằng
`business_request_from_a2a`, chỉ chấp nhận target/capability Feature-RAG. Một
`FeatureRagMCPClient` mở một bounded Streamable HTTP session với MCP Gateway,
initialize, list tools, kiểm tra exact required subset rồi gọi từng requested
tool. Mỗi structured result được validate bằng typed Pydantic models và bind
với request/transaction. Specialist tạo đúng một `SpecialistEvidence` terminal
artifact; A2A adapter và kagent task store dùng shared BYO runtime helpers.

Mandatory values được copy từ MCP structured content. Phần giải thích mặc định
là deterministic metadata-free text; LLM synthesis không nằm trên critical
business path. Nếu bất kỳ tool bắt buộc nào thất bại hoặc evidence thiếu,
A2A task kết thúc failed/rejected, không partial fabrication.

## Source boundaries

- Shared runtime: `ai_platform/llm_agent/byo_runtime.py`.
- Specialist core/server: `ai_platform/llm_agent/byo_feature_rag_specialist.py`.
- Coordinator chỉ đổi import shared helper, không đổi business behavior.
- Docker project hiện hữu được mở rộng cho cả hai entrypoint; specialist dùng
  image/repository riêng và exact Git SHA tag.
- Helm thêm opt-in `byo` riêng dưới specialist; chỉ Agent
  `feature-rag-specialist` được semantic-diff.

## Testing and rollout gates

TDD bao phủ input, capability, MCP endpoint, initialization/list/call,
ordering, malformed/missing results, prediction identity, citations, timeout,
cancellation, idempotency/session isolation và real local A2A protocol. Helm
test chứng minh chỉ một Agent thay đổi và credential/identity/resources được
giữ nguyên. Cloud rollout chỉ diễn ra sau locked suites, image import smoke,
scan, remote digest readback, node capacity và semantic diff PASS.

Rollback target là revision live ngay trước upgrade, được read back tại thời
điểm rollout. Full Business E2E chỉ PASS nếu Coordinator → A2A Gateway → BYO
Specialist → MCP Gateway → Feast/KServe/RAG → Coordinator hoàn tất thật.
