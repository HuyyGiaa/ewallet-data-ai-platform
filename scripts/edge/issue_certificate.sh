#!/usr/bin/env bash
# Issue the edge certificate with Let's Encrypt HTTP-01 through the gateway (G3-B), and
# optionally import it as ewallet-ingress/edge-tls.
# usage: issue_certificate.sh --domain D --context C --private-dir DIR [--kubeconfig F]
#          [--environment staging|production|self-signed] [--confirm-production] [--import] [--server URL] [--ca-bundle F]
# Preconditions (G3-B plan): ewallet-edge acmeHttp01.enabled=true, the public LB open on port 80,
# and the four names resolving to it. Staging is the default; production also needs
# --confirm-production. self-signed makes a 1-day ECDSA certificate for the four names (the
# temporary certificate before Let's Encrypt; fullchain.pem is its own CA file). The ACME account,
# key and certificate stay in --private-dir (outside the
# repository, mode 700); nothing secret is printed. --server/--ca-bundle point at another ACME CA
# (local Pebble tests only).
set -euo pipefail
domain="" context="" kubeconfig="" private="" environment=staging confirm="" import="" server="" bundle=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain=$2; shift 2 ;; --context) context=$2; shift 2 ;; --kubeconfig) kubeconfig=$2; shift 2 ;;
    --private-dir) private=$2; shift 2 ;; --environment) environment=$2; shift 2 ;;
    --confirm-production) confirm=1; shift ;; --import) import=1; shift ;;
    --server) server=$2; shift 2 ;; --ca-bundle) bundle=$2; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[[ "$domain" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "--domain is required" >&2; exit 2; }
[ -n "$context" ] || { echo "--context is required" >&2; exit 2; }
[ -n "$private" ] || { echo "--private-dir is required" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$private"; private=$(cd "$private" && pwd)
case "$private/" in "$root"/*) echo "--private-dir must be outside the repository" >&2; exit 2 ;; esac
chmod 700 "$private"
if [ "$environment" == self-signed ]; then
  live="$private/self-signed"; mkdir -p "$live"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=features.$domain" \
    -addext "subjectAltName=DNS:features.$domain,DNS:agents.$domain,DNS:registry.$domain,DNS:observe.$domain" \
    -keyout "$live/privkey.pem" -out "$live/fullchain.pem" 2>/dev/null
  chmod 600 "$live/privkey.pem"
fi
case "$environment" in
  self-signed) ;;
  staging) acme=${server:-https://acme-staging-v02.api.letsencrypt.org/directory}; name=edge-staging ;;
  production) [ -n "$confirm" ] || { echo "production needs --confirm-production (owner approval)" >&2; exit 2; }
              acme=${server:-https://acme-v02.api.letsencrypt.org/directory}; name=edge ;;
  *) echo "--environment must be staging, production or self-signed" >&2; exit 2 ;;
esac
names=(features agents registry observe); args=()
for n in "${names[@]}"; do args+=(-d "$n.$domain"); done
if [ "$environment" != self-signed ]; then
  export EDGE_ACME_CONTEXT=$context EDGE_ACME_KUBECONFIG=$kubeconfig
  [ -z "$bundle" ] || export REQUESTS_CA_BUNDLE=$bundle
  uvx --from certbot==5.10.0 certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --server "$acme" --config-dir "$private/config" --work-dir "$private/work" --logs-dir "$private/logs" \
    --manual --preferred-challenges http --manual-auth-hook "$root/scripts/edge/acme_http01_hook.sh auth" \
    --manual-cleanup-hook "$root/scripts/edge/acme_http01_hook.sh cleanup" --key-type ecdsa --cert-name "$name" "${args[@]}"
  live="$private/config/live/$name"
fi
sans=$(openssl x509 -in "$live/fullchain.pem" -noout -ext subjectAltName | tail -1)
for n in "${names[@]}"; do
  [[ "$sans" == *"DNS:$n.$domain"* ]] || { echo "certificate lacks $n.$domain" >&2; exit 1; }
done
echo "issued ($environment): $(openssl x509 -in "$live/fullchain.pem" -noout -issuer -enddate | tr '\n' ' ') files: $live"
if [ -n "$import" ]; then
  k() { kubectl ${kubeconfig:+--kubeconfig "$kubeconfig"} --context "$context" -n ewallet-ingress "$@"; }
  k create secret tls edge-tls --cert="$live/fullchain.pem" --key="$live/privkey.pem" --dry-run=client -o yaml | k apply -f - >/dev/null
  echo "imported into ewallet-ingress/edge-tls (Istio reloads it by SDS)"
fi
