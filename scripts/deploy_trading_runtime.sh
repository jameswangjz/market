#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$ ]]
: "${IMAGE_TAG:?Pass the exact timestamp-GitSha release tag from the local pipeline}"
: "${PROVENANCE_SHA256:?Pass the manifest digest from the trusted local pipeline}"
TAG="$IMAGE_TAG"
# Fail before any cluster access or database backup, including prebuilt mode.
python3 scripts/trading_artifact_gate.py verify --commit "$GIT_SHA" --tag "$TAG" --manifest-sha256 "$PROVENANCE_SHA256"
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
  test -s frontend/dist/index.html
  nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$WEB_BASE" -t "market-web:${TAG}" -f frontend/Dockerfile.runtime frontend
else
  nerdctl --namespace k8s.io build -t "market-api:${TAG}" -f backend/Dockerfile backend
  nerdctl --namespace k8s.io build --build-arg BASE_IMAGE=nginx:1.27-alpine -t "market-web:${TAG}" -f frontend/Dockerfile.runtime frontend
fi
kubectl -n market set image deployment/market-api "api=market-api:${TAG}"
kubectl -n market rollout status deployment/market-api --timeout=240s
kubectl -n market set image deployment/message-center-worker "worker=market-api:${TAG}"
kubectl -n market rollout status deployment/message-center-worker --timeout=240s
if kubectl -n market get deployment online-delivery-worker >/dev/null 2>&1; then
  kubectl -n market set image deployment/online-delivery-worker "worker=market-api:${TAG}"
  kubectl -n market rollout status deployment/online-delivery-worker --timeout=240s
fi
if [[ -f k8s/version-change.yaml ]]; then
  sed "s/market-api:REPLACE_WITH_DEPLOYED_TAG/market-api:${TAG}/" k8s/version-change.yaml | kubectl apply -f -
  kubectl -n market rollout status deployment/version-change-worker --timeout=240s
elif kubectl -n market get deployment version-change-worker >/dev/null 2>&1; then
  kubectl -n market set image deployment/version-change-worker "worker=market-api:${TAG}"
  kubectl -n market rollout status deployment/version-change-worker --timeout=240s
fi
kubectl -n market set image deployment/market-web "web=market-web:${TAG}"
kubectl -n market rollout status deployment/market-web --timeout=240s
if [[ -f k8s/ticket-sla-monitor.yaml ]]; then
  sed "s/market-api:REPLACE_WITH_DEPLOYED_TAG/market-api:${TAG}/" k8s/ticket-sla-monitor.yaml | kubectl apply -f -
fi
if [[ -f k8s/sla-online-monitor.yaml ]]; then
  sed "s/market-api:REPLACE_WITH_DEPLOYED_TAG/market-api:${TAG}/" k8s/sla-online-monitor.yaml | kubectl apply -f -
fi
echo "Deployed: $TAG"
kubectl -n market get deployment market-api market-web message-center-worker -o wide
