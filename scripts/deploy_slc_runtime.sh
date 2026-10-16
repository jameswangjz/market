#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?tested Git SHA required}"
: "${IMAGE_TAG:?YYYYMMDDHHMMSS-GitSha required}"
: "${PROVENANCE_SHA256:?trusted manifest digest required}"
python3 scripts/trading_artifact_gate.py verify --commit "$GIT_SHA" --tag "$IMAGE_TAG" --manifest-sha256 "$PROVENANCE_SHA256"
umask 077
BACKUP="/root/market/backups/slc-${IMAGE_TAG}"
mkdir "$BACKUP"
kubectl -n market get deployment market-api -o json > "$BACKUP/api.json"
kubectl -n market get deployment market-web -o json > "$BACKUP/web.json"
kubectl -n market get cronjob sla-online-monitor -o json > "$BACKUP/sla-online-monitor.json"
kubectl -n market exec deployment/market-postgres -- pg_dump -U market -d market > "$BACKUP/database.sql"
test -s "$BACKUP/database.sql"
API_BASE=$(kubectl -n market get deployment market-api -o jsonpath='{.spec.template.spec.containers[?(@.name=="api")].image}')
WEB_BASE=$(kubectl -n market get deployment market-web -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].image}')
MONITOR_BASE=$(kubectl -n market get cronjob sla-online-monitor -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')
MONITOR_NAME=$(kubectl -n market get cronjob sla-online-monitor -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].name}')
test -n "$API_BASE"
test -n "$WEB_BASE"
test -n "$MONITOR_NAME"
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$API_BASE" -t "market-api:${IMAGE_TAG}" -f backend/Dockerfile.runtime backend
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$WEB_BASE" -t "market-web:${IMAGE_TAG}" -f frontend/Dockerfile.runtime frontend
rollback() {
  local code=$?
  trap - ERR
  kubectl -n market set image deployment/market-api "api=$API_BASE"
  kubectl -n market set image deployment/market-web "web=$WEB_BASE"
  kubectl -n market set image cronjob/sla-online-monitor "$MONITOR_NAME=$MONITOR_BASE"
  kubectl -n market rollout status deployment/market-api --timeout=240s
  kubectl -n market rollout status deployment/market-web --timeout=240s
  echo "SLA console release failed; images restored, additive tables preserved" >&2
  exit "$code"
}
trap rollback ERR
kubectl -n market set image deployment/market-api "api=market-api:${IMAGE_TAG}"
kubectl -n market rollout status deployment/market-api --timeout=240s
kubectl -n market set image deployment/market-web "web=market-web:${IMAGE_TAG}"
kubectl -n market rollout status deployment/market-web --timeout=240s
kubectl -n market set image cronjob/sla-online-monitor "$MONITOR_NAME=market-api:${IMAGE_TAG}"
curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 2 --retry-max-time 60 --max-time 10 http://192.168.10.10:30080/api/health
trap - ERR
echo "SLA console API/Web/online-monitor deployed: ${IMAGE_TAG}; financial data unchanged"
