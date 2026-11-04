# Living Decision addendum — G3 BYO Feature-RAG Specialist

Ngày 2026-11-02, owner-approved G3 campaign đã thay riêng
`feature-rag-specialist` từ kagent Declarative runtime sang BYO Agent chính thức.
Source runtime là `aea85535dca52dbf14c45ed260212b8bcd237568`; image immutable là
`g3-byo-feature-rag-specialist@sha256:bb0fae06250038e6489524426969597517e94a885c629531ee0ccbc3c40cd4d0`;
Helm `llm-agent` kết thúc ở revision 21, rollback baseline trước campaign là 19.

Semantic diff Helm chỉ thay `Agent/ewallet-agents/feature-rag-specialist` và
controller-owned workload tương ứng. Agent name, ServiceAccount, resource
contract, placement, Secret reference, A2A/MCP Gateways và security policies
được giữ nguyên. Full synthetic flow Client→Coordinator→A2A Gateway→Specialist
→MCP Gateway→Feast/KServe/RAG đã PASS với trace
`a5998ce82df5fc0a335511b4c62f15c5`; ba tool call thật, bốn feature, một KServe
prediction và ba RAG citations đã được xác thực. Negative input, missing
evidence, wrong credential và direct-bypass tests đều fail-closed.

OTEL Collector hiện không tồn tại nên application span export còn BLOCKED;
Gateway trace vẫn được lưu. Canonical GCS RAG registry, DataHub writeback và
rubric screenshot verification vẫn DEFERRED/UNVERIFIED. Không được tuyên bố
`RUBRIC_VERIFIED` từ decision này.
