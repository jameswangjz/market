#!/usr/bin/env bash
set -euo pipefail
: "${RUNTIME_TAG:?Use the deployed platform YYYYMMDDHHMMSS-GitSha tag}"
[[ "$RUNTIME_TAG" =~ ^[0-9]{14}-[0-9a-f]{7,40}$ ]]
if ! kubectl -n market get secret market-mock-saas-admin >/dev/null 2>&1; then
  umask 077
  KEY_FILE=$(mktemp)
  trap 'rm -f "$KEY_FILE"' EXIT
  openssl rand -hex 32 > "$KEY_FILE"
  kubectl -n market create secret generic market-mock-saas-admin --from-file="secret=$KEY_FILE"
fi
nerdctl --namespace k8s.io build -t "market-mock-saas:${RUNTIME_TAG}" mock-saas
sed "s/market-mock-saas:oidc-development/market-mock-saas:${RUNTIME_TAG}/" k8s/mock-saas-oidc.yaml | kubectl apply -f -
kubectl -n market rollout status deployment/market-mock-saas --timeout=240s
echo "Mock SaaS deployed: ${RUNTIME_TAG}; browser NodePort=30083"
