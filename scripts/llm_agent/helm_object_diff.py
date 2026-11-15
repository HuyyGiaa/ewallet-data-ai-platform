"""Object-level diff between a deployed Helm manifest and a local render (read-only).

Prints added, removed and changed objects (kind, namespace, name) and a unified diff of
each changed or added object. Use it before a `helm upgrade` to confirm that a change
touches only the objects its approval names. Secrets are listed by name only.

usage: helm -n default get manifest llm-agent > live.yaml
       helm template llm-agent infra/helm/llm-agent-platform -n default -f values.yaml [-f overlay.yaml] > new.yaml
       python3 scripts/llm_agent/helm_object_diff.py live.yaml new.yaml [--expect-objects N]
           [--expect-changed Kind/namespace/name ...] [--expect-added ...] [--expect-removed ...]
           [--expect-annotation-only Kind/namespace/name=<pod-template annotation> ...] [--expect-identities]

``--expect-annotation-only`` names an object that must change, and only in that pod-template
annotation (for example a config checksum that rolls a Deployment when a shared ConfigMap
changes). It counts as an expected change; any other difference fails.

``--expect-identities`` makes the three ``--expect-*`` lists exact: an object that
differs without being listed, or a listed object that does not differ, fails
the check (exit 1) even when the count is right. Without it only
``--expect-objects`` (a count) is checked, as before.
"""

from __future__ import annotations

import argparse
import difflib
import sys

import yaml


def objects(path: str) -> dict[tuple[str, str, str], dict]:
    with open(path, encoding="utf-8") as handle:
        return {(d["kind"], d["metadata"].get("namespace", ""), d["metadata"]["name"]): d
                for d in yaml.safe_load_all(handle) if d}


Key = tuple[str, str, str]


def identity(value: str) -> Key:
    """``Kind/namespace/name`` (namespace may be empty for cluster-scoped objects)."""

    parts = value.split("/")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise argparse.ArgumentTypeError(f"identity must be Kind/namespace/name: {value!r}")
    return parts[0], parts[1], parts[2]


def compare(live: dict[Key, dict], new: dict[Key, dict]) -> dict[str, list[Key]]:
    return {
        "added": sorted(set(new) - set(live)),
        "removed": sorted(set(live) - set(new)),
        "changed": sorted(k for k in set(live) & set(new) if live[k] != new[k]),
    }


def annotation_only(value: str) -> tuple[Key, str]:
    """``Kind/namespace/name=annotation``."""

    target, sep, annotation = value.partition("=")
    if not sep or not annotation:
        raise argparse.ArgumentTypeError(f"expected Kind/namespace/name=annotation: {value!r}")
    return identity(target), annotation


def differs_only_in_annotation(before: dict, after: dict, annotation: str) -> bool:
    """True when the two objects differ, and only in spec.template.metadata.annotations[annotation]."""

    import copy

    def strip(obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        annotations = ((obj.get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations")
        if isinstance(annotations, dict):
            annotations.pop(annotation, None)
        return obj

    return before != after and strip(before) == strip(after)


def identity_errors(diff: dict[str, list[Key]], expected: dict[str, list[Key]]) -> list[str]:
    """Every unexpected or missing identity, per category; empty means an exact match."""

    errors = []
    for label in ("changed", "added", "removed"):
        actual, wanted = set(diff[label]), set(expected[label])
        errors += [f"unexpected {label}: {'/'.join(key)}" for key in sorted(actual - wanted)]
        errors += [f"expected {label} but not {label}: {'/'.join(key)}" for key in sorted(wanted - actual)]
    return errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("live")
    p.add_argument("new")
    p.add_argument("--expect-objects", type=int, help="fail unless exactly this many objects differ")
    for label in ("changed", "added", "removed"):
        p.add_argument(f"--expect-{label}", type=identity, action="append", default=[], metavar="KIND/NS/NAME")
    p.add_argument("--expect-annotation-only", type=annotation_only, action="append", default=[],
                   metavar="KIND/NS/NAME=ANNOTATION")
    p.add_argument("--expect-identities", action="store_true",
                   help="the --expect-changed/added/removed lists must match exactly")
    a = p.parse_args(argv)
    try:
        live, new = objects(a.live), objects(a.new)
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(f"cannot read manifests: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    diff = compare(live, new)
    added, removed, changed = diff["added"], diff["removed"], diff["changed"]
    for label, keys in (("added", added), ("removed", removed), ("changed", changed)):
        print(f"{label}: {len(keys)}")
        for key in keys:
            print(f"  {key[0]} {key[1]}/{key[2]}")
    for key in changed + added:
        print(f"\n### {'CHANGED' if key in live else 'ADDED'} {key[0]} {key[1]}/{key[2]}")
        if key[0] == "Secret":
            print("(Secret content not printed)")
            continue
        before = yaml.safe_dump(live[key]).splitlines() if key in live else []
        print("\n".join(difflib.unified_diff(before, yaml.safe_dump(new[key]).splitlines(), lineterm="", n=1)))
    total = len(added) + len(removed) + len(changed)
    failed = False
    if a.expect_objects is not None and total != a.expect_objects:
        print(f"\nexpected {a.expect_objects} differing objects, found {total}", file=sys.stderr)
        failed = True
    if a.expect_identities:
        errors = []
        annotation_changed = []
        for key, annotation in a.expect_annotation_only:
            if key in live and key in new and differs_only_in_annotation(live[key], new[key], annotation):
                annotation_changed.append(key)
            else:
                errors.append(f"expected only annotation {annotation} to change: {'/'.join(key)}")
        errors += identity_errors(diff, {"changed": a.expect_changed + annotation_changed, "added": a.expect_added,
                                         "removed": a.expect_removed})
        for error in errors:
            print(error, file=sys.stderr)
        failed = failed or bool(errors)
    print("\nRESULT: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
