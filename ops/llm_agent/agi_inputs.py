"""Stage 1 inputs of the agent-integration (AGI) campaign, created only after owner approval.

redis-secret      derive a Feast Online connection Secret that names its Redis
                  database explicitly (``,db=N``) from an existing Secret in the
                  same namespace. The value flows kubectl -> this process ->
                  kubectl and is never printed or written to disk. DB 0 needs
                  ``--allow-db0-read``. Selecting a database does not reduce the
                  credential's privileges: the derived Secret has the same Redis
                  rights as its source (requirepass, no ACLs). Only the MCP
                  server's read-only code path keeps access read-only.
drift-configmaps  publish the drift baseline registry and window extract
                  (ops/llm_agent/drift_artifacts.py) as two immutable
                  ConfigMaps after loading them with the drift server's own
                  loaders. Synthetic artifacts are refused.

Every command prints its plan with ``--dry-run`` (no Secret value) and applies
nothing without ``--approve``.

usage:
  python -m ops.llm_agent.agi_inputs redis-secret --context CTX --namespace ewallet-mcp \\
      --source feast-online-redis --db 3 --name feast-online-redis-agi-rag --approve-sensitive-copy [--approve]
  python -m ops.llm_agent.agi_inputs drift-configmaps --context CTX --namespace ewallet-mcp \\
      --baselines DIR/baselines.json --window-manifest DIR/window/window-manifest.json [--approve]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

from ai_platform.llm_agent.rag.online_target import RagTargetError, parse_connection

LABELS = {"app.kubernetes.io/part-of": "ewallet-llm-agent", "ewallet.io/agi-input": "true"}
MAX_CONFIGMAP_BYTES = 900 * 1024
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


class InputError(ValueError):
    """The requested input is unsafe or invalid; the message never holds a credential."""


def with_database(connection: str, database: int) -> str:
    """Return the connection string with exactly one explicit database."""

    if not 0 <= database <= 15:
        raise InputError("database must be in 0-15")
    value = connection.strip()
    if value.startswith(("redis://", "rediss://")):
        parts = urlsplit(value)
        derived = urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))
    else:
        first, *options = value.split(",")
        kept = [option for option in options if option.partition("=")[0].strip() != "db"]
        derived = ",".join([first, *kept, f"db={database}"])
    try:
        endpoint = parse_connection(derived)
    except RagTargetError as exc:
        raise InputError("the source Secret is not a Feast Redis connection") from exc
    if endpoint.database != database:
        raise InputError("derived connection does not select the requested database")
    return derived


def derived_secret(name: str, namespace: str, key: str, connection: str, source: str, database: int) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {**LABELS, "ewallet.io/derived-from": source},
                         "annotations": {"ewallet.io/redis-database": str(database)}},
            "data": {key: base64.b64encode(connection.encode("utf-8")).decode("ascii")}}


def drift_configmaps(baselines: Path, manifest: Path, namespace: str, suffix: str) -> list[dict]:
    """Two immutable ConfigMaps; each file keeps its name, binary files go to binaryData."""

    from ops.llm_agent.drift_artifacts import describe

    report = describe(baselines, manifest)
    if report["synthetic"]:
        raise InputError("synthetic fixture artifacts are for local tests only")
    window_files = [manifest] + [manifest.parent / item["path"] for item in report["window"]["files"]]
    documents = []
    for name, files in ((f"drift-baselines-{suffix}", [baselines]), (f"drift-window-{suffix}", window_files)):
        data: dict[str, str] = {}
        binary: dict[str, str] = {}
        for path in files:
            raw = path.read_bytes()
            if path.suffix == ".json":
                data[path.name] = raw.decode("utf-8")
            else:
                binary[path.name] = base64.b64encode(raw).decode("ascii")
        size = sum(len(value) for value in (*data.values(), *binary.values()))
        if size > MAX_CONFIGMAP_BYTES:
            raise InputError(f"{name} is {size} bytes; regenerate the window with a lower --max-rows")
        document = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                    "metadata": {"name": name, "namespace": namespace, "labels": LABELS, "annotations": {
                        "ewallet.io/dataset-revision": report["window"]["dataset_revision"],
                        **{f"ewallet.io/sha256-{path.name.replace('.', '-')}"[:63]: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in files}}},
                    "data": data}
        if binary:
            document["binaryData"] = binary
        documents.append(document)
    return documents


def _kubectl(context: str, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["kubectl", "--context", context, *args], input=stdin, capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        raise SystemExit(f"kubectl {args[0]} failed: " + (result.stderr.strip().splitlines() or ["?"])[-1][:300])
    return result.stdout


def _plan(document: dict) -> dict:
    """Printable plan: names, sizes and annotations, never Secret data."""

    keys = sorted({*document.get("data", {}), *document.get("binaryData", {})})
    return {"kind": document["kind"], "namespace": document["metadata"]["namespace"],
            "name": document["metadata"]["name"], "keys": keys,
            "annotations": document["metadata"].get("annotations", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    secret = commands.add_parser("redis-secret")
    secret.add_argument("--source", required=True)
    secret.add_argument("--key", default="connection_string")
    secret.add_argument("--db", type=int, required=True)
    secret.add_argument("--name", required=True)
    secret.add_argument("--allow-db0-read", action="store_true")
    secret.add_argument("--approve-sensitive-copy", action="store_true")
    drift = commands.add_parser("drift-configmaps")
    drift.add_argument("--baselines", type=Path, required=True)
    drift.add_argument("--window-manifest", type=Path, required=True)
    drift.add_argument("--suffix", default="agi")
    for command in (secret, drift):
        command.add_argument("--context", required=True)
        command.add_argument("--namespace", default="ewallet-mcp")
        command.add_argument("--approve", action="store_true", help="apply; without it only the plan is printed")
    args = parser.parse_args(argv)
    if not NAME.fullmatch(args.namespace):
        parser.error("namespace must be a DNS label")
    try:
        if args.command == "redis-secret":
            if not NAME.fullmatch(args.name) or not args.name.startswith("feast-online-redis-agi-"):
                parser.error("--name must be feast-online-redis-agi-<role>")
            if args.db == 0 and not args.allow_db0_read:
                parser.error("DB 0 is production; pass --allow-db0-read (the MCP server reads it; the credential is not read-only)")
            if not args.approve_sensitive_copy:
                parser.error("redis-secret creates a second credential copy: pass --approve-sensitive-copy")
            source = json.loads(_kubectl(args.context, "-n", args.namespace, "get", "secret", args.source, "-o", "json"))
            encoded = (source.get("data") or {}).get(args.key)
            if not encoded:
                raise InputError(f"Secret {args.source} has no key {args.key}")
            connection = with_database(base64.b64decode(encoded).decode("utf-8"), args.db)
            documents = [derived_secret(args.name, args.namespace, args.key, connection, args.source, args.db)]
        else:
            documents = drift_configmaps(args.baselines, args.window_manifest, args.namespace, args.suffix)
    except (InputError, OSError, KeyError, ValueError) as exc:
        print(f"agi_inputs: {exc}", file=sys.stderr)
        return 2
    print(json.dumps([_plan(document) for document in documents], indent=1, sort_keys=True))
    if not args.approve:
        print("plan only: re-run with --approve to create these objects", file=sys.stderr)
        return 0
    for document in documents:
        print(_kubectl(args.context, "create", "-f", "-", "-o", "name", stdin=json.dumps(document)).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
