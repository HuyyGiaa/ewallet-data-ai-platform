"""Merge-base change detection and catalog dependency closure."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess

from scripts.ci.catalog import Catalog, load_catalog


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Scope:
    head: str | None
    merge_base: str | None
    target_ref: str | None
    changed_files: tuple[str, ...]
    components: tuple[str, ...]
    full_validation_reason: str | None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["changed_files"] = list(self.changed_files)
        data["components"] = list(self.components)
        return data


def _reverse_closure(catalog: Catalog, seeds: set[str]) -> tuple[str, ...]:
    selected = set(seeds)
    changed = True
    while changed:
        changed = False
        for name, component in catalog.components.items():
            if name not in selected and selected.intersection(component.dependencies):
                selected.add(name)
                changed = True
    return tuple(sorted(selected))


def detect_scope(
    catalog: Catalog,
    paths: list[str] | tuple[str, ...],
    full_reason: str | None = None,
    *,
    head: str | None = None,
    merge_base: str | None = None,
    target_ref: str | None = None,
) -> Scope:
    normalized = tuple(sorted({path.strip().lstrip("./") for path in paths if path.strip()}))
    if full_reason:
        return Scope(head, merge_base, target_ref, normalized, catalog.active_components, full_reason)
    if any(path.startswith(prefix) for path in normalized for prefix in catalog.shared_paths):
        return Scope(head, merge_base, target_ref, normalized, catalog.active_components, "shared_path_changed")
    selected: set[str] = set()
    unmapped: list[str] = []
    sources = [
        (prefix, name)
        for name, component in catalog.components.items()
        for prefix in component.source_paths
    ]
    for path in normalized:
        owners = {name for prefix, name in sources if path.startswith(prefix)}
        if owners:
            selected.update(owners)
        else:
            unmapped.append(path)
    if unmapped:
        return Scope(head, merge_base, target_ref, normalized, catalog.active_components, "unmapped_changed_paths")
    return Scope(head, merge_base, target_ref, normalized, _reverse_closure(catalog, selected), None)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )


def resolve_git_scope(
    root: Path,
    catalog: Catalog,
    head: str,
    target_ref: str | None,
) -> Scope:
    if not FULL_SHA.fullmatch(head):
        return detect_scope(catalog, (), "invalid_head_sha", head=head, target_ref=target_ref)
    if not target_ref:
        return detect_scope(catalog, (), "target_ref_not_provided", head=head)
    target = _git(root, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
    if target.returncode != 0:
        return detect_scope(catalog, (), "target_ref_unavailable", head=head, target_ref=target_ref)
    merge = _git(root, "merge-base", head, target_ref)
    if merge.returncode != 0 or not FULL_SHA.fullmatch(merge.stdout.strip()):
        return detect_scope(catalog, (), "merge_base_unavailable", head=head, target_ref=target_ref)
    merge_base = merge.stdout.strip()
    diff = _git(root, "diff", "--name-only", f"{merge_base}...{head}")
    if diff.returncode != 0:
        return detect_scope(
            catalog, (), "git_diff_failed", head=head, merge_base=merge_base, target_ref=target_ref
        )
    return detect_scope(
        catalog,
        diff.stdout.splitlines(),
        head=head,
        merge_base=merge_base,
        target_ref=target_ref,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--head")
    parser.add_argument("--target-ref")
    parser.add_argument("--changed-files", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    catalog.validate(args.root)
    head = args.head or _git(args.root, "rev-parse", "HEAD").stdout.strip()
    if args.changed_files:
        scope = detect_scope(catalog, args.changed_files.read_text(encoding="utf-8").splitlines(), head=head)
    else:
        scope = resolve_git_scope(args.root, catalog, head, args.target_ref)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scope.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
