"""Resolve and validate the Python interpreter used by Airflow Bash tasks."""

from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


PYTHON_ENV_VAR = "EWALLET_AIRFLOW_PYTHON"
REQUIRED_GCP_MODULES = (
    "ops.gcp.dp2_method2",
    "ops.gcp.submit_bronze_to_silver",
    "ops.gcp.submit_historical_features",
    "ops.gcp.submit_silver_to_gold",
    "ops.gcp.submit_source_to_bronze",
    "ops.gcp.verify_delta_run",
    "ops.gcp.wait_for_dataproc_quota",
)


@lru_cache(maxsize=None)
def resolve_airflow_python(repository_root: str | Path) -> str:
    """Return a validated interpreter for project module tasks.

    ``EWALLET_AIRFLOW_PYTHON`` is the explicit override. Otherwise the DAG
    processor's own interpreter is used, which keeps task execution aligned
    with the Airflow environment without relying on its shell ``PATH``.
    """

    root = Path(repository_root).resolve()
    configured = os.environ.get(PYTHON_ENV_VAR)
    candidate = Path(configured or sys.executable).expanduser().resolve()
    source = PYTHON_ENV_VAR if configured else "Airflow sys.executable"

    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(
            f"Invalid {source}: {candidate}. Configure {PYTHON_ENV_VAR} with "
            "an executable Python interpreter from the Airflow environment."
        )

    modules = ",".join(REQUIRED_GCP_MODULES)
    validation = subprocess.run(
        [
            str(candidate),
            "-c",
            "import importlib,sys; "
            "[importlib.import_module(name) for name in sys.argv[1].split(',')]",
            modules,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if validation.returncode != 0:
        detail = validation.stderr.strip() or validation.stdout.strip()
        raise RuntimeError(
            f"Python interpreter {candidate} cannot import required project "
            f"modules from {root}: {detail}"
        )
    return str(candidate)


def python_task_environment(repository_root: str | Path) -> dict[str, str]:
    """Build the environment shared by project Python BashOperator tasks."""

    return {PYTHON_ENV_VAR: resolve_airflow_python(repository_root)}


def python_module_command(module: str) -> str:
    """Build a fail-fast shell prefix for running a validated Python module."""

    if module not in REQUIRED_GCP_MODULES:
        raise ValueError(f"Python module is not in the Airflow allowlist: {module}")
    return (
        f'"${{{PYTHON_ENV_VAR}:?ERROR: {PYTHON_ENV_VAR} is not configured}}" '
        f"-m {module}"
    )
