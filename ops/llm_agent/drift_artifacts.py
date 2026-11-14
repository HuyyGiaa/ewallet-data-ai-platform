"""Build the drift MCP artifacts: a versioned baseline registry and a current-window extract.

    python -m ops.llm_agent.drift_artifacts baseline --snapshot DIR --expect-manifest-sha256 SHA \\
        --feature F [--feature F ...] --reference-start TS --reference-end TS \\
        --baseline-id-prefix ID [--bins 10] [--min-current-count 100] [--thresholds 0.1,0.25] --out FILE
    python -m ops.llm_agent.drift_artifacts window --snapshot DIR --expect-manifest-sha256 SHA \\
        --feature F [--feature F ...] --start TS --end TS [--max-rows 20000] --out-dir DIR
    python -m ops.llm_agent.drift_artifacts fixture --out-dir DIR
    python -m ops.llm_agent.drift_artifacts describe --baselines FILE --window-manifest FILE

``baseline`` and ``window`` read a local copy of an immutable Feast Offline
snapshot (``_control/export_manifest.json`` plus ``feat_<view>/`` Parquet, see
``ai_platform/features/feast/sources/export_gold_to_parquet.py``). The manifest
digest must equal ``--expect-manifest-sha256``; its ``lineage.snapshot_id``
becomes the artifacts' ``dataset_revision``. Reading Parquet needs pyarrow (the
``ai_platform/features/feast`` or ``infra/docker/llm-agent-tooling --extra feast``
uv environment). Nothing is written to GCS, Redis or Kubernetes.

``fixture`` writes a small, deterministic, explicitly ``synthetic`` integration
fixture (dataset revision ``agi-fixture-synthetic-v1``) for local tests and
demos. The drift MCP server refuses synthetic artifacts unless
``EWALLET_DRIFT_ALLOW_SYNTHETIC=true``.

``describe`` prints the identities and digests a deployment pins
(``EWALLET_DRIFT_*``), after loading both artifacts with the server's own loaders.

The window extract is a deterministic systematic sample when the window holds
more rows than ``--max-rows``: rows are ordered by (event_timestamp, entity key)
and every k-th row is kept; the stride is recorded in the manifest.
"""

from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_left
from collections.abc import Iterable, Sequence
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import random
import sys
from typing import Any

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.llm_agent.business_request import MONITORED_DRIFT_FEATURES
from ai_platform.llm_agent.contracts import REVISION_RE
from ai_platform.llm_agent.tools.drift_sources import BASELINES_SCHEMA, WINDOW_SCHEMA, parse_utc

TOOL_REVISION = "drift-artifacts/v1"
FIXTURE_REVISION = "agi-fixture-synthetic-v1"
MAX_BINS = 50
CONFIGMAP_LIMIT_BYTES = 900 * 1024

Row = tuple[datetime, dict[str, float | None]]


class ArtifactError(ValueError):
    """The inputs cannot produce a trustworthy artifact."""


def _view_of(feature: str) -> tuple[str, str]:
    view, separator, column = feature.partition("__")
    if not separator or view not in FEATURE_SPECS or column not in FEATURE_SPECS[view]["features"]:
        raise ArtifactError(f"{feature} is not a Feast historical feature (<view>__<feature>)")
    return view, column


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def quantile_cut_points(values: Sequence[float], bins: int) -> tuple[float, ...]:
    """Distinct reference quantiles; ties (counts) collapse into fewer, wider bins."""

    if not 2 <= bins <= MAX_BINS:
        raise ArtifactError(f"bins must be in 2..{MAX_BINS}")
    ordered = sorted(values)
    cuts: list[float] = []
    for position in range(1, bins):
        candidate = ordered[min(len(ordered) - 1, math.ceil(position * len(ordered) / bins) - 1)]
        if (not cuts or candidate > cuts[-1]) and candidate < ordered[-1]:
            cuts.append(float(candidate))
    if not cuts:
        raise ArtifactError("the reference distribution is constant; PSI bins cannot be formed")
    return tuple(cuts)


def reference_proportions(values: Sequence[float], cut_points: Sequence[float]) -> tuple[float, ...]:
    counts = [0] * (len(cut_points) + 1)
    for value in values:
        counts[bisect_left(cut_points, value)] += 1
    return tuple(count / len(values) for count in counts)


def build_baseline(
    rows: Iterable[Row], *, feature: str, baseline_id: str, reference_start: datetime,
    reference_end: datetime, bins: int, min_current_count: int, smoothing: float,
    thresholds: tuple[float, float] | None,
) -> dict[str, Any]:
    """One baseline entry from the reference observations of one feature."""

    if feature not in MONITORED_DRIFT_FEATURES:
        raise ArtifactError(f"{feature} is not a monitored feature")
    if not REVISION_RE.fullmatch(baseline_id):
        raise ArtifactError(f"baseline id {baseline_id!r} is not an immutable revision identifier")
    values: list[float] = []
    nulls = 0
    for stamp, observation in rows:
        if reference_start <= stamp < reference_end:
            value = observation.get(feature)
            if value is None:
                nulls += 1
            elif math.isfinite(value):
                values.append(float(value))
            else:
                raise ArtifactError("reference observations contain a non-finite value")
    if len(values) < max(bins, min_current_count):
        raise ArtifactError(f"{feature}: only {len(values)} reference observations")
    cuts = quantile_cut_points(values, bins)
    proportions = reference_proportions(values, cuts)
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(f"{value!r}\n".encode("ascii"))
    return {
        "baseline_id": baseline_id,
        "feature_name": feature,
        "method": "psi",
        "cut_points": list(cuts),
        "reference_proportions": list(proportions),
        "smoothing": smoothing,
        "min_current_count": min_current_count,
        "reference_window": {"start": _iso(reference_start), "end": _iso(reference_end)},
        "thresholds": None if thresholds is None else {"warning": thresholds[0], "drift": thresholds[1]},
        "reference_count": len(values),
        "reference_null_count": nulls,
        # Digest of the sorted reference values: reproducible from the same snapshot and window.
        "source_sha256": digest.hexdigest(),
    }


def baselines_document(entries: list[dict[str, Any]], *, dataset_revision: str, source: dict[str, Any],
                       synthetic: bool) -> bytes:
    ids = [entry["baseline_id"] for entry in entries]
    if len(set(ids)) != len(ids):
        raise ArtifactError("baseline ids must be unique")
    return _canonical_json({
        "schema_version": BASELINES_SCHEMA,
        "dataset_revision": dataset_revision,
        "synthetic": synthetic,
        "generated_by": TOOL_REVISION,
        "source": source,
        "baselines": entries,
    })


def window_files(rows: Sequence[Row], *, features: Sequence[str], start: datetime, end: datetime,
                 max_rows: int, order_key: Sequence[str] | None = None) -> tuple[bytes, int, int]:
    """Deterministic gzip CSV of the window (systematic sample above ``max_rows``)."""

    selected = [row for row in rows if start <= row[0] < end]
    stride = max(1, math.ceil(len(selected) / max_rows)) if selected else 1
    if order_key is None:
        selected.sort(key=lambda row: row[0])
    kept = selected[::stride]
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(["event_timestamp", *features])
    for stamp, observation in kept:
        writer.writerow([_iso(stamp), *("" if observation.get(name) is None else repr(float(observation[name]))
                                        for name in features)])
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as stream:
        stream.write(text.getvalue().encode("utf-8"))
    return buffer.getvalue(), len(kept), stride


def write_window(out_dir: Path, groups: dict[str, list[str]], rows_by_view: dict[str, Sequence[Row]], *,
                 dataset_revision: str, source: dict[str, Any], start: datetime, end: datetime,
                 max_rows: int, synthetic: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for view, features in sorted(groups.items()):
        raw, kept, stride = window_files(rows_by_view[view], features=features, start=start, end=end,
                                         max_rows=max_rows)
        name = f"{view}.csv.gz"
        target = out_dir / name
        if target.exists():
            raise ArtifactError(f"{target} exists; artifacts are create-only")
        target.write_bytes(raw)
        files.append({"path": name, "sha256": _sha256_bytes(raw), "rows": kept, "features": features,
                      "sampling": {"method": "all" if stride == 1 else "systematic", "stride": stride,
                                   "order": ["event_timestamp", "entity_key"]}})
    manifest = out_dir / "window-manifest.json"
    if manifest.exists():
        raise ArtifactError(f"{manifest} exists; artifacts are create-only")
    manifest.write_bytes(_canonical_json({
        "schema_version": WINDOW_SCHEMA,
        "format": "csv.gz",
        "dataset_revision": dataset_revision,
        "synthetic": synthetic,
        "generated_by": TOOL_REVISION,
        "source": source,
        "coverage": {"start": _iso(start), "end": _iso(end)},
        "files": files,
    }))
    total = sum(path.stat().st_size for path in out_dir.iterdir())
    if total > CONFIGMAP_LIMIT_BYTES:
        print(f"WARNING: {total} bytes exceed one ConfigMap ({CONFIGMAP_LIMIT_BYTES}); lower --max-rows",
              file=sys.stderr)
    return manifest


# ---------------------------------------------------------------- snapshot I/O


def _snapshot(snapshot: Path, expected_sha256: str) -> tuple[str, dict[str, Any]]:
    path = snapshot / "_control" / "export_manifest.json"
    raw = path.read_bytes()
    if _sha256_bytes(raw) != expected_sha256:
        raise ArtifactError("export manifest digest differs from --expect-manifest-sha256")
    manifest = json.loads(raw)
    revision = (manifest.get("lineage") or {}).get("snapshot_id")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ArtifactError("export manifest has no immutable lineage.snapshot_id")
    return revision, manifest


def read_view(snapshot: Path, view: str, features: Sequence[str], start: datetime, end: datetime) -> list[Row]:
    """Rows of one FeatureView in [start, end), ordered by (event_timestamp, entity key)."""

    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    spec = FEATURE_SPECS[view]
    columns = [_view_of(name)[1] for name in features]
    dataset = ds.dataset(str(snapshot / spec["table"]), format="parquet")
    stamp_type = dataset.schema.field("event_timestamp").type
    if not pa.types.is_timestamp(stamp_type):
        raise ArtifactError(f"{view}.event_timestamp is not a timestamp")
    # Naive snapshot timestamps are UTC (export manifest: microsecond UTC).
    bound = (lambda value: value) if stamp_type.tz else (lambda value: value.astimezone(timezone.utc).replace(tzinfo=None))
    expression = ((ds.field("event_timestamp") >= pa.scalar(bound(start), type=stamp_type))
                  & (ds.field("event_timestamp") < pa.scalar(bound(end), type=stamp_type)))
    table = dataset.to_table(columns=["event_timestamp", spec["entity_key"], *columns], filter=expression)
    table = table.sort_by([("event_timestamp", "ascending"), (spec["entity_key"], "ascending")])
    stamps = pc.cast(table.column("event_timestamp"), pa.timestamp("us", tz="UTC")).to_pylist()
    values = {name: table.column(column).to_pylist() for name, column in zip(features, columns)}
    return [(stamp, {name: values[name][index] for name in features}) for index, stamp in enumerate(stamps)]


def _group(features: Sequence[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for feature in features:
        if feature not in MONITORED_DRIFT_FEATURES:
            raise ArtifactError(f"{feature} is not a monitored feature")
        groups.setdefault(_view_of(feature)[0], []).append(feature)
    return groups


# -------------------------------------------------------------------- fixture


def fixture_rows(seed: int = 20261115) -> dict[str, list[Row]]:
    """Deterministic synthetic observations; May shifts account velocity on purpose."""

    rng = random.Random(seed)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    account: list[Row] = []
    user: list[Row] = []
    for index in range(6_000):
        stamp = start + timedelta(minutes=14 * index + rng.randrange(14))
        shifted = stamp >= datetime(2026, 5, 1, tzinfo=timezone.utc)
        rate = 3.0 if shifted else 1.5
        count = 0
        threshold, product = math.exp(-rate), rng.random()
        while product > threshold:
            count += 1
            product *= rng.random()
        account.append((stamp, {"account_behavior__account_tx_count_1h": float(count)}))
        amount = None if rng.random() < 0.05 else round(math.exp(rng.gauss(12.0, 0.6)), 2)
        user.append((stamp, {"user_behavior__user_avg_amount_30d": amount}))
    return {"account_behavior": account, "user_behavior": user}


def write_fixture(out_dir: Path) -> dict[str, str]:
    rows = fixture_rows()
    source = {"uri": "synthetic://agi-integration-fixture", "export_manifest_sha256": None,
              "note": "Synthetic integration fixture for local tests; not project or production data."}
    reference = (datetime(2026, 4, 1, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc))
    entries = [
        build_baseline(rows["account_behavior"], feature="account_behavior__account_tx_count_1h",
                       baseline_id="bl-agi-fixture-v1.account_tx_count_1h", reference_start=reference[0],
                       reference_end=reference[1], bins=10, min_current_count=100, smoothing=1e-4,
                       thresholds=(0.1, 0.25)),
        build_baseline(rows["user_behavior"], feature="user_behavior__user_avg_amount_30d",
                       baseline_id="bl-agi-fixture-v1.user_avg_amount_30d", reference_start=reference[0],
                       reference_end=reference[1], bins=10, min_current_count=100, smoothing=1e-4,
                       thresholds=None),
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    baselines = out_dir / "baselines.json"
    if baselines.exists():
        raise ArtifactError(f"{baselines} exists; artifacts are create-only")
    baselines.write_bytes(baselines_document(entries, dataset_revision=FIXTURE_REVISION, source=source,
                                             synthetic=True))
    manifest = write_window(
        out_dir / "window",
        {"account_behavior": ["account_behavior__account_tx_count_1h"],
         "user_behavior": ["user_behavior__user_avg_amount_30d"]},
        rows, dataset_revision=FIXTURE_REVISION, source=source,
        start=datetime(2026, 5, 1, tzinfo=timezone.utc), end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        max_rows=20_000, synthetic=True,
    )
    return environment(baselines, manifest, FIXTURE_REVISION, allow_synthetic=True)


def environment(baselines: Path, manifest: Path, revision: str, *, allow_synthetic: bool) -> dict[str, str]:
    """The ``EWALLET_DRIFT_*`` settings that pin these artifacts."""

    return {
        "EWALLET_DRIFT_SOURCE": "artifact",
        "EWALLET_DRIFT_BASELINES_PATH": str(baselines),
        "EWALLET_DRIFT_BASELINES_SHA256": _sha256_bytes(baselines.read_bytes()),
        "EWALLET_DRIFT_WINDOW_MANIFEST": str(manifest),
        "EWALLET_DRIFT_WINDOW_MANIFEST_SHA256": _sha256_bytes(manifest.read_bytes()),
        "EWALLET_DRIFT_DATASET_REVISION": revision,
        "EWALLET_DRIFT_ALLOW_SYNTHETIC": "true" if allow_synthetic else "false",
    }


def describe(baselines: Path, manifest: Path) -> dict[str, Any]:
    """Load both artifacts with the server's own loaders and print what a deployment pins."""

    from ai_platform.llm_agent.tools.drift_sources import ArtifactBaselineStore, CsvWindowSampler

    document = json.loads(baselines.read_text(encoding="utf-8"))
    window = json.loads(manifest.read_text(encoding="utf-8"))
    revision = document["dataset_revision"]
    env = environment(baselines, manifest, revision, allow_synthetic=bool(document["synthetic"]))
    store = ArtifactBaselineStore(baselines, sha256=env["EWALLET_DRIFT_BASELINES_SHA256"],
                                  dataset_revision=revision, allow_synthetic=bool(document["synthetic"]))
    sampler = CsvWindowSampler(manifest, sha256=env["EWALLET_DRIFT_WINDOW_MANIFEST_SHA256"],
                               dataset_revision=window["dataset_revision"],
                               allow_synthetic=bool(window["synthetic"]))
    asyncio.run(store.ready())
    asyncio.run(sampler.ready())
    return {
        "environment": env,
        "baselines": [{key: entry[key] for key in ("baseline_id", "feature_name", "reference_window",
                                                   "reference_count", "thresholds", "min_current_count")}
                      for entry in document["baselines"]],
        "window": {"dataset_revision": window["dataset_revision"], "coverage": window["coverage"],
                   "files": [{key: item[key] for key in ("path", "rows", "features", "sha256", "sampling")}
                             for item in window["files"]]},
        "synthetic": bool(document["synthetic"] or window["synthetic"]),
    }


def _thresholds(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    warning, drift = (float(part) for part in value.split(","))
    if not 0 < warning < drift:
        raise ArtifactError("--thresholds must be warning,drift with 0 < warning < drift")
    return warning, drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("baseline", "window"):
        command = commands.add_parser(name)
        command.add_argument("--snapshot", type=Path, required=True)
        command.add_argument("--expect-manifest-sha256", required=True)
        command.add_argument("--feature", action="append", required=True)
        command.add_argument("--source-uri", default="", help="Recorded origin, e.g. the gs:// snapshot URI")
    baseline = commands.choices["baseline"]
    baseline.add_argument("--reference-start", required=True)
    baseline.add_argument("--reference-end", required=True)
    baseline.add_argument("--baseline-id-prefix", required=True)
    baseline.add_argument("--bins", type=int, default=10)
    baseline.add_argument("--min-current-count", type=int, default=100)
    baseline.add_argument("--smoothing", type=float, default=1e-4)
    baseline.add_argument("--thresholds", help="warning,drift (omit for an unclassified baseline)")
    baseline.add_argument("--out", type=Path, required=True)
    window = commands.choices["window"]
    window.add_argument("--start", required=True)
    window.add_argument("--end", required=True)
    window.add_argument("--max-rows", type=int, default=20_000)
    window.add_argument("--out-dir", type=Path, required=True)
    fixture = commands.add_parser("fixture")
    fixture.add_argument("--out-dir", type=Path, required=True)
    describe_command = commands.add_parser("describe")
    describe_command.add_argument("--baselines", type=Path, required=True)
    describe_command.add_argument("--window-manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "fixture":
            print(json.dumps(write_fixture(args.out_dir), indent=1, sort_keys=True))
            return 0
        if args.command == "describe":
            print(json.dumps(describe(args.baselines, args.window_manifest), indent=1, sort_keys=True))
            return 0
        revision, _ = _snapshot(args.snapshot, args.expect_manifest_sha256)
        source = {"uri": args.source_uri or str(args.snapshot), "export_manifest_sha256": args.expect_manifest_sha256}
        groups = _group(args.feature)
        if args.command == "baseline":
            start, end = parse_utc(args.reference_start, "reference-start"), parse_utc(args.reference_end, "reference-end")
            if args.out.exists():
                raise ArtifactError(f"{args.out} exists; artifacts are create-only")
            entries = []
            for view, features in sorted(groups.items()):
                rows = read_view(args.snapshot, view, features, start, end)
                for feature in features:
                    entries.append(build_baseline(
                        rows, feature=feature, baseline_id=f"{args.baseline_id_prefix}.{_view_of(feature)[1]}",
                        reference_start=start, reference_end=end, bins=args.bins,
                        min_current_count=args.min_current_count, smoothing=args.smoothing,
                        thresholds=_thresholds(args.thresholds)))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(baselines_document(entries, dataset_revision=revision, source=source, synthetic=False))
            print(json.dumps({"baselines": str(args.out), "dataset_revision": revision,
                              "sha256": _sha256_bytes(args.out.read_bytes()),
                              "ids": [entry["baseline_id"] for entry in entries]}, indent=1))
            return 0
        start, end = parse_utc(args.start, "start"), parse_utc(args.end, "end")
        rows_by_view = {view: read_view(args.snapshot, view, features, start, end) for view, features in groups.items()}
        manifest = write_window(args.out_dir, groups, rows_by_view, dataset_revision=revision, source=source,
                                start=start, end=end, max_rows=args.max_rows, synthetic=False)
        print(json.dumps({"window_manifest": str(manifest), "dataset_revision": revision,
                          "sha256": _sha256_bytes(manifest.read_bytes())}, indent=1))
        return 0
    except (ArtifactError, OSError, KeyError, ValueError) as exc:
        print(f"drift_artifacts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
