"""Typed release units: what a release deploys, how, in which order and with which approval.

CI components (``scripts/ci/catalog.py``) say what to test and which images to build. Release
units say what is deployed. Keeping them apart means a change-detection scope can never turn
into a deployment by itself, and every deployment mechanism is one of a closed set of kinds
that ``scripts/ci/release_dispatch.py`` implements:

* ``rag-rehearsal``          RAG task image: CronJob + smoke Job in ``ewallet-cicd`` (``scripts/ci/deploy/rag_pipeline.sh``)
* ``bounded-rehearsal``      contract-driven rehearsal in ``ewallet-cicd`` (``scripts/ci/bounded_cd.py``)
* ``registry-gate``          read-only Prompt Management gate (``scripts/ci/registry_gate.py``)
* ``streaming-verification`` image smoke + isolated Block A campaign (``scripts/ci/streaming_verify.py``)
* ``agent-staging``          rehearsal stage, explicit release only: the real Agent stack (Helm staging profile,
                             staging WorkerPool and SandboxAgents) in the staging namespaces, E2E, then removed
                             (``scripts/ci/agent_staging.py``)
* ``sandbox-candidate``      production stage, before any promotion: release-scoped candidate
                             SandboxAgents on the shared WorkerPool, verified then deleted
                             (``scripts/ci/sandbox_candidate.py``)
* ``helm-image-promotion``   production, image-only Helm promotion (``scripts/ci/promote.py``)
* ``fleet-image-promotion``  production SandboxAgent image, JSON patch with test operations, no Helm
                             migration (``scripts/ci/fleet_promote.py``)

Production units run as one transaction (``scripts/ci/production_release.py``) with reverse rollback.

A release branch (``release/<group>``) selects one group. A group lists its rehearsal units and
its production units; production units are planned only for an approved production stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from scripts.ci.catalog import Catalog, CatalogError


KINDS = frozenset({"rag-rehearsal", "bounded-rehearsal", "registry-gate", "streaming-verification",
                   "agent-staging", "sandbox-candidate", "helm-image-promotion", "fleet-image-promotion"})
STAGES = frozenset({"rehearsal", "production"})
PRODUCTION_KINDS = frozenset({"sandbox-candidate", "helm-image-promotion", "fleet-image-promotion"})
SANDBOX_NAMESPACE = "ewallet-sandbox"
REHEARSAL_NAMESPACES = frozenset({"ewallet-cicd"})
# The isolated Agent staging namespaces (Architecture A', V2-P3A); only agent-staging units may target them.
STAGING_NAMESPACE = "ewallet-agents-staging"
# Namespaces a rehearsal may never target, whatever its contract says.
PRODUCTION_NAMESPACES = frozenset({"default", "ewallet-agents", "ewallet-mcp", "ewallet-sandbox", "feature-store",
                                   "fraud-serving", "kubeflow", "kagent", "keda", "ewallet-ci"})
# Helm releases a production promotion may touch, with the namespace each lives in.
HELM_RELEASES = {"llm-agent": "default", "llm-agent-mcp": "ewallet-mcp"}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
GROUP = re.compile(r"^release/[a-z0-9][a-z0-9-]{1,62}$")


class ReleaseUnitError(CatalogError):
    """The release-unit catalog is malformed or unsafe."""


@dataclass(frozen=True)
class HelmTarget:
    release: str
    namespace: str
    image_values: dict[str, str]


@dataclass(frozen=True)
class ReleaseUnit:
    name: str
    kind: str
    stage: str
    description: str
    components: tuple[str, ...]
    images: tuple[str, ...]
    depends_on: tuple[str, ...]
    prerequisites: tuple[str, ...]
    target_namespace: str
    lock: str
    verify: tuple[str, ...]
    contract: str | None = None
    profile: str | None = None
    helm: HelmTarget | None = None
    automation: str = "enabled"
    blocked_reason: str | None = None

    def to_plan(self) -> dict[str, Any]:
        """The fields a release plan records for this unit (re-checked before dispatch)."""

        return {
            "name": self.name, "kind": self.kind, "stage": self.stage, "components": list(self.components),
            "depends_on": list(self.depends_on), "prerequisites": list(self.prerequisites),
            "target_namespace": self.target_namespace, "lock": self.lock, "verify": list(self.verify),
            "contract": self.contract, "profile": self.profile,
            "helm": None if self.helm is None else {"release": self.helm.release, "namespace": self.helm.namespace,
                                                     "image_values": dict(self.helm.image_values)},
        }


@dataclass(frozen=True)
class ReleaseGroup:
    branch: str
    rehearsal: tuple[str, ...]
    production: tuple[str, ...]

    def units_for(self, stage: str) -> tuple[str, ...]:
        return self.production if stage == "production" else self.rehearsal


@dataclass(frozen=True)
class ReleaseUnits:
    schema_version: int
    lock_namespace: str
    units: dict[str, ReleaseUnit]
    groups: dict[str, ReleaseGroup] = field(default_factory=dict)

    def group(self, branch: str) -> ReleaseGroup:
        if branch not in self.groups:
            raise ReleaseUnitError(f"release branch {branch} has no release group; known: {sorted(self.groups)}")
        return self.groups[branch]

    def ordered(self, names: tuple[str, ...] | list[str]) -> list[str]:
        """Topological order of ``names``; every ``depends_on`` must be inside ``names``."""

        selected = list(dict.fromkeys(names))
        if len(selected) != len(list(names)):
            raise ReleaseUnitError("a release lists the same unit twice")
        for name in selected:
            if name not in self.units:
                raise ReleaseUnitError(f"unknown release unit {name}")
            missing = [item for item in self.units[name].depends_on if item not in selected]
            if missing:
                raise ReleaseUnitError(f"release unit {name} depends on {missing}, which this release does not deploy")
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in ordered:
                return
            if name in visiting:
                raise ReleaseUnitError(f"release unit dependency cycle at {name}")
            visiting.add(name)
            for dependency in self.units[name].depends_on:
                visit(dependency)
            visiting.remove(name)
            ordered.append(name)

        for name in selected:
            visit(name)
        return ordered

    def validate(self, root: Path, catalog: Catalog) -> None:
        if self.schema_version != 1:
            raise ReleaseUnitError(f"unsupported release-unit schema_version {self.schema_version}")
        if self.lock_namespace not in REHEARSAL_NAMESPACES:
            raise ReleaseUnitError("release locks must live in the CD namespace")
        images = catalog.images
        for name, unit in self.units.items():
            self._validate_unit(root, catalog, images, name, unit)
        self.ordered(list(self.units))  # cycles and unknown dependencies
        for branch, group in self.groups.items():
            if not GROUP.fullmatch(branch):
                raise ReleaseUnitError(f"invalid release group name {branch}")
            if not group.rehearsal and not group.production:
                raise ReleaseUnitError(f"release group {branch} deploys nothing")
            for stage in ("rehearsal", "production"):
                names = group.units_for(stage)
                for unit_name in names:
                    if unit_name not in self.units:
                        raise ReleaseUnitError(f"release group {branch} names unknown unit {unit_name}")
                    if self.units[unit_name].stage != stage:
                        raise ReleaseUnitError(f"release group {branch} lists {unit_name} under {stage}")
                self.ordered(names)  # dependency closure inside the group

    def _validate_unit(self, root: Path, catalog: Catalog, images: dict, name: str, unit: ReleaseUnit) -> None:
        if not NAME.fullmatch(name) or unit.name != name:
            raise ReleaseUnitError(f"invalid release unit name {name}")
        if unit.kind not in KINDS:
            raise ReleaseUnitError(f"release unit {name} has unsupported kind {unit.kind}")
        if unit.stage not in STAGES:
            raise ReleaseUnitError(f"release unit {name} has unsupported stage {unit.stage}")
        if (unit.kind in PRODUCTION_KINDS) != (unit.stage == "production"):
            raise ReleaseUnitError(f"release unit {name}: kind {unit.kind} cannot run in stage {unit.stage}")
        if not unit.components or set(unit.components) - set(catalog.components):
            raise ReleaseUnitError(f"release unit {name} must name known CI components")
        for image in unit.images:
            if image not in images:
                raise ReleaseUnitError(f"release unit {name} consumes image {image}, which no component builds")
        if unit.kind in {"rag-rehearsal", "bounded-rehearsal", "streaming-verification", "helm-image-promotion",
                         "fleet-image-promotion", "sandbox-candidate", "agent-staging"} and not unit.images:
            raise ReleaseUnitError(f"release unit {name} of kind {unit.kind} needs at least one image")
        if unit.kind == "registry-gate" and unit.images:
            raise ReleaseUnitError(f"release unit {name}: a registry gate deploys no image")
        if not NAME.fullmatch(unit.lock):
            raise ReleaseUnitError(f"release unit {name} has an invalid lock name")
        if not unit.verify:
            raise ReleaseUnitError(f"release unit {name} declares no runtime verification")
        if unit.automation not in {"enabled", "blocked"}:
            raise ReleaseUnitError(f"release unit {name} has invalid automation {unit.automation}")
        if unit.automation == "blocked" and not unit.blocked_reason:
            raise ReleaseUnitError(f"blocked release unit {name} must say why")
        if unit.kind == "agent-staging":
            if unit.target_namespace != STAGING_NAMESPACE or set(unit.images) != {"g3-byo-coordinator", "mcp-tools"}:
                raise ReleaseUnitError(f"release unit {name}: agent staging targets {STAGING_NAMESPACE} "
                                       "with the g3-byo-coordinator and mcp-tools images")
        elif unit.stage == "rehearsal" and unit.kind != "streaming-verification" and unit.kind != "registry-gate":
            if unit.target_namespace not in REHEARSAL_NAMESPACES:
                raise ReleaseUnitError(f"rehearsal unit {name} targets {unit.target_namespace}, not the CD namespace")
        if unit.stage == "rehearsal" and unit.target_namespace in PRODUCTION_NAMESPACES - {"ml-platform"}:
            raise ReleaseUnitError(f"rehearsal unit {name} targets production namespace {unit.target_namespace}")
        if unit.kind in {"sandbox-candidate", "fleet-image-promotion"}:
            if unit.target_namespace != SANDBOX_NAMESPACE or tuple(unit.images) != ("g3-byo-coordinator",):
                raise ReleaseUnitError(f"release unit {name}: SandboxAgent units target {SANDBOX_NAMESPACE} "
                                       "with the g3-byo-coordinator image only")
        if unit.kind == "fleet-image-promotion" and unit.automation == "enabled":
            candidates = [dep for dep in unit.depends_on if self.units.get(dep) and self.units[dep].kind == "sandbox-candidate"]
            if not candidates:
                raise ReleaseUnitError(f"release unit {name}: a fleet promotion must depend on a sandbox candidate")
        needs_contract = unit.kind in {"rag-rehearsal", "bounded-rehearsal", "streaming-verification", "sandbox-candidate",
                                       "agent-staging"}
        if needs_contract != bool(unit.contract):
            raise ReleaseUnitError(f"release unit {name}: kind {unit.kind} {'needs' if needs_contract else 'takes no'} contract")
        if unit.contract:
            path = root / unit.contract
            if not path.is_file():
                raise ReleaseUnitError(f"release unit {name} contract does not exist: {unit.contract}")
            if unit.kind == "sandbox-candidate":
                from scripts.ci.sandbox_agents import SandboxContractError, load_contract

                try:
                    load_contract(path)
                except (SandboxContractError, ValueError, KeyError) as error:
                    raise ReleaseUnitError(f"release unit {name} candidate contract is invalid: {error}") from error
            elif unit.kind == "agent-staging":
                from scripts.ci.agent_staging import StagingContractError, load_contract as load_staging

                try:
                    load_staging(path)
                except (StagingContractError, ValueError, KeyError) as error:
                    raise ReleaseUnitError(f"release unit {name} staging contract is invalid: {error}") from error
            elif unit.kind == "bounded-rehearsal" or unit.kind == "streaming-verification":
                contract = json.loads(path.read_text(encoding="utf-8"))
                if contract.get("namespace") not in REHEARSAL_NAMESPACES:
                    raise ReleaseUnitError(f"release unit {name} contract targets {contract.get('namespace')}")
                if not (root / contract.get("manifest", "")).is_file():
                    raise ReleaseUnitError(f"release unit {name} contract manifest does not exist")
        if unit.kind == "streaming-verification":
            if not unit.profile or not (root / unit.profile).is_file():
                raise ReleaseUnitError(f"streaming unit {name} needs its campaign profile")
        elif unit.profile:
            raise ReleaseUnitError(f"release unit {name}: only streaming verification takes a profile")
        if unit.kind == "helm-image-promotion":
            helm = unit.helm
            if helm is None or HELM_RELEASES.get(helm.release) != helm.namespace:
                raise ReleaseUnitError(f"release unit {name} must target a known Helm release in its namespace")
            if set(helm.image_values) != set(unit.images):
                raise ReleaseUnitError(f"release unit {name}: every consumed image needs exactly one Helm image value")
        elif unit.helm is not None:
            raise ReleaseUnitError(f"release unit {name}: only Helm promotions name a Helm release")


def _strings(data: Any, context: str) -> tuple[str, ...]:
    if not isinstance(data, list) or not all(isinstance(item, str) and item for item in data):
        raise ReleaseUnitError(f"{context} must be a list of non-empty strings")
    return tuple(data)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseUnitError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


UNIT_FIELDS = frozenset({"description", "kind", "stage", "components", "images", "depends_on", "prerequisites",
                         "contract", "profile", "target_namespace", "lock", "verify", "helm", "automation",
                         "blocked_reason"})


def _unit(name: str, item: Any) -> ReleaseUnit:
    if not isinstance(item, dict):
        raise ReleaseUnitError(f"release unit {name} must be an object")
    unknown = set(item) - UNIT_FIELDS
    if unknown:
        raise ReleaseUnitError(f"release unit {name} has unknown fields {sorted(unknown)} (handlers are not configurable)")
    helm = item.get("helm")
    target = None
    if helm is not None:
        if not isinstance(helm, dict) or not isinstance(helm.get("image_values"), dict):
            raise ReleaseUnitError(f"release unit {name} helm must name release, namespace and image_values")
        target = HelmTarget(str(helm.get("release", "")), str(helm.get("namespace", "")),
                            {str(key): str(value) for key, value in helm["image_values"].items()})
    for required in ("kind", "stage", "target_namespace", "lock", "description"):
        if not isinstance(item.get(required), str) or not item[required]:
            raise ReleaseUnitError(f"release unit {name} needs {required}")
    return ReleaseUnit(
        name=name, kind=item["kind"], stage=item["stage"], description=item["description"],
        components=_strings(item.get("components"), f"{name}.components"),
        images=_strings(item.get("images", []), f"{name}.images") if item.get("images") else (),
        depends_on=_strings(item.get("depends_on", []), f"{name}.depends_on") if item.get("depends_on") else (),
        prerequisites=_strings(item.get("prerequisites", []), f"{name}.prerequisites") if item.get("prerequisites") else (),
        target_namespace=item["target_namespace"], lock=item["lock"],
        verify=_strings(item.get("verify"), f"{name}.verify"),
        contract=item.get("contract"), profile=item.get("profile"), helm=target,
        automation=item.get("automation", "enabled"), blocked_reason=item.get("blocked_reason"),
    )


def load_release_units(path: Path) -> ReleaseUnits:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseUnitError(f"cannot load release units {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("units"), dict) or not isinstance(raw.get("groups"), dict):
        raise ReleaseUnitError("release units need units and groups objects")
    units = {name: _unit(name, item) for name, item in raw["units"].items()}
    groups = {}
    for branch, item in raw["groups"].items():
        if not isinstance(item, dict) or set(item) != {"rehearsal", "production"}:
            raise ReleaseUnitError(f"release group {branch} needs exactly rehearsal and production lists")
        groups[branch] = ReleaseGroup(branch, tuple(item["rehearsal"]), tuple(item["production"]))
    return ReleaseUnits(raw.get("schema_version"), raw.get("lock_namespace", ""), units, groups)
