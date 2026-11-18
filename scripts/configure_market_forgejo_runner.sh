#!/usr/bin/env bash
set -euo pipefail
umask 077
directory=${1:?Usage: configure_market_forgejo_runner.sh /path/to/k8s/ci}
namespace=market-ci
ca=/root/market/ops/harbor-env1/ca.crt
manifest="$directory/forgejo-runner.yaml"
test -r "$ca"
test -r "$manifest"
test -r "$directory/buildkitd.toml"

if [[ -z $(kubectl get namespace "$namespace" --ignore-not-found -o name) ]]; then
  kubectl create namespace "$namespace"
fi
kubectl -n "$namespace" create configmap market-harbor-ca --from-file="ca.crt=$ca" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$namespace" create configmap market-buildkit --from-file="$directory/buildkitd.toml" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply --dry-run=server -f "$manifest" >/dev/null

if [[ -z $(kubectl -n "$namespace" get secret market-forgejo-runner --ignore-not-found -o name) ]]; then
  if [[ ! -t 0 ]]; then
    echo 'Runner preflight passed. Run this script in a terminal after creating the repository runner in Forgejo.'
    exit 0
  fi
  read -r -p 'Repository runner UUID: ' uuid
  read -r -s -p 'Repository runner token: ' token
  printf '\n'
  if [[ ! $uuid =~ ^[a-f0-9-]{36}$ || ! $token =~ ^[a-zA-Z0-9_-]{20,256}$ ]]; then
    echo 'Invalid runner registration values' >&2
    exit 1
  fi
  printf '{"runner":{"capacity":1,"timeout":"1h","labels":["market-build:host"],"envs":{"BUILDKIT_HOST":"tcp://127.0.0.1:1234","SSL_CERT_FILE":"/tools/ca-bundle.crt"}},"cache":{"enabled":false},"server":{"connections":{"operation-platform":{"url":"http://8.145.35.173:3000/","uuid":"%s","token":"%s"}}}}\n' "$uuid" "$token" |
    kubectl -n "$namespace" create secret generic market-forgejo-runner --from-file=runner.json=/dev/stdin
  unset token
fi
kubectl apply -f "$manifest"
kubectl -n "$namespace" rollout status deployment/market-forgejo-runner --timeout=10m
