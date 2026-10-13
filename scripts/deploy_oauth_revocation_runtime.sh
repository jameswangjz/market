#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$ ]]
TAG="$(date -u +%Y%m%d%H%M%S)-${GIT_SHA}"
umask 077
mkdir -p /root/market/backups
BACKUP="/root/market/backups/pre-oauth-revocation-${TAG}.sql"
kubectl -n market exec deployment/market-postgres -- pg_dump -U market -d market > "$BACKUP"
test -s "$BACKUP"
SECRET_BEFORE=$(kubectl -n market get secret market-api-credentials -o jsonpath='{.data}' | sha256sum)
API_BASE=$(kubectl -n market get deployment market-api -o jsonpath='{.spec.template.spec.containers[0].image}')
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$API_BASE" -t "market-api:${TAG}" -f backend/Dockerfile.runtime backend
kubectl -n market set image deployment/market-api "api=market-api:${TAG}"
kubectl -n market rollout status deployment/market-api --timeout=240s
for WORKER in message-center-worker online-delivery-worker version-change-worker; do
  kubectl -n market set image "deployment/${WORKER}" "worker=market-api:${TAG}"
  kubectl -n market rollout status "deployment/${WORKER}" --timeout=240s
done
SECRET_AFTER=$(kubectl -n market get secret market-api-credentials -o jsonpath='{.data}' | sha256sum)
test "$SECRET_BEFORE" = "$SECRET_AFTER"
echo "Deployed: $TAG; persistent credential Secret unchanged; backup: $BACKUP"
