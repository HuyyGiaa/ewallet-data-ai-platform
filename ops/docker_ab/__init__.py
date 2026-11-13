"""Docker image A/B benchmark for the EDAI-1 campaign verifier (rubric rows 3-4).

Builds a 2x2 matrix of the same application - {single-stage full-context
packaging, multi-stage minimal payload} x {full Bookworm, slim Bookworm base} -
runs each image on identical read-only inputs, and records size, layers, build
time, runtime user, functional fingerprint and Compose execution. The matrix
separates the effect of the base image from the effect of packaging instead of
crediting the whole reduction to the multi-stage build.

Only images tagged ``edai1/campaign-verifier:*`` and the Compose project
``edai1-dk5gb`` are created. Nothing else on the Docker host is stopped, removed
or pruned.
"""

REPORT_SCHEMA = "edai1-docker-ab-v2"
IMAGE_REPOSITORY = "edai1/campaign-verifier"
COMPOSE_PROJECT = "edai1-dk5gb"
