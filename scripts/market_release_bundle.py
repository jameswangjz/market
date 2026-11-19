#!/usr/bin/env python3
"""Build a Flux OCI payload from a complete, verified Market image build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

IMAGES = (
    'market-api', 'market-web', 'market-gateway', 'market-mock-api',
    'market-mock-saas', 'market-redis-maintenance',
)
COMMIT = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
VERSION = re.compile(r'\d{14}-[a-f0-9]{7}')
SCHEMA = 'market-release/v1'
SOURCE = 'http://8.145.35.173:3000/hxyl/OperationPlatform.git'

# Environments own infrastructure, service exposure, secrets and PVC definitions.
STAGES = {
    'api': [('all-in-one.yaml', 'Deployment', 'market-api')],
    'workers': [
        ('api-charge-ledger.yaml', 'Deployment', 'api-charge-ledger-worker'),
        ('online-delivery.yaml', 'Deployment', 'online-delivery-worker'),
        ('version-change.yaml', 'Deployment', 'version-change-worker'),
        ('refund-execution.yaml', 'Deployment', 'refund-execution-worker'),
        ('message-center.yaml', 'ConfigMap', 'message-center-scripts'),
        ('message-center.yaml', 'Deployment', 'message-center-worker'),
        ('redis-maintenance.yaml', 'Deployment', 'redis-maintenance-worker'),
    ],
    'web': [('all-in-one.yaml', 'Deployment', 'market-web'),
            ('all-in-one.yaml', 'Deployment', 'market-gateway')],
    'jobs': [
        ('api-provider-expiry-cronjob.yaml', 'CronJob', 'api-provider-expiry'),
        ('application-health-cronjob.yaml', 'CronJob', 'application-health'),
        ('message-center.yaml', 'CronJob', 'message-center-cleanup'),
        ('sla-calendar-sync.yaml', 'CronJob', 'sla-calendar-sync'),
        ('sla-online-monitor.yaml', 'CronJob', 'sla-online-monitor'),
        ('ticket-sla-monitor.yaml', 'CronJob', 'ticket-sla-monitor'),
    ],
    'dev-mocks': [
        ('all-in-one.yaml', 'Deployment', 'market-mock-api'),
        ('all-in-one.yaml', 'Deployment', 'market-mock-saas'),
    ],
}
DEPENDENCIES = {'api': [], 'workers': ['api'], 'web': ['api'],
                'jobs': ['api', 'workers'], 'dev-mocks': ['api']}


def stage_index():
    return {stage: {'path': f'./apps/{stage}', 'depends_on': DEPENDENCIES[stage],
                    'optional': stage == 'dev-mocks'} for stage in STAGES}


def repository(registry, project):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?', registry):
        raise ValueError('Registry must be a hostname or IPv4 address with an optional port')
    if ':' in registry and not 0 < int(registry.rsplit(':', 1)[1]) <= 65535:
        raise ValueError('Invalid registry port')
    if not re.fullmatch(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', project):
        raise ValueError('Invalid Harbor project')
    return f'{registry}/{project}'


def validate_identity(commit, version):
    if (not COMMIT.fullmatch(commit) or not VERSION.fullmatch(version)
            or version.rsplit('-', 1)[1] != commit[:7]):
        raise ValueError('Release tag must be YYYYMMDDHHMMSS-GitSha and match the full commit')


def image_digests(directory, source, version):
    result = {}
    for name in IMAGES:
        path = directory / f'{name}.json'
        if not path.is_file():
            raise ValueError(f'Missing BuildKit metadata for {name}')
        metadata = json.loads(path.read_text(encoding='utf-8'))
        digest = metadata.get('containerimage.digest', '')
        expected = f'{source}/{name}:{version}'
        if not DIGEST.fullmatch(digest) or metadata.get('image.name') != expected:
            raise ValueError(f'BuildKit metadata does not identify the expected image and tag for {name}')
        descriptor = metadata.get('containerimage.descriptor')
        if descriptor and descriptor.get('digest') != digest:
            raise ValueError(f'BuildKit descriptor digest mismatch for {name}')
        result[name] = digest
    return result


def pod_spec(obj):
    spec = obj['spec']
    if obj['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']['spec']


def containers(spec):
    return spec.get('initContainers', []) + spec.get('containers', [])


def environment_refs(spec):
    references = set()
    for container in containers(spec):
        for entry in [*container.get('envFrom', []), *(item.get('valueFrom', {}) for item in container.get('env', []))]:
            for key, kind in [('secretRef', 'Secret'), ('secretKeyRef', 'Secret'),
                              ('configMapRef', 'ConfigMap'), ('configMapKeyRef', 'ConfigMap')]:
                value = entry.get(key)
                if value and not value.get('optional'):
                    references.add((kind, value['name']))
    for volume in spec.get('volumes', []):
        if volume.get('persistentVolumeClaim'):
            references.add(('PersistentVolumeClaim', volume['persistentVolumeClaim']['claimName']))
        for key, kind, field in [('secret', 'Secret', 'secretName'), ('configMap', 'ConfigMap', 'name')]:
            value = volume.get(key)
            if value and not value.get('optional'):
                references.add((kind, value[field]))
    references.update(('Secret', item['name']) for item in spec.get('imagePullSecrets', []))
    return references


def manifest_documents(args, filename):
    if args.manifest_dir is not None:
        content = (args.manifest_dir / filename).read_text(encoding='utf-8')
    else:
        content = subprocess.check_output(
            ['git', '-C', str(args.repo), 'show', f'{args.commit}:k8s/{filename}'],
            text=True, encoding='utf-8')
    return [value for value in yaml.safe_load_all(content) if value is not None]


def build(args):
    validate_identity(args.commit, args.version)
    source = repository(args.registry, args.project)
    digests = image_digests(args.metadata, source, args.version)
    stages = {}
    cached = {}
    for stage, specs in STAGES.items():
        resources = []
        for file, kind, name in specs:
            if file not in cached:
                cached[file] = manifest_documents(args, file)
            matches = [obj for obj in cached[file] if obj.get('kind') == kind
                       and obj.get('metadata', {}).get('name') == name]
            if len(matches) != 1:
                raise ValueError(f'Expected exactly one {kind}/{name} in {file}')
            obj = matches[0]
            if obj['metadata'].get('namespace') != 'market':
                raise ValueError(f'Unexpected namespace on {kind}/{name}')
            if kind in ('Deployment', 'CronJob'):
                spec = pod_spec(obj)
                for container in containers(spec):
                    image = container['image'].split(':', 1)[0]
                    if image not in digests:
                        raise ValueError(f'{kind}/{name} uses an image outside this release: {image}')
                    container['image'] = f'{source}/{image}@{digests[image]}'
                    container['imagePullPolicy'] = 'IfNotPresent'
                pull_secrets = spec.setdefault('imagePullSecrets', [])
                if {'name': 'market-harbor-pull'} not in pull_secrets:
                    pull_secrets.append({'name': 'market-harbor-pull'})
            resources.append(obj)
        stages[stage] = resources

    args.output.mkdir(parents=True, exist_ok=False)
    files = {}
    for stage, resources in stages.items():
        folder = args.output / 'apps' / stage
        folder.mkdir(parents=True)
        content = yaml.safe_dump_all(resources, allow_unicode=True, sort_keys=False, explicit_start=True)
        (folder / 'resources.yaml').write_text(content, encoding='utf-8')
        kustomization = {'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
                         'resources': ['resources.yaml']}
        (folder / 'kustomization.yaml').write_text(yaml.safe_dump(kustomization, sort_keys=False), encoding='utf-8')
        for path in folder.iterdir():
            files[path.relative_to(args.output).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    included = {(obj['kind'], obj['metadata']['name']) for values in stages.values() for obj in values}
    references = {ref for values in stages.values() for obj in values if obj['kind'] in ('Deployment', 'CronJob')
                  for ref in environment_refs(pod_spec(obj))} - included
    release = {'schema': SCHEMA, 'version': args.version, 'commit': args.commit, 'git_source': SOURCE,
               'source_repository': source, 'images': digests, 'files_sha256': files,
               'stages': stage_index(),
               'environment_resources': [{'kind': kind, 'name': name} for kind, name in sorted(references)]}
    (args.output / 'release.json').write_text(json.dumps(release, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'version': args.version, 'commit': args.commit, 'images': len(digests),
                      'workload_resources': len(included), 'bundle': str(args.output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'project', 'commit', 'version'):
        parser.add_argument(f'--{name}', required=True)
    for name in ('metadata', 'output'):
        parser.add_argument(f'--{name}', required=True, type=Path)
    manifests = parser.add_mutually_exclusive_group(required=True)
    manifests.add_argument('--repo', type=Path, help='Git repository containing the release commit')
    manifests.add_argument('--manifest-dir', type=Path, help='k8s directory extracted from the release commit')
    args = parser.parse_args()
    try:
        build(args)
    except (ValueError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        print(f'Release packaging failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
