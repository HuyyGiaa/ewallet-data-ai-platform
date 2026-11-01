"""Object-level GCS mirror for a Feast file registry.

Feast 0.66's GCS registry store calls ``Client.get_bucket()``, which needs
``storage.buckets.get``. Workloads that only hold object permissions mirror
the registry to a local file instead and write it back with a generation
precondition (0 = create-only), so concurrent writers cannot overwrite each
other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import storage


@dataclass
class RegistryMirror:
    uri: str
    local_path: Path
    generation: int  # 0 when the object did not exist

    @property
    def _blob(self) -> storage.Blob:
        bucket, _, name = self.uri[len("gs://"):].partition("/")
        return storage.Client().bucket(bucket).blob(name)

    def push(self) -> int:
        blob = self._blob
        blob.upload_from_filename(str(self.local_path), if_generation_match=self.generation)
        self.generation = int(blob.generation)
        return self.generation


def pull(uri: str, local_dir: Path) -> RegistryMirror:
    if not uri.startswith("gs://"):
        raise ValueError(f"expected a gs:// registry URI, got {uri!r}")
    local_dir.mkdir(parents=True, exist_ok=True)
    mirror = RegistryMirror(uri=uri, local_path=local_dir / "registry.db", generation=0)
    blob = mirror._blob
    try:
        blob.reload()
    except NotFound:
        return mirror
    blob.download_to_filename(str(mirror.local_path), if_generation_match=blob.generation)
    mirror.generation = int(blob.generation)
    return mirror


def mirror_from_environment(variable: str = "EWALLET_FEAST_REGISTRY") -> RegistryMirror | None:
    """Replace a gs:// registry in ``variable`` with a local mirror path."""

    uri = os.environ.get(variable, "")
    if not uri.startswith("gs://"):
        return None
    mirror = pull(uri, Path(os.environ.get("EWALLET_FEAST_REGISTRY_CACHE", "/tmp/feast-registry")))
    os.environ[variable] = str(mirror.local_path)
    return mirror
