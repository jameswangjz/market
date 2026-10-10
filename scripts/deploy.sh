#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=market
GIT_SHA="${GIT_SHA:-$(git rev-parse --short=12 HEAD 2>/dev/null || true)}"
if [[ ! "$GIT_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
  echo "GIT_SHA is required when deploying from a directory without Git metadata" >&2
  exit 1
fi
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)-${GIT_SHA}}"
echo "Deploying image tag: ${IMAGE_TAG}"

if command -v nerdctl >/dev/null 2>&1; then
  # Kubernetes uses containerd's k8s.io namespace, and every local image gets
  # the immutable timestamp/SHA tag used by this deployment.
  nerdctl --namespace k8s.io build -t "market-api:${IMAGE_TAG}" -f backend/Dockerfile backend
  nerdctl --namespace k8s.io build -t "market-web:${IMAGE_TAG}" -f frontend/Dockerfile frontend
  nerdctl --namespace k8s.io build -t "market-gateway:${IMAGE_TAG}" -f gateway/Dockerfile gateway
  nerdctl --namespace k8s.io build -t "market-mock-saas:${IMAGE_TAG}" -f mock-saas/Dockerfile mock-saas
  nerdctl --namespace k8s.io build -t "market-mock-api:${IMAGE_TAG}" -f mock-api/Dockerfile mock-api
else
  echo "nerdctl is required to build the tagged local images" >&2
  exit 1
fi

RENDERED_MANIFEST="$(mktemp)"
trap 'rm -f "$RENDERED_MANIFEST"' EXIT
sed \
  -e "s#market-api:dev#market-api:${IMAGE_TAG}#g" \
  -e "s#market-web:dev#market-web:${IMAGE_TAG}#g" \
  -e "s#market-gateway:dev#market-gateway:${IMAGE_TAG}#g" \
  -e "s#market-mock-saas:dev#market-mock-saas:${IMAGE_TAG}#g" \
  -e "s#market-mock-api:dev#market-mock-api:${IMAGE_TAG}#g" \
  k8s/all-in-one.yaml > "$RENDERED_MANIFEST"

kubectl apply -f "$RENDERED_MANIFEST"
kubectl apply -f k8s/apisix.yaml
kubectl -n "$NAMESPACE" create configmap market-apisix-policy-plugin --from-file=market-gateway-quota.lua=k8s/market-gateway-quota.lua --from-file=market-gateway-oauth.lua=k8s/market-gateway-oauth.lua --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/monitoring.yaml
kubectl -n "$NAMESPACE" rollout status deployment/market-postgres --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-redis --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-clamav --timeout=300s
kubectl -n "$NAMESPACE" rollout status deployment/market-etcd --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-apisix --timeout=240s
kubectl -n "$NAMESPACE" rollout status deployment/market-minio --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-api --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-web --timeout=180s
if [[ -f k8s/message-center.yaml ]]; then
  sed "s#market-api:REPLACE_WITH_DEPLOYED_TAG#market-api:${IMAGE_TAG}#g" k8s/message-center.yaml > "$RENDERED_MANIFEST"
  kubectl apply -f "$RENDERED_MANIFEST"
  kubectl -n "$NAMESPACE" rollout status deployment/market-mailpit --timeout=240s
  kubectl -n "$NAMESPACE" rollout status deployment/message-center-worker --timeout=180s
fi
kubectl -n "$NAMESPACE" get pods,svc -o wide
