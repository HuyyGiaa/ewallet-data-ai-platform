#!/usr/bin/env bash
# certbot --manual hook for HTTP-01 through the edge gateway (ewallet-edge acmeHttp01.enabled).
# usage: certbot ... --manual-auth-hook "acme_http01_hook.sh auth" --manual-cleanup-hook "acme_http01_hook.sh cleanup"
# certbot sets CERTBOT_DOMAIN, CERTBOT_TOKEN and CERTBOT_VALIDATION. Required env:
#   EDGE_ACME_CONTEXT       kube context of the cluster that runs the gateway
# Optional env:
#   EDGE_ACME_KUBECONFIG    kubeconfig file (default: kubectl's)
#   EDGE_ACME_NAMESPACE     default ewallet-ingress
#   EDGE_ACME_HTTP_PORT     port the challenge is checked on (default 80, as Let's Encrypt uses)
#   EDGE_ACME_RESOLVE_ADDR  address to send the check to instead of DNS (local tests only)
# "auth" returns only once the token is served through the gateway at the real hostname, so
# the CA never validates early. The token goes to the responder on stdin, never as an argument.
set -euo pipefail
action=${1:?auth or cleanup}
: "${EDGE_ACME_CONTEXT:?EDGE_ACME_CONTEXT is required}" "${CERTBOT_DOMAIN:?}" "${CERTBOT_TOKEN:?}"
[[ "$CERTBOT_TOKEN" =~ ^[A-Za-z0-9_-]{1,128}$ ]] || { echo "unexpected token format" >&2; exit 2; }
[[ "$CERTBOT_DOMAIN" =~ ^[a-z0-9.-]+$ ]] || { echo "unexpected domain" >&2; exit 2; }
ns=${EDGE_ACME_NAMESPACE:-ewallet-ingress} port=${EDGE_ACME_HTTP_PORT:-80}
k() { kubectl ${EDGE_ACME_KUBECONFIG:+--kubeconfig "$EDGE_ACME_KUBECONFIG"} --context "$EDGE_ACME_CONTEXT" -n "$ns" "$@"; }
case "$action" in
  auth)
    : "${CERTBOT_VALIDATION:?}"
    printf '%s' "$CERTBOT_VALIDATION" | k exec -i deploy/edge-acme-responder -- sh -c 'cat > "/challenges/$1"' sh "$CERTBOT_TOKEN"
    url="http://$CERTBOT_DOMAIN:$port/.well-known/acme-challenge/$CERTBOT_TOKEN"
    resolve=(); [ -z "${EDGE_ACME_RESOLVE_ADDR:-}" ] || resolve=(--resolve "$CERTBOT_DOMAIN:$port:$EDGE_ACME_RESOLVE_ADDR")
    for _ in $(seq 1 30); do
      [ "$(curl -s -m 5 "${resolve[@]}" "$url" || true)" == "$CERTBOT_VALIDATION" ] && exit 0
      sleep 2
    done
    echo "token for $CERTBOT_DOMAIN is not served at $url" >&2; exit 1 ;;
  cleanup)
    k exec deploy/edge-acme-responder -- rm -f "/challenges/$CERTBOT_TOKEN" ;;
  *) echo "action must be auth or cleanup" >&2; exit 2 ;;
esac
