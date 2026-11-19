#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${HARBOR_REGISTRY:?Set the Forgejo repository variable HARBOR_REGISTRY}"
: "${HARBOR_PROJECT:?Set the Forgejo repository variable HARBOR_PROJECT}"
: "${HARBOR_USERNAME:?Set the Forgejo repository secret HARBOR_USERNAME}"
: "${HARBOR_PASSWORD:?Set the Forgejo repository secret HARBOR_PASSWORD}"
[[ $HARBOR_REGISTRY =~ ^[a-zA-Z0-9.-]+(:[0-9]+)?$ ]]
[[ $HARBOR_PROJECT =~ ^[a-z0-9]+([_-][a-z0-9]+)*$ ]]

source_sha=$(git rev-parse HEAD)
tag="$(TZ=UTC-8 date +%Y%m%d%H%M%S)-${source_sha:0:7}"
base="$HARBOR_REGISTRY/$HARBOR_PROJECT"
buildctl=${MARKET_BUILDCTL:-/tools/buildctl}
if [[ ${MARKET_PUBLISH_OCI:-0} == 1 ]]; then
  test -r "${SSL_CERT_FILE:?Set SSL_CERT_FILE to the Harbor CA bundle}"
fi
if [[ -n ${MARKET_IMAGE_METADATA_DIR:-} && -e $MARKET_IMAGE_METADATA_DIR ]]; then
  printf 'Metadata output directory already exists.\n' >&2
  exit 1
fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export DOCKER_CONFIG="$work/docker"
mkdir "$DOCKER_CONFIG"
auth=$(printf '%s:%s' "$HARBOR_USERNAME" "$HARBOR_PASSWORD" | base64 | tr -d '\n')
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$HARBOR_REGISTRY" "$auth" > "$DOCKER_CONFIG/config.json"
unset auth HARBOR_PASSWORD HARBOR_USERNAME

build() {
  local name=$1 context=$2 dockerfile_dir=$3 dockerfile=$4
  shift 4
  "$buildctl" build --frontend dockerfile.v0 --local "context=$context" \
    --local "dockerfile=$dockerfile_dir" --opt "filename=$dockerfile" \
    --output "type=image,name=$base/$name:$tag,push=true" \
    --metadata-file "$work/$name.json" "$@"
}

publish_oci() {
  local context="$work/oci"
  mkdir -- "$context"
  git archive "$source_sha" k8s | tar -x -C "$context"
  cp -- "$work"/market-*.json "$context/"
  cp -- scripts/market_release_bundle.py scripts/verify_market_release.py \
    scripts/publish_market_release_bundle.sh "$context/"
  "$buildctl" build --frontend dockerfile.v0 --local "context=$context" \
    --local "dockerfile=$context/k8s/ci" --opt filename=Dockerfile.release \
    --opt "build-arg:MARKET_RELEASE_COMMIT=$source_sha" \
    --opt "build-arg:MARKET_RELEASE_VERSION=$tag" \
    --opt "build-arg:HARBOR_REGISTRY=$HARBOR_REGISTRY" \
    --opt "build-arg:HARBOR_PROJECT=$HARBOR_PROJECT" \
    --secret "id=harbor-auth,src=$DOCKER_CONFIG/config.json" \
    --secret "id=harbor-ca,src=$SSL_CERT_FILE" \
    --output "type=local,dest=$work/release-result"
  cat -- "$work/release-result/oci-release.log"
}

build market-api backend backend Dockerfile
build market-web frontend frontend Dockerfile
build market-gateway gateway gateway Dockerfile
build market-mock-api mock-api mock-api Dockerfile
build market-mock-saas mock-saas mock-saas Dockerfile
build market-redis-maintenance . k8s Dockerfile.redis-maintenance \
  --opt "build-arg:BASE_IMAGE=$base/market-api:$tag"

if [[ -n ${MARKET_IMAGE_METADATA_DIR:-} ]]; then
  mkdir -- "$MARKET_IMAGE_METADATA_DIR"
  cp -- "$work"/market-*.json "$MARKET_IMAGE_METADATA_DIR/"
fi

if [[ ${MARKET_PUBLISH_OCI:-0} == 1 ]]; then
  publish_oci
fi

printf 'Published Market release %s (commit %s)\n' "$tag" "$source_sha"
for name in market-api market-web market-gateway market-mock-api market-mock-saas market-redis-maintenance; do
  printf '%s/%s:%s\n' "$base" "$name" "$tag"
  cat "$work/$name.json"
  printf '\n'
done
