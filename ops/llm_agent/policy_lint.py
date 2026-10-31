"""Static Pod Security Baseline and allow-flow checks for rendered manifests.

Passing proves only the rendered source. Kubernetes admission, NetworkPolicy
enforcement and Istio Ambient identities remain runtime gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

import yaml


# Capabilities Pod Security Baseline permits in capabilities.add.
BASELINE_CAPABILITIES = frozenset({
    "AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD",
    "NET_BIND_SERVICE", "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT",
})
WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"})
OPEN_CIDRS = frozenset({"0.0.0.0/0", "::/0"})


def _pod_spec(document: Mapping[str, Any]) -> Mapping[str, Any]:
    if document["kind"] == "Pod":
        return document.get("spec", {})
    return document.get("spec", {}).get("template", {}).get("spec", {})


def baseline_violations(documents: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return Pod Security Baseline violations in rendered workloads."""

    errors: list[str] = []
    for document in documents:
        if document.get("kind") not in WORKLOAD_KINDS:
            continue
        name = f"{document['kind']}/{document['metadata']['name']}"
        spec = _pod_spec(document)
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(field):
                errors.append(f"{name}: {field} is forbidden")
        if any("hostPath" in volume for volume in spec.get("volumes") or ()):
            errors.append(f"{name}: hostPath volumes are forbidden")
        contexts = [("pod", spec.get("securityContext") or {})]
        for container in [*(spec.get("initContainers") or ()), *(spec.get("containers") or ())]:
            label = f"container {container.get('name')}"
            context = container.get("securityContext") or {}
            contexts.append((label, context))
            if context.get("privileged"):
                errors.append(f"{name}: {label} is privileged")
            added = set((context.get("capabilities") or {}).get("add") or ())
            if added - BASELINE_CAPABILITIES:
                errors.append(f"{name}: {label} adds {sorted(added - BASELINE_CAPABILITIES)}")
            if any(port.get("hostPort") for port in container.get("ports") or ()):
                errors.append(f"{name}: {label} uses hostPort")
        for label, context in contexts:
            if (context.get("seccompProfile") or {}).get("type") == "Unconfined":
                errors.append(f"{name}: {label} uses seccomp Unconfined")
    return errors


def _peer_errors(name: str, direction: str, rules: list[Mapping[str, Any]]) -> list[str]:
    peer_key = "from" if direction == "ingress" else "to"
    errors: list[str] = []
    for rule in rules:
        peers = rule.get(peer_key)
        if not peers:
            errors.append(f"{name}: {direction} rule without peers allows every source")
        if not rule.get("ports"):
            errors.append(f"{name}: {direction} rule without ports allows every port")
        for peer in peers or ():
            if (peer.get("ipBlock") or {}).get("cidr") in OPEN_CIDRS:
                errors.append(f"{name}: {direction} to an open CIDR")
            if "namespaceSelector" in peer and not peer["namespaceSelector"]:
                errors.append(f"{name}: {direction} peer selects every namespace")
            if "podSelector" in peer and not peer["podSelector"] and "namespaceSelector" not in peer:
                errors.append(f"{name}: {direction} peer selects every pod")
    return errors


def flow_violations(documents: Iterable[Mapping[str, Any]], *, namespace: str) -> list[str]:
    """Return default-deny and least-privilege allow-flow violations."""

    documents = list(documents)
    errors: list[str] = []
    policies = [item for item in documents if item.get("kind") == "NetworkPolicy"]
    authz = [item for item in documents if item.get("kind") == "AuthorizationPolicy"]
    if not any(
        item["metadata"].get("namespace") == namespace
        and item["spec"].get("podSelector") == {}
        and set(item["spec"].get("policyTypes") or ()) == {"Ingress", "Egress"}
        and not item["spec"].get("ingress") and not item["spec"].get("egress")
        for item in policies
    ):
        errors.append(f"{namespace}: missing NetworkPolicy default-deny for Ingress and Egress")
    if not any(item["metadata"].get("namespace") == namespace and not item.get("spec") for item in authz):
        errors.append(f"{namespace}: missing Istio AuthorizationPolicy default-deny (spec: {{}})")
    for item in policies:
        name = f"NetworkPolicy/{item['metadata']['name']}"
        spec = item["spec"]
        errors.extend(_peer_errors(name, "ingress", spec.get("ingress") or []))
        errors.extend(_peer_errors(name, "egress", spec.get("egress") or []))
    for item in authz:
        spec = item.get("spec") or {}
        if not spec:
            continue
        name = f"AuthorizationPolicy/{item['metadata']['name']}"
        if spec.get("action", "ALLOW") != "ALLOW":
            continue
        if not (spec.get("selector") or {}).get("matchLabels"):
            errors.append(f"{name}: ALLOW policy must select specific workloads")
        for rule in spec.get("rules") or [{}]:
            sources = [source.get("source") or {} for source in rule.get("from") or ()]
            principals = [value for source in sources for value in source.get("principals") or ()]
            if not principals or any("*" in value for value in principals):
                errors.append(f"{name}: ALLOW rule must name exact source principals")
            ports = [value for op in rule.get("to") or () for value in (op.get("operation") or {}).get("ports") or ()]
            if not ports:
                errors.append(f"{name}: ALLOW rule must restrict destination ports")
    return errors


def _selects(selector: Mapping[str, Any] | None, labels: Mapping[str, str]) -> bool:
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        raise ValueError("matchExpressions are not evaluated")
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())


def _in_cidr(ip: str, block: Mapping[str, Any]) -> bool:
    import ipaddress

    address = ipaddress.ip_address(ip)
    if address not in ipaddress.ip_network(block["cidr"]):
        return False
    return not any(address in ipaddress.ip_network(cidr) for cidr in block.get("except", []))


def _peer_matches(peer: Mapping[str, Any], endpoint: Mapping[str, Any], policy_ns: str) -> bool:
    if "ipBlock" in peer:
        return "ip" in endpoint and _in_cidr(endpoint["ip"], peer["ipBlock"])
    if "labels" not in endpoint:
        return False
    if "namespaceSelector" in peer:
        if not _selects(peer["namespaceSelector"], endpoint.get("namespace_labels", {})):
            return False
    elif endpoint.get("namespace") != policy_ns:
        return False
    return "podSelector" not in peer or _selects(peer["podSelector"], endpoint["labels"])


def _direction_allows(policies: list[Mapping[str, Any]], pod: Mapping[str, Any], peer: Mapping[str, Any],
                      port: int, direction: str) -> bool:
    key, kind = ("ingress", "Ingress") if direction == "ingress" else ("egress", "Egress")
    isolating = [p for p in policies if p["metadata"].get("namespace") == pod.get("namespace")
                 and kind in p["spec"].get("policyTypes", [])
                 and _selects(p["spec"].get("podSelector", {}), pod["labels"])]
    if not isolating:
        return True
    for policy in isolating:
        for rule in policy["spec"].get(key) or []:
            peers = rule.get("from" if key == "ingress" else "to")
            ports = rule.get("ports")
            if ports and not any(int(p["port"]) == port and p.get("protocol", "TCP") == "TCP" for p in ports):
                continue
            if peers is None or any(_peer_matches(x, peer, policy["metadata"]["namespace"]) for x in peers):
                return True
    return False


def allows(documents: Iterable[Mapping[str, Any]], source: Mapping[str, Any], destination: Mapping[str, Any],
           port: int, *, namespace: str) -> bool:
    """Whether rendered NetworkPolicies allow one TCP connection.

    An endpoint is a pod ({"namespace", "labels", "namespace_labels"}) or an
    address ({"ip"}). Kubernetes semantics: the source's egress and the
    destination's ingress must both allow it; an address side is not isolated.
    """

    policies = [d for d in documents if d.get("kind") == "NetworkPolicy"]
    for policy in policies:
        policy.setdefault("metadata", {}).setdefault("namespace", namespace)
    egress_ok = "labels" not in source or _direction_allows(policies, source, destination, port, "egress")
    ingress_ok = "labels" not in destination or _direction_allows(policies, destination, source, port, "ingress")
    return egress_ok and ingress_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered", type=Path, help="output of helm template")
    parser.add_argument("--namespace", default="ewallet-agents")
    args = parser.parse_args(argv)
    documents = [item for item in yaml.safe_load_all(args.rendered.read_text(encoding="utf-8")) if item]
    errors = baseline_violations(documents) + flow_violations(documents, namespace=args.namespace)
    print(json.dumps({"status": "FAIL" if errors else "SOURCE_ONLY_PASS", "errors": errors,
                      "runtime_verified": False}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
