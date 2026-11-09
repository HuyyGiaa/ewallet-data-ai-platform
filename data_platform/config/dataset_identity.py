"""Approved dataset identities for the cloud batch campaigns (DP1, DP2, DP3).

A campaign binds one dataset version end to end: the source manifest, every winner
artifact and every run ID derived from it. Only the versions listed here may be
used. This keeps the historical guards explicit. ``canonical-100gb-v1`` keeps its
strict 100 GB source gate, and a rehearsal version cannot be confused with it.

* ``canonical-100gb-v1``: the canonical dataset in the canonical data bucket. Its
  historical outputs are immutable.
* ``rehearsal-5gb-v1``: an isolated rehearsal. It must live in its own bucket and is
  refused in the canonical data bucket, so its source, lakehouse runs, Spark event
  logs and evidence can never land next to the 100 GB outputs.

A dataset may also carry source extensions beyond the nine legacy physical
tables. ``merchant_history`` is the synthetic merchant snapshot table that feeds
the SCD Type 2 merchant dimension. ``canonical-100gb-v1`` was published without
it and stays that way; the rehearsal is generated with it. The stage submitters
read this flag so Silver and Gold include the extension exactly when the source
dataset carries it.

Adding a version (for example a fresh final 100 GB campaign) is a reviewed code
change, not a runtime parameter.
"""

from __future__ import annotations

from dataclasses import dataclass


CANONICAL_100GB = "canonical-100gb-v1"
REHEARSAL_5GB = "rehearsal-5gb-v1"
CANONICAL_DATA_BUCKET = "ewallet-data-ai-platform-hg26-data"


@dataclass(frozen=True)
class DatasetIdentity:
    version: str
    role: str
    target_source_bytes: int
    forbidden_buckets: frozenset[str]
    merchant_history: bool = False
    # Immutable plan identity of the published source, checked before publication
    # and by the campaign source gate. None means "not pinned here".
    generator_config: str | None = None
    global_seed: int | None = None
    source_shard_count: int | None = None
    size_tolerance_ratio: float | None = None


APPROVED_DATASETS: dict[str, DatasetIdentity] = {
    CANONICAL_100GB: DatasetIdentity(CANONICAL_100GB, "canonical", 100_000_000_000, frozenset()),
    REHEARSAL_5GB: DatasetIdentity(
        REHEARSAL_5GB, "rehearsal", 5_000_000_000, frozenset({CANONICAL_DATA_BUCKET}),
        merchant_history=True,
        generator_config="data_platform/generation/config/settings_merchant_history.yaml",
        global_seed=7001,
        source_shard_count=186,
        size_tolerance_ratio=0.1,
    ),
}


def require_approved_dataset(version: str) -> DatasetIdentity:
    """Return the identity of an approved dataset version, or refuse it."""

    identity = APPROVED_DATASETS.get(version)
    if identity is None:
        raise ValueError(f"dataset_version {version!r} is not an approved campaign dataset: "
                         f"{sorted(APPROVED_DATASETS)}")
    return identity


def require_dataset_bucket(version: str, bucket: str) -> DatasetIdentity:
    """Refuse a dataset version in a bucket it must not use."""

    identity = require_approved_dataset(version)
    if bucket in identity.forbidden_buckets:
        raise ValueError(f"{version} is a {identity.role} dataset and must not use bucket {bucket}; "
                         "use its dedicated bucket")
    return identity


def refuse_misplaced_dataset(version: str, bucket: str) -> None:
    """Refuse an approved dataset in a bucket it must not use; other versions pass.

    The stage submitters also serve smoke and calibration datasets that are not
    campaign identities, so they apply only this bucket rule, not the approval list.
    """

    identity = APPROVED_DATASETS.get(version)
    if identity is not None and bucket in identity.forbidden_buckets:
        raise ValueError(f"{version} is a {identity.role} dataset and must not use bucket {bucket}; "
                         "use its dedicated bucket")


def dataset_has_merchant_history(version: str) -> bool:
    """Return whether an approved dataset carries the merchant_history extension.

    Versions outside the registry (smoke and calibration datasets) never carry it.
    """

    identity = APPROVED_DATASETS.get(version)
    return identity is not None and identity.merchant_history


def require_publishable_dataset(version: str, bucket: str) -> DatasetIdentity | None:
    """Refuse publishing a canonical dataset or a dataset into a forbidden bucket.

    Returns the identity of a registered dataset, or None for smoke and calibration
    versions, which keep their existing freedom.
    """

    refuse_misplaced_dataset(version, bucket)
    identity = APPROVED_DATASETS.get(version)
    if identity is not None and identity.role == "canonical":
        raise ValueError(f"{version} is an immutable canonical dataset and cannot be published again")
    return identity
