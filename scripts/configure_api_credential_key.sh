#!/usr/bin/env bash
set -euo pipefail
umask 077

KEY_FILE=$(mktemp)
trap 'rm -f "$KEY_FILE"' EXIT

if ! kubectl -n market get secret market-api-credentials >/dev/null 2>&1; then
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n' > "$KEY_FILE"
  kubectl -n market create secret generic market-api-credentials \
    --from-file="API_CREDENTIAL_ENCRYPTION_KEY=$KEY_FILE" >/dev/null
fi

if ! kubectl -n market get secret market-api-credentials \
    -o 'go-template={{index .data "API_CREDENTIAL_ENCRYPTION_KEY"}}' | grep -q .; then
  echo 'market-api-credentials exists without API_CREDENTIAL_ENCRYPTION_KEY; existing secret was preserved.' >&2
  exit 1
fi

for deployment in market-api online-delivery-worker; do
  kubectl -n market set env "deployment/$deployment" \
    --from=secret/market-api-credentials >/dev/null
done

echo 'Persistent API credential encryption key configured; existing keys preserved.'
