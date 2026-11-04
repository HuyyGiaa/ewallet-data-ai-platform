# EDAI-2 Agent Platform runtime closure evidence

Capture started at `2026-11-02T10:47:44Z` from worktree HEAD
`98680a5f85a328342421bda91c6e7b286eb5cf40`.

This directory contains redacted management-plane, Helm, Kubernetes,
autoscaling and regression evidence for the owner-approved runtime closure
campaign. Secret payloads are intentionally excluded.

## Kết quả

- `SOURCE_PASS`: Helm candidate chỉ thêm 3 HPA + 3 PDB và bỏ quyền sở hữu
  `deployment.replicas` khỏi đúng 3 Agent. Drift Agent Card đang chạy được giữ
  nguyên bằng `a2aSkill.enabled=false`.
- `RUNTIME_SCALE_OUT_PASS`: cả ba Deployment tăng từ 2 lên 3 replica, 3/3
  Ready, EndpointSlice có 3 endpoint sẵn sàng và HPA ghi nhận
  `SuccessfulRescale`.
- `BUSINESS_E2E_PASS` cho một request: Coordinator -> A2A Gateway ->
  Feature-RAG -> MCP -> Feast/KServe trả 4 feature, model
  `fraud-detector/1`, probability `0.0639895560144887` và 3 RAG citations.
- `MULTI_REPLICA_IDEMPOTENCY_FAIL`: gửi lại cùng business `request_id` trong
  một A2A request mới đã tạo task mới và gọi chuỗi MCP lần hai. Chi tiết và
  nguyên nhân có trong `hpa/idempotency-failure.md`.
- Theo gate của owner, Helm revision 25 đã rollback về revision 24, tạo
  revision đang deployed là 26. HPA/PDB của ba Agent không còn trong cluster;
  ba Agent trở lại 1/1 Ready.
- `POST_ROLLBACK_PASS`: G3 business E2E, direct-bypass denial, wrong-token
  denial, RemoteMCP, Feast Redis, KServe và observability đều được kiểm lại.
- `SUBSTRATE_NOT_DEPLOYED`: không tiếp tục pilot sau critical G3 regression;
  không tạo CRD, cluster RBAC, namespace hay privileged workload nào.

## Phạm vi kiểm thử nguồn

- 82/82 test cho pilot bounds, Agent chart, probe và G3 runtime config: PASS.
- 33/33 test trong locked BYO Coordinator environment: PASS.
- `helm lint`: PASS (0 chart failed).
- `git diff --check`: PASS.

Unit test xanh không nâng trạng thái idempotency lên PASS: các test hiện tại
chỉ bao phủ cùng worker/context hoặc lookup theo A2A task ID, trong khi lỗi
runtime xảy ra với cùng business request ID qua context/replica mới.

## Phân loại cuối

- Source cho candidate autoscaling: `SOURCE_PASS`.
- Scale-out tạm thời: `RUNTIME_PASS`.
- Idempotency đa replica: `RUNTIME_FAIL`.
- HPA production hiện tại: `ROLLED_BACK / BLOCKED`.
- Substrate: `NOT_DEPLOYED / BLOCKED_BY_G3_GATE`.
- Agent Platform tổng thể: `RUNTIME_PARTIALLY_BLOCKED`.

Không có Secret payload, token, credential hoặc dữ liệu nhạy cảm trong evidence.
