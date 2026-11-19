#!/usr/bin/env python3
"""Check a release against the destination Harbor before enabling deployment."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import sys
import tempfile
from urllib import error, parse, request

import yaml

from market_release_bundle import (DIGEST, IMAGES, SCHEMA, SOURCE, STAGES,
                                   containers, pod_spec, repository, stage_index,
                                   validate_identity)

MEDIA_TYPES = ', '.join((
    'application/vnd.oci.image.manifest.v1+json', 'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
))


class DisabledRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Registry:
    def __init__(self, registry, credentials, ca):
        self.registry = registry
        tls = ssl.create_default_context(cafile=str(ca) if ca else None)
        self.client = request.build_opener(DisabledRedirect, request.HTTPSHandler(context=tls))
        self.auth = None
        if credentials:
            data = json.loads(credentials.read_text())
            self.auth = 'Basic ' + base64.b64encode(f"{data['name']}:{data['secret']}".encode()).decode()

    def fetch(self, url, headers):
        with self.client.open(request.Request(url, headers=headers), timeout=15) as response:
            content = response.read(4 * 1024**2 + 1)
            if len(content) > 4 * 1024**2:
                raise ValueError('Registry response exceeded the verification limit')
            return content, response.headers

    def token(self, challenge, name):
        scheme, _, entries = challenge.partition(' ')
        fields = request.parse_keqv_list(request.parse_http_list(entries))
        realm = parse.urlsplit(fields.get('realm', ''))
        if (scheme.lower() != 'bearer' or realm.scheme != 'https' or realm.netloc != self.registry
                or not fields.get('service')):
            raise ValueError('Unexpected Harbor token service')
        params = parse.parse_qsl(realm.query, keep_blank_values=True)
        params += [('service', fields['service']), ('scope', f'repository:{name}:pull')]
        url = parse.urlunsplit(realm._replace(query=parse.urlencode(params), fragment=''))
        content, _ = self.fetch(url, {'Authorization': self.auth} if self.auth else {})
        result = json.loads(content)
        token = result.get('token') or result.get('access_token')
        if not isinstance(token, str) or not token:
            raise ValueError('Harbor did not return a pull token')
        return 'Bearer ' + token

    def digest(self, name, ref):
        url = f'https://{self.registry}/v2/{name}/manifests/{parse.quote(ref, safe=":")}'
        headers = {'Accept': MEDIA_TYPES}
        try:
            content, response_headers = self.fetch(url, headers)
        except error.HTTPError as exc:
            if exc.code != 401:
                raise
            with exc:
                challenge = exc.headers.get('WWW-Authenticate', '')
            headers['Authorization'] = self.token(challenge, name)
            content, response_headers = self.fetch(url, headers)
        digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        if response_headers.get('Docker-Content-Digest', digest) != digest:
            raise ValueError('Registry manifest content does not match its reported digest')
        return digest


def load_bundle(directory):
    raw = (directory / 'release.json').read_bytes()
    release = json.loads(raw)
    if release.get('schema') != SCHEMA or set(release.get('images', {})) != set(IMAGES):
        raise ValueError('Unexpected schema or incomplete image set')
    validate_identity(release['commit'], release['version'])
    if release.get('git_source') != SOURCE or release.get('stages') != stage_index():
        raise ValueError('Unexpected source or deployment dependencies')
    if any(not DIGEST.fullmatch(value) for value in release['images'].values()):
        raise ValueError('Invalid image digest')
    source = repository(*release['source_repository'].rsplit('/', 1))
    files = release.get('files_sha256', {})
    expected_files = {f'apps/{stage}/{name}' for stage in STAGES
                      for name in ('resources.yaml', 'kustomization.yaml')}
    if set(files) != expected_files:
        raise ValueError('Unexpected or incomplete Kubernetes stage files')
    for path, expected in files.items():
        parts = PurePosixPath(path).parts
        if len(parts) != 3 or parts[0] != 'apps' or any(x in ('..', '.') for x in parts):
            raise ValueError('Unexpected bundle path')
        item = directory / path
        if item.is_symlink() or not item.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Unexpected link in bundle')
        if hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Bundle checksum mismatch: {path}')
    local_files = {f.relative_to(directory).as_posix() for f in directory.rglob('*') if f.is_file()}
    if local_files != {'release.json', *files}:
        raise ValueError('Unexpected extra files in bundle')
    expected_images = {f'{source}/{name}@{digest}' for name, digest in release['images'].items()}
    for stage, specs in STAGES.items():
        stage_dir = directory / 'apps' / stage
        config = yaml.safe_load((stage_dir / 'kustomization.yaml').read_text())
        if config != {'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
                      'resources': ['resources.yaml']}:
            raise ValueError(f'Unexpected Kustomize resources in {stage}')
        resources = list(yaml.safe_load_all((stage_dir / 'resources.yaml').read_text()))
        actual = [(obj['kind'], obj['metadata']['name']) for obj in resources]
        if actual != [(kind, name) for _, kind, name in specs]:
            raise ValueError(f'Unexpected resource set in {stage}')
        for obj in resources:
            if obj['metadata'].get('namespace') != 'market':
                raise ValueError('Unexpected namespace in bundle')
            if obj['kind'] in {'Deployment', 'CronJob'}:
                for container in containers(pod_spec(obj)):
                    if container['image'] not in expected_images:
                        raise ValueError('Workload image is not bound to release metadata')
    return release, hashlib.sha256(raw).hexdigest()


def check_images(release, project, registry):
    issues = []
    for name in IMAGES:
        expected = release['images'][name]
        image = f'{project}/{name}'
        for ref in (release['version'], expected):
            try:
                actual = registry.digest(image, ref)
            except error.HTTPError as exc:
                reason = 'missing' if exc.code == 404 else f'registry_http_{exc.code}'
                exc.close()
            except (error.URLError, OSError, ValueError):
                reason = 'registry_verification_failed'
            else:
                if actual == expected:
                    continue
                reason = 'digest_mismatch'
            issues.append({'image': name, 'reference': ref, 'reason': reason})
    return issues


def record_ready(state_path, verified):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if state_path.exists():
            previous = json.loads(state_path.read_text())
            if previous['target'] != verified['target']:
                raise ValueError('Use a separate verification state file for each destination')
            if previous['version'] == verified['version']:
                return 'duplicate' if previous['fingerprint'] == verified['fingerprint'] else 'version_conflict'
            if previous['version'].split('-')[0] >= verified['version'].split('-')[0]:
                return 'stale'
        with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=state_path.parent) as stream:
            json.dump(verified, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(state_path)
    return 'ready'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--ca', type=Path)
    parser.add_argument('--state', type=Path, help='Record the latest verified release, without applying it')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        target = repository(args.registry, args.project)
        release, fingerprint = load_bundle(args.bundle)
        registry = Registry(args.registry, args.auth_file, args.ca)
        issues = check_images(release, args.project, registry)
        summary = {'target': target, 'version': release['version'], 'commit': release['commit'],
                   'fingerprint': fingerprint}
        if issues:
            print(json.dumps({**summary, 'status': 'blocked', 'issues': issues}))
            return 2
        status = record_ready(args.state, summary) if args.state else 'ready'
        print(json.dumps({**summary, 'status': status, 'verified_images': len(IMAGES)}))
        return 0 if status in {'ready', 'duplicate'} else 2
    except (ValueError, OSError, TypeError, KeyError, yaml.YAMLError) as exc:
        # Do not print remote authorization data or the contents of an invalid credential file.
        print(f'Release verification error ({type(exc).__name__})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
