"""Load and validate the authoritative CI/CD component catalog."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
from typing import Any


class CatalogError(ValueError):
    """The catalog is malformed or contradicts repository state."""


@dataclass(frozen=True)
class Component:
    name: str
    maturity: str
    source_paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    python_projects: tuple[str, ...]
    commands: tuple[str, ...]
    build_artifact: str | None
    deployment_target: str | None


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    shared_paths: tuple[str, ...]
    python_projects: tuple[str, ...]
    components: dict[str, Component]
    legacy_path_components: dict[str, tuple[str, ...]]
    profiles: dict[str, tuple[str, ...]]

    @property
    def active_components(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, item in self.components.items() if item.maturity == "active"))

    def validate(self, root: Path) -> None:
        if self.schema_version != 1:
            raise CatalogError(f"unsupported schema_version: {self.schema_version}")
        if not self.components:
            raise CatalogError("catalog must define components")
        if not self.legacy_path_components:
            raise CatalogError("catalog must define legacy_path_components")
        for profile, names in self.profiles.items():
            unknown_profile_components = set(names) - set(self.components)
            if unknown_profile_components:
                raise CatalogError(
                    f"profile {profile} has unknown components: {sorted(unknown_profile_components)}"
                )
        for project in self.python_projects:
            directory = root / project
            for required in ("pyproject.toml", "uv.lock"):
                if not (directory / required).is_file():
                    raise CatalogError(f"python project {project} is missing {required}")
        known_projects = set(self.python_projects)
        for name, component in self.components.items():
            if component.maturity not in {"active", "extension-only"}:
                raise CatalogError(f"component {name} has invalid maturity {component.maturity}")
            if component.maturity == "extension-only" and component.deployment_target:
                raise CatalogError(f"extension-only component {name} cannot define a deployment target")
            if not component.source_paths:
                raise CatalogError(f"component {name} has no source paths")
            for source in component.source_paths:
                if not (root / source.rstrip("/")).exists():
                    raise CatalogError(f"component {name} source path does not exist: {source}")
            unknown = set(component.dependencies) - set(self.components)
            if unknown:
                raise CatalogError(f"component {name} has unknown dependency: {sorted(unknown)}")
            unknown_projects = set(component.python_projects) - known_projects
            if unknown_projects:
                raise CatalogError(f"component {name} has unknown python project: {sorted(unknown_projects)}")
            if not component.commands:
                raise CatalogError(f"component {name} has no reproducible commands")
            for command in component.commands:
                if not shlex.split(command):
                    raise CatalogError(f"component {name} has an empty command")
                if "|| true" in command:
                    raise CatalogError(f"component {name} command hides failures")
        self._validate_acyclic()

    def _validate_acyclic(self) -> None:
        state: dict[str, int] = {}

        def visit(name: str, trail: tuple[str, ...]) -> None:
            if state.get(name) == 1:
                raise CatalogError(f"component dependency cycle: {' -> '.join((*trail, name))}")
            if state.get(name) == 2:
                return
            state[name] = 1
            for dependency in self.components[name].dependencies:
                visit(dependency, (*trail, name))
            state[name] = 2

        for name in sorted(self.components):
            visit(name, ())

    def matrix_markdown(self) -> str:
        header = "| Component | Source paths | Dependencies | Tests | Build artifact | Deployment target |"
        separator = "|---|---|---|---|---|---|"
        rows = [header, separator]
        for name, component in sorted(self.components.items()):
            rows.append(
                "| {name} | {paths} | {deps} | {tests} | {artifact} | {target} |".format(
                    name=name,
                    paths="<br>".join(f"`{path}`" for path in component.source_paths),
                    deps=", ".join(component.dependencies) or "None",
                    tests="<br>".join(f"`{command}`" for command in component.commands),
                    artifact=f"`{component.build_artifact}`" if component.build_artifact else "None",
                    target=f"`{component.deployment_target}`" if component.deployment_target else "None",
                )
            )
        return "\n".join(rows) + "\n"


def _strings(data: Any, field: str, context: str) -> tuple[str, ...]:
    if not isinstance(data, list) or not all(isinstance(item, str) and item for item in data):
        raise CatalogError(f"{context}.{field} must be a list of non-empty strings")
    return tuple(data)


def load_catalog(path: Path) -> Catalog:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot load catalog {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("components"), dict):
        raise CatalogError("catalog root and components must be objects")
    components: dict[str, Component] = {}
    for name, item in raw["components"].items():
        if not isinstance(name, str) or not name or not isinstance(item, dict):
            raise CatalogError("component entries must be named objects")
        components[name] = Component(
            name=name,
            maturity=item.get("maturity", ""),
            source_paths=_strings(item.get("source_paths"), "source_paths", name),
            dependencies=_strings(item.get("dependencies"), "dependencies", name),
            python_projects=_strings(item.get("python_projects"), "python_projects", name),
            commands=_strings(item.get("commands"), "commands", name),
            build_artifact=item.get("build_artifact"),
            deployment_target=item.get("deployment_target"),
        )
    return Catalog(
        schema_version=raw.get("schema_version"),
        shared_paths=_strings(raw.get("shared_paths"), "shared_paths", "catalog"),
        python_projects=_strings(raw.get("python_projects"), "python_projects", "catalog"),
        components=components,
        legacy_path_components={
            prefix: _strings(names, "legacy_path_components", prefix)
            for prefix, names in raw.get("legacy_path_components", {}).items()
        },
        profiles={
            profile: _strings(names, "profiles", profile)
            for profile, names in raw.get("profiles", {}).items()
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "matrix", "json"))
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    catalog.validate(args.root)
    if args.command == "validate":
        content = json.dumps({"status": "PASS", "components": len(catalog.components), "python_projects": len(catalog.python_projects)}, sort_keys=True) + "\n"
    elif args.command == "matrix":
        content = catalog.matrix_markdown()
    else:
        content = json.dumps({"active_components": catalog.active_components, "python_projects": catalog.python_projects}, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
