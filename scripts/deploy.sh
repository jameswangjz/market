#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=market
if command -v nerdctl >/dev/null 2>&1; then
  # Kubernetes uses containerd's k8s.io namespace; building in the default
  # nerdctl namespace leaves imagePullPolicy=Never pods on the old image.
  nerdctl --namespace k8s.io build -t market-api:dev -f backend/Dockerfile backend
  nerdctl --namespace k8s.io build -t market-web:dev -f frontend/Dockerfile frontend
fi
kubectl apply -f k8s/all-in-one.yaml
kubectl apply -f k8s/apisix.yaml
kubectl -n "$NAMESPACE" create configmap market-apisix-policy-plugin --from-file=market-gateway-quota.lua=k8s/market-gateway-quota.lua --from-file=market-gateway-oauth.lua=k8s/market-gateway-oauth.lua --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/monitoring.yaml
kubectl -n "$NAMESPACE" rollout status deployment/market-postgres --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-redis --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-etcd --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-apisix --timeout=240s
kubectl -n "$NAMESPACE" rollout status deployment/market-minio --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-api --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-web --timeout=180s
kubectl -n "$NAMESPACE" get pods,svc -o wide
