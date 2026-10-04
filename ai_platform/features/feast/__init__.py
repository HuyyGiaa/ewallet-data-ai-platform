"""Feast offline historical retrieval for the Gold fraud features."""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
FEATURE_REPO = PACKAGE_ROOT / "feature_repo"
RUNTIME_DATA_ROOT = FEATURE_REPO / "data"
OFFLINE_DATA_ROOT = RUNTIME_DATA_ROOT / "offline"
