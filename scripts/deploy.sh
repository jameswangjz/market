#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=market
kubectl apply -f k8s/all-in-one.yaml
kubectl apply -f k8s/apisix.yaml
kubectl apply -f k8s/monitoring.yaml
kubectl -n "$NAMESPACE" rollout status deployment/market-postgres --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-redis --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-etcd --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-apisix --timeout=240s
kubectl -n "$NAMESPACE" rollout status deployment/market-minio --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-api --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/market-web --timeout=180s
kubectl -n "$NAMESPACE" get pods,svc -o wide
