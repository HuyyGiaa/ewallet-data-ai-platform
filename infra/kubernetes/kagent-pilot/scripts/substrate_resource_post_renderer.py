#!/usr/bin/env python3
"""Bound resources, pin images and keep Valkey addressable after pod moves.

Substrate 0.0.9 has no values knob for any of these.
"""

from __future__ import annotations

import sys

import yaml


DEFAULT_RESOURCES = {
    "requests": {"cpu": "25m", "memory": "64Mi"},
    "limits": {"cpu": "250m", "memory": "256Mi"},
}

LARGER_CONTAINERS = {
    "ate-api-server": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "atelet": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "rustfs": {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "valkey": {
        "requests": {"cpu": "25m", "memory": "64Mi"},
        "limits": {"cpu": "250m", "memory": "256Mi"},
    },
}

# The chart artifact is digest-pinned, but several v0.0.9 templates still emit
# mutable image tags. These are the independently read-back multi-architecture
# manifest digests used by the approved pilot. Fail closed on an unknown tag;
# silently deploying a newly resolved image would defeat the review boundary.
PINNED_IMAGES = {
    "ghcr.io/kagent-dev/substrate/ateapi:v0.0.9": "sha256:3c50661b4e7d9e697ada97a97b2a8df563057a2cab5b9e7b0b09060ff80d9e85",
    "ghcr.io/kagent-dev/substrate/atecontroller:v0.0.9": "sha256:dc29e79987fe4177b92c8b30a4fa4f36f3cf1a80937b03a7a8cd3411f98693e5",
    "ghcr.io/kagent-dev/substrate/atelet:v0.0.9": "sha256:ec3e842a1a0a060192710c34be83354d0b45e8d0e40342384d3b836413c3c8e1",
    "ghcr.io/kagent-dev/substrate/atenet:v0.0.9": "sha256:15892218c70e736bca06bd9f1134425897f8b9c265428f932de5d6d779e9452a",
    "valkey/valkey:8.0": "sha256:04b9002ae24bbe66687fb025052ed9bcebb54f8da3937338eb718d98d362887f",
    "cr.agentgateway.dev/agentgateway:v1.3.0-alpha.1": "sha256:95e8d849c44ba31399daf504367ecfcfe2d3d3e59ffd96b1098234353afc3ef7",
    "coredns/coredns:1.11.1": "sha256:1eeb4c7316bacb1d4c8ead65571cd92dd21e27359f0d4917f1a5822a73b75db1",
    "busybox:1.36": "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
}


VALKEY_STATEFULSET = "valkey-cluster"
VALKEY_COMMAND = ["valkey-server", "/etc/valkey/valkey.conf"]
VALKEY_SERVICE = "valkey-cluster-service"
VALKEY_UID = 999  # the "valkey" user in the pinned valkey/valkey:8.0 image
MAX_MEET_ATTEMPTS = 5

# G5-R. Valkey keeps peer addresses in nodes.conf on its PVC, and pod IPs change on every
# reschedule. Each pass of this loop, per peer pod of the StatefulSet:
#   1. resolve its current IP through the headless Service;
#   2. ask it for its node ID; skip it unless that ID is already in this node's table,
#      so an unknown node (for example one whose PVC was lost) is never introduced;
#   3. do nothing if the table already holds exactly "<id> <ip>:6379@";
#   4. otherwise CLUSTER MEET it, at most MAX_MEET_ATTEMPTS times per IP, then give up
#      until the IP changes. MEET to a known ID only updates its address: no slot moves.
VALKEY_RECONCILER = r"""
set -u
attempts=/tmp/meet-attempts
mkdir -p "$attempts"
until valkey-cli -h 127.0.0.1 ping >/dev/null 2>&1; do sleep 2; done
while true; do
  if nodes=$(valkey-cli -h 127.0.0.1 cluster nodes 2>/dev/null); then
    i=0
    while [ "$i" -lt "$VALKEY_REPLICAS" ]; do
      host="valkey-cluster-$i.$VALKEY_SERVICE.$POD_NAMESPACE.svc.cluster.local"
      i=$((i + 1))
      ip=$(getent hosts "$host" | awk '{print $1; exit}')
      [ -n "$ip" ] && [ "$ip" != "$POD_IP" ] || continue
      id=$(timeout 3 valkey-cli -h "$ip" cluster myid 2>/dev/null || true)
      if [ -z "$id" ] || ! printf '%s\n' "$nodes" | awk '{print $1}' | grep -qx "$id"; then
        echo "skip $host $ip: node id '$id' is not a known member"
        continue
      fi
      if printf '%s\n' "$nodes" | grep -q "^$id $ip:6379@"; then
        rm -f "$attempts/$ip"
        continue
      fi
      n=$(cat "$attempts/$ip" 2>/dev/null || echo 0)
      if [ "$n" -ge "$MAX_MEET_ATTEMPTS" ]; then
        [ "$n" -eq "$MAX_MEET_ATTEMPTS" ] && echo "give up $host $ip after $n MEETs; needs an operator"
        echo $((n + 1)) > "$attempts/$ip"
        continue
      fi
      echo $((n + 1)) > "$attempts/$ip"
      echo "meet $host $ip (node $id), attempt $((n + 1))"
      valkey-cli -h 127.0.0.1 cluster meet "$ip" 6379
    done
  fi
  sleep 15
done
"""


def _pod_ip() -> dict:
    return {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}


def patch_valkey(document: dict) -> None:
    """Announce the pod IP and keep peer addresses current after any pod move."""
    if document.get("kind") != "StatefulSet" or document.get("metadata", {}).get("name") != VALKEY_STATEFULSET:
        return
    spec = document["spec"]["template"]["spec"]
    valkey = next((c for c in spec.get("containers", []) if c.get("name") == "valkey"), None)
    if valkey is None or valkey.get("command") != VALKEY_COMMAND or valkey.get("args") or valkey.get("env"):
        raise ValueError("valkey-cluster container changed upstream; review the G5-R announce-ip patch")
    if any(c.get("name") == "cluster-address-reconciler" for c in spec["containers"]):
        raise ValueError("valkey-cluster already has a reconciler; refusing to patch twice")
    valkey["env"] = [_pod_ip()]
    valkey["command"] = [*VALKEY_COMMAND, "--cluster-announce-ip", "$(POD_IP)"]
    # Process liveness for Service endpoints and DNS only; it says nothing about cluster health.
    # A cluster_state check here would hide every pod from DNS after a full restart.
    valkey["readinessProbe"] = {"exec": {"command": ["valkey-cli", "-h", "127.0.0.1", "ping"]},
                                "periodSeconds": 5, "timeoutSeconds": 2, "failureThreshold": 3}
    spec["containers"].append({
        "name": "cluster-address-reconciler",
        "image": valkey["image"],
        "command": ["/bin/sh", "-c", VALKEY_RECONCILER],
        "env": [
            _pod_ip(),
            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
            {"name": "VALKEY_SERVICE", "value": VALKEY_SERVICE},
            {"name": "VALKEY_REPLICAS", "value": str(document["spec"].get("replicas", 6))},
            {"name": "MAX_MEET_ATTEMPTS", "value": str(MAX_MEET_ATTEMPTS)},
            {"name": "HOME", "value": "/tmp"},
        ],
        "resources": {"requests": {"cpu": "5m", "memory": "16Mi"}, "limits": {"cpu": "50m", "memory": "32Mi"}},
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": VALKEY_UID, "runAsGroup": VALKEY_UID,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [{"name": "reconciler-tmp", "mountPath": "/tmp"}],
    })
    spec.setdefault("volumes", []).append({"name": "reconciler-tmp", "emptyDir": {"sizeLimit": "1Mi"}})


CA_BUNDLE_CONFIGMAP = "ateapi-ca"


def keep_ca_bundle_stable(document: dict) -> None:
    """Undo a chart bug that adds one leading newline to ca.crt on every upgrade.

    On upgrade the chart reuses the existing CA through ``lookup`` but indents it again with
    ``nindent``. The certificate is unchanged, but the ConfigMap would change on every release.
    Normalising to the install-time form (one leading newline) keeps upgrades from touching it.
    """
    if document.get("kind") != "ConfigMap" or document.get("metadata", {}).get("name") != CA_BUNDLE_CONFIGMAP:
        return
    data = document.get("data") or {}
    if isinstance(data.get("ca.crt"), str):
        data["ca.crt"] = "\n" + data["ca.crt"].lstrip("\n")


def pod_spec(document: dict) -> dict | None:
    kind = document.get("kind")
    spec = document.get("spec", {})
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return spec.get("template", {}).get("spec")
    return None


def main() -> int:
    documents = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
    for document in documents:
        patch_valkey(document)
        keep_ca_bundle_stable(document)
        spec = pod_spec(document)
        if spec is None:
            continue
        for section in ("initContainers", "containers"):
            for container in spec.get(section, []):
                image = container.get("image", "")
                if "@sha256:" not in image:
                    digest = PINNED_IMAGES.get(image)
                    if digest is None:
                        raise ValueError(f"unreviewed mutable Substrate image: {image}")
                    container["image"] = f"{image}@{digest}"
                container.setdefault(
                    "resources", LARGER_CONTAINERS.get(container.get("name"), DEFAULT_RESOURCES)
                )
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False, explicit_start=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
