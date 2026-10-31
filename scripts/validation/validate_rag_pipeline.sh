#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPOSITORY_ROOT"

python3 -m unittest -v tests.llm_agent.test_phase_c
python3 -m ops.llm_agent.rag_pipeline \
  --manifest data/llm_agent/rag/fraud-policy-v1/manifest.json \
  --validate-only
python3 -m compileall -q \
  ai_platform/llm_agent/rag \
  ops/llm_agent/rag_pipeline.py \
  ops/llm_agent/run_rag_evaluation.py \
  ops/llm_agent/verify_rag_artifacts.py \
  data_platform/orchestration/airflow/dags/rag_fraud_policy.py

UV_EXECUTABLE="${UV_BIN:-uv}"
command -v "$UV_EXECUTABLE" >/dev/null
"$UV_EXECUTABLE" run --locked --project ai_platform/features/feast python -c \
  "from ai_platform.features.feast.feature_repo.rag_feature_definitions import fraud_policy_chunks; f=next(x for x in fraud_policy_chunks.schema if x.name == 'embedding'); assert str(f.dtype) == 'Array(Float32)' and f.vector_index is True and f.vector_search_metric == 'COSINE'; print('FEAST_RAG_SCHEMA_PASS')"

echo "RAG_SOURCE_GATE_PASS runtime_verified=false"
