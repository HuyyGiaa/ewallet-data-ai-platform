#!/usr/bin/env bash
# The edge credential Secret (ewallet-ingress/edge-basic-auth). The password never appears in
# an argument, an environment variable or the output; the hash is only compared or piped.
#
#   printf '%s' "$PASSWORD" | create_basic_auth.sh <username> [namespace] [context]   create/replace
#   create_basic_auth.sh --credentials-file FILE --check|--apply [--namespace NS] [--context CTX] [--kubeconfig PATH]
#
# FILE: one line "user:password", outside the repository, mode 600.
# --check prints EDGE_AUTH_MATCH (exit 0), EDGE_AUTH_MISMATCH / EDGE_AUTH_MISSING (exit 1) or
# EDGE_AUTH_CHECK_FAILED (exit 2). It is read-only. --apply replaces the Secret (a cluster
# mutation: owner approval). Envoy reads the users file at start-up, so restart the gateway
# after --apply (g3b_campaign.py edge-auth does it and checks the behaviour).
# Envoy accepts only the {SHA} htpasswd format, which is fast to attack: use a long random password.
set -euo pipefail
mode=apply namespace=ewallet-ingress context="" kubeconfig="" file=""
if [ "${1:-}" = "--credentials-file" ]; then
  file=${2:?credentials file required}; shift 2
  mode=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --check) mode=check ;;
      --apply) mode=apply ;;
      --namespace) namespace=${2:?}; shift ;;
      --context) context=${2:?}; shift ;;
      --kubeconfig) kubeconfig=${2:?}; shift ;;
      *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
  done
  [ -n "$mode" ] || { echo "--check or --apply required" >&2; exit 2; }
  [ -f "$file" ] || { echo "EDGE_AUTH_CHECK_FAILED: credentials file missing"; exit 2; }
  if (( 8#$(stat -c %a "$file") & 8#077 )); then echo "EDGE_AUTH_CHECK_FAILED: credentials file must be mode 600"; exit 2; fi
  [ "$(grep -c . "$file")" -eq 1 ] || { echo "EDGE_AUTH_CHECK_FAILED: credentials file must hold exactly one user:password line"; exit 2; }
  line=$(grep . "$file")
  case "$line" in *:*) ;; *) echo "EDGE_AUTH_CHECK_FAILED: credentials file is not user:password"; exit 2 ;; esac
  user=${line%%:*} password=${line#*:}
  unset line
else
  user=${1:?username required} namespace=${2:-ewallet-ingress} context=${3:-}
  password=$(cat)
fi
[[ "$user" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "EDGE_AUTH_CHECK_FAILED: invalid username"; exit 2; }
[[ "$password" != *$'\r'* ]] || { echo "EDGE_AUTH_CHECK_FAILED: password contains a carriage return"; exit 2; }
[ "${#password}" -ge 24 ] || { echo "EDGE_AUTH_CHECK_FAILED: password must be at least 24 characters"; exit 2; }
kube=(kubectl ${kubeconfig:+--kubeconfig "$kubeconfig"} ${context:+--context "$context"} -n "$namespace")
expected="$user:{SHA}$(printf '%s' "$password" | openssl dgst -binary -sha1 | base64)"
unset password

if [ "$mode" = check ]; then
  if ! current=$("${kube[@]}" get secret edge-basic-auth --ignore-not-found -o 'jsonpath={.data.htpasswd}' 2>/dev/null); then
    echo "EDGE_AUTH_CHECK_FAILED: cannot read the Secret"; exit 2
  fi
  [ -n "$current" ] || { echo "EDGE_AUTH_MISSING"; exit 1; }
  if ! decoded=$(printf '%s' "$current" | base64 -d 2>/dev/null); then
    echo "EDGE_AUTH_CHECK_FAILED: Secret data is not base64"; exit 2
  fi
  # exactly this one user line: any other or extra user is a mismatch
  if [ "$decoded" = "$expected" ]; then echo "EDGE_AUTH_MATCH"; exit 0; fi
  echo "EDGE_AUTH_MISMATCH"; exit 1
fi

"${kube[@]}" create secret generic edge-basic-auth --from-file=htpasswd=<(printf '%s\n' "$expected") \
  --dry-run=client -o yaml | "${kube[@]}" apply -f - >/dev/null
echo "EDGE_AUTH_APPLIED"
