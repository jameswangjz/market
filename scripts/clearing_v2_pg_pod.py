"""Render an isolated, bounded test pod from the API's runtime environment."""
import copy
import json
import re
import sys


def render(deployment, name):
    if not re.fullmatch(r'clr2-pg-[0-9]{14}', name):
        raise ValueError('Only uniquely named clearing test pods are allowed')
    source = deployment['spec']['template']['spec']
    api = next(c for c in source['containers'] if c['name'] == 'api')
    container = {k: copy.deepcopy(api[k]) for k in ('image', 'imagePullPolicy', 'env', 'envFrom') if k in api}
    container.update(name='test', command=['sh', '-c', 'sleep 1800'],
                     resources={'requests': {'cpu': '100m', 'memory': '256Mi'},
                                'limits': {'cpu': '2', 'memory': '1536Mi'}})
    # No business PVCs, ports, probes, background workers, or service selectors.
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': name, 'namespace': 'market', 'labels': {'purpose': 'clearing-isolated-pg-test'}},
            'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 1800,
                     'automountServiceAccountToken': False, 'containers': [container]}}


if __name__ == '__main__':
    print(json.dumps(render(json.load(sys.stdin), sys.argv[1])))
