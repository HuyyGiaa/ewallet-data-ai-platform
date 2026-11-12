"""Deterministic verifier of the committed EDAI-1 5GB campaign evidence.

This is the workload of the Docker A/B experiment (rubric rows 3-4). It reads the
small, committed campaign artifacts of one benchmark: the four write-once
campaign documents per Spark profile, the validation reports, the semantic
comparison and the Airflow run record. It checks them against a workload
specification and prints one canonical JSON report whose SHA-256 is the
functional fingerprint compared across image variants.

The package uses the Python standard library only, so the same code runs
unchanged in a full or a slim Python image. It never reads the 5GB data and
never touches the network. Relative imports let it run both as
``python -m ops.edai1_verifier`` in the repository and as
``python -m edai1_verifier`` inside an image.
"""

REPORT_SCHEMA = "edai1-campaign-verification-v1"
