#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ $# != 5 ]]; then
  printf 'Usage: %s bundle-dir registry project robot.json ca.crt\n' "$0" >&2
  exit 2
fi
bundle=$1 registry=$2 project=$3 robot=$4 ca=$5
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
flux=${MARKET_FLUX:-flux}

python3 "$directory/verify_market_release.py" --bundle "$bundle" --registry "$registry" \
  --project "$project" --auth-file "$robot" --ca "$ca"
readarray -t fields < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["version"]); print(d["commit"]); print(d["git_source"])' "$bundle/release.json")
[[ ${#fields[@]} == 3 ]]
version=${fields[0]} commit=${fields[1]} source=${fields[2]}
reference="oci://$registry/$project/market-release:$version"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export DOCKER_CONFIG="$work/docker"
export SSL_CERT_FILE="$ca"
mkdir "$DOCKER_CONFIG" "$work/pulled"
python3 - "$registry" "$robot" > "$DOCKER_CONFIG/config.json" <<'PY'
import base64
import json
import sys

registry, robot_file = sys.argv[1:]
with open(robot_file, encoding='utf-8') as file:
    robot = json.load(file)
value = base64.b64encode(f"{robot['name']}:{robot['secret']}".encode()).decode()
print(json.dumps({'auths': {registry: {'auth': value}}}))
PY

"$flux" push artifact "$reference" --path "$bundle" --source "$source" \
  --revision "main@sha1:$commit" --reproducible --output json > "$work/artifact.json"
digest=$(python3 -c 'import json,re,sys; value=json.load(open(sys.argv[1]))["digest"]; assert re.fullmatch(r"sha256:[0-9a-f]{64}",value); print(value)' "$work/artifact.json")
pinned="oci://$registry/$project/market-release@$digest"
"$flux" pull artifact "$pinned" --output "$work/pulled"
diff -qr -- "$bundle" "$work/pulled"
cat "$work/artifact.json"
printf 'OCI release payload re-pulled and verified: %s\n' "$pinned"
