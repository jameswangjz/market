#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$ ]]
TAG="$(date -u +%Y%m%d%H%M%S)-${GIT_SHA}"
mkdir -p /root/market/backups
BACKUP="/root/market/backups/pre-trd-runtime-${TAG}.sql"
umask 077
kubectl -n market exec deployment/market-postgres -- pg_dump -U market -d market > "$BACKUP"
test -s "$BACKUP"
echo "Backup: $BACKUP"
if [[ "${REUSE_RUNTIME:-false}" == "true" ]]; then
  API_BASE=$(kubectl -n market get deployment market-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  WEB_BASE=$(kubectl -n market get deployment market-web -o jsonpath='{.spec.template.spec.containers[0].image}')
  nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$API_BASE" -t "market-api:${TAG}" -f backend/Dockerfile.runtime backend
  if [[ "${PREBUILT_WEB:-false}" != "true" ]]; then
    (cd frontend && npm run build)
  fi
  test -s frontend/dist/index.html
  nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$WEB_BASE" -t "market-web:${TAG}" -f frontend/Dockerfile.runtime frontend
else
  nerdctl --namespace k8s.io build -t "market-api:${TAG}" -f backend/Dockerfile backend
  nerdctl --namespace k8s.io build -t "market-web:${TAG}" -f frontend/Dockerfile frontend
fi
kubectl -n market patch configmap market-config --type merge \
  -p '{"data":{"UPSTREAM_ALLOWED_HOSTS":"market-mock-api,market-mock-saas","UPSTREAM_ALLOWED_CIDRS":""}}'
kubectl -n market set image deployment/market-api "api=market-api:${TAG}"
kubectl -n market rollout status deployment/market-api --timeout=240s
kubectl -n market set image deployment/message-center-worker "worker=market-api:${TAG}"
kubectl -n market rollout status deployment/message-center-worker --timeout=240s
kubectl -n market set image deployment/market-web "web=market-web:${TAG}"
kubectl -n market rollout status deployment/market-web --timeout=240s
echo "Deployed: $TAG"
kubectl -n market get deployment market-api market-web message-center-worker -o wide
