"""Airflow-free parameter contract for the manual fraud-policy RAG DAG.

Values reach BashOperator tasks only as environment variables that the shell
expands once inside double quotes; they are never rendered into Bash source.
The JSON schemas are enforced by Airflow when a run is triggered and by
``validate_rag_params`` for direct callers and tests.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping


EVIDENCE_ROOT = "docs/evidence/llm_agent/rag"

# "(?![\s\S])" is end-of-input in both Python and ECMA-262; "$" would also
# accept a trailing newline under the re.search used by jsonschema.
RAG_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "embedding_endpoint": {
        "default": "http://rag-embedding.ewallet-agents.svc.cluster.local:8000",
        "type": "string",
        "pattern": r"^https?://rag-embedding\.ewallet-agents\.svc(?:\.cluster\.local)?:8000(?![\s\S])",
    },
    "api_key_file": {
        "default": "/var/run/secrets/rag-embedding/api-key",
        "type": "string",
        "pattern": r"^/var/run/secrets/rag-embedding/[A-Za-z0-9_-]{1,64}(?![\s\S])",
    },
    "online_store": {"default": "redis", "type": "string", "enum": ["redis"]},
    # One immutable directory per run: the runners open outputs with mode "x".
    "evidence_dir": {
        "default": f"{EVIDENCE_ROOT}/manual-run-id",
        "type": "string",
        "pattern": rf"^{re.escape(EVIDENCE_ROOT)}/[a-z0-9][a-z0-9_-]{{2,79}}(?![\s\S])",
    },
}

RAG_PARAM_ENV = {
    "embedding_endpoint": "RAG_EMBEDDING_ENDPOINT",
    "api_key_file": "RAG_API_KEY_FILE",
    "online_store": "RAG_ONLINE_STORE",
    "evidence_dir": "RAG_EVIDENCE_DIR",
}


# Shell prefix that fails before any embedding call when an output identity is reused.
EVIDENCE_DIR_GUARD = (
    'test ! -e "${RAG_EVIDENCE_DIR}" || '
    '{ echo "ERROR: evidence_dir already exists: ${RAG_EVIDENCE_DIR}" >&2; exit 1; }; '
)


def rag_task_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Return the task env whose values are the only place params are templated."""

    return {**base, **{env: f"{{{{ params.{name} }}}}" for name, env in RAG_PARAM_ENV.items()}}


def validate_rag_params(values: Mapping[str, Any], *, repository_root: Path | None = None) -> dict[str, str]:
    """Validate a complete parameter set; optionally reject a reused evidence_dir."""

    unexpected = set(values) - set(RAG_PARAM_SCHEMAS)
    if unexpected:
        raise ValueError(f"unexpected RAG parameters: {sorted(unexpected)}")
    result: dict[str, str] = {}
    for name, schema in RAG_PARAM_SCHEMAS.items():
        value = values.get(name, schema["default"])
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{name} must be one of {schema['enum']}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{name} does not match its allowed pattern")
        result[name] = value
    if repository_root is not None and (repository_root / result["evidence_dir"]).exists():
        raise ValueError(f"evidence_dir already exists; output identity must be unique: {result['evidence_dir']}")
    return result
