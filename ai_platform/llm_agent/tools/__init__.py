"""Phase D MCP tool services: feature_lookup, fraud_scoring, knowledge_search, drift_detection.

Every backend is dependency-injected. Production wiring fails closed when a
backend is unavailable; fixtures exist only in tests.
"""
