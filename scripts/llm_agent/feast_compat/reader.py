"""Consumer side of the Feast cross-version check (the MCP runtime's locked Feast).

Loads the registries the writer published and reads through the MCP server's own code:
``FeastOnlineAdapter.get_features`` (feature_lookup and fraud_scoring) and
``FeastChunkRowSource.fetch`` (knowledge_search and chunk_lookup). Fails on any schema,
value, type, timestamp or response-key difference. Usage: reader.py <work-dir> <redis-host:port>
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys


@dataclass(frozen=True)
class Lookup:
    user_id: str
    account_id: str
    device_id: str
    merchant_id: str | None
    event_timestamp: datetime


def main(work: Path, redis: str) -> None:
    import feast
    from feast import FeatureStore
    from feast.repo_config import RepoConfig
    from feast.types import Float64, Int64

    from ai_platform.features.feast.schema import FEATURE_SPECS
    from ai_platform.inference.feast_online import FeastOnlineAdapter
    from ai_platform.llm_agent.rag.online_retriever import FeastChunkRowSource

    expected = json.loads((work / "expected.json").read_text())
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    fraud = FeatureStore(config=RepoConfig(
        project="ewallet_fraud_online", provider="local", registry=str(work / "fraud-registry.db"),
        online_store={"type": "redis", "connection_string": f"{redis},db=0"}, entity_key_serialization_version=3))
    views = {view.name: view for view in fraud.list_feature_views()}
    check(set(views) == set(FEATURE_SPECS), f"fraud views {sorted(views)}")
    for view_name, spec in FEATURE_SPECS.items():
        view = views.get(view_name)
        if view is None:
            continue
        integers = set(spec["integer_features"])
        dtypes = {field.name: field.dtype for field in view.schema}
        for feature in spec["features"]:
            check(dtypes.get(feature) == (Int64 if feature in integers else Float64), f"{view_name}.{feature} dtype {dtypes.get(feature)}")
        check([entity for entity in view.entities] == [spec["entity_name"]], f"{view_name} entities {view.entities}")

    keys = {spec["entity_key"]: expected["fraud"][name][spec["entity_key"]] for name, spec in FEATURE_SPECS.items()}
    request = Lookup(event_timestamp=datetime(2026, 5, 31, 23, 0, 30, tzinfo=timezone.utc), **keys)
    result = asyncio.run(FeastOnlineAdapter(fraud, timeout_seconds=10).get_features(request))
    values = result.values
    for view_name, spec in FEATURE_SPECS.items():
        written = expected["fraud"][view_name]
        for feature in spec["features"]:
            got = values.get(f"{view_name}__{feature}", values.get(feature))
            want = written[feature]
            same = got == want if isinstance(want, int) else (got is not None and math.isclose(float(got), want))
            check(same and type(got) in ((int,) if isinstance(want, int) else (float,)),
                  f"{view_name}.{feature}: {got!r} ({type(got).__name__}) != {want!r}")
        stamps = {key: value for key, value in result.timestamps.items() if key.startswith(view_name + "__")
                  or key in spec["features"]}
        check(len(stamps) == len(spec["features"]), f"{view_name} timestamps for {len(stamps)} features")
        for key, stamp in stamps.items():
            check(stamp is not None and stamp.astimezone(timezone.utc).isoformat() == written["event_timestamp"],
                  f"{key} timestamp {stamp}")

    rag = FeatureStore(config=RepoConfig(
        project="ewallet_rag_compat", provider="local", registry=str(work / "rag-registry.db"),
        online_store={"type": "redis", "connection_string": f"{redis},db=1"}, entity_key_serialization_version=3))
    rows = asyncio.run(FeastChunkRowSource(rag, timeout_seconds=10).fetch(["chunk-compat-1"]))
    want = expected["rag"]
    check(len(rows) == 1, f"rag rows {len(rows)}")
    if rows:
        row = rows[0]
        for name in ("document_id", "chunk_text", "source_uri", "source_checksum", "corpus_revision",
                     "embedding_model_revision", "feature_index_revision"):
            check(row.get(name) == want[name], f"rag.{name}: {row.get(name)!r}")
        embedding = list(row.get("embedding") or [])
        check(len(embedding) == len(want["embedding"]) and all(math.isclose(a, b, rel_tol=1e-6)
              for a, b in zip(embedding, want["embedding"])), f"rag.embedding {embedding}")

    verdict = "PASS" if not failures else "FAIL"
    report = {"writer_feast": expected["writer_feast"], "reader_feast": feast.__version__, "verdict": verdict,
              "checked": {"fraud_views": len(FEATURE_SPECS), "fraud_features": sum(len(s["features"]) for s in FEATURE_SPECS.values()),
                          "rag_fields": 8}, "failures": failures}
    (work / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report, indent=1, sort_keys=True))
    raise SystemExit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
