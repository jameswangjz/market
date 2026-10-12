#!/usr/bin/env bash
set -euo pipefail
umask 077
KEY_FILE=$(mktemp)
trap 'rm -f "$KEY_FILE"' EXIT
if ! kubectl -n market get secret market-oidc-credentials >/dev/null 2>&1; then
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n' > "$KEY_FILE"
  kubectl -n market create secret generic market-oidc-credentials \
    --from-file="OIDC_CREDENTIAL_ENCRYPTION_KEY=$KEY_FILE" >/dev/null
fi
for deployment in market-api online-delivery-worker; do
  kubectl -n market set env "deployment/$deployment" --from=secret/market-oidc-credentials >/dev/null
done
kubectl -n market set env deployment/online-delivery-worker --from=secret/market-oidc-signing >/dev/null
kubectl -n market set env deployment/online-delivery-worker \
  OIDC_ISSUER=http://192.168.10.10:30080 OIDC_ALLOW_HTTP_DEVELOPMENT=true >/dev/null
echo 'Persistent OIDC credential key configured; existing keys preserved.'
