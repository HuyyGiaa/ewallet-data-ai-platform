#!/usr/bin/env bash
# Create the edge credential Secret. The password is read from stdin, never an argument.
# usage: printf '%s' "$PASSWORD" | create_basic_auth.sh <username> [namespace] [context]
# Envoy accepts only the {SHA} htpasswd format, which is fast to attack, so use a long
# random password. Restart the gateway after a change: Envoy reads the file at start-up.
set -euo pipefail
user=${1:?username required} namespace=${2:-ewallet-edge}; shift 2 || shift $#
[[ "$user" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "invalid username" >&2; exit 2; }
password=$(cat)
[ "${#password}" -ge 24 ] || { echo "password must be at least 24 characters" >&2; exit 2; }
hash=$(printf '%s' "$password" | openssl dgst -binary -sha1 | base64)
kubectl ${1:+--context "$1"} -n "$namespace" create secret generic edge-basic-auth \
  --from-file=htpasswd=<(printf '%s:{SHA}%s\n' "$user" "$hash") --dry-run=client -o yaml \
  | kubectl ${1:+--context "$1"} apply -f -
