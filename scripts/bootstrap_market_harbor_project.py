#!/usr/bin/env python3
"""Create the private Market registry project and its project-scoped robots."""
import base64
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.request

STATE = Path('/root/market/ops/harbor-env1')
REGISTRY = 'https://192.168.10.10:30443'
PROJECT = 'market'
QUOTA = 8 * 1024**3


def main():
    os.umask(0o077)
    ca = ssl.create_default_context(cafile=str(STATE / 'ca.crt'))
    basic = base64.b64encode(b'admin:' + (STATE / 'admin-password').read_bytes()).decode()

    def request(path, body=None):
        headers = {'Authorization': 'Basic ' + basic, 'Content-Type': 'application/json'}
        query = urllib.request.Request(REGISTRY + '/api/v2.0' + path,
                                       headers=headers, data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(query, context=ca, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else None

    try:
        project = request('/projects/' + PROJECT)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        request('/projects', {'project_name': PROJECT, 'storage_limit': QUOTA,
                              'metadata': {'public': 'false'}})
        project = request('/projects/' + PROJECT)
    if project.get('metadata', {}).get('public') != 'false':
        raise RuntimeError('Existing project is public; refusing to initialize publishing credentials')

    for name, actions in [('forgejo-publish', ('pull', 'push')), ('replication-read', ('pull',))]:
        credentials = STATE / (name + '.json')
        if not credentials.exists():
            existing = request('/robots?level=project&page_size=100')
            if any(robot['name'].endswith('+' + name) for robot in existing):
                raise RuntimeError(f'Robot {name} already exists and credentials are absent')
            robot = request('/robots', {'name': name, 'level': 'project', 'duration': 180,
                                        'permissions': [{'kind': 'project', 'namespace': PROJECT,
                                                         'access': [{'resource': 'repository', 'action': action,
                                                                     'effect': 'allow'} for action in actions]}]})
            with credentials.open('x') as output:
                json.dump(robot, output)
        print(f'Robot credentials available in {credentials}')

    quotas = request(f"/quotas?reference=project&reference_id={project['project_id']}")
    storage = quotas[0].get('hard', {}).get('storage') if quotas else None
    if storage != QUOTA:
        raise RuntimeError('Market storage quota verification failed')
    print(f'Private Harbor project {PROJECT} verified with {storage // 1024**3} GiB quota')


if __name__ == '__main__':
    main()
