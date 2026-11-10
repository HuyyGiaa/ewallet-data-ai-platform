"""Object-level diff between a deployed Helm manifest and a local render (read-only).

Prints added, removed and changed objects (kind, namespace, name) and a unified diff of
each changed or added object. Use it before a `helm upgrade` to confirm that a change
touches only the objects its approval names. Secrets are listed by name only.

usage: helm -n default get manifest llm-agent > live.yaml
       helm template llm-agent infra/helm/llm-agent-platform -n default -f values.yaml [-f overlay.yaml] > new.yaml
       python3 scripts/llm_agent/helm_object_diff.py live.yaml new.yaml [--expect-objects N]
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("live")
    p.add_argument("new")
    p.add_argument("--expect-objects", type=int, help="fail unless exactly this many objects differ")
    a = p.parse_args()
    live, new = objects(a.live), objects(a.new)
    added, removed = sorted(set(new) - set(live)), sorted(set(live) - set(new))
    changed = sorted(k for k in set(live) & set(new) if live[k] != new[k])
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
    if a.expect_objects is not None and total != a.expect_objects:
        print(f"\nexpected {a.expect_objects} differing objects, found {total}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
