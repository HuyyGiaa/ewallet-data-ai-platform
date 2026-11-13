"""DataHub URN conventions shared by every EDAI publisher.

Datasets on GCS are named by their full ``gs://`` URI on the ``gcs`` platform in
the ``PROD`` environment; Airflow DAGs are DataFlows of the ``ewallet-gcp``
cluster and their tasks are DataJobs. The Feast materialization publisher
(``lineage/feast_materialization.py``) uses the same format, so a batch dataset
and a Feast dataset with the same URI would be one DataHub entity.
"""

from __future__ import annotations


ENVIRONMENT = "PROD"
FLOW_CLUSTER = "ewallet-gcp"
ORCHESTRATOR = "airflow"


def dataset_urn(uri: str, platform: str = "gcs") -> str:
    """Return the dataset URN of one physical dataset.

    Raises:
        ValueError: A GCS dataset is not named by an absolute ``gs://`` URI, or the
            name contains a character that would break the URN tuple.
    """

    if platform == "gcs" and not uri.startswith("gs://"):
        raise ValueError(f"GCS dataset names must be gs:// URIs, got {uri!r}")
    if any(character in uri for character in ",()") or uri.endswith("/"):
        raise ValueError(f"dataset name cannot be used in a URN: {uri!r}")
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{uri},{ENVIRONMENT})"


def flow_urn(dag_id: str) -> str:
    """Return the DataFlow URN of one Airflow DAG."""

    return f"urn:li:dataFlow:({ORCHESTRATOR},{dag_id},{FLOW_CLUSTER})"


def job_urn(dag_id: str, task_id: str) -> str:
    """Return the DataJob URN of one Airflow task."""

    return f"urn:li:dataJob:({flow_urn(dag_id)},{task_id})"
