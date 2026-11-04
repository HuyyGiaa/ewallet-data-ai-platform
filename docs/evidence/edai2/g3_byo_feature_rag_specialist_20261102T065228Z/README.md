# Báo cáo evidence G3 BYO Feature-RAG Specialist — 2026-11-02

## Kết luận

Luồng business thật đã hoàn tất qua đúng chuỗi:

`Client → BYO Coordinator → A2A Gateway → BYO Feature-RAG Specialist → MCP Gateway → Feast/KServe/RAG`.

Không thay đổi Coordinator, Drift Specialist, Gateway, policy, Secret, MLOps,
IAM/RBAC, node hoặc dữ liệu. Helm `llm-agent` được nâng từ revision 19 lên 21;
revision 20 là lần rollout đầu và được thay bằng revision 21 sau khi runtime
phát hiện một lỗi serialization fail-closed.

## Git và image provenance

- Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`
- Branch: `codex/llm-g2-llmd-routing`
- Source runtime cuối: `aea85535dca52dbf14c45ed260212b8bcd237568`
- Image cuối: `asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-feature-rag-specialist@sha256:bb0fae06250038e6489524426969597517e94a885c629531ee0ccbc3c40cd4d0`
- Trivy: 0 HIGH/CRITICAL fixable; 0 secret.
- Helm rollback trước campaign: revision 19. Rollback gần nhất cho packaging:
  revision 20.

## Nguyên nhân và sửa lỗi

Declarative kagent 0.10.3 Specialist chỉ sinh JSON giống tool call; MCP Gateway
thấy `tools/list` nhưng không có business `tools/call`. Giải pháp là BYO Agent
chính thức, dùng A2A SDK và MCP SDK Streamable HTTP, giữ nguyên tên Agent,
ServiceAccount và credential.

Lần runtime đầu tại revision 20 chứng minh `tools/call feature_lookup` đi qua
Gateway nhưng bị backend từ chối `invalid_arguments`. Nguyên nhân là
`model_dump()` chèn computed field `merchant_present` vào transaction có
`extra=forbid`. Commit `aea8553` loại computed fields khỏi wire payload và thêm
regression test. Revision 21 sau đó hoàn tất đủ ba tool call.

## Kết quả thật

- Agent CR: `BYO`, Accepted=True, Ready=True.
- Pod: 1/1, restart 0, ServiceAccount `feature-rag-specialist`.
- Resource giữ nguyên: request 100m/256Mi, limit 500m/512Mi.
- Authorized tool list: `feature_lookup`, `fraud_scoring`, `knowledge_search`.
- Direct Specialist: completed trong 0,595 giây.
- Integrated Coordinator: completed trong 0,399 giây.
- A2A Gateway trace: `a5998ce82df5fc0a335511b4c62f15c5`, duration 227 ms.
- MCP integrated durations: feature lookup 23 ms, fraud scoring 72 ms,
  knowledge search 12 ms.
- Feast: 4 feature thật, timestamp `2026-05-31T23:59:56+00:00`.
- KServe: probability `0.0639895560144887`, prediction `0`, model
  `fraud-detector/1`.
- RAG: 3 citation có document/chunk/checksum/revision.
- Coordinator giữ nguyên request/transaction identity và trả terminal artifact
  `g3-orchestration/v1`.

## Security-negative

- Input amount âm: A2A `rejected` trước tool call.
- Entity không có backend evidence: A2A `failed`, không bịa dữ liệu.
- Credential giả qua MCP Gateway: HTTP 401 `authentication_failed`.
- Coordinator gọi thẳng Specialist: timeout/deny.
- Specialist gọi thẳng MCP backend: timeout/deny.
- Candidate semantic diff: chỉ `Agent/ewallet-agents/feature-rag-specialist`.
- Coordinator, Drift, MCP, KServe, Feast/Redis, Fraud API và MLflow vẫn healthy.

## Gate status

| Gate | Trạng thái |
|---|---|
| SPECIALIST_SOURCE_READY | PASS |
| SPECIALIST_A2A_SERVER_READY | PASS |
| SPECIALIST_DEPLOYED | PASS |
| SPECIALIST_MCP_TOOLS_CALL | PASS |
| COORDINATOR_A2A_ROUTE | PASS |
| COORDINATOR_BUSINESS_E2E | PASS |
| SECURITY_REGRESSION | PASS — không phát hiện regression |
| FULL_BUSINESS_E2E | PASS cho request synthetic đã ghi nhận |
| RUBRIC_VERIFIED | CHƯA XÁC MINH — chưa chạy bộ screenshot/rubric độc lập |

## Hạn chế còn lại

Không có Service OTEL Collector trong cluster nên SDK exporter ghi cảnh báo
`UNAVAILABLE`; trace Gateway vẫn đầy đủ và business không bị ảnh hưởng. Canonical
GCS RAG registry/DataHub writeback và bộ screenshot rubric đầy đủ chưa được thực
hiện trong phạm vi này. Không được suy diễn các mục đó là PASS.

## Evidence chính

- `render/final-semantic-diff.json`
- `build/revision-aea8553/image-digests.json`
- `build/revision-aea8553/remote-image.json`
- `e2e/specialist-response-r21.json`
- `e2e/coordinator-response.json`
- `e2e/integrated-trace-summary.json`
- `security/a2a-negative-summary.json`
- `security/wrong-credential-via-gateway.json`
- `security/direct-coordinator-specialist-denial.json`
- `tests/regression-final.log`
