#!/usr/bin/env python3
"""Keep Helm the only writer of production images, and prove a release changes only images.

Jenkins holds no production rights. It publishes a promotion file holding digests and
nothing else, and whoever is approved to deploy runs it through these checks:

* ``drift``: every image Helm recorded must be the image running. A digest patched by hand
  would otherwise be rolled back silently by the next ``helm upgrade``.
* ``check``: the local chart must reproduce the deployed manifest, and adding the promotion
  must change only image identity. RBAC can't express "only the image field"; this can.
* ``promote``: runs both, then ``helm upgrade --atomic``, and refuses without
  ``--execute`` and ``PROMOTION_APPROVED=yes``.

``drift`` and ``check`` are read-only. Manifests and values can hold sensitive settings, so
they stay in memory or in a private temporary file, and only object names and paths are
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
DIGEST_REF = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
IDENTITY_ENV = frozenset({"EWALLET_IMAGE_DIGEST", "EWALLET_GIT_SHA"})
IDENTITY_ANNOTATIONS = frozenset({"ewallet.io/release-sha"})
# Promotion may set exactly these values, per Helm release.
PROMOTABLE = {
    "llm-agent": {"coordinator": ("agents", "coordinator", "byo", "image"),
                  "feature-rag-specialist": ("agents", "specialists", "feature-rag-specialist", "byo", "image")},
    "llm-agent-mcp": {"mcp-tools": ("mcpServices", "image")},
}
RELEASE_NAMESPACE = {"llm-agent": "default", "llm-agent-mcp": "ewallet-mcp"}
Key = tuple[str, str, str]
Runner = Callable[[Sequence[str]], str]


class ReleaseStateError(ValueError):
    """A release can't proceed; the message says why and holds no setting values."""


@dataclass(frozen=True)
class Change:
    """One difference between two renders."""

    key: Key
    path: str


def run_command(command: Sequence[str]) -> str:
    """Run a command; its output is never printed, and neither is its error text."""

    result = subprocess.run(list(command), capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        raise ReleaseStateError(f"{command[0]} {command[1]} failed with exit {result.returncode}")
    return result.stdout


def load_documents(text: str) -> dict[Key, dict[str, Any]]:
    """Index a rendered manifest by kind, namespace and name."""

    documents = {}
    for item in yaml.safe_load_all(text):
        if item and "kind" in item:
            metadata = item.get("metadata", {})
            documents[(item["kind"], metadata.get("namespace", ""), metadata.get("name", ""))] = item
    return documents


def declared_images(document: Mapping[str, Any]) -> list[str]:
    """Return the images an object declares, for Deployments and BYO Agents."""

    if document.get("kind") == "Agent":
        image = document.get("spec", {}).get("byo", {}).get("deployment", {}).get("image")
        return [image] if image else []
    if document.get("kind") in {"Deployment", "StatefulSet"}:
        return [item["image"] for item in document["spec"]["template"]["spec"]["containers"]]
    return []


def drift(declared: Mapping[Key, Mapping[str, Any]], live: Callable[[Key], Mapping[str, Any] | None]) -> list[str]:
    """Return every object whose running images differ from what Helm recorded."""

    problems = []
    for key, document in sorted(declared.items()):
        images = declared_images(document)
        if not images:
            continue
        found = live(key)
        if found is None:
            problems.append(f"{key[0]} {key[1]}/{key[2]}: recorded by Helm but absent")
        elif declared_images(found) != images:
            problems.append(f"{key[0]} {key[1]}/{key[2]}: running image differs from Helm's record")
    return problems


def _walk(left: Any, right: Any, path: str) -> Iterator[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        for name in sorted(set(left) | set(right), key=str):
            if name not in left or name not in right:
                yield f"{path}.{name}"
            else:
                yield from _walk(left[name], right[name], f"{path}.{name}")
    elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, pair in enumerate(zip(left, right)):
            label = pair[0].get("name", index) if isinstance(pair[0], dict) else index
            yield from _walk(pair[0], pair[1], f"{path}[{label}]")
    elif left != right:
        yield path


def is_identity_path(path: str) -> bool:
    """Return whether a changed path only states which image is running."""

    if re.search(r"\.(containers|initContainers)\[[^\]]+\]\.image$", path) or path.endswith(".byo.deployment.image"):
        return True
    if any(f".env[{name}].value" in path for name in IDENTITY_ENV):
        return True
    return any(path.endswith(f"annotations.{name}") for name in IDENTITY_ANNOTATIONS)


def changes(before: Mapping[Key, Any], after: Mapping[Key, Any]) -> list[Change]:
    """List every difference between two renders, including added and removed objects."""

    found = []
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            found.append(Change(key, "<object added or removed>"))
        else:
            found.extend(Change(key, path) for path in _walk(before[key], after[key], ""))
    return found


def non_image_changes(before: Mapping[Key, Any], after: Mapping[Key, Any]) -> list[Change]:
    """Return every change a promotion isn't allowed to make."""

    return [item for item in changes(before, after) if not is_identity_path(item.path)]


def promotion_values(release: str, images: Mapping[str, str], git_sha: str) -> dict[str, Any]:
    """Build the values overlay for one release, from digests only.

    Raises:
        ReleaseStateError: An image is unknown or not pinned, or the SHA is malformed.
    """

    if release not in PROMOTABLE:
        raise ReleaseStateError(f"{release} has no promotable images")
    if not SHA.fullmatch(git_sha):
        raise ReleaseStateError("git_sha must be a full commit SHA")
    unknown = sorted(set(images) - set(PROMOTABLE[release]))
    if unknown or not images:
        raise ReleaseStateError(f"images not promotable to {release}: {unknown or 'none given'}")
    values: dict[str, Any] = {}
    for name, reference in images.items():
        if not DIGEST_REF.fullmatch(reference):
            raise ReleaseStateError(f"{name} must be pinned by a sha256 digest")
        node = values
        *parents, leaf = PROMOTABLE[release][name]
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = reference
        if name == "mcp-tools":
            values["mcpServices"]["identity"] = {"imageDigest": reference.rsplit("@", 1)[1], "gitSha": git_sha}
    return values


def _helm_read(run: Runner, kind: str, release: str, context: str) -> str:
    extra = ["--output", "yaml"] if kind == "values" else []
    return run(["helm", "get", kind, release, "--kube-context", context, "--namespace", RELEASE_NAMESPACE[release], *extra])


def live_reader(run: Runner, context: str) -> Callable[[Key], Mapping[str, Any] | None]:
    """Return a reader of live objects; absence is ``None`` and a failed read is an error."""

    def read(key: Key) -> Mapping[str, Any] | None:
        resource = "agents.kagent.dev" if key[0] == "Agent" else key[0].lower()
        output = run(["kubectl", "--context", context, "--request-timeout=30s", "-n", key[1], "get", resource, key[2],
                      "--ignore-not-found", "-o", "json"])
        return json.loads(output) if output.strip() else None

    return read


def check_drift(run: Runner, release: str, context: str) -> list[str]:
    """Compare Helm's record of a release with the cluster."""

    declared = load_documents(_helm_read(run, "manifest", release, context))
    if not any(declared_images(document) for document in declared.values()):
        raise ReleaseStateError(f"{release} declares no images, so nothing was compared")
    return drift(declared, live_reader(run, context))


def render(run: Runner, release: str, values: Sequence[Path]) -> dict[Key, dict[str, Any]]:
    """Render the local chart with the given values files."""

    arguments = ["helm", "template", release, str(CHART), "--namespace", RELEASE_NAMESPACE[release]]
    for path in values:
        arguments += ["-f", str(path)]
    return load_documents(run(arguments))


def check_promotion(run: Runner, release: str, context: str, promotion: Path, live_values: Path) -> list[str]:
    """Return every reason this promotion is more than an image change."""

    live_values.write_text(_helm_read(run, "values", release, context), encoding="utf-8")
    deployed = load_documents(_helm_read(run, "manifest", release, context))
    before = render(run, release, [live_values])
    after = render(run, release, [live_values, promotion])
    problems = [f"chart differs from the deployed release: {item.key[0]} {item.key[2]}{item.path}"
                for item in changes(deployed, before)]
    problems += [f"promotion changes more than images: {item.key[0]} {item.key[2]}{item.path}"
                 for item in non_image_changes(before, after)]
    return problems


def promote(run: Runner, release: str, context: str, promotion: Path, live_values: Path) -> None:
    """Upgrade through Helm, so the release record stays true."""

    run(["helm", "upgrade", release, str(CHART), "--kube-context", context, "--namespace", RELEASE_NAMESPACE[release],
         "-f", str(live_values), "-f", str(promotion), "--atomic", "--wait", "--timeout", "15m"])


def report(problems: Iterable[str]) -> int:
    """Print problems and return an exit code."""

    items = list(problems)
    for item in items[:40]:
        print(item)
    if len(items) > 40:
        print(f"... and {len(items) - 40} more")
    print(f"{len(items)} problem(s)")
    return 1 if items else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("drift", "values", "check", "promote"))
    parser.add_argument("--release", choices=sorted(PROMOTABLE), required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--promotion", type=Path, help="values overlay written by the values command")
    parser.add_argument("--image", action="append", default=[], help="name=digest reference")
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "values":
            images = dict(item.split("=", 1) for item in args.image)
            text = yaml.safe_dump(promotion_values(args.release, images, args.git_sha), sort_keys=True)
            if args.output is None:
                parser.error("--output is required")
            args.output.write_text(text, encoding="utf-8")
            return 0
        if not args.context:
            parser.error("--context is required; the kubeconfig default is never used")
        if args.command == "drift":
            return report(check_drift(run_command, args.release, args.context))
        if args.promotion is None:
            parser.error("--promotion is required")
        with tempfile.TemporaryDirectory(prefix="release-state.") as directory:
            live_values = Path(directory) / "live-values.yaml"
            problems = [*check_drift(run_command, args.release, args.context),
                        *check_promotion(run_command, args.release, args.context, args.promotion, live_values)]
            code = report(problems)
            if args.command == "check" or code:
                return code
            if not args.execute or os.environ.get("PROMOTION_APPROVED") != "yes":
                print("checks passed; nothing changed (needs --execute and PROMOTION_APPROVED=yes)")
                return 0
            promote(run_command, args.release, args.context, args.promotion, live_values)
            return report(check_drift(run_command, args.release, args.context))
    except ReleaseStateError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
