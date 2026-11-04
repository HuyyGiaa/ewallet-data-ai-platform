# Living Decision addendum — G3 BYO Coordinator controlled rollout

**Trạng thái writeback:** `NOT_WRITTEN_TO_CANONICAL_GOOGLE_DOCS` — nội dung dưới đây copy-ready; campaign này không sửa Google Docs/canonical branch.

## Nội dung copy-ready

Ngày 2026-11-02, owner-approved G3 BYO Coordinator được triển khai có kiểm soát trên `ewallet-dev` từ source commit `ce05f0f98b3775da2d313f5632970d0134771249`. Runtime sử dụng kagent `0.10.3`, kagent-core commit `8878c39ac64421cb665c8e62ecc6411dd12f2fc2`, A2A SDK `0.3.26`, và private image digest:

`asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:397ae0cd1e2ef1671ce9163a3922db97442b8c38c81e899ff96e9d65af274446`.

Helm `llm-agent` revision 19 thay đúng image của `Agent/ewallet-agents/fraud-coordinator`; semantic diff giữ 94 resources, không thêm hoặc xóa resource. Agent, Deployment, Service, ServiceAccount/SPIFFE boundary, A2A Gateway, MCP Gateway, kagent, MLOps, IAM/RBAC, Secrets và node count được giữ nguyên. Agent `Accepted=True`, `Ready=True`; Agent Card, health/readiness, task persistence và kagent controller API đều được xác minh runtime.

Timeout Specialist được giới hạn mặc định 480 giây với hard maximum 600 giây sau khi một run thực tế vượt timeout cũ 120 giây. Thiết kế tiếp tục fail-closed, không gọi trực tiếp Specialist, không bypass A2A Gateway và không thực thi pseudo-tool text.

Runtime đã chứng minh Coordinator gửi request qua A2A Gateway tới exact Feature-RAG Specialist route bằng trace `eb996ea76c0580a8be811eb800b65382`. Tuy nhiên `BUSINESS_E2E` chưa PASS: Specialist chỉ trả pseudo-tool JSON và không thực hiện MCP `tools/call`; Coordinator từ chối artifact với `invalid_specialist_evidence`. Các MCP `feature_lookup`, `fraud_scoring` và `knowledge_search` độc lập đều gọi backend thật thành công, nhưng không được dùng để thay thế full Coordinator business path.

Security regression tests PASS: direct Coordinator→Specialist, wrong ServiceAccount với forged header, wrong Specialist credential và unknown credential đều bị chặn. Không phát hiện bypass. Revision 18 là BYO rollback target gần nhất; revision 17 là declarative fallback ổn định. OpenTelemetry correlation có trong ứng dụng nhưng cluster chưa có collector, nên trace backend storage vẫn là blocker ngoài scope.

Quyết định trạng thái:

- `SOURCE_READY=PASS`
- `A2A_SERVER_READY=PASS`
- `BYO_DEPLOYED=PASS`
- `AGENT_REGISTERED=PASS`
- `A2A_GATEWAY_VERIFIED=PASS`
- `BUSINESS_E2E=FAIL_CLOSED/BLOCKED`
- `SECURITY_REGRESSION=PASS`
- `RUBRIC_VERIFIED=NOT_VERIFIED`

Approval tiếp theo cần tách riêng và giới hạn vào native tool invocation của Feature-RAG Specialist/kagent (hoặc Specialist deterministic được review). Không được chữa bằng pseudo-tool execution, direct MCP/Specialist call hoặc Gateway bypass. RAG DB1/production publication và DataHub rubric/runtime vẫn là các gate riêng.

Evidence bundle: `docs/evidence/edai2/g3_byo_coordinator_20261102T061500Z/`, được bảo vệ bằng `SHA256SUMS`.
