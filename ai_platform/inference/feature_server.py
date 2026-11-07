"""Production entrypoint of the Feature API.

Shares the Fraud API image and start-up steps: optional Vault secrets, the read-only
registry mirror, then OpenTelemetry with ``/metrics`` on the application port.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from prometheus_client import make_asgi_app

from ai_platform.features.feast.registry_mirror import mirror_from_environment
from ai_platform.inference.feature_app import (
    FeatureApiSettings,
    OpenTelemetryFeatureTelemetry,
    build_reader,
    create_feature_app,
)
from ai_platform.inference.server import configure_telemetry
from ai_platform.inference.vault import load_from_environment


def build():
    """Assemble the application from the environment."""
    load_from_environment(os.environ)
    mirror_from_environment()
    configure_telemetry("feature-api")
    app = create_feature_app(
        reader=build_reader(FeatureApiSettings.from_env()),
        telemetry=OpenTelemetryFeatureTelemetry(),
    )
    app.mount("/metrics", make_asgi_app())
    return app


def main() -> None:
    """Serve the Feature API."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
