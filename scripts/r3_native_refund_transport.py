"""Bounded transport of reviewed acceptance helpers into the deployed API pod."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--coverage-fault', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'market-api-[a-z0-9-]+', args.pod):
        parser.error('Invalid API pod')
    if not re.fullmatch(r'/tmp/qa-native-[a-z0-9-]+\.json', args.manifest):
        parser.error('Expected private /tmp/qa-native-*.json manifest')
    if not os.environ.get('SSHPASS'):
        parser.error('Private SSHPASS environment is required')
    if args.output and args.output.exists():
        parser.error('Evidence output already exists')
    payload = 'import sys,types,json,logging,os\nlogging.disable(logging.CRITICAL)\n'
    if args.coverage_fault:
        payload += "os.environ['QA_NATIVE_REFUND_MODE']='coverage_fault'\n"
    sources = {}
    for name in ('version_change_quote_acceptance',
                 'version_change_saga_runtime_acceptance', 'r3_native_refund_cleanup',
                 'r3_native_refund_runtime_acceptance', 'r3_native_refund_flow'):
        source = Path(__file__).with_name(name + '.py').read_text()
        sources[name] = hashlib.sha256(source.encode()).hexdigest()
        payload += ('m=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;'
                    'exec(compile(' + repr(source) + ',m.__name__,"exec"),m.__dict__)\n')
    payload += ('import r3_native_refund_runtime_acceptance as r\n'
                'import version_change_quote_acceptance as guarded\ntry:\n')
    if args.cleanup:
        payload += (' manifest=r.base.read_manifest(' + repr(args.manifest) + ')\n'
                    ' result=r.cleanup(r.base.load_main(),manifest,' + repr(args.manifest) + ')\n'
                    ' print(json.dumps({"status":"cleaned","cleanup":result}))\n')
    else:
        payload += ' print(json.dumps(r.run(' + repr(args.manifest) + ',reviewed=True)))\n'
    payload += ('except Exception as exc:\n'
                ' print(json.dumps({"status":"failed","error":str(exc) if isinstance(exc,'
                '(r.base.AcceptanceFailure,guarded.AcceptanceFailure)) else '
                'r.base.safe_exception_metadata(exc)}))\n')
    command = ('kubectl -n market exec -i ' + args.pod + ' -- env '
               'MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 '
               'PYTHONPATH=/app python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5',
        '-o', 'ConnectionAttempts=1', '-o', 'LogLevel=ERROR', 'root@192.168.10.10', command],
        input=payload, capture_output=True, text=True, timeout=240)
    try:
        result = json.loads(process.stdout)
    except ValueError:
        print(json.dumps({'status': 'transport_failed', 'returncode': process.returncode,
                          'manifest': args.manifest, 'private_diagnostics_suppressed': True}))
        return 1
    result['helper_sources_sha256'] = sources
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if process.returncode == 0 and result.get('status') in {'passed', 'cleaned'} else 1


if __name__ == '__main__':
    sys.exit(main())
