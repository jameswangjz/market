"""Preserve live APISIX policies while enabling verified SLA telemetry sources."""
import argparse
import base64
from datetime import datetime, timezone
import json
import ipaddress
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys

import yaml


def kubectl(*args, body=None):
    return subprocess.check_output(['kubectl', '-n', 'market', *args],
        input=json.dumps(body).encode() if body is not None else None)


def get(kind, name):
    return json.loads(kubectl('get', kind, name, '-o', 'json'))


def patch(kind, name, value):
    kubectl('patch', kind, name, '--type', 'merge', '--patch-file', '/dev/stdin', body=value)


def global_rule(method, payload=None):
    code = """import httpx,json,os,sys
payload=json.load(sys.stdin)
url=os.environ['APISIX_ADMIN_URL'].rstrip('/')+'/global_rules/market-sla-terminal'
r=httpx.request(payload['method'],url,headers={'X-API-KEY':os.environ['APISIX_ADMIN_KEY']},json=payload['body'],timeout=15)
if r.status_code != 404: r.raise_for_status()
print(json.dumps(None if r.status_code == 404 else r.json()))
"""
    return json.loads(subprocess.check_output(['kubectl', '-n', 'market', 'exec', '-i',
        'deployment/market-api', '--', 'python', '-c', code],
        input=json.dumps({'method': method, 'body': payload}).encode()))


def producer_registry(retire=None):
    code = """import redis,json,os,sys
from datetime import datetime,timezone
payload=json.load(sys.stdin)
c=redis.Redis.from_url(os.getenv('REDIS_URL','redis://market-redis:6379/0'),decode_responses=True)
key='market:sla:terminal:producers'
if payload is None: print(json.dumps(c.hgetall(key)))
else:
 count=0
 for identity, original in payload['snapshot'].items():
  if c.hget(key,identity) != original: continue
  value=json.loads(original)
  if isinstance(value,int): value={'started_at_ms':value}
  if 'stopped_at_ms' in value: continue
  value.update(stopped_at_ms=int(datetime.now(timezone.utc).timestamp()*1000),retirement_evidence_id='deployment:'+payload['tag'])
  c.hset(key,identity,json.dumps(value)); count+=1
 print(json.dumps({'retired':count}))
c.close()
"""
    return json.loads(subprocess.check_output(['kubectl', '-n', 'market', 'exec', '-i',
        'deployment/market-api', '--', 'python', '-c', code], input=json.dumps(retire).encode()))


def container_env(spec, name, keys):
    containers = spec['template']['spec']['containers']
    container = next(value for value in containers if value['name'] == name)
    env = container.setdefault('env', [])
    for key in keys:
        env[:] = [item for item in env if item['name'] != key]
        env.append({'name': key, 'valueFrom': {'secretKeyRef': {
            'name': 'market-sla-telemetry', 'key': key}}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--backup-root', type=Path, default=Path('/root/market/backups'))
    parser.add_argument('--probe-source-cidrs', required=True)
    args = parser.parse_args()
    # No cluster or credential access before provenance verification.
    subprocess.run([sys.executable, 'scripts/trading_artifact_gate.py', 'verify',
        '--commit', args.commit, '--tag', args.tag, '--manifest-sha256', args.manifest_sha256], check=True)
    networks = [ipaddress.ip_network(value.strip(), strict=True)
                for value in args.probe_source_cidrs.split(',') if value.strip()]
    if not networks or any(value.prefixlen == 0 for value in networks):
        raise ValueError('Explicit restricted probe source networks required')
    os.umask(0o077)
    directory = args.backup_root / ('sla-' + args.tag + '-' + datetime.now(timezone.utc).strftime('%H%M%S%f'))
    directory.mkdir(parents=True)
    originals = {key: get(kind, name) for key, kind, name in (
        ('config', 'configmap', 'market-apisix-config'),
        ('plugins', 'configmap', 'market-apisix-policy-plugin'),
        ('apisix', 'deployment', 'market-apisix'), ('api', 'deployment', 'market-api'),
        ('runtime', 'configmap', 'market-config'))}
    for key, value in originals.items():
        (directory / (key + '.json')).write_text(json.dumps(value))
    previous_rule = global_rule('GET')
    (directory / 'global-rule.json').write_text(json.dumps(previous_rule))
    producer_snapshot = producer_registry()
    old_pods = json.loads(kubectl('get', 'pods', '-l', 'app=market-apisix', '-o', 'json'))
    (directory / 'producer-snapshot.json').write_text(json.dumps(producer_snapshot))
    secret_check = subprocess.run(['kubectl', '-n', 'market', 'get', 'secret',
        'market-sla-telemetry', '--ignore-not-found', '-o', 'json'], check=True, capture_output=True)
    secret = json.loads(secret_check.stdout) if secret_check.stdout.strip() else {
        'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'market-sla-telemetry', 'namespace': 'market'},
        'type': 'Opaque', 'data': {}}
    values = secret.setdefault('data', {})
    for key in ('SLA_GATEWAY_INGEST_SECRET', 'SLA_PROBE_SECRET'):
        if key not in values:
            values[key] = base64.b64encode(secrets.token_urlsafe(48).encode()).decode()
        if len(base64.b64decode(values[key])) < 32:
            raise ValueError('Existing telemetry secret is too short')
    # Secret material never appears in command arguments or output.
    kubectl('apply', '-f', '-', body=secret)
    configuration = yaml.safe_load(originals['config']['data']['config.yaml'])
    plugins = configuration.setdefault('plugins', [])
    if 'market-sla-logger' not in plugins:
        plugins.append('market-sla-logger')
    nginx = configuration.setdefault('nginx_config', {})
    envs = nginx.setdefault('envs', [])
    for key in ('HOSTNAME', 'SLA_GATEWAY_INGEST_SECRET', 'SLA_PROBE_SECRET'):
        if key not in envs:
            envs.append(key)
    spec = json.loads(json.dumps(originals['apisix']['spec']))
    container_env(spec, 'apisix', ('SLA_GATEWAY_INGEST_SECRET', 'SLA_PROBE_SECRET'))
    mounts = next(item for item in spec['template']['spec']['containers'] if item['name'] == 'apisix')['volumeMounts']
    name = 'market-sla-logger.lua'
    target = '/usr/local/apisix/apisix/plugins/' + name
    if not any(item['mountPath'] == target for item in mounts):
        mounts.append({'name': 'policy-plugin', 'mountPath': target, 'subPath': name})
    spec['template'].setdefault('metadata', {}).setdefault('annotations', {})['market.sla-revision'] = args.tag
    api_spec = json.loads(json.dumps(originals['api']['spec']))
    container_env(api_spec, 'api', ('SLA_GATEWAY_INGEST_SECRET', 'SLA_PROBE_SECRET'))
    api_container = next(item for item in api_spec['template']['spec']['containers'] if item['name'] == 'api')
    runtime_image = api_container['image']
    if not re.fullmatch(r'(?:[A-Za-z0-9._:/-]+/)?market-api:\d{14}-[a-f0-9]{7,40}', runtime_image):
        raise ValueError('Workers require the immutable deployed timestamp-SHA API image')
    hosts = originals['runtime']['data'].get('UPSTREAM_ALLOWED_HOSTS', '')
    explicit_hosts = next((item.get('value', '') for item in api_container.get('env', [])
                           if item['name'] == 'UPSTREAM_ALLOWED_HOSTS'), '')
    allowed_hosts = ','.join(dict.fromkeys(value.strip() for value in
        (hosts + ',' + explicit_hosts + ',market-apisix').split(',') if value.strip()))
    api_container['env'] = [item for item in api_container['env'] if item['name'] != 'UPSTREAM_ALLOWED_HOSTS']
    api_container['env'].append({'name': 'UPSTREAM_ALLOWED_HOSTS', 'value': allowed_hosts})
    rule_changed = False
    try:
        patch('configmap', 'market-config', {'data': {
            'SLA_PROBE_SOURCE_CIDRS': ','.join(str(value) for value in networks),
            'SLA_PROBE_GATEWAY_ORIGIN': 'http://market-apisix:9080',
            'UPSTREAM_ALLOWED_HOSTS': allowed_hosts}})
        patch('configmap', 'market-apisix-config', {'data': {'config.yaml': yaml.safe_dump(configuration)}})
        patch('configmap', 'market-apisix-policy-plugin', {'data': {
            name: Path('k8s/' + name).read_text(),
            'market-gateway-quota.lua': Path('k8s/market-gateway-quota.lua').read_text(),
            'market-gateway-oauth.lua': Path('k8s/market-gateway-oauth.lua').read_text()}})
        patch('deployment', 'market-api', {'spec': api_spec})
        kubectl('rollout', 'status', 'deployment/market-api', '--timeout=240s')
        patch('deployment', 'market-apisix', {'spec': spec})
        kubectl('rollout', 'status', 'deployment/market-apisix', '--timeout=300s')
        if old_pods['items']:
            kubectl('wait', '--for=delete', *('pod/' + item['metadata']['name'] for item in old_pods['items']),
                    '--timeout=120s')
        global_rule('PUT', {'plugins': {'market-sla-logger': {}}})
        rule_changed = True
        label_report = subprocess.check_output(['kubectl', '-n', 'market', 'exec', '-i',
            'deployment/market-api', '--', 'python', '-', '--apply'],
            input=Path('scripts/sla_gateway_labels.py').read_bytes())
        (directory / 'label-refresh.json').write_bytes(label_report)
        for manifest in ('k8s/ticket-sla-monitor.yaml', 'k8s/sla-online-monitor.yaml'):
            rendered = Path(manifest).read_text().replace('market-api:REPLACE_WITH_DEPLOYED_TAG', runtime_image)
            subprocess.run(['kubectl', '-n', 'market', 'apply', '-f', '-'],
                           input=rendered.encode(), check=True, capture_output=True)
        retired = producer_registry({'snapshot': producer_snapshot, 'tag': args.tag})
        (directory / 'producer-retirement.json').write_text(json.dumps(retired))
    except Exception:
        # Restore only this operation's saved configuration, never reset etcd/routes.
        if rule_changed:
            if previous_rule is None:
                global_rule('DELETE')
            else:
                saved_rule = previous_rule.get('node', previous_rule)
                saved_rule = saved_rule.get('value', saved_rule)
                global_rule('PUT', {'plugins': saved_rule['plugins']})
        for key, kind, name in (('config', 'configmap', 'market-apisix-config'),
                               ('plugins', 'configmap', 'market-apisix-policy-plugin'),
                               ('runtime', 'configmap', 'market-config')):
            restored = dict(originals[key]['data'])
            added = {'plugins': ('market-sla-logger.lua',),
                     'runtime': ('SLA_PROBE_SOURCE_CIDRS', 'SLA_PROBE_GATEWAY_ORIGIN',
                                 'UPSTREAM_ALLOWED_HOSTS')}.get(key, ())
            restored.update({field: None for field in added if field not in restored})
            patch(kind, name, {'data': restored})
        patch('deployment', 'market-apisix', {'spec': originals['apisix']['spec']})
        patch('deployment', 'market-api', {'spec': originals['api']['spec']})
        raise
    print(json.dumps({'status': 'configured', 'tag': args.tag, 'backup': str(directory),
                      'runtime_image': runtime_image, 'routes_rewritten': False, 'business_credentials_changed': False}))


if __name__ == '__main__':
    main()
