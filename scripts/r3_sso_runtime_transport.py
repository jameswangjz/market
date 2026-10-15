"""Local transport only; never runs until explicitly invoked by the operator.

prepare --manifest /tmp/qa-r3-sso-NAME.json --runtime-tag TAG
state --manifest PATH --state account-disabled
restore --manifest PATH
copy-private --manifest PATH  (prints a new local 0700 directory path)
cleanup-rp --manifest PATH
cleanup --manifest PATH

Use copy-private's browser file with Windows CDP -DirectRP -NoProxy. Restore
between negative scenarios. SSHPASS is consumed by sshpass -e, never argv.
Failures retain API journals and any mock-pod cleanup journal for recovery.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


MODES = ('prepare', 'state', 'restore', 'copy-private', 'cleanup-rp', 'cleanup')
STATES = ('account-disabled', 'subscription-expired', 'membership-disabled', 'membership-removed')
LIMIT = 2_000_000
PHASES = ('local_validation', 'ssh_transport', 'secret_read', 'api_helper', 'rp_cleanup', 'control_copy')


def safe_type(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', value) else 'UnknownError'


class TransportFailure(RuntimeError):
    def __init__(self, phase, error_type, helper_error_type=None):
        self.details = dict(status='failed', phase=phase if phase in PHASES else 'ssh_transport',
                            error_type=safe_type(error_type))
        if helper_error_type is not None:
            self.details['helper_error_type'] = safe_type(helper_error_type)
        super().__init__('Transport failed; journals retained')


SSH = ['sshpass', '-e', 'ssh', '-T', '-o', 'BatchMode=no', '-o',
       'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o',
       'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
       'root@192.168.10.10', 'python3', '-']


def validate(args):
    if args.mode not in MODES or not re.fullmatch(
            r'/tmp/qa-r3-sso-[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.json', args.manifest):
        raise ValueError('Invalid mode or manifest')
    if args.mode == 'prepare':
        if not re.fullmatch(r'\d{14}-[0-9a-f]{7,40}', args.runtime_tag or ''):
            raise ValueError('Runtime tag required')
    elif args.runtime_tag:
        raise ValueError('Runtime tag is prepare only')
    if (args.mode == 'state' and args.state not in STATES) or (args.mode != 'state' and args.state):
        raise ValueError('Invalid state command')


# This program is sent to Python in either pod, with the two helper modules
# registered in memory. It has no startup hooks, shell evaluation or HTTP server.
POD = r'''
import contextlib, io, json, os, pathlib, sys, tempfile, types
os.chdir('/app')
sys.path.insert(0, '/app')
for name, source in p['sources'].items():
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, name + '.py', 'exec'), module.__dict__)
scope = sys.modules['version_change_quote_acceptance']
helper = sys.modules['oidc_browser_acceptance']
path = pathlib.Path(p['manifest'])
paths = [path, helper.control_path(path), path.with_suffix('.browser.json')]
if p['op'] != 'invoke' or '--prepare' not in p['args']:
    # The mock transfer is validated below after its private files are created.
    if not p.get('artifacts'):
        scope.check(scope.read_scope(path)['run_id'].startswith('qa-r3-sso-'), 'SSO fixture required')
if p['op'] == 'read':
    ids = scope.read_scope(path)
    helper.read_control(path, ids)
    print(json.dumps([helper.private_read(x) for x in paths[:p['count']]]))
elif p['op'] == 'write-control':
    ids = scope.read_scope(path)
    old = helper.read_control(path, ids)
    updated = p['control']
    scope.check(old == p['previous'] and updated == dict(old, rp_cleanup_verified=True),
                'Control handoff mismatch')
    scope.private_json(paths[1], updated)
    print(json.dumps({'status':'passed'}))
else:
    if p.get('artifacts'):
        directory = pathlib.Path(tempfile.mkdtemp(prefix='qa-r3-sso-rp-', dir='/tmp'))
        directory.chmod(0o700)
        path = directory / path.name
        for target, value in zip([path, helper.control_path(path)], p['artifacts']):
            scope.private_json(target, value, create=True)
        scope.check(scope.read_scope(path)['run_id'].startswith('qa-r3-sso-'), 'SSO fixture required')
    if p.get('secret'):
        os.environ['MOCK_SAAS_ADMIN_SECRET'] = p['secret']
    sys.argv = ['oidc_browser_acceptance.py', *p['args'], '--manifest', str(path)]
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = helper.main()
    result = json.loads(captured.getvalue())
    if code:
        print(json.dumps({'status':'failed', 'error_type':result.get('error_type', 'UnknownError')}))
    elif p.get('artifacts'):
        print(json.dumps({'status':'passed', 'control':helper.private_read(helper.control_path(path))}))
    else:
        print(json.dumps({'status':result['status']}))
'''


HOST = r'''
import base64, json, re, subprocess
def safe_type(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', value) else 'UnknownError'
class HelperFailure(Exception):
    def __init__(self, error_type):
        self.helper_error_type = safe_type(error_type)
def run(argv, data=None):
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=180, check=False)
    if result.returncode or len(result.stdout) > 2000000:
        raise RuntimeError('Remote operation failed; journals retained')
    return result.stdout
def pod(deployment, payload):
    source = 'p = ' + repr(payload) + '\ntry:\n' + '\n'.join('    ' + line for line in program.splitlines())
    source += "\nexcept Exception as exc:\n    import json\n    print(json.dumps({'status':'failed','error_type':type(exc).__name__}))\n"
    result = json.loads(run(['kubectl','-n','market','exec','-i',
                            'deployment/' + deployment,'--','python','-'], source.encode()))
    if isinstance(result, dict) and result.get('status') == 'failed':
        raise HelperFailure(result.get('error_type'))
    return result
phase = 'api_helper'
try:
    mode = payload.pop('mode')
    if mode == 'prepare':
        phase = 'secret_read'
        raw = run(['kubectl','-n','market','get','secret','market-mock-saas-admin',
                   '-o','jsonpath={.data.secret}'])
        secret = base64.b64decode(raw, validate=True).decode()
        if not secret or len(secret) > 16384:
            raise ValueError('Invalid secret')
        payload['secret'] = secret
    phase = 'api_helper'
    if mode == 'copy-private':
        payload.update(op='read', count=3)
        result = pod('market-api', payload)
    elif mode == 'cleanup-rp':
        payload.update(op='read', count=2)
        artifacts = pod('market-api', payload)
        phase = 'rp_cleanup'
        payload.update(op='invoke', artifacts=artifacts,
                       args=['--cleanup-rp','--rp-store','/data/mock-saas.sqlite3'])
        result = pod('market-mock-saas', payload)
        if result.get('status') == 'failed':
            print(json.dumps({'status':'failed', 'phase':phase, 'error_type':'HelperFailure',
                              'helper_error_type':safe_type(result.get('error_type'))}))
            raise SystemExit(1)
        phase = 'control_copy'
        payload.pop('artifacts')
        payload.update(op='write-control', previous=artifacts[1], control=result['control'])
        result = pod('market-api', payload)
    else:
        result = pod('market-api', payload)
    if isinstance(result, dict) and result.get('status') == 'failed':
        print(json.dumps({'status':'failed', 'phase':phase, 'error_type':'HelperFailure',
                          'helper_error_type':safe_type(result.get('error_type'))}))
        raise SystemExit(1)
    print(json.dumps(result))
except Exception as exc:
    failure = {'status':'failed', 'phase':phase, 'error_type':safe_type(type(exc).__name__)}
    if isinstance(exc, HelperFailure):
        failure['helper_error_type'] = exc.helper_error_type
    print(json.dumps(failure))
    raise SystemExit(1)
'''


def build_program(args):
    validate(args)
    sources = {name: Path(__file__).with_name(name + '.py').read_text()
               for name in ('version_change_quote_acceptance', 'oidc_browser_acceptance')}
    command = {'prepare': ['--prepare', '--runtime-tag', args.runtime_tag,
                          '--base-url', 'http://192.168.10.10:30080',
                          '--rp-public', 'http://192.168.10.10:30083',
                          '--rp-base', 'http://market-mock-saas:8200'],
               'state': ['--set-owned-state', args.state],
               'restore': ['--set-owned-state', 'restore'],
               'cleanup': ['--cleanup']}.get(args.mode, [])
    payload = dict(mode=args.mode, op='invoke', manifest=args.manifest, sources=sources, args=command)
    return ('payload = ' + repr(payload) + '\nprogram = ' + repr(POD) + '\n' + HOST).encode()


def execute(args):
    program = build_program(args)
    if not os.environ.get('SSHPASS'):
        raise ValueError('SSHPASS required')
    try:
        result = subprocess.run(SSH, input=program, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600, check=False)
        if len(result.stdout) > LIMIT:
            raise TransportFailure('ssh_transport', 'OutputLimitError')
        value = json.loads(result.stdout)
    except TransportFailure:
        raise
    except Exception as exc:
        raise TransportFailure('ssh_transport', type(exc).__name__) from None
    if isinstance(value, dict) and value.get('status') == 'failed':
        raise TransportFailure(value.get('phase'), value.get('error_type'), value.get('helper_error_type'))
    if result.returncode:
        raise TransportFailure('ssh_transport', 'RemoteExitError')
    if args.mode != 'copy-private':
        if value.get('status') not in ('passed', 'prepared'):
            raise RuntimeError('Helper failed')
        return dict(status=value['status'], mode=args.mode, manifest=args.manifest)
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError('Private artifact transfer failed')
    directory = Path(tempfile.mkdtemp(prefix='qa-r3-sso-private-', dir='/tmp'))
    directory.chmod(0o700)
    names = (Path(args.manifest).name, Path(args.manifest).with_suffix('.oidc.json').name,
             Path(args.manifest).with_suffix('.browser.json').name)
    for name, artifact in zip(names, value):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(artifact, stream)
    return dict(status='passed', mode=args.mode, private_directory=str(directory), files=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=MODES)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--runtime-tag')
    parser.add_argument('--state', choices=STATES)
    args = parser.parse_args()
    try:
        result = execute(args)
    except TransportFailure as exc:
        print(json.dumps(exc.details))
        return 1
    except Exception as exc:
        print(json.dumps({'status':'failed', 'phase':'local_validation',
                          'error_type':safe_type(type(exc).__name__)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
