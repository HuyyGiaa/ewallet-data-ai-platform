#!/usr/bin/env bash
# Read-only G3/KServe/Feast status capture. It performs no rollout or scaling.
# usage: KUBE_CONTEXT=<context> scripts/llm_agent/g3_runtime_readback.sh <evidence-dir>
set -euo pipefail

[ -n "${KUBE_CONTEXT:-}" ] || { echo "KUBE_CONTEXT is required" >&2; exit 2; }
[ "$#" -eq 1 ] || { echo "usage: g3_runtime_readback.sh <evidence-dir>" >&2; exit 2; }
evidence=$1
mkdir -p "$evidence"

kubectl --context "$KUBE_CONTEXT" config current-context > "$evidence/kube-context.txt"
{
  helm --kube-context "$KUBE_CONTEXT" -n default list --filter '^(llm-agent|llm-agent-mcp|kagent)$'
  kubectl --context "$KUBE_CONTEXT" -n ewallet-agents get deploy,pods -o wide
  kubectl --context "$KUBE_CONTEXT" -n ewallet-mcp get deploy,pods -o wide
  kubectl --context "$KUBE_CONTEXT" -n feature-store get statefulset,pods -o wide
  kubectl --context "$KUBE_CONTEXT" -n fraud-serving get inferenceservice,pods -o wide
} > "$evidence/runtime-health.txt"

kubectl --context "$KUBE_CONTEXT" -n ewallet-agents \
  get agent,remotemcpserver -o json > "$evidence/agent-mcp-status.json"
kubectl --context "$KUBE_CONTEXT" top pods -A --sort-by=memory \
  > "$evidence/pod-resources.txt" 2>&1 || true
sha256sum "$evidence"/* > "$evidence/SHA256SUMS.txt"
echo "read-only G3 runtime evidence written to $evidence"
