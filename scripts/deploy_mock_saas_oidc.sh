#!/usr/bin/env bash
set -euo pipefail
: "${RUNTIME_TAG:?Use the deployed platform YYYYMMDDHHMMSS-GitSha tag}"
[[ "$RUNTIME_TAG" =~ ^[0-9]{14}-[0-9a-f]{7,40}$ ]]
umask 077
KEY_FILE=$(mktemp)
MANIFEST_FILE=$(mktemp)
trap 'rm -f "$KEY_FILE" "$MANIFEST_FILE"' EXIT
sed -E "s#market-mock-saas:(REPLACE_WITH_DEPLOYED_TAG|oidc-development)([[:space:]]*)\$#market-mock-saas:${RUNTIME_TAG}\\2#" \
  k8s/mock-saas-oidc.yaml > "$MANIFEST_FILE"
if grep -Eq 'REPLACE_WITH_|oidc-development' "$MANIFEST_FILE" || \
   [[ $(grep -Ec "^[[:space:]]*image: market-mock-saas:${RUNTIME_TAG}[[:space:]]*$" "$MANIFEST_FILE") -ne 1 ]]; then
  echo 'Mock SaaS manifest image replacement failed; refusing deployment' >&2
  exit 1
fi
if kubectl -n market get secret market-mock-saas-admin >/dev/null 2>&1; then
  kubectl -n market get secret market-mock-saas-admin -o jsonpath='{.data.secret}' | base64 -d | tr -d '\r\n' > "$KEY_FILE"
else
  openssl rand -hex 32 | tr -d '\r\n' > "$KEY_FILE"
fi
[[ "$(wc -c < "$KEY_FILE")" -eq 64 ]]
kubectl -n market create secret generic market-mock-saas-admin --from-file="secret=$KEY_FILE" --dry-run=client -o yaml | kubectl apply -f -
nerdctl --namespace k8s.io build -t "market-mock-saas:${RUNTIME_TAG}" mock-saas
kubectl apply -f "$MANIFEST_FILE"
kubectl -n market rollout status deployment/market-mock-saas --timeout=240s
echo "Mock SaaS deployed: ${RUNTIME_TAG}; browser NodePort=30083"
