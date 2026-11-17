"""Load and validate the authoritative CI/CD component catalog.

A component is a CI unit: source ownership, locked Python projects, test commands and the
container images built from it. Components never deploy anything. Deployment is described
by typed release units (``jenkins/catalog/release-units.json``, ``scripts/ci/release_units.py``),
so a change-detection scope can no longer turn into a deployment by itself.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Any

IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class CatalogError(ValueError):
    """The catalog is malformed or contradicts repository state."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class ImageSpec:
    """One container image built from a component at the release SHA."""

    name: str
    dockerfile: str
    target: str | None = None


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
    deploy_handler: str | None
    release_artifact_selector: str | None = None
    build_target: str | None = None
    images: tuple[ImageSpec, ...] = ()


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    shared_paths: tuple[str, ...]
    python_projects: tuple[str, ...]
    components: dict[str, Component]
    legacy_path_components: dict[str, tuple[str, ...]]
    profiles: dict[str, tuple[str, ...]]
    runtime_units: dict[str, str]
    non_runtime_paths: tuple[str, ...] = ()

    @property
    def images(self) -> dict[str, tuple[str, ImageSpec]]:
        """Every buildable image, keyed by image name: (owning component, spec)."""

        return {spec.name: (name, spec) for name, component in sorted(self.components.items())
                for spec in component.images}

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
        unknown_runtime_owners = set(self.runtime_units.values()) - set(self.components)
        if unknown_runtime_owners:
            raise CatalogError(
                "runtime_units have unknown owners: "
                f"{sorted(unknown_runtime_owners)}"
            )
        if not self.runtime_units:
            raise CatalogError("catalog must define runtime_units")
        for project in self.python_projects:
            directory = root / project
            for required in ("pyproject.toml", "uv.lock"):
                if not (directory / required).is_file():
                    raise CatalogError(f"python project {project} is missing {required}")
        known_projects = set(self.python_projects)
        for name, component in self.components.items():
            if component.maturity not in {"active", "extension-only"}:
                raise CatalogError(f"component {name} has invalid maturity {component.maturity}")
            if component.deploy_handler or component.deployment_target or component.release_artifact_selector:
                if component.maturity == "extension-only":
                    raise CatalogError(f"extension-only component {name} cannot define a deployment target")
                raise CatalogError(
                    f"component {name} declares a deployment; components only test and build, "
                    "deployments are release units (jenkins/catalog/release-units.json)"
                )
            for spec in component.images:
                if not IMAGE_NAME.fullmatch(spec.name):
                    raise CatalogError(f"component {name} has invalid image name {spec.name!r}")
                if not (root / spec.dockerfile).is_file():
                    raise CatalogError(f"component {name} image {spec.name} Dockerfile does not exist: {spec.dockerfile}")
                if component.maturity != "active":
                    raise CatalogError(f"extension-only component {name} cannot build release image {spec.name}")
            if component.build_artifact and not (root / component.build_artifact).is_file():
                raise CatalogError(
                    f"component {name} build artifact does not exist: {component.build_artifact}"
                )
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
        image_owners: dict[str, str] = {}
        for name, component in self.components.items():
            for spec in component.images:
                if spec.name in image_owners:
                    raise CatalogError(f"image {spec.name} is built by both {image_owners[spec.name]} and {name}")
                image_owners[spec.name] = name
        self._validate_acyclic()

    def runtime_units_for_components(
        self, components: tuple[str, ...] | list[str] | set[str]
    ) -> tuple[str, ...]:
        selected = set(components)
        return tuple(sorted(
            name for name, owner in self.runtime_units.items() if owner in selected
        ))

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


def _images(data: Any, context: str) -> tuple[ImageSpec, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise CatalogError(f"{context}.images must be a list")
    specs = []
    for item in data:
        if not isinstance(item, dict) or set(item) - {"name", "dockerfile", "target"}:
            raise CatalogError(f"{context}.images entries take only name, dockerfile and target")
        name, dockerfile, target = item.get("name"), item.get("dockerfile"), item.get("target")
        if not isinstance(name, str) or not isinstance(dockerfile, str) or not dockerfile:
            raise CatalogError(f"{context}.images entries need a name and a dockerfile")
        if target is not None and (not isinstance(target, str) or not target):
            raise CatalogError(f"{context}.images target must be a non-empty string")
        specs.append(ImageSpec(name, dockerfile, target))
    return tuple(specs)


def load_catalog(path: Path) -> Catalog:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
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
            deploy_handler=item.get("deploy_handler"),
            release_artifact_selector=item.get("release_artifact_selector"),
            build_target=item.get("build_target"),
            images=_images(item.get("images"), name),
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
        runtime_units={
            name: owner
            for name, owner in raw.get("runtime_units", {}).items()
            if isinstance(name, str) and name and isinstance(owner, str) and owner
        },
        non_runtime_paths=_strings(
            raw.get("non_runtime_paths", []), "non_runtime_paths", "catalog"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "matrix", "json"))
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--units", type=Path, default=Path("jenkins/catalog/release-units.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    catalog.validate(args.root)
    if args.command == "validate":
        from scripts.ci.release_units import load_release_units

        units = load_release_units(args.units)
        units.validate(args.root, catalog)
        content = json.dumps({"status": "PASS", "components": len(catalog.components),
                              "python_projects": len(catalog.python_projects),
                              "images": len(catalog.images), "release_units": len(units.units),
                              "release_groups": len(units.groups)}, sort_keys=True) + "\n"
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
