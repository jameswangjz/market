#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
REUSE_RUNTIME=true PREBUILT_WEB=true bash scripts/deploy_trading_runtime.sh
PATCH=$(python3 -c 'import json,pathlib; print(json.dumps({"data":{"market-gateway-quota.lua":pathlib.Path("k8s/market-gateway-quota.lua").read_text()}}))')
kubectl -n market patch configmap market-apisix-policy-plugin --type merge -p "$PATCH"
REVISION=$(python3 -c 'import json,os; print(json.dumps({"spec":{"template":{"metadata":{"annotations":{"market.quota-revision":os.environ["GIT_SHA"]}}}}}))')
kubectl -n market patch deployment market-apisix --type merge -p "$REVISION"
kubectl -n market rollout status deployment/market-apisix --timeout=240s
kubectl -n market get deployment market-api market-web message-center-worker market-apisix -o wide
