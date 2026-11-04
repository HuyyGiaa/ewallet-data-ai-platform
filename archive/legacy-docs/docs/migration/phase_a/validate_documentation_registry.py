#!/usr/bin/env python3
"""Validate the documentation registry and bounded Markdown navigation links."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit


ALLOWED_STATUSES = {"CURRENT", "HISTORICAL", "PARTIAL", "PROPOSED", "UNKNOWN"}
NAVIGATION = {
    "docs/README.md",
    "docs/edai1/README.md",
    "docs/edai2/README.md",
    "docs/evidence/README.md",
    "docs/runbooks/README.md",
}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def markdown_links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return [match.group(1).strip() for match in LINK.finditer(text)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    root = args.repo_root.resolve()

    with args.registry.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    paths = [row["document_path"] for row in rows]
    duplicates = sorted(path for path, count in Counter(paths).items() if count > 1)
    missing_paths = sorted(path for path in paths if not (root / path).is_file())
    invalid_statuses = sorted(
        f"{row['document_path']}:{row['status']}"
        for row in rows
        if row["status"] not in ALLOWED_STATUSES
    )

    checked = valid = external = absolute = 0
    missing_links: list[str] = []
    navigation_missing: list[str] = []
    unsafe: list[str] = []
    for relative in paths:
        source = root / relative
        if not source.is_file() or source.suffix.lower() != ".md":
            continue
        for raw in markdown_links(source):
            checked += 1
            target = raw
            if target.startswith("<") and ">" in target:
                target = target[1 : target.index(">")]
            elif " " in target:
                target = target.split(" ", 1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("//"):
                external += 1
                continue
            target = unquote(parsed.path)
            if not target:
                valid += 1
                continue
            if target.startswith("/") or target.startswith("~"):
                absolute += 1
                continue
            resolved = (source.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                unsafe.append(f"{relative} -> {raw}")
                continue
            if resolved.exists():
                valid += 1
            else:
                item = f"{relative} -> {raw}"
                missing_links.append(item)
                if relative in NAVIGATION:
                    navigation_missing.append(item)

    errors = duplicates + missing_paths + invalid_statuses + unsafe + navigation_missing
    report = {
        "schema_version": "1.0",
        "registry_rows": len(rows),
        "unique_paths": len(set(paths)),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "missing_registry_paths": missing_paths,
        "duplicate_registry_paths": duplicates,
        "invalid_statuses": invalid_statuses,
        "markdown_links_checked": checked,
        "valid_relative_links": valid,
        "external_links": external,
        "absolute_local_references": absolute,
        "missing_link_count": len(missing_links),
        "missing_links": missing_links,
        "navigation_missing_links": navigation_missing,
        "unsafe_repository_escapes": unsafe,
        "status": "PASS" if not errors else "FAIL",
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
