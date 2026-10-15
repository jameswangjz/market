"""180-second transport of reviewed helpers; uses installed application sources."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


HELPERS = ('version_change_quote_acceptance', 'version_change_saga_runtime_acceptance',
           'r3_native_refund_runtime_acceptance', 'r3_native_refund_flow',
           'version_change_saas_runtime_acceptance', 'r3_backend_acceptance_pg', 'r3_saas_refund_live')


def build_payload(manifest, *, cleanup=False, admin_secret=None):
    payload = 'import sys,types,json,logging,signal,hashlib,importlib.util,os\nfrom pathlib import Path\n'
    if admin_secret is not None:
        payload += "os.environ['MOCK_SAAS_ADMIN_SECRET']=" + repr(admin_secret) + '\n'
    payload += 'logging.disable(logging.CRITICAL)\n'
    hashes = {}
    for name in HELPERS:
        source = Path(__file__).with_name(name + '.py').read_text()
        hashes[name] = hashlib.sha256(source.encode()).hexdigest()
        payload += ('m=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;'
                    'exec(compile(' + repr(source) + ',m.__name__,"exec"),m.__dict__)\n')
    payload += '''import r3_saas_refund_live as r
def deadline(*_):
 raise TimeoutError()
signal.signal(signal.SIGALRM,deadline)
signal.alarm(150)
def installed_sources():
 spec=importlib.util.find_spec('app')
 r.check(spec is not None and spec.submodule_search_locations,'installed_app_package_missing')
 root=Path(next(iter(spec.submodule_search_locations)))
 return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*.py'))}
try:
 before=installed_sources()
'''
    call = 'resume_cleanup' if cleanup else 'run'
    payload += ' result=r.' + call + '(' + repr(manifest) + ',reviewed=True)\n'
    payload += ''' after=installed_sources()
 r.check(before==after,'installed_sources_changed_during_run')
 result['installed_source_sha256']=before
except Exception as exc:
 result={'status':'failed','error':r.safe_error(exc)}
 if 'before' in globals(): result['installed_source_sha256']=before
finally:
 signal.alarm(0)
print(json.dumps(result))
'''
    return payload, hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'market-api-[a-z0-9-]+', args.pod):
        parser.error('Invalid API pod')
    if not re.fullmatch(r'/tmp/qa-saas-refund-[a-z0-9-]+\.json', args.manifest):
        parser.error('Expected private /tmp/qa-saas-refund-*.json manifest')
    if not os.environ.get('SSHPASS'):
        parser.error('Private SSHPASS environment is required')
    # Reserve output before sending anything; never overwrite earlier evidence.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        started = time.monotonic()
        _, hashes = build_payload(args.manifest, cleanup=args.cleanup)
        ssh = ['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5',
               '-o', 'ConnectionAttempts=1', '-o', 'LogLevel=ERROR', 'root@192.168.10.10']
        command = ('kubectl -n market exec -i ' + args.pod + ' -- env '
                   'MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 '
                   'PYTHONPATH=/app python -B -')
        try:
            secret_response = subprocess.run([*ssh,
                'kubectl -n market get secret market-mock-saas-admin -o json'],
                capture_output=True, text=True, timeout=20)
            if secret_response.returncode != 0:
                raise ValueError('secret_read_failed')
            secret = base64.b64decode(json.loads(secret_response.stdout)['data']['secret'], validate=True).decode()
            if not secret or len(secret) > 4096:
                raise ValueError('secret_invalid')
            payload, sent_hashes = build_payload(args.manifest, cleanup=args.cleanup, admin_secret=secret)
            if sent_hashes != hashes:
                raise ValueError('helpers_changed_before_send')
            remaining = 180 - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError('transport_deadline_exhausted')
            process = subprocess.run([*ssh, command], input=payload,
                capture_output=True, text=True, timeout=remaining)
            result = json.loads(process.stdout)
            if not isinstance(result, dict) or process.returncode != 0:
                result = dict(status='transport_failed', private_diagnostics_suppressed=True)
        except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError):
            result = dict(status='transport_failed', private_diagnostics_suppressed=True)
        result.update(helper_sources_sha256=hashes, manifest=args.manifest, pod=args.pod)
        result['transport_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps(result))
    return 0 if result.get('status') in {'passed', 'cleaned'} else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='transport_failed', error_type=type(exc).__name__,
                              private_diagnostics_suppressed=True)))
        raise SystemExit(1)
