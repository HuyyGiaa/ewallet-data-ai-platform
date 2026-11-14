"""Airflow-free parameter and command contract for the fraud-policy RAG DAG.

Values reach BashOperator tasks only as environment variables that the shell
expands once inside double quotes; they are never rendered into Bash source.
The JSON schemas are enforced by Airflow when a run is triggered and by
``validate_rag_params`` for direct callers and tests. Credentials (Redis,
Feast registry, DataHub) are inherited from the worker environment and never
appear in params or DAG source.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping


RAG_PYTHON_ENV_VAR = "EWALLET_RAG_PYTHON"
RAG_RUNTIME_PROJECT = "infra/docker/rag-ingestion"
DEFAULT_ENV_FILE = str(Path.home() / ".config/ewallet-rag/runtime.env")
RAG_STAGES = ("discover", "chunk", "embed", "publish", "validate", "datahub")

# "(?![\s\S])" is end-of-input in both Python and ECMA-262; "$" would also
# accept a trailing newline under the re.search used by jsonschema.
RAG_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    # A repository corpus directory or a GCS prefix holding manifest.json.
    "source_location": {
        "default": "data/llm_agent/rag/fraud-policy-v1",
        "type": "string",
        "pattern": r"^(?!.*\.\.)(?:data/llm_agent/rag/[a-z0-9][a-z0-9._-]{0,63}"
                   r"|gs://[a-z0-9][a-z0-9._-]{2,62}(?:/[A-Za-z0-9._-]{1,100}){1,8})(?![\s\S])",
    },
    # redis = canonical Feast Online; sqlite = explicit local-dev profile.
    "online_store": {"default": "redis", "type": "string", "enum": ["redis", "sqlite"]},
    # plan_only is an explicit, recorded choice; publish fails without DataHub configuration.
    "datahub_mode": {"default": "publish", "type": "string", "enum": ["publish", "plan_only"]},
    # Empty: the evidence run id is derived from the Airflow run id.
    "run_label": {
        "default": "",
        "type": "string",
        "pattern": r"^(?:[a-z0-9][a-z0-9_-]{2,63})?(?![\s\S])",
    },
}

RAG_PARAM_ENV = {
    "source_location": "RAG_SOURCE_LOCATION",
    "online_store": "RAG_ONLINE_STORE",
    "datahub_mode": "RAG_DATAHUB_MODE",
    "run_label": "RAG_RUN_LABEL",
}


def rag_python(repository_root: Path) -> str:
    """The uv-locked runtime interpreter (``uv sync --locked --no-dev``), overridable."""

    default = repository_root / RAG_RUNTIME_PROJECT / ".venv/bin/python"
    return os.environ.get(RAG_PYTHON_ENV_VAR, str(default))


def rag_task_environment(repository_root: Path) -> dict[str, str]:
    """Task env; its values are the only place params and the run id are templated."""

    return {
        RAG_PYTHON_ENV_VAR: rag_python(repository_root),
        # Private KEY=VALUE file with Redis/Feast/DataHub settings (ops.llm_agent.rag_ingest),
        # so they can change without restarting the shared scheduler.
        "EWALLET_RAG_ENV_FILE": os.environ.get("EWALLET_RAG_ENV_FILE", DEFAULT_ENV_FILE),
        # Run directories; empty means the repository's docs/evidence/llm_agent (ops.llm_agent.rag_ingest).
        "EWALLET_RAG_EVIDENCE_ROOT": os.environ.get("EWALLET_RAG_EVIDENCE_ROOT", ""),
        "RAG_AIRFLOW_RUN_ID": "{{ run_id }}",
        **{env: f"{{{{ params.{name} }}}}" for name, env in RAG_PARAM_ENV.items()},
    }


# Fails before any work when the locked runtime has not been synced.
RUNTIME_GUARD = (
    f'test -x "${{{RAG_PYTHON_ENV_VAR}}}" || '
    f'{{ echo "ERROR: ${{{RAG_PYTHON_ENV_VAR}}} is not executable; run: '
    f'uv sync --locked --no-dev --project {RAG_RUNTIME_PROJECT}" >&2; exit 1; }}; '
)


def rag_stage_command(stage: str) -> str:
    if stage not in RAG_STAGES:
        raise ValueError(f"unknown RAG stage: {stage}")
    return (
        RUNTIME_GUARD
        + f'"${{{RAG_PYTHON_ENV_VAR}}}" -m ops.llm_agent.rag_ingest {stage}'
        + ' --airflow-run-id "${RAG_AIRFLOW_RUN_ID}" --run-label "${RAG_RUN_LABEL}"'
        + ' --source-location "${RAG_SOURCE_LOCATION}"'
        + ' --online-store "${RAG_ONLINE_STORE}" --datahub-mode "${RAG_DATAHUB_MODE}"'
    )


def validate_rag_params(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate a complete parameter set exactly as the Airflow JSON schemas do."""

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
    return result
