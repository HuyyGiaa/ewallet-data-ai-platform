"""G3 runtime inputs that come from the MLOps platform, without IAM changes.

replicate-secret  copy one explicitly approved Secret (by name, data only) from
                  the MLOps namespace into a named destination namespace. Values
                  flow kubectl -> kubectl and are
                  never printed or written to disk (owner option 2, scoped).
registry-configmap  publish a Feast file registry as an immutable ConfigMap
                  (binaryData ``registry.db``) after a sha256 check, from a
                  gs:// object read with the operator's own credentials or
                  from a local file (the RAG publication). The MCP pod mounts
                  it read-only, so it needs no GCS permission.

usage:
  python3 -m ops.llm_agent.g3_platform_inputs replicate-secret --context <ctx> \
      --source-namespace ml-platform --destination-namespace ewallet-mcp \
      --name feast-online-redis --approve-sensitive-copy
  python3 -m ops.llm_agent.g3_platform_inputs registry-configmap --context <ctx> \
      --source gs://.../registry.db --destination-namespace ewallet-mcp \
      --name feast-registry-fraud-online [--sha256 <hex>]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

NAMESPACE = "ewallet-agents"
NAMESPACE_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
LABELS = {"app.kubernetes.io/part-of": "ewallet-llm-agent", "ewallet.io/g3-input": "true"}
MAX_CONFIGMAP_BYTES = 900 * 1024


def _kubectl(context: str, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["kubectl", "--context", context, *args], input=stdin,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise SystemExit(f"kubectl {args[0]} failed: " + (result.stderr.strip().splitlines() or ["?"])[-1][:300])
    return result.stdout


def replicated_secret(source: dict, namespace: str = NAMESPACE) -> dict:
    """Same type and data, new namespace; no owner, annotations or server fields."""

    validate_namespace(namespace)
    return {"apiVersion": "v1", "kind": "Secret", "type": source.get("type", "Opaque"),
            "metadata": {"name": source["metadata"]["name"], "namespace": namespace,
                         "labels": {**LABELS, "ewallet.io/replicated-from": source["metadata"]["namespace"]}},
            "data": source.get("data") or {}}


def registry_configmap(name: str, content: bytes, origin: str, namespace: str = NAMESPACE) -> dict:
    validate_namespace(namespace)
    if len(content) > MAX_CONFIGMAP_BYTES:
        raise ValueError("registry too large for a ConfigMap")
    digest = hashlib.sha256(content).hexdigest()
    return {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": name, "namespace": namespace, "labels": LABELS,
                         "annotations": {"ewallet.io/registry-origin": origin, "ewallet.io/registry-sha256": digest}},
            "binaryData": {"registry.db": base64.b64encode(content).decode()}}


def validate_namespace(namespace: str) -> str:
    if not NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("destination namespace must be a Kubernetes DNS label")
    return namespace


def _read_registry(source: str) -> bytes:
    if source.startswith("gs://"):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "registry.db"
            subprocess.run(["gcloud", "storage", "cp", source, str(target)], check=True,
                           capture_output=True, timeout=120)
            return target.read_bytes()
    return Path(source).read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("replicate-secret")
    rep.add_argument("--context", required=True)
    rep.add_argument("--source-namespace", required=True)
    rep.add_argument("--destination-namespace", required=True)
    rep.add_argument("--name", required=True)
    rep.add_argument(
        "--approve-sensitive-copy", action="store_true",
        help="explicitly acknowledge creation of a second credential copy and its rotation duty",
    )
    reg = sub.add_parser("registry-configmap")
    reg.add_argument("--context", required=True)
    reg.add_argument("--source", required=True)
    reg.add_argument("--name", required=True)
    reg.add_argument("--destination-namespace", required=True)
    reg.add_argument("--sha256", default="")
    args = parser.parse_args(argv)
    try:
        validate_namespace(args.destination_namespace)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "replicate-secret":
        if not args.approve_sensitive_copy:
            parser.error("replicate-secret requires --approve-sensitive-copy before reading Secret material")
        source = json.loads(_kubectl(args.context, "-n", args.source_namespace, "get", "secret", args.name, "-o", "json"))
        print(_kubectl(args.context, "apply", "-f", "-", "-o", "name",
                       stdin=json.dumps(replicated_secret(source, args.destination_namespace))).strip())
        return 0
    content = _read_registry(args.source)
    manifest = registry_configmap(args.name, content, args.source, args.destination_namespace)
    actual = manifest["metadata"]["annotations"]["ewallet.io/registry-sha256"]
    if args.sha256 and args.sha256 != actual:
        raise SystemExit(f"sha256 mismatch: expected {args.sha256}, got {actual}")
    print(_kubectl(args.context, "apply", "-f", "-", "-o", "name", stdin=json.dumps(manifest)).strip(),
          f"sha256={actual} bytes={len(content)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
