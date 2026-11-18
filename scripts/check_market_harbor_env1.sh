#!/usr/bin/env bash
set -euo pipefail
umask 077
state=/root/market/ops/harbor-env1
registry=192.168.10.10:30443
image="$registry/market/ci-smoke:$(TZ=UTC-8 date +%Y%m%d%H%M%S)-probe"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
nerd=(nerdctl --namespace k8s.io --hosts-dir "$work/hosts")
install -d "$work/hosts/$registry" "$work/push" "$work/pull"
install -m 644 "$state/ca.crt" "$work/hosts/$registry/ca.crt"
printf 'server = "https://%s"\n[host."https://%s"]\n  capabilities = ["pull", "resolve", "push"]\n  ca = "ca.crt"\n' \
  "$registry" "$registry" > "$work/hosts/$registry/hosts.toml"

login() {
  local profile=$1 config=$2 name
  name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$profile")
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["secret"])' "$profile" |
    DOCKER_CONFIG="$config" "${nerd[@]}" login "$registry" --username "$name" --password-stdin
}
login "$state/forgejo-publish.json" "$work/push"
login "$state/replication-read.json" "$work/pull"
"${nerd[@]}" image inspect docker.io/library/busybox:1.36 >/dev/null
"${nerd[@]}" tag docker.io/library/busybox:1.36 "$image"
DOCKER_CONFIG="$work/push" "${nerd[@]}" push --platform linux/amd64 "$image"
"${nerd[@]}" image rm "$image"
DOCKER_CONFIG="$work/pull" "${nerd[@]}" pull --platform linux/amd64 "$image"
"${nerd[@]}" image rm "$image"
printf 'HTTPS publication and pull verified: %s\n' "$image"
