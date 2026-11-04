"""Deterministically map changed paths to independently deployable components."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.ci.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[2]


def detect(paths: list[str]) -> list[str]:
    mapping = load_catalog(ROOT / "jenkins/catalog/components.json").legacy_path_components
    components: set[str] = set()
    for path in paths:
        for prefix, names in sorted(mapping.items(), key=lambda item: -len(item[0])):
            if path.startswith(prefix):
                components.update(names)
                break
    return sorted(components)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = [line.strip() for line in args.changed_files.read_text().splitlines() if line.strip()]
    args.output.write_text(json.dumps({"components": detect(paths)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
