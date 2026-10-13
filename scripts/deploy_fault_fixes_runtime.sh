#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
bash scripts/deploy_oauth_revocation_runtime.sh
umask 077
BACKUP="/root/market/backups/pre-oauth-plugin-${GIT_SHA}.json"
kubectl -n market get configmap market-apisix-policy-plugin -o json > "$BACKUP"
PATCH=$(python3 -c 'import json,pathlib; print(json.dumps({"data":{"market-gateway-oauth.lua":pathlib.Path("k8s/market-gateway-oauth.lua").read_text()}}))')
kubectl -n market patch configmap market-apisix-policy-plugin --type merge -p "$PATCH"
REVISION=$(python3 -c 'import json,os; print(json.dumps({"spec":{"template":{"metadata":{"annotations":{"market.oauth-recovery-revision":os.environ["GIT_SHA"]}}}}}))')
kubectl -n market patch deployment market-apisix --type merge -p "$REVISION"
if ! kubectl -n market rollout status deployment/market-apisix --timeout=240s; then
  OLD=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); print(json.dumps({"data":{"market-gateway-oauth.lua":value["data"]["market-gateway-oauth.lua"]}}))' "$BACKUP")
  kubectl -n market patch configmap market-apisix-policy-plugin --type merge -p "$OLD"
  kubectl -n market rollout undo deployment/market-apisix
  kubectl -n market rollout status deployment/market-apisix --timeout=240s
  exit 1
fi
kubectl -n market get deployment market-api market-apisix message-center-worker online-delivery-worker version-change-worker
