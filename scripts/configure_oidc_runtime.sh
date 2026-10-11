#!/usr/bin/env bash
set -euo pipefail
: "${OIDC_ISSUER:?Set the externally reachable issuer, without a trailing slash}"
ALLOW_HTTP="${OIDC_ALLOW_HTTP_DEVELOPMENT:-false}"
if [[ "$ALLOW_HTTP" != 'true' && "$OIDC_ISSUER" != https://* ]]; then
  echo 'Production OIDC requires an HTTPS issuer' >&2
  exit 1
fi
if ! kubectl -n market get secret market-oidc-signing >/dev/null 2>&1; then
  umask 077
  KEY=$(mktemp)
  trap 'rm "$KEY"' EXIT
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$KEY" 2>/dev/null
  kubectl -n market create secret generic market-oidc-signing --from-file="OIDC_SIGNING_KEY_PEM=$KEY" >/dev/null
fi
# Reuse the persisted Secret. Repeated deployments never rotate live keys.
kubectl -n market set env deployment/market-api --from=secret/market-oidc-signing >/dev/null
kubectl -n market set env deployment/market-api "OIDC_ISSUER=$OIDC_ISSUER" \
  "OIDC_ALLOW_HTTP_DEVELOPMENT=$ALLOW_HTTP" >/dev/null
kubectl -n market rollout status deployment/market-api --timeout=240s
