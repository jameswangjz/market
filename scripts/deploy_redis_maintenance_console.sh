#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${GIT_SHA:?tested commit required}"
: "${IMAGE_TAG:?YYYYMMDDHHMMSS-GitSha required}"
: "${PROVENANCE_SHA256:?trusted manifest digest required}"
python3 scripts/trading_artifact_gate.py verify --commit "$GIT_SHA" --tag "$IMAGE_TAG" --manifest-sha256 "$PROVENANCE_SHA256"
backup=$(mktemp -d "/root/market/backups/redis-maintenance-console-$IMAGE_TAG.XXXXXXXX")
if kubectl -n market get deployment redis-maintenance-worker >/dev/null 2>&1; then
  echo 'Existing maintenance executor requires a reviewed upgrade, not this first-install script' >&2
  exit 1
fi
for deployment in market-api market-web api-charge-ledger-worker; do
  kubectl -n market get deployment "$deployment" -o json > "$backup/$deployment.json"
done
kubectl -n market exec deployment/market-postgres -- pg_dump -U market -d market > "$backup/database-before.sql"
test -s "$backup/database-before.sql"
api_base=$(kubectl -n market get deployment market-api -o jsonpath='{.spec.template.spec.containers[0].image}')
web_base=$(kubectl -n market get deployment market-web -o jsonpath='{.spec.template.spec.containers[0].image}')
charge_base=$(kubectl -n market get deployment api-charge-ledger-worker -o jsonpath='{.spec.template.spec.containers[0].image}')
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$api_base" -t "market-api:$IMAGE_TAG" -f backend/Dockerfile.runtime backend > "$backup/api-build.log" 2>&1
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=$web_base" -t "market-web:$IMAGE_TAG" -f frontend/Dockerfile.runtime frontend > "$backup/web-build.log" 2>&1
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=market-api:$IMAGE_TAG" -t "market-redis-maintenance:$IMAGE_TAG" -f k8s/Dockerfile.redis-maintenance . > "$backup/ops-build.log" 2>&1
kubectl create --dry-run=client -f k8s/redis-maintenance.yaml -o json > "$backup/ops-template.json"
python3 - "$backup/ops-template.json" "$IMAGE_TAG" > "$backup/ops-install.json" <<'PY'
import json,sys
remaining=open(sys.argv[1]).read().strip()
decoder=json.JSONDecoder()
items=[]
while remaining:
    value,end=decoder.raw_decode(remaining)
    items.extend(value['items'] if value.get('kind')=='List' else [value])
    remaining=remaining[end:].lstrip()
deployment=next(item for item in items if item['kind']=='Deployment')
deployment['spec']['template']['spec']['containers'][0]['image']='market-redis-maintenance:'+sys.argv[2]
print(json.dumps({'apiVersion':'v1','kind':'List','items':items}))
PY
created_ops=false
rollback() {
  trap - ERR
  if test "$created_ops" = true; then
    kubectl -n market delete deployment redis-maintenance-worker --ignore-not-found --wait=true --timeout=330s
  fi
  kubectl -n market set image deployment/market-api "api=$api_base"
  kubectl -n market set image deployment/market-web "web=$web_base"
  kubectl -n market set image deployment/api-charge-ledger-worker "worker=$charge_base"
  echo 'Previous application images restored; additive tables and evidence PVC retained' >&2
  exit 1
}
trap rollback ERR
kubectl -n market set image deployment/market-api "api=market-api:$IMAGE_TAG"
kubectl -n market rollout status deployment/market-api --timeout=240s
kubectl -n market set image deployment/api-charge-ledger-worker "worker=market-api:$IMAGE_TAG"
kubectl -n market rollout status deployment/api-charge-ledger-worker --timeout=180s
created_ops=true
kubectl apply -f "$backup/ops-install.json"
kubectl -n market rollout status deployment/redis-maintenance-worker --timeout=240s
kubectl -n market set image deployment/market-web "web=market-web:$IMAGE_TAG"
kubectl -n market rollout status deployment/market-web --timeout=180s
curl --fail --silent --show-error --retry 8 --retry-all-errors --retry-delay 2 --max-time 15 http://192.168.10.10:30080/api/health
curl --fail --silent --show-error --retry 8 --retry-all-errors --retry-delay 2 --max-time 15 http://192.168.10.10:30080/market-build.json > "$backup/served-build.json"
cmp "$backup/served-build.json" frontend/dist/market-build.json
trap - ERR
printf '\nMaintenance console deployed: %s; Redis was not restarted or sealed\n' "$IMAGE_TAG"
