#!/usr/bin/env python3
"""Validate a captured kagent registry/lifecycle inventory without mutations.

The probe consumes JSON files exported by kubectl and HTTP Agent Card reads.
It deliberately emits only identities, health, capabilities and replica
counts; Kubernetes specs, environment values and Secret data are never copied
to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


class ProbeError(ValueError):
    """The captured registry violates the versioned platform contract."""


def _items(value: Mapping[str, Any], label: str) -> list[Mapping[str, Any]]:
    items = value.get("items")
    if not isinstance(items, list):
        raise ProbeError(f"{label}: expected a Kubernetes List with items")
    return [item for item in items if isinstance(item, Mapping)]


def _by_name(value: Mapping[str, Any], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in _items(value, label):
        name = item.get("metadata", {}).get("name")
        if not isinstance(name, str) or not name:
            raise ProbeError(f"{label}: resource without metadata.name")
        if name in result:
            raise ProbeError(f"{label}: duplicate resource {name}")
        result[name] = item
    return result


def _condition(resource: Mapping[str, Any], condition_type: str) -> str | None:
    for condition in resource.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type:
            status = condition.get("status")
            return status if isinstance(status, str) else None
    return None


def _require_condition(resource: Mapping[str, Any], name: str, condition_type: str) -> None:
    if _condition(resource, condition_type) != "True":
        raise ProbeError(f"{name}: expected {condition_type}=True")


def _require_owner(resource: Mapping[str, Any], agent: Mapping[str, Any], kind: str) -> None:
    metadata = resource.get("metadata", {})
    agent_metadata = agent.get("metadata", {})
    expected_uid = agent_metadata.get("uid")
    for owner in metadata.get("ownerReferences", []):
        if (
            owner.get("kind") == "Agent"
            and owner.get("name") == agent_metadata.get("name")
            and owner.get("uid") == expected_uid
            and owner.get("controller") is True
        ):
            return
    raise ProbeError(f"{agent_metadata.get('name')}: {kind} controller owner does not match Agent UID")


def build_report(contract: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
    """Validate captured resources and return a deterministic sanitized report."""

    expected_agents = contract.get("agents")
    if not isinstance(expected_agents, Mapping) or not expected_agents:
        raise ProbeError("contract.agents must be a non-empty object")

    agents = _by_name(capture.get("agents", {}), "agents")
    deployments = _by_name(capture.get("deployments", {}), "deployments")
    services = _by_name(capture.get("services", {}), "services")
    remotes = _by_name(capture.get("remote_mcp_servers", {}), "remote_mcp_servers")
    cards = capture.get("cards")
    if not isinstance(cards, Mapping):
        raise ProbeError("cards must be an object keyed by Agent resource name")

    if set(agents) != set(expected_agents):
        raise ProbeError(
            f"Agent inventory mismatch: expected {sorted(expected_agents)}, got {sorted(agents)}"
        )
    expected_remotes = contract.get("remoteMCPServers", [])
    if not isinstance(expected_remotes, list) or set(remotes) != set(expected_remotes):
        raise ProbeError(
            f"RemoteMCPServer inventory mismatch: expected {sorted(expected_remotes)}, got {sorted(remotes)}"
        )

    report_agents: list[dict[str, Any]] = []
    for name in sorted(expected_agents):
        expected = expected_agents[name]
        if not isinstance(expected, Mapping):
            raise ProbeError(f"contract agent {name} must be an object")
        agent = agents[name]
        _require_condition(agent, name, "Accepted")
        _require_condition(agent, name, "Ready")

        deployment = deployments.get(name)
        service = services.get(name)
        if deployment is None or service is None:
            raise ProbeError(f"{name}: generated Deployment and Service are required")
        _require_owner(deployment, agent, "Deployment")
        _require_owner(service, agent, "Service")

        card = cards.get(name)
        if not isinstance(card, Mapping):
            raise ProbeError(f"{name}: Agent Card is required")
        expected_card_name = expected.get("cardName", name)
        if card.get("name") != expected_card_name:
            raise ProbeError(f"{name}: Agent Card name must be {expected_card_name}")
        version = expected.get("version")
        if card.get("version") != version or agent.get("spec", {}).get("version") != version:
            raise ProbeError(f"{name}: Agent and Agent Card version must be {version}")
        skill_ids = sorted(
            skill.get("id") for skill in card.get("skills", [])
            if isinstance(skill, Mapping) and isinstance(skill.get("id"), str)
        )
        missing = sorted(set(expected.get("requiredSkills", [])) - set(skill_ids))
        if missing:
            raise ProbeError(f"{name}: Agent Card is missing required skill(s) {', '.join(missing)}")

        status = deployment.get("status", {})
        report_agents.append({
            "name": name,
            "namespace": agent.get("metadata", {}).get("namespace"),
            "uid": agent.get("metadata", {}).get("uid"),
            "type": agent.get("spec", {}).get("type"),
            "version": version,
            "conditions": {"Accepted": "True", "Ready": "True"},
            "skills": skill_ids,
            "replicas": {
                "available": int(status.get("availableReplicas", 0)),
                "ready": int(status.get("readyReplicas", 0)),
            },
            "ownedResources": ["Deployment", "Service"],
        })

    report_remotes: list[dict[str, Any]] = []
    for name in sorted(remotes):
        remote = remotes[name]
        _require_condition(remote, name, "Accepted")
        report_remotes.append({
            "name": name,
            "namespace": remote.get("metadata", {}).get("namespace"),
            "conditions": {"Accepted": "True"},
        })

    return {
        "schema": "ewallet-agent-platform-probe/v1",
        "verdict": "PASS",
        "counts": {"agents": len(report_agents), "remoteMCPServers": len(report_remotes)},
        "agents": report_agents,
        "remoteMCPServers": report_remotes,
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read {path}: {exc}") from exc


def _load_capture(root: Path) -> dict[str, Any]:
    return {
        "agents": _read_json(root / "agents.json"),
        "deployments": _read_json(root / "deployments.json"),
        "services": _read_json(root / "services.json"),
        "remote_mcp_servers": _read_json(root / "remote_mcp_servers.json"),
        "cards": _read_json(root / "cards.json"),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(_read_json(args.contract), _load_capture(args.capture_dir))
    payload = _canonical_json(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    print(f"PASS {args.output} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

