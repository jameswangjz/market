#!/usr/bin/env bash
set -euo pipefail
: "${GIT_SHA:?Pass the tested Git SHA}"
[[ "$GIT_SHA" =~ ^[0-9a-f]{7,40}$ ]]
: "${IMAGE_TAG:?Pass the exact timestamp-GitSha tag from the trusted build}"
: "${PROVENANCE_SHA256:?Pass the trusted release manifest digest}"
TAG="$IMAGE_TAG"
python3 scripts/trading_artifact_gate.py verify --commit "$GIT_SHA" --tag "$TAG" --manifest-sha256 "$PROVENANCE_SHA256"
BASE=$(kubectl -n market get deployment market-web -o jsonpath='{.spec.template.spec.containers[0].image}')
mkdir -p /root/market/backups
umask 077
kubectl -n market get deployment market-web -o json > "/root/market/backups/pre-integration-web-${TAG}.json"
test -s frontend/dist/index.html
nerdctl --namespace k8s.io build --build-arg "BASE_IMAGE=${BASE}" -t "market-web:${TAG}" -f frontend/Dockerfile.runtime frontend
kubectl -n market set image deployment/market-web "web=market-web:${TAG}"
if ! kubectl -n market rollout status deployment/market-web --timeout=240s; then
  kubectl -n market set image deployment/market-web "web=${BASE}"
  kubectl -n market rollout status deployment/market-web --timeout=240s
  exit 1
fi
printf 'Deployed frontend: %s\n' "$TAG"
