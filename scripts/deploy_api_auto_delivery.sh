#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the reviewed application commit}"
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$ ]]
ROOT="${SOURCE_ROOT:-/root/market}"
TAG="$(date -u +%Y%m%d%H%M%S)-${GIT_SHA}"
API_BASE=$(kubectl -n market get deployment market-api -o jsonpath='{.spec.template.spec.containers[0].image}')
WORKER_BASE=$(kubectl -n market get deployment online-delivery-worker -o jsonpath='{.spec.template.spec.containers[0].image}')
kubectl -n market exec deployment/market-api -- python -c '
from app import main as m
from sqlalchemy import select, func
with m.SessionLocal() as db:
    pending = db.scalar(select(func.count()).select_from(m.Order).join(m.DeliveryTask, m.DeliveryTask.order_id == m.Order.id).where(m.Order.snapshot_version == 1, m.Order.delivery_method_snapshot.in_(["api", "model_api"]), m.DeliveryTask.status.in_(["in_delivery", "retrying"])))
    if pending:
        raise RuntimeError("Review pending API/model deliveries before enabling worker")
print("Preflight: no pending API/model orders")
'
mkdir -p /root/market/backups
umask 077
BACKUP="/root/market/backups/pre-api-auto-${TAG}.sql"
kubectl -n market exec deployment/market-postgres -- pg_dump -U market -d market > "$BACKUP"
test -s "$BACKUP"
echo "Backup: $BACKUP"
echo "Previous API: $API_BASE; previous worker: $WORKER_BASE"
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$API_BASE" \
    -t "market-api:${TAG}" -f "$ROOT/backend/Dockerfile.runtime" "$ROOT/backend"
restore_images() {
    kubectl -n market set image deployment/market-api "api=$API_BASE"
    kubectl -n market set image deployment/online-delivery-worker "worker=$WORKER_BASE"
    kubectl -n market rollout status deployment/market-api --timeout=240s
    kubectl -n market rollout status deployment/online-delivery-worker --timeout=240s
}
trap 'status=$?; if [[ $status -ne 0 ]]; then restore_images; fi' EXIT
kubectl -n market set image deployment/market-api "api=market-api:${TAG}"
kubectl -n market rollout status deployment/market-api --timeout=240s
kubectl -n market set env --from=secret/market-secrets --keys=APISIX_ADMIN_KEY deployment/online-delivery-worker
kubectl -n market set image deployment/online-delivery-worker "worker=market-api:${TAG}"
kubectl -n market rollout status deployment/online-delivery-worker --timeout=240s
echo "Deployed: $TAG"
echo 'Only API and online-delivery-worker changed; frontend/message workers unchanged.'
