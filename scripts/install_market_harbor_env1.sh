#!/usr/bin/env bash
set -euo pipefail
umask 077
values=${1:?Usage: install_market_harbor_env1.sh harbor-env1-values.yaml}
chart=${2:-}
namespace=market-registry
state=/root/market/ops/harbor-env1
ip=192.168.10.10

kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' | grep -Fxq "$ip"
kubectl get storageclass local-path >/dev/null
helm_args=(harbor --repo https://helm.goharbor.io --version 1.19.4)
if [[ -n $chart ]]; then
  helm_args=("$chart")
fi
install -d -m 700 "$state"
if [[ ! -f $state/admin-password ]]; then
  openssl rand -hex 24 | tr -d '\n' > "$state/admin-password"
fi
if [[ ! -f $state/tls.crt ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -sha256 \
    -subj '/CN=Market Environment 1 Harbor CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -keyout "$state/ca.key" -out "$state/ca.crt"
  openssl req -new -newkey rsa:3072 -nodes -subj "/CN=$ip" \
    -keyout "$state/tls.key" -out "$state/tls.csr"
  openssl x509 -req -in "$state/tls.csr" -CA "$state/ca.crt" -CAkey "$state/ca.key" \
    -CAcreateserial -days 825 -sha256 -out "$state/tls.crt" \
    -extfile <(printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\n' "$ip")
fi
openssl verify -CAfile "$state/ca.crt" -verify_ip "$ip" "$state/tls.crt"

if [[ -z $(kubectl get namespace "$namespace" --ignore-not-found -o name) ]]; then
  kubectl create namespace "$namespace"
fi
if [[ -z $(kubectl -n "$namespace" get secret harbor-env1-tls --ignore-not-found -o name) ]]; then
  kubectl -n "$namespace" create secret tls harbor-env1-tls \
    --cert="$state/tls.crt" --key="$state/tls.key"
fi
if [[ -z $(kubectl -n "$namespace" get secret harbor-env1-admin --ignore-not-found -o name) ]]; then
  kubectl -n "$namespace" create secret generic harbor-env1-admin \
    --from-file=HARBOR_ADMIN_PASSWORD="$state/admin-password"
fi

helm upgrade --install harbor "${helm_args[@]}" --namespace "$namespace" \
  -f "$values" --wait --timeout 10m
curl --fail --silent --show-error --retry 6 --retry-all-errors --max-time 20 \
  --cacert "$state/ca.crt" "https://$ip:30443/api/v2.0/health" |
  python3 -c 'import json,sys; health=json.load(sys.stdin); print(json.dumps(health)); sys.exit(health.get("status") != "healthy")'
