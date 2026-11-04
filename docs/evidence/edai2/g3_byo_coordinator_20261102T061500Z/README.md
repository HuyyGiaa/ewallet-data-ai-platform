# G3 BYO Coordinator — bằng chứng triển khai có kiểm soát

Thời điểm campaign: 2026-11-02 (UTC/Vietnam time được giữ trong từng log).
Project/cluster: `ewallet-data-ai-platform-hg26` / `ewallet-dev`.
Worktree: `/home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2`.
Branch: `codex/llm-g2-llmd-routing`.

## Kết luận kiểm soát

| Gate | Kết quả | Căn cứ chính |
|---|---|---|
| `SOURCE_READY` | **PASS** | Source commit `ce05f0f98b3775da2d313f5632970d0134771249`; các bộ test khóa dependency đều PASS. |
| `A2A_SERVER_READY` | **PASS** | A2A SDK server thật, Agent Card, task/context lifecycle, persistence, timeout/cancel/fail-closed và local protocol test. |
| `BYO_DEPLOYED` | **PASS** | Helm `llm-agent` revision 19; Pod dùng đúng remote digest bất biến. |
| `AGENT_REGISTERED` | **PASS** | Agent `fraud-coordinator` `Accepted=True`, `Ready=True`; Agent Card và kagent task API đọc được. |
| `A2A_GATEWAY_VERIFIED` | **PASS** | Trace `eb996ea76c0580a8be811eb800b65382`, exact Feature-RAG route, HTTP 200, 87.206 s. |
| `BUSINESS_E2E` | **FAIL_CLOSED / BLOCKED** | Specialist chỉ trả pseudo-tool JSON, không phát sinh MCP `tools/call`; Coordinator từ chối chứng cứ không hợp lệ. |
| `SECURITY_REGRESSION` | **PASS (không phát hiện regression)** | Direct bypass, wrong-SA/forged-header, wrong/unknown credential đều bị chặn; MLOps vẫn healthy. |
| `RUBRIC_VERIFIED` | **NOT VERIFIED** | Full Business E2E, RAG DB1/production gate và DataHub gate chưa đóng. |

Không được dùng gói evidence này để tuyên bố `FULL_BUSINESS_E2E PASS` hoặc G3/rubric hoàn tất.

## Source và các commit

Source deployment HEAD trước commit evidence:

- `ce05f0f98b3775da2d313f5632970d0134771249`
- tree `00bed36ec763b7086659f3f5dd54c7d9d5e87ec1`

Các commit triển khai:

1. `d09c175cc9981c6c209b2b43ba58d3e979dade5f` — deterministic BYO Coordinator runtime.
2. `f6aab2d9ecc8bd4097b01962f3f5bb222b1427d7` — cài Git trong image build stage.
3. `9b292345a25c04f36d4be0b217fe92ab0a2720f8` — kiểm tra đầy đủ runtime image.
4. `ce05f0f98b3775da2d313f5632970d0134771249` — timeout Specialist mặc định 480 giây, hard maximum 600 giây.

Implementation sử dụng kagent `0.10.3`, kagent-core commit được pin `8878c39ac64421cb665c8e62ecc6411dd12f2fc2` và A2A SDK `0.3.26`. Coordinator giữ nguyên transaction ID, chỉ route qua A2A Gateway, không gọi trực tiếp Specialist, không thực thi pseudo-tool text và fail-closed khi thiếu chứng cứ business bắt buộc.

## Image và triển khai

Remote image bất biến:

```text
asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:397ae0cd1e2ef1671ce9163a3922db97442b8c38c81e899ff96e9d65af274446
```

- Artifact Registry readback trùng digest.
- Trivy: 0 lỗ hổng HIGH/CRITICAL có bản vá; 0 secret.
- Build và scan chạy tuần tự để giữ RAM local ổn định.
- Helm `llm-agent`: revision 18 → 19.
- Semantic diff: vẫn 94 resources, không thêm/xóa resource; chỉ đổi image digest của `Agent/ewallet-agents/fraud-coordinator`.
- UID Agent, Deployment và Service được giữ nguyên; ClusterIP giữ nguyên `34.118.233.179`.
- Pod: `fraud-coordinator-7bf69cb76c-qlt2m`, ServiceAccount `fraud-coordinator`, port 8080, imageID đúng digest, requests `100m/256Mi`, limits `500m/512Mi`.
- Không tạo node, không thay IAM/RBAC, Secret, MCP, kagent hoặc MLOps.

Rollback đã xác minh:

- Revision 18: BYO image trước thay đổi timeout.
- Revision 17: declarative Coordinator ổn định gần nhất.
- Một rollout revision 16 trước đó bị lỗi thiếu feature package đã được rollback; lịch sử này được giữ nguyên trong evidence.

## Kiểm thử source và contract

Các bộ chạy trong đúng locked environment:

- BYO server: **11/11 PASS**.
- Programmatic Coordinator: **21/21 PASS**.
- G3 runtime/chart/security: **39/39 PASS**.
- Affected LLM Agent suite: **491/491 PASS**, 13 skipped có ghi nhận.
- `uv lock --check`: PASS.
- `uv sync --locked`: PASS.
- `git diff --check`: PASS.

Các log full-discovery chạy nhầm environment được giữ lại như chẩn đoán phạm vi môi trường, không được tính là source regression. Chúng thiếu dependency theo từng project riêng; toàn bộ test thuộc phạm vi đã được chạy lại trong đúng locked environment và PASS như trên.

## Runtime A2A và task persistence

E2E lần 1 tại revision 18 hết timeout 120 giây trong khi Specialist tiếp tục đến khoảng 280 giây. Sự kiện này dẫn đến bản sửa timeout có giới hạn 480 giây/hard maximum 600 giây; không mở timeout vô hạn.

E2E lần 2 tại revision 19:

- Request: `g3-byo-e2e-20261102-002`.
- Transaction: `dc58ff4e-b5f7-4486-b1f8-4c31383741a9`.
- Coordinator task: `5e4de2c3-9b10-4ac8-a287-27cc862ec133`.
- Specialist task: `01a19aee-0875-734c-acf1-0da7f95e327f`.
- A2A Gateway trace: `eb996ea76c0580a8be811eb800b65382`.
- Route: exact `default/feature-rag-specialist` endpoint.
- Gateway result: HTTP 200, 87.206 giây.
- Coordinator terminal result: `invalid_specialist_evidence` sau 87.5 giây.

kagent controller API đọc lại được cả Coordinator task và Specialist task, chứng minh task được đăng ký/persist trong runtime. Endpoint `/health`, `/ready` và Agent Card đều hợp lệ.

## Nguyên nhân Business E2E chưa đạt

RemoteMCPServer đang Accepted và danh sách tool đã discovery đúng. Agent Feature-RAG cũng tham chiếu ba tool và credential đúng. Tuy nhiên Specialist terminal artifact chỉ chứa lời gọi giả lập dạng:

```json
{"name":"feature_lookup","arguments":{"transaction_id":"..."}}
```

MCP Gateway chỉ nhận `tools/list`, không nhận `tools/call`. Theo architecture contract, BYO Coordinator không được diễn giải hoặc tự thực thi pseudo-tool text, vì vậy nó từ chối kết quả và kết thúc task ở trạng thái fail-closed.

Blocker cần owner phê duyệt riêng: sửa hẹp cơ chế native tool invocation của Feature-RAG Specialist/kagent, hoặc triển khai Specialist deterministic đã được review. Không được giải quyết blocker bằng cách cho Coordinator thực thi pseudo-tool, gọi trực tiếp MCP/Specialist hoặc bypass A2A Gateway.

## Backend được xác minh độc lập

Các MCP call thật qua MCP Gateway chứng minh backend vẫn hoạt động, nhưng **không thay thế Business E2E**:

- `feature_lookup`: PASS, 201.551 ms; trả feature thực từ Feast Online.
- `fraud_scoring`: PASS, 349.748 ms; `probability=0.0639895560144887`, `prediction=0`, model `fraud-detector`, version 1.
- `knowledge_search`: PASS, 195.523 ms; 3 kết quả/citation, backend `feast-online-redis+exact-cosine`, có checksum/revision.

MLOps health readback:

- `fraud-api`, MLflow và Postgres: Ready.
- KServe predictor: Ready.
- InferenceService `fraud-detector`: `Ready=True`.

## Security regression

Các negative test thực tế:

- Invalid/placeholder request bị từ chối.
- Direct Coordinator → Specialist bị deny/timeout.
- Caller sai ServiceAccount kèm forged `x-kagent-host` bị deny/timeout.
- Credential của sai Specialist bị MCP Gateway từ chối.
- Credential unknown bị MCP Gateway từ chối.
- Không tìm thấy đường bypass đã kiểm tra.

## Observability và giới hạn còn lại

Coordinator phát trace correlation, nhưng cluster hiện không có `otel-collector` service/workload; exporter báo `UNAVAILABLE`. Việc cài collector nằm ngoài mutation được phê duyệt nên không thực hiện.

Các gate riêng chưa đóng:

1. Native Specialist tool invocation để hoàn tất Coordinator business path.
2. RAG DB1/production publication/cutover (không thực hiện trong campaign này).
3. DataHub runtime/rubric gate (không khởi động heavy DataHub stack).
4. OpenTelemetry collector nếu owner muốn backend trace storage thay vì chỉ correlation/log evidence.

## Bản đồ evidence

- `build/`: build, scan, remote registry/digest.
- `helm/`: semantic diff, preflight, upgrade, history, rollout và runtime readback.
- `tests/`: locked dependency checks và toàn bộ test logs.
- `runtime/`: RAM snapshots, tool registration audit và OTel readback.
- `a2a-*`, `coordinator-*`, `specialist-*`: request/response, timing và Gateway/Coordinator/Specialist logs.
- `kagent-api-*`: task state đọc từ controller API.
- `mcp-*`: positive calls và authentication negatives.
- `security-negative-tests.txt`: security gates.
- `runtime-health-snapshot.txt`: workload/MLOps health.
- `SHA256SUMS`: checksum bất biến cho toàn bộ file trong bundle.

## Lệnh tái kiểm tra an toàn

```bash
cd /home/huyy-giaa/Develop/ewallet-data-ai-platform/.worktrees/codex-llm-g2
sha256sum --check docs/evidence/edai2/g3_byo_coordinator_20261102T061500Z/SHA256SUMS
git show --stat --oneline ce05f0f98b3775da2d313f5632970d0134771249
helm status llm-agent --namespace default
helm history llm-agent --namespace default
kubectl -n ewallet-agents get agent fraud-coordinator -o yaml
kubectl -n ewallet-agents get deploy,svc,pod -l app.kubernetes.io/name=fraud-coordinator -o wide
```

Các lệnh trên chỉ read-only ngoại trừ việc kiểm tra checksum local. Không chạy lại E2E/cloud workload nếu chưa có campaign tiếp theo được phê duyệt.
