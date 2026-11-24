#!/usr/bin/env bash
# Compare one key of two Kubernetes Secrets without printing either value (READ-ONLY).
# usage: shadow_compare.sh <context> <namespace> <existing-secret> <shadow-secret> [key=connection_string]
#
# Prints exactly one status and exits 0 only for SHADOW_MATCH:
#   SHADOW_MATCH                      both present, both non-empty, values equal
#   SHADOW_MISMATCH                   both readable, values differ
#   SHADOW_<WHICH>_SECRET_MISSING     the Secret does not exist          (WHICH = EXISTING | SHADOW)
#   SHADOW_<WHICH>_KEY_MISSING        the Secret has no such key
#   SHADOW_<WHICH>_VALUE_EMPTY        the key is present but empty
#   SHADOW_<WHICH>_READ_FAILED        kubectl, jq or base64 failed (no conclusion possible)
# Values exist only inside this process; only SHA-256 digests are compared, and never printed.
set -uo pipefail
context=${1:?context} namespace=${2:?namespace} existing=${3:?existing secret} shadow=${4:?shadow secret}
key=${5:-connection_string}

digest() {  # <secret> <WHICH>: print the value's SHA-256, or print a failure status and return 1
  local json err b64 sum
  err=$(mktemp)
  if ! json=$(kubectl --context "$context" -n "$namespace" get secret "$1" -o json 2>"$err"); then
    if grep -q 'NotFound' "$err"; then echo "SHADOW_$2_SECRET_MISSING"; else echo "SHADOW_$2_READ_FAILED"; fi
    rm -f "$err"; return 1
  fi
  rm -f "$err"
  # "missing", or "v:" followed by the base64 value (a JSON null or non-string value fails jq).
  if ! b64=$(jq -er --arg k "$key" 'if (.data | type) == "object" and (.data | has($k)) then (if (.data[$k] | type) == "string" then "v:" + .data[$k] else error("not a string") end) else "missing" end' <<<"$json" 2>/dev/null); then
    echo "SHADOW_$2_READ_FAILED"; return 1
  fi
  case "$b64" in
    missing) echo "SHADOW_$2_KEY_MISSING"; return 1 ;;
    v:) echo "SHADOW_$2_VALUE_EMPTY"; return 1 ;;
  esac
  b64=${b64#v:}
  if ! sum=$(printf '%s' "$b64" | base64 -d 2>/dev/null | sha256sum); then
    echo "SHADOW_$2_READ_FAILED"; return 1
  fi
  echo "${sum%% *}"
}

if ! a=$(digest "$existing" EXISTING); then echo "$a"; exit 1; fi
if ! b=$(digest "$shadow" SHADOW); then echo "$b"; exit 1; fi
if [[ ! "$a" =~ ^[0-9a-f]{64}$ || ! "$b" =~ ^[0-9a-f]{64}$ ]]; then echo SHADOW_READ_FAILED; exit 1; fi
if [ "$a" = "$b" ]; then echo SHADOW_MATCH; exit 0; fi
echo SHADOW_MISMATCH; exit 1
