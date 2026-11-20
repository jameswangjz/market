#!/usr/bin/env python3
"""Promote only complete Market OCI releases from the local Harbor to Flux."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tarfile
import tempfile
import uuid
from urllib import error, parse, request

import yaml

from market_release_bundle import DIGEST, IMAGES, SOURCE, STAGES, VERSION, repository
from verify_market_release import DisabledRedirect, Registry, check_images, load_bundle

BUNDLE_REPO = 'market/market-release'
OCI_MEDIA_TYPE = 'application/vnd.oci.image.manifest.v1+json'
LAYER_MEDIA_TYPE = 'application/vnd.cncf.flux.content.v1.tar+gzip'
GROUPS = (('api',), ('workers', 'web'), ('jobs',))
STAGE_NAMES = {name: 'market-' + name for group in GROUPS for name in group}
RESOURCES = {
    'source': ('apis/source.toolkit.fluxcd.io/v1', 'ocirepositories', 'market-release'),
    'stage': ('apis/kustomize.toolkit.fluxcd.io/v1', 'kustomizations', None),
    'state': ('api/v1', 'configmaps', 'market-release-promotion-state'),
    'lease': ('apis/coordination.k8s.io/v1', 'leases', 'market-release-promotion'),
}
TOKEN_DIR = Path('/var/run/secrets/kubernetes.io/serviceaccount')


def utcnow():
    return datetime.now(timezone.utc)


def sha(content):
    return 'sha256:' + hashlib.sha256(content).hexdigest()


def event(status, **data):
    print(json.dumps({'status': status, **data}, ensure_ascii=False), flush=True)


class ReleaseRegistry(Registry):
    def __init__(self, host, docker_config, ca):
        repository(host, 'market')
        super().__init__(host, None, ca)
        auth = json.loads(docker_config.read_text())['auths'][host]['auth']
        self.auth = 'Basic ' + auth
        self.root = f'https://{host}'
        self.prefix = f'/v2/{BUNDLE_REPO}'

    def read(self, path, accept=None):
        headers = {'Accept': accept} if accept else {}
        try:
            return self.fetch(self.root + path, headers)
        except error.HTTPError as exc:
            if exc.code != 401:
                raise
            with exc:
                headers['Authorization'] = self.token(exc.headers.get('WWW-Authenticate', ''), BUNDLE_REPO)
        return self.fetch(self.root + path, headers)

    def tags(self):
        path = self.prefix + '/tags/list'
        cursor = ''
        for _ in range(100):
            query = {'n': 100}
            if cursor:
                query['last'] = cursor
            raw, headers = self.read(path + '?' + parse.urlencode(query))
            data = json.loads(raw)
            tags = data.get('tags') or []
            if data['name'] != BUNDLE_REPO or (cursor and tags and tags[0] <= cursor):
                raise ValueError('Unexpected registry tag page')
            yield from tags
            if not tags or (len(tags) < 100 and not headers.get('Link')):
                return
            cursor = tags[-1]
        raise ValueError('Registry tag listing exceeds configured bounds')

    def bundle(self, digest):
        if not DIGEST.fullmatch(digest):
            raise ValueError('Invalid release digest')
        raw, _ = self.read(f'{self.prefix}/manifests/{digest}', OCI_MEDIA_TYPE)
        if sha(raw) != digest:
            raise ValueError('OCI manifest digest differs from the selected digest')
        manifest = json.loads(raw)
        layers = manifest.get('layers', [])
        if manifest.get('mediaType') != OCI_MEDIA_TYPE or len(layers) != 1:
            raise ValueError('Expected a single-layer Flux release')
        layer = layers[0]
        if layer.get('mediaType') != LAYER_MEDIA_TYPE or not DIGEST.fullmatch(layer.get('digest', '')):
            raise ValueError('Unexpected Flux content descriptor')
        payload, _ = self.read(f'{self.prefix}/blobs/{layer["digest"]}')
        if len(payload) != layer.get('size') or sha(payload) != layer['digest']:
            raise ValueError('Flux release layer does not match its descriptor')

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            extract_bundle(payload, root)
            release, fingerprint = load_bundle(root)

        tags = manifest.get('annotations', {})
        if (tags.get('org.opencontainers.image.source') != SOURCE
                or tags.get('org.opencontainers.image.revision') != 'main@sha1:' + release['commit']):
            raise ValueError('OCI provenance differs from the release manifest')
        return release, fingerprint


def extract_bundle(payload, root):
    files = {'release.json'} | {f'apps/{stage}/{name}' for stage in STAGES
                               for name in ('resources.yaml', 'kustomization.yaml')}
    directories = {'.', 'apps'} | {f'apps/{stage}' for stage in STAGES}
    seen = set()
    size = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as stream:
        for member in stream:
            name = member.name
            if name in seen or len(seen) >= len(files) + len(directories):
                raise ValueError('Repeated or oversized bundle inventory')
            seen.add(name)
            if member.isdir() and name in directories:
                continue
            size += member.size
            if not member.isfile() or name not in files or size > 1024 * 1024:
                raise ValueError('Unexpected bundle member')
            item = root / name
            item.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source:
                item.write_bytes(source.read())
    if not files <= seen:
        raise ValueError('Incomplete Flux bundle')


class Kubernetes:
    def __init__(self):
        self.namespace = TOKEN_DIR.joinpath('namespace').read_text().strip()
        ca = ssl.create_default_context(cafile=str(TOKEN_DIR / 'ca.crt'))
        self.client = request.build_opener(request.ProxyHandler({}), DisabledRedirect(),
                                           request.HTTPSHandler(context=ca))

    def call(self, kind, obj=None, payload=None):
        version, plural, default = RESOURCES[kind]
        name = STAGE_NAMES[obj] if kind == 'stage' else default
        url = f'https://kubernetes.default.svc/{version}/namespaces/{self.namespace}/{plural}/{name}'
        headers = {'Authorization': 'Bearer ' + (TOKEN_DIR / 'token').read_text().strip()}
        method, body = 'GET', None
        if payload is not None:
            headers['Content-Type'] = 'application/merge-patch+json'
            method, body = 'PATCH', json.dumps(payload).encode()
        with self.client.open(request.Request(url, body, headers, method=method), timeout=12) as resp:
            return json.load(resp)

    def patch(self, kind, previous, data, obj=None):
        meta = {'resourceVersion': previous['metadata']['resourceVersion']}
        return self.call(kind, obj, {'metadata': meta, **data})


def observed(obj, revision, source=False):
    status = obj.get('status', {})
    recorded = status.get('artifact', {}).get('revision') if source else status.get('lastAppliedRevision')
    gen = obj['metadata']['generation']
    if revision != recorded or status.get('observedGeneration') != gen:
        return False
    return any(c['type'] == 'Ready' and c['status'] == 'True' and c.get('observedGeneration') == gen
               for c in status.get('conditions', []))


def version_time(tag):
    if VERSION.fullmatch(tag):
        try:
            return datetime.strptime(tag[:14], '%Y%m%d%H%M%S')
        except ValueError:
            pass
    return None


class Promotion:
    def __init__(self, kube, registry, target_namespace, database):
        self.kube, self.registry = kube, registry
        self.target_namespace, self.database = target_namespace, database
        self.url = f'oci://{registry.registry}/{BUNDLE_REPO}'
        self.owner = str(uuid.uuid4())

    def acquire(self):
        lock = self.kube.call('lease')
        spec = lock.get('spec', {})
        now = utcnow()
        if spec.get('holderIdentity'):
            renewed = datetime.fromisoformat(spec['renewTime'].replace('Z', '+00:00'))
            if renewed + timedelta(seconds=spec['leaseDurationSeconds']) > now:
                return False
        self.kube.patch('lease', lock, {'spec': {'holderIdentity': self.owner, 'leaseDurationSeconds': 180,
                                                'renewTime': now.isoformat(timespec='microseconds')}})
        return True

    def release(self):
        lease = self.kube.call('lease')
        if lease.get('spec', {}).get('holderIdentity') == self.owner:
            self.kube.patch('lease', lease, {'spec': {'holderIdentity': None}})

    def guard(self):
        lease = self.kube.call('lease')['spec']
        fresh = datetime.fromisoformat(lease['renewTime'].replace('Z', '+00:00'))
        if lease['holderIdentity'] != self.owner or fresh + timedelta(seconds=lease['leaseDurationSeconds']) <= utcnow():
            raise RuntimeError('Promotion lease expired')
        obj = self.kube.call('state')
        if obj.get('data', {}).get('paused') != 'false':
            raise PromotionPaused()
        return obj

    def save(self, state):
        obj = self.guard()
        self.kube.patch('state', obj, {'data': {'state.json': json.dumps(state, separators=(',', ':'))}})

    def edit(self, kind, obj, spec, stage=None):
        self.guard()
        return self.kube.patch(kind, obj, {'spec': spec}, stage)

    def stages(self):
        result = {}
        for stage in STAGE_NAMES:
            obj = self.kube.call('stage', stage)
            spec = obj['spec']
            if (spec['targetNamespace'] != self.target_namespace or spec['path'] != './apps/' + stage
                    or spec['sourceRef'] != {'kind': 'OCIRepository', 'name': 'market-release'}
                    or not spec.get('wait')
                    or {x['name'] for x in spec.get('dependsOn', [])} !=
                       ({'market-api'} if stage in ('workers', 'web') else
                        {'market-api', 'market-workers'} if stage == 'jobs' else set())):
                raise ValueError(f'Unexpected Flux template for {stage}')
            result[stage] = obj
        return result

    def source(self):
        src = self.kube.call('source')
        if src['spec']['url'] != self.url or set(src['spec']['ref']) != {'digest'}:
            raise ValueError('This promoter requires its designated digest-pinned OCI source')
        return src

    def mapping(self, current, release):
        result = {}
        for stage, obj in current.items():
            images = obj['spec']['images']
            names = {x['name'].rsplit('/', 1)[-1] for x in images}
            if len(images) != len(IMAGES) or names != set(IMAGES):
                raise ValueError(f'Incomplete mirror mapping at {stage}')
            desired = []
            for entry in images:
                name = entry['name'].rsplit('/', 1)[-1]
                if (set(entry) != {'name', 'newName', 'digest'}
                        or entry['name'] != f'{release["source_repository"]}/{name}'
                        or entry['newName'] != f'{self.registry.registry}/market/{name}'):
                    raise ValueError(f'Unexpected image mapping at {stage}')
                desired.append({**entry, 'digest': release['images'][name]})
            result[stage] = desired
        return result

    def open_plan(self, state, skip_through):
        src = self.source()
        current = self.stages()
        digest = src['spec']['ref']['digest']
        if (src['spec'].get('suspend') or any(o['spec'].get('suspend') for o in current.values())):
            event('flux_suspended')
            return False
        if state.get('active') and digest != state['active']['digest']:
            raise ValueError('The live digest changed outside the promoter, review before re-arming')
        if not observed(src, digest, source=True) or any(not observed(o, digest) for o in current.values()):
            event('waiting_for_active_flux_health')
            return False
        if 'active_images' not in state:
            release, fp = self.registry.bundle(digest)
            pinned = self.mapping(current, release)
            if any(current[name]['spec']['images'] != images for name, images in pinned.items()):
                raise ValueError('The initial Flux image digest mapping does not match its OCI bundle')
            state['active'] = {'version': release['version'], 'digest': digest, 'fingerprint': fp}
            state['active_images'] = pinned
            self.save(state)
        elif any(o['spec']['images'] != state['active_images'][stage] for stage, o in current.items()):
            raise ValueError('The live image mapping changed outside the promoter, review before re-arming')
        old = state['active']
        tags = {t for t in self.registry.tags() if version_time(t)}
        if old['version'] in tags and self.registry.digest(BUNDLE_REPO, old['version']) != digest:
            raise ValueError('The tag of the live release was overwritten')
        latest = max(tags, key=lambda tag: (version_time(tag), tag), default=None)
        if skip_through and not version_time(skip_through):
            raise ValueError('skip-through must be a Market release tag')
        if latest and skip_through and version_time(latest) <= version_time(skip_through):
            event('release_held', version=latest, skip_through=skip_through)
            return False
        if latest and sum(version_time(t) == version_time(latest) for t in tags) > 1:
            raise ValueError('Release tags have an ambiguous timestamp')
        if not latest or version_time(latest) < version_time(old['version']) or latest == old['version']:
            event('up_to_date', **old)
            return False
        if version_time(latest) == version_time(old['version']):
            raise ValueError('Release tags have an ambiguous timestamp')
        candidate = self.registry.digest(BUNDLE_REPO, latest)
        release, fp = self.registry.bundle(candidate)
        if release['version'] != latest or self.database not in release.get('database_types', ['Postgres']):
            raise ValueError('Candidate tag or database compatibility differs from the release configuration')
        issues = check_images(release, 'market', self.registry)
        if issues:
            event('waiting_for_complete_release', version=latest, issues=issues)
            return False
        if self.registry.digest(BUNDLE_REPO, latest) != candidate:
            raise ValueError('Candidate OCI tag changed during verification')

        desired = self.mapping(current, release)
        state['pending'] = {
            'version': latest, 'digest': candidate, 'fingerprint': fp, 'step': -2,
            'timeout': (utcnow() + timedelta(minutes=25)).isoformat(),
            'target_images': desired,
            'previous_images': {stage: o['spec']['images'] for stage, o in current.items()},
        }
        self.save(state)
        event('verified_release', version=latest, digest=candidate)
        return True

    def advance(self, state):
        plan = state['pending']
        wanted = plan['digest']
        if plan.get('blocked'):
            event('promotion_blocked', version=plan['version'], reason=plan['blocked'])
            return
        if utcnow() > datetime.fromisoformat(plan['timeout']):
            plan['blocked'] = 'flux_readiness_timeout'
            self.save(state)
            event('promotion_blocked', version=plan['version'], reason=plan['blocked'])
            return
        src = self.source()
        current = self.stages()
        if src['spec'].get('suspend'):
            event('flux_source_suspended')
            return
        live_digest = src['spec']['ref']['digest']
        if plan['step'] == -2:
            if live_digest not in (state['active']['digest'], wanted):
                raise ValueError('Unexpected source digest while preparing release')
            for stage, o in current.items():
                want = plan['target_images'][stage]
                if o['spec']['images'] not in (plan['previous_images'][stage], want):
                    raise ValueError(f'Image mapping changed externally at {stage}')
                if not o['spec'].get('suspend') or o['spec']['images'] != want:
                    if live_digest == wanted:
                        raise ValueError('Candidate was selected before stages were safely suspended')
                    self.edit('stage', o, {'suspend': True, 'images': want}, stage)
            if live_digest != wanted:
                self.edit('source', src, {'ref': {'digest': wanted}})
            plan['step'] = -1
            self.save(state)
            event('flux_source_selected', digest=wanted)
            return
        if live_digest != wanted or any(o['spec']['images'] != plan['target_images'][s] for s, o in current.items()):
            raise ValueError('Flux changed outside the current promotion')
        if not observed(src, wanted, source=True):
            event('waiting_for_source', digest=wanted)
            return

        for step, group in enumerate(GROUPS):
            if step > plan['step'] + 1:
                raise ValueError('Promotion phase is inconsistent')
            for stage in group:
                o = current[stage]
                if o['spec'].get('suspend'):
                    if step <= plan['step']:
                        event('flux_stage_suspended', stage=stage)
                        return
                    current[stage] = self.edit('stage', o, {'suspend': False}, stage)
            if step > plan['step']:
                plan['step'] = step
                self.save(state)
            if any(not observed(current[stage], wanted) for stage in group):
                event('waiting_for_stage', digest=wanted, stages=group)
                return
        state['rollback'] = {'active': state['active'], 'images': plan['previous_images']}
        state['active'] = {key: plan[key] for key in ('version', 'digest', 'fingerprint')}
        state['active_images'] = plan['target_images']
        del state['pending']
        self.save(state)
        event('deployed', **state['active'])

    def run(self):
        if not self.acquire():
            event('another_job_active')
            return
        try:
            data = self.guard().get('data', {})
            state = json.loads(data.get('state.json', '{}'))
            if 'pending' in state or self.open_plan(state, data.get('skip-through', '')):
                self.advance(state)
        except PromotionPaused:
            event('paused')
        finally:
            self.release()


class PromotionPaused(Exception):
    pass


def main():
    try:
        kube = Kubernetes()
        registry = ReleaseRegistry(os.environ['MARKET_REGISTRY'], Path('/harbor/auth/.dockerconfigjson'),
                                   Path('/harbor/ca/ca.crt'))
        Promotion(kube, registry, os.environ['MARKET_TARGET_NAMESPACE'],
                  os.environ['MARKET_DB_TYPE']).run()
    except error.HTTPError as exc:
        event('request_failed', http_status=exc.code)
        return 1
    except (ValueError, RuntimeError) as exc:
        event('promotion_stopped', reason=str(exc))
        return 1
    except (KeyError, OSError, tarfile.TarError, TypeError, yaml.YAMLError) as exc:
        event('promotion_failed', reason=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
