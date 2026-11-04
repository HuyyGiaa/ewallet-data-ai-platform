"""Copy authentic N9-N12 screenshots into the repository with a provenance manifest.

Originals under ~/ewallet-evidence are never modified. Each entry records the
source path, SHA-256, capture campaign, run/build/trace identity and rubric rows.
"""
import hashlib, json, os, shutil, sys
E = os.path.expanduser("~/ewallet-evidence")
OUT = os.path.dirname(os.path.abspath(__file__))
N10, N11, N12 = f"{E}/n10-final-cloud-e2e-20261104", f"{E}/n11-p0-p1-integration", f"{E}/n12-observability"
ITEMS = [
 # topic, file, source, campaign, identity, rows, caption
 ("llm","01_kagent_drift_chat_modelconfig.png",f"{N10}/E2-kagent/c3-drift-session-final.png","N10-E2 2026-11-04","kagent session 01a1a419; ModelConfig ewallet-global-llm","edai-2 3,4,7","drift-specialist chat answered by the deployed Qwen3-0.6B through ModelConfig ewallet-global-llm (header: OpenAI ewallet-fraud-llm)"),
 ("llm","02_grafana_tokens_per_request.png",f"{N12}/G-ui/row50-tokens-per-request-input--output--total.png","N12 Gate F 2026-11-04","Helm r37","edai-2 3,6,50","vLLM input/output/total tokens per request"),
 ("llm","03_grafana_ttft.png",f"{N12}/G-ui/row50-time-to-first-token-p50--p95.png","N12 Gate F","Helm r37","edai-2 6,50","vLLM time to first token p50/p95"),
 ("llm","04_grafana_generation_rtt.png",f"{N12}/G-ui/row50-generation-round-trip-time-p50--p95.png","N12 Gate F","Helm r37","edai-2 6,50","vLLM end-to-end generation latency p50/p95"),
 ("llm","05_tempo_llm_gateway_spans.png",f"{N12}/O-packetB-rollout/row49-tempo-search-llm-gateway-spans.png","N12 Packet B","trace f341b410568fb649ee0f95bba50cd27d","edai-2 5,49","agentgateway internal/llm route spans (agentgateway -> vLLM)"),
 ("agents","01_registry_catalog_agents.png",f"{N10}/E2-registry-scaling/registry-catalog-agents.png","N10-E2 2026-11-04","agentregistry v0.4.0 ewallet-prompts","edai-2 8,21,26","Agent registry UI: 3 agents v1.0.0"),
 ("agents","02_registry_agent_fraud_coordinator.png",f"{N10}/E2-registry-scaling/registry-agent-fraud-coordinator.png","N10-E2","agentregistry","edai-2 8,26","fraud-coordinator entry in the registry"),
 ("agents","03_registry_catalog_mcp_servers.png",f"{N10}/E2-registry-scaling/registry-catalog-servers.png","N10-E2","agentregistry","edai-2 8,18","drift-mcp and feature-rag-mcp published as MCP servers"),
 ("agents","04_registry_catalog_prompts.png",f"{N10}/E2-registry-scaling/registry-catalog-prompts.png","N10-E2","agentregistry","edai-2 8,34","Versioned system prompts in the registry"),
 ("agents","05_registry_deployed.png",f"{N10}/E2-registry-scaling/registry-deployed.png","N10-E2","agentregistry","edai-2 8","Registry deployed view"),
 ("agents","06_kagent_agents_list.png",f"{N10}/UI/kagent-ui-agents.png","N10-E1 2026-11-04","kagent 0.10.3","edai-2 16,19,20,25","kagent UI agent list (coordinator, specialists, pilot sandbox agent)"),
 ("agents","07_kagent_coordinator_prompt.png",f"{N11}/G-ui-kagent/u1b-coordinator-nl-live-prompt.png","N11 2026-11-04","session 01a1a4e8 request nl-eb00ade694f158ff85b004a35a9d1051","edai-2 16,22,25","Analyst question in kagent UI; LLM selects investigate_transaction"),
 ("agents","08_kagent_coordinator_answer.png",f"{N11}/G-ui-kagent/u1b-coordinator-nl-live-answer.png","N11","trace 7e651a4f38c648bd2ca3ec22e33a5d7b","edai-2 9,16,22,25","Final grounded answer: Feast features, KServe probability 0.0639895560144887, 3 RAG citations"),
 ("agents","09_kagent_specialist_answer.png",f"{N11}/G-ui-kagent/u2-specialist-nl-live-answer.png","N11","request nl-d61817187a5d735cea1ba3737dce93cb","edai-2 16,22","feature-rag-specialist answering directly in kagent UI with real MCP tool calls"),
 ("agents","10_kagent_rejection_missing_timestamp.png",f"{N11}/G-ui-kagent/n2-missing-timestamp-live-rejection.png","N11","negative N2","edai-2 16,22","Explicit rejection when a critical field is missing (no fabricated score)"),
 ("agents","11_kagent_rejection_unsupported.png",f"{N11}/G-ui-kagent/n1-unsupported-live-rejection.png","N11","negative N1","edai-2 16,22","Explicit rejection of an unsupported question"),
 ("agents","12_kagent_pii_prompt_answer.png",f"{N12}/M-section4/s4-coordinator-nl-pii-live-answer.png","N12 Section 4","request nl-89422d35e28d62650705782f2f25b1f0","edai-2 22,50","Answer to a prompt containing synthetic PII; PII masked before the LLM and never echoed"),
 ("agents","13_tempo_gateway_spans_table.png",f"{N12}/O-packetB-rollout/row49-tempo-gateway-spans-table.png","N12 Packet B","trace f341b410568fb649ee0f95bba50cd27d","edai-2 23,24,49","Spans of a2a-gateway, mcp-gateway and LLM gateway in one business trace"),
 ("agents","14_tempo_trace_waterfall.png",f"{N12}/O-packetB-rollout/row49-tempo-trace-all-gateways.png","N12 Packet B","trace f341b410568fb649ee0f95bba50cd27d","edai-2 22,25,49","251-span trace across fraud-coordinator, gateways, feature-rag-specialist and feature-rag-mcp"),
 ("mlops","01_kfp_runs.png",f"{N10}/E2-ui/kfp-runs.png","N10-E2 2026-11-04","KFP run f9-training-20261031-151638-ml2-r3","edai-2 13","Kubeflow Pipelines run list"),
 ("mlops","02_kfp_run_graph.png",f"{N10}/E2-ui/kfp-run-dc29f5cc-graph.png","N10-E2","KFP run dc29f5cc-cc9e-49ae-b861-eacddd6269b5","edai-2 13","Successful training pipeline graph (validate-snapshot, split, 4x train, select, evaluate, publish)"),
 ("mlops","03_kfp_run_details.png",f"{N10}/E2-ui/kfp-run-dc29f5cc-details.png","N10-E2","KFP run dc29f5cc","edai-2 13","Run details"),
 ("mlops","04_mlflow_experiments.png",f"{N10}/E2-ui/mlflow-experiments.png","N10-E2","MLflow","edai-2 14","MLflow experiments"),
 ("mlops","05_mlflow_f9_runs.png",f"{N10}/E2-ui/mlflow-experiment-f9-runs.png","N10-E2","MLflow F9 experiment","edai-2 14","Training runs per candidate"),
 ("mlops","06_mlflow_run_artifacts.png",f"{N10}/E2-ui/mlflow-run-fd639926-artifacts.png","N10-E2","run fd639926","edai-2 14","Run artifacts (reports, model)"),
 ("mlops","07_mlflow_registered_models.png",f"{N10}/E2-ui/mlflow-registered-models.png","N10-E2","fraud-detector","edai-2 14","Registered model"),
 ("mlops","08_mlflow_model_version_1.png",f"{N10}/UI/mlflow-model-version-1.png","N10-E1","fraud-detector v1","edai-2 14","Model version 1 with alias and data-version tags"),
 ("cicd","01_jenkins_dashboard.png",f"{N10}/F-cicd/ui/jenkins-dashboard.png","N9 readback (N10-E1)","Jenkins gen 13","edai-2 33-36","Jenkins jobs"),
 ("cicd","02_jenkins_build_history.png",f"{N10}/F-cicd/ui/jenkins-build-history.png","N9","builds docs#2/#3 ml-ci#3/#4 agent-ci#2 agent-negative#2","edai-2 33-35","Build history of the N9 CI campaign"),
    # cicd/03 (edai2-agent-ci #2 build page) is intentionally not curated: its change list shows
    # commit trailers that must not be redistributed. The build's JUnit result is cited from Jenkins test data instead.
 ("cicd","04_jenkins_ml_ci.png",f"{N10}/F-cicd/ui/jenkins-ml-full.png","N9","edai2-ml-ci","edai-2 33","ML CI pipeline"),
 ("cicd","05_jenkins_platform_ci_stageview.png",f"{N10}/F-cicd/ui/jenkins-platform-full-stageview.png","N9","edai2-docs-change-detection","edai-2 33-36","Platform CI stage view"),
 ("cicd","06_jenkins_negative_gate_stageview.png",f"{N10}/F-cicd/ui/jenkins-negative-gate-stageview.png","N9","edai2-agent-negative #2 (exit 42)","edai-2 34","Intentional negative contract gate fails the build"),
 ("cicd","07_jenkins_cd_rag_stageview.png",f"{N10}/E2-cd/ui/jenkins-cd-rag-stageview.png","N10-E2 2026-11-04","edai2-cd-rag-pipeline #8","edai-2 9,33","RAG pipeline CD: 7 stages SUCCESS"),
 ("cicd","08_jenkins_cd_rag_build.png",f"{N10}/E2-cd/ui/jenkins-cd-rag-build8.png","N10-E2","edai2-cd-rag-pipeline #8","edai-2 33","CD build #8 summary"),
 ("cicd","09_jenkins_cd_rag_console.png",f"{N10}/E2-cd/ui/jenkins-cd-rag-build8-console.png","N10-E2","Cloud Build c38e6e96; rag-ingestion@f89ecfa7","edai-2 9,33","Console: build, deploy to ewallet-cicd, smoke (4 chunks), cleanup"),
 ("observability","46_fraud_api_req_per_sec.png",f"{N12}/G-ui/row46-fraud-api-requests-per-second.png","N12 Gate F","30 bounded requests n12-row46-*","edai-2 46","Fraud API req/s by outcome"),
 ("observability","46_fraud_api_total_requests.png",f"{N12}/G-ui/row46-fraud-api-total-requests-time-range.png","N12","n12-row46-*","edai-2 46","Fraud API total requests"),
 ("observability","46_fraud_api_failed_requests.png",f"{N12}/G-ui/row46-fraud-api-failed-requests-time-range.png","N12","5 x 424 missing_feature","edai-2 46","Fraud API failed requests"),
 ("observability","47_cpu.png",f"{N12}/G-ui/row47-cpu.png","N12 Gate D","Helm r35","edai-2 47","Container CPU (cAdvisor)"),
 ("observability","47_memory.png",f"{N12}/G-ui/row47-memory.png","N12 Gate D","Helm r35","edai-2 47","Container memory"),
 ("observability","47_network.png",f"{N12}/G-ui/row47-network.png","N12 Gate D","Helm r35","edai-2 47","Network receive/transmit"),
 ("observability","47_disk_io.png",f"{N12}/G-ui/row47-disk-io.png","N12 Gate D","Helm r35","edai-2 47","Disk I/O"),
 ("observability","47_node_fs.png",f"{N12}/G-ui/row47-node-fs.png","N12 Gate D","Helm r35","edai-2 47","Node root filesystem usage"),
 ("observability","47_pvc.png",f"{N12}/G-ui/row47-pvc.png","N12 Gate D","Helm r35","edai-2 47","PVC usage"),
 ("observability","48_loki_gateways_by_trace.png",f"{N12}/G-ui/row48-loki-gateways-mcp-by-trace.png","N12 Gate E","trace 6de09d68d7a4b70fc9012b00e41d13d7","edai-2 48","Gateway and MCP logs correlated by trace ID"),
 ("observability","48_loki_fraud_api_kserve.png",f"{N12}/G-ui/row48-loki-fraud-api-kserve.png","N12 Gate E","Helm r36","edai-2 48","Fraud API and KServe logs in Loki"),
 ("observability","48_loki_volume_by_service.png",f"{N12}/G-ui/row48-loki-volume-by-service.png","N12 Gate E","Helm r36","edai-2 48","Log volume per service (8 services)"),
 ("observability","48_loki_agents_by_request_id.png",f"{N11}/H-observability/ui/grafana-loki-agents-by-request-id.png","N11","request nl-eb00ade694f158ff85b004a35a9d1051","edai-2 48","Agent audit logs by request ID"),
 ("observability","49_tempo_trace.png",f"{N12}/O-packetB-rollout/row49-tempo-trace-all-gateways.png","N12 Packet B","trace f341b410568fb649ee0f95bba50cd27d","edai-2 49","Distributed trace"),
 ("observability","49_tempo_gateway_spans.png",f"{N12}/O-packetB-rollout/row49-tempo-gateway-spans-table.png","N12 Packet B","trace f341b410568fb649ee0f95bba50cd27d","edai-2 49","Gateway spans in the trace"),
 ("observability","50_pii_caught.png",f"{N12}/M-section4/S4-row50-pii-caught.png","N12 Section 4","request nl-89422d35e28d62650705782f2f25b1f0","edai-2 50","Prompts caught by the PII screen"),
 ("observability","50_pii_by_type.png",f"{N12}/M-section4/S4-row50-pii-by-type.png","N12 Section 4","same","edai-2 50","PII detections by type"),
 ("observability","51_agent_calls_replacement_window.png",f"{N12}/L-G3-rollout/G3-row51-agent-calls-replacement-window-9-9.png","N12 G3","F2 window, Helm r40","edai-2 51","Times each agent was called: 9/9 equals audit events"),
 ("observability","51_agent_calls_by_outcome.png",f"{N12}/M-section4/S4-row51-agent-calls-by-outcome.png","N12 Section 4","Helm r40","edai-2 51","Agent calls by outcome"),
 ("observability","51_mcp_calls_per_tool.png",f"{N12}/L-G3-rollout/G3-row51-mcp-calls-per-tool-17-16-19.png","N12 G3","Helm r40","edai-2 51","MCP tool calls per tool 17/16/19"),
 ("observability","51_mcp_failures_per_tool.png",f"{N12}/L-G3-rollout/G3-row51-mcp-failures-per-tool-1.png","N12 G3","authorized negative test n12-tool-failure","edai-2 51","MCP failures per tool: feature_lookup = 1"),
]
manifest = []
for topic, name, src, campaign, ident, rows, caption in ITEMS:
    if not os.path.isfile(src):
        print("MISSING SOURCE", src); sys.exit(1)
    d = os.path.join(OUT, topic); os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, name); shutil.copy2(src, dst)
    h = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    assert h == hashlib.sha256(open(src, "rb").read()).hexdigest()
    manifest.append({"file": f"{topic}/{name}", "sha256": h, "source": src.replace(os.path.expanduser("~"), "~"),
                     "campaign": campaign, "identity": ident, "rubric_rows": rows, "caption": caption})
json.dump(manifest, open(os.path.join(OUT, "PROVENANCE.json"), "w"), indent=1)
print(len(manifest), "screenshots curated")
