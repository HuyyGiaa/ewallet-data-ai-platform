"""Artifact-backed drift backends: a versioned baseline registry and a window sampler.

Both read immutable, checksum-pinned files produced by
``ops/llm_agent/drift_artifacts.py`` from an approved Feast Offline snapshot:

- ``ewallet-drift-baselines/v1`` (JSON): one PSI reference distribution per
  ``(baseline_id, feature_name)`` with its reference window, dataset revision,
  bins, smoothing, minimum sample size, optional thresholds and source digest.
- ``ewallet-drift-window/v1`` (JSON manifest + gzip CSV): numeric observations
  of monitored features with their event timestamps, the dataset revision they
  came from, the time range they cover and how they were sampled. CSV keeps the
  server free of a columnar engine and fits a Kubernetes ConfigMap (1 MiB).

The service pins the SHA-256 of both JSON documents (and the manifest pins every
observation file), so a changed artifact is refused rather than silently used. An
artifact marked ``synthetic`` (the local integration fixture) is refused unless
the deployment explicitly allows it. A window that is not fully covered by the
observations is rejected instead of being computed from a partial population;
a single Online Store value is never treated as a population.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from ai_platform.llm_agent.contracts import REVISION_RE
from ai_platform.llm_agent.tools.drift import MONITORED_FEATURES, DriftBaseline, WindowSample
from ai_platform.llm_agent.tools.runtime import ToolRejectedError, ToolUnavailableError

BASELINES_SCHEMA = "ewallet-drift-baselines/v1"
WINDOW_SCHEMA = "ewallet-drift-window/v1"
MAX_JSON_BYTES = 1_048_576
MAX_OBSERVATION_BYTES = 16 * 1_048_576
MAX_OBSERVATION_ROWS = 2_000_000
_SHA256 = "0123456789abcdef"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _SHA256 for char in value)


def parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must carry a timezone")
    return parsed.astimezone(timezone.utc)


def _read_pinned_json(path: Path, expected_sha256: str, *, code: str, label: str) -> dict[str, Any]:
    """Read a small JSON document whose digest must equal the deployment pin."""

    if not _is_sha256(expected_sha256):
        raise ToolUnavailableError(code, f"{label} digest pin is not configured")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolUnavailableError(code, f"{label} is not readable") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ToolUnavailableError(code, f"{label} exceeds its size bound")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ToolUnavailableError(f"{code.rsplit('_', 1)[0]}_checksum_mismatch",
                                   f"{label} does not match its pinned SHA-256")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolUnavailableError(code, f"{label} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ToolUnavailableError(code, f"{label} must be a JSON object")
    return document


def _check_provenance(document: Mapping[str, Any], *, schema: str, expected_revision: str,
                      allow_synthetic: bool, code: str, label: str) -> str:
    if document.get("schema_version") != schema:
        raise ToolUnavailableError(code, f"{label} has an unsupported schema_version")
    revision = document.get("dataset_revision")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ToolUnavailableError(code, f"{label} has no immutable dataset_revision")
    if revision != expected_revision:
        raise ToolUnavailableError("drift_dataset_revision_mismatch",
                                   f"{label} dataset_revision differs from the configured revision")
    synthetic = document.get("synthetic")
    if not isinstance(synthetic, bool):
        raise ToolUnavailableError(code, f"{label} must declare whether it is synthetic")
    if synthetic and not allow_synthetic:
        raise ToolUnavailableError("drift_synthetic_artifact_refused",
                                   f"{label} is a synthetic fixture and this deployment does not allow it")
    return revision


def _baseline(item: object, *, dataset_revision: str, source_uri: str | None) -> DriftBaseline:
    if not isinstance(item, dict):
        raise ValueError("baseline entries must be objects")
    window = item.get("reference_window")
    if not isinstance(window, dict):
        raise ValueError("reference_window is required")
    thresholds = item.get("thresholds")
    pair: tuple[float, float] | None = None
    if thresholds is not None:
        if not isinstance(thresholds, dict) or set(thresholds) != {"warning", "drift"}:
            raise ValueError("thresholds must be null or {warning, drift}")
        pair = (thresholds["warning"], thresholds["drift"])
    source_sha256 = item.get("source_sha256")
    if source_sha256 is not None and not _is_sha256(source_sha256):
        raise ValueError("source_sha256 must be 64 lowercase hex characters")
    reference_count = item.get("reference_count")
    if reference_count is not None and (isinstance(reference_count, bool)
                                        or not isinstance(reference_count, int) or reference_count < 1):
        raise ValueError("reference_count must be a positive integer")
    return DriftBaseline(
        baseline_id=item["baseline_id"],
        feature_name=item["feature_name"],
        method=item["method"],
        cut_points=tuple(item["cut_points"]),
        reference_proportions=tuple(item["reference_proportions"]),
        smoothing=item["smoothing"],
        min_current_count=item["min_current_count"],
        reference_start=parse_utc(window.get("start"), "reference_window.start"),
        reference_end=parse_utc(window.get("end"), "reference_window.end"),
        dataset_revision=dataset_revision,
        thresholds=pair,
        source_uri=source_uri,
        source_sha256=source_sha256,
        reference_count=reference_count,
    )


class ArtifactBaselineStore:
    """Immutable baseline registry read from one checksum-pinned JSON document."""

    def __init__(self, path: Path, *, sha256: str, dataset_revision: str,
                 allow_synthetic: bool = False) -> None:
        self._path = path
        self._sha256 = sha256
        self._dataset_revision = dataset_revision
        self._allow_synthetic = allow_synthetic
        self._lock = Lock()
        self._baselines: dict[str, DriftBaseline] | None = None

    def _load(self) -> dict[str, DriftBaseline]:
        with self._lock:
            if self._baselines is not None:
                return self._baselines
            code, label = "drift_baseline_unavailable", "Drift baseline registry"
            document = _read_pinned_json(self._path, self._sha256, code=code, label=label)
            revision = _check_provenance(document, schema=BASELINES_SCHEMA,
                                         expected_revision=self._dataset_revision,
                                         allow_synthetic=self._allow_synthetic, code=code, label=label)
            source = document.get("source") if isinstance(document.get("source"), dict) else {}
            source_uri = source.get("uri") if isinstance(source.get("uri"), str) else None
            items = document.get("baselines")
            if not isinstance(items, list) or not items:
                raise ToolUnavailableError(code, "Drift baseline registry has no baselines")
            baselines: dict[str, DriftBaseline] = {}
            for item in items:
                try:
                    baseline = _baseline(item, dataset_revision=revision, source_uri=source_uri)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ToolUnavailableError(code, "Drift baseline registry contains an invalid baseline") from exc
                if baseline.baseline_id in baselines:
                    raise ToolUnavailableError(code, "Drift baseline ids must be unique")
                baselines[baseline.baseline_id] = baseline
            self._baselines = baselines
            return baselines

    async def get(self, baseline_id: str, feature_name: str) -> DriftBaseline | None:
        baseline = self._load().get(baseline_id)
        if baseline is None or baseline.feature_name != feature_name:
            return None
        return baseline

    async def ready(self) -> None:
        self._load()

    def describe(self) -> dict[str, object]:
        """Non-sensitive identity for capability and evidence reports."""

        baselines = self._load()
        return {"dataset_revision": self._dataset_revision, "sha256": self._sha256,
                "baselines": {key: value.feature_name for key, value in sorted(baselines.items())}}


@dataclass(frozen=True)
class _WindowFile:
    path: Path
    sha256: str
    rows: int
    features: tuple[str, ...]


@dataclass(frozen=True)
class _Observations:
    timestamps: tuple[datetime, ...]
    columns: Mapping[str, tuple[float | None, ...]]


class CsvWindowSampler:
    """Numeric observations of the current window from a checksum-pinned gzip CSV extract.

    Each file has the header ``event_timestamp,<feature>...``; timestamps are
    ISO-8601 UTC, values are decimal numbers and an empty cell is a null
    (excluded from the PSI and counted in ``excluded_null_count``).
    """

    def __init__(self, manifest_path: Path, *, sha256: str, dataset_revision: str,
                 allow_synthetic: bool = False) -> None:
        self._manifest_path = manifest_path
        self._sha256 = sha256
        self._dataset_revision = dataset_revision
        self._allow_synthetic = allow_synthetic
        self._lock = Lock()
        self._manifest: tuple[datetime, datetime, dict[str, _WindowFile]] | None = None
        self._tables: dict[Path, _Observations] = {}

    def _load_manifest(self) -> tuple[datetime, datetime, dict[str, _WindowFile]]:
        with self._lock:
            if self._manifest is not None:
                return self._manifest
            code, label = "drift_window_unavailable", "Drift window manifest"
            document = _read_pinned_json(self._manifest_path, self._sha256, code=code, label=label)
            _check_provenance(document, schema=WINDOW_SCHEMA, expected_revision=self._dataset_revision,
                              allow_synthetic=self._allow_synthetic, code=code, label=label)
            try:
                if document.get("format") != "csv.gz":
                    raise ValueError("unsupported observation format")
                coverage = document["coverage"]
                start = parse_utc(coverage["start"], "coverage.start")
                end = parse_utc(coverage["end"], "coverage.end")
                if end <= start:
                    raise ValueError("coverage end must be after start")
                by_feature: dict[str, _WindowFile] = {}
                for item in document["files"]:
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                        raise ValueError("window files must sit beside the manifest")
                    if not _is_sha256(item["sha256"]):
                        raise ValueError("window file sha256 is invalid")
                    rows = item["rows"]
                    if isinstance(rows, bool) or not isinstance(rows, int) or not 0 <= rows <= MAX_OBSERVATION_ROWS:
                        raise ValueError("window file rows out of bounds")
                    features = tuple(item["features"])
                    entry = _WindowFile(self._manifest_path.parent / relative, item["sha256"], rows, features)
                    for feature in features:
                        if feature not in MONITORED_FEATURES or feature in by_feature:
                            raise ValueError("window features must be unique monitored features")
                        by_feature[feature] = entry
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolUnavailableError(code, "Drift window manifest is invalid") from exc
            if not by_feature:
                raise ToolUnavailableError(code, "Drift window manifest lists no features")
            self._manifest = (start, end, by_feature)
            return self._manifest

    def _table(self, entry: _WindowFile, coverage: tuple[datetime, datetime]) -> _Observations:
        with self._lock:
            cached = self._tables.get(entry.path)
            if cached is not None:
                return cached
            try:
                raw = entry.path.read_bytes()
            except OSError as exc:
                raise ToolUnavailableError("drift_window_unavailable", "Drift window file is not readable") from exc
            if len(raw) > MAX_OBSERVATION_BYTES:
                raise ToolUnavailableError("drift_window_unavailable", "Drift window file exceeds its size bound")
            if hashlib.sha256(raw).hexdigest() != entry.sha256:
                raise ToolUnavailableError("drift_window_checksum_mismatch",
                                           "Drift window file does not match its manifest SHA-256")
            try:
                reader = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8"))
                header = next(reader)
                if header[0] != "event_timestamp" or tuple(header[1:]) != entry.features:
                    raise ValueError("header must be event_timestamp followed by the listed features")
                timestamps: list[datetime] = []
                columns: list[list[float | None]] = [[] for _ in entry.features]
                for row in reader:
                    if len(row) != len(header):
                        raise ValueError("ragged observation row")
                    stamp = parse_utc(row[0], "event_timestamp")
                    if not coverage[0] <= stamp < coverage[1]:
                        raise ValueError("observation outside the declared coverage")
                    timestamps.append(stamp)
                    for position, cell in enumerate(row[1:]):
                        value = float(cell) if cell != "" else None
                        if value is not None and not math.isfinite(value):
                            raise ValueError("non-finite observation")
                        columns[position].append(value)
            except (OSError, EOFError, StopIteration, UnicodeDecodeError, csv.Error, ValueError) as exc:
                raise ToolUnavailableError("drift_window_unavailable", "Drift window file is malformed") from exc
            if len(timestamps) != entry.rows:
                raise ToolUnavailableError("drift_window_unavailable", "Drift window file row count mismatch")
            table = _Observations(tuple(timestamps), {
                name: tuple(values) for name, values in zip(entry.features, columns)
            })
            self._tables[entry.path] = table
            return table

    async def sample(self, feature_name: str, start: datetime, end: datetime) -> WindowSample:
        coverage_start, coverage_end, by_feature = self._load_manifest()
        entry = by_feature.get(feature_name)
        if entry is None:
            raise ToolRejectedError("unsupported_feature",
                                    "No current-window observations exist for this feature")
        if start < coverage_start or end > coverage_end:
            raise ToolRejectedError("window_outside_coverage",
                                    "The current window is not fully covered by the approved observations")
        table = self._table(entry, (coverage_start, coverage_end))
        column = table.columns[feature_name]
        selected = [column[index] for index, stamp in enumerate(table.timestamps) if start <= stamp < end]
        values = tuple(value for value in selected if value is not None)
        return WindowSample(
            feature_name=feature_name, start=start, end=end, values=values,
            dataset_revision=self._dataset_revision, excluded_null_count=len(selected) - len(values),
            source_sha256=entry.sha256,
        )

    async def ready(self) -> None:
        coverage_start, coverage_end, by_feature = self._load_manifest()
        for entry in {item.path: item for item in by_feature.values()}.values():
            self._table(entry, (coverage_start, coverage_end))


def drift_backends_from_env(values: Mapping[str, str]) -> tuple[Any, Any]:
    """Artifact backends when ``EWALLET_DRIFT_SOURCE=artifact``; fail-closed stubs otherwise.

    Every path, digest pin and the dataset revision are required together, so a
    half-configured deployment stops at startup instead of serving partial data.
    """

    from ai_platform.inference.errors import ConfigurationError
    from ai_platform.llm_agent.tools.drift import UnavailableBaselineStore, UnavailableWindowSampler

    mode = values.get("EWALLET_DRIFT_SOURCE", "unavailable").strip() or "unavailable"
    if mode == "unavailable":
        return UnavailableBaselineStore(), UnavailableWindowSampler()
    if mode != "artifact":
        raise ConfigurationError()
    required = ("EWALLET_DRIFT_BASELINES_PATH", "EWALLET_DRIFT_BASELINES_SHA256",
                "EWALLET_DRIFT_WINDOW_MANIFEST", "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256",
                "EWALLET_DRIFT_DATASET_REVISION")
    if any(not values.get(name, "").strip() for name in required):
        raise ConfigurationError()
    revision = values["EWALLET_DRIFT_DATASET_REVISION"].strip()
    allow = values.get("EWALLET_DRIFT_ALLOW_SYNTHETIC", "false").strip().lower()
    if not REVISION_RE.fullmatch(revision) or allow not in ("true", "false"):
        raise ConfigurationError()
    for name in ("EWALLET_DRIFT_BASELINES_SHA256", "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"):
        if not _is_sha256(values[name].strip()):
            raise ConfigurationError()
    allow_synthetic = allow == "true"
    return (
        ArtifactBaselineStore(Path(values["EWALLET_DRIFT_BASELINES_PATH"]),
                              sha256=values["EWALLET_DRIFT_BASELINES_SHA256"].strip(),
                              dataset_revision=revision, allow_synthetic=allow_synthetic),
        CsvWindowSampler(Path(values["EWALLET_DRIFT_WINDOW_MANIFEST"]),
                             sha256=values["EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"].strip(),
                             dataset_revision=revision, allow_synthetic=allow_synthetic),
    )
