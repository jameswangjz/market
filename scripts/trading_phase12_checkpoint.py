"""Record only the partial TRD-BE-014 OIDC backend milestone.

start sets in_progress/15. finish sets in_progress/65, never done: browser
login bridge, BE-015 and QA-006 remain incomplete and are not updated here.
Finish requires --runtime-tag YYYYMMDDHHMMSS-<7..40 lowercase hex characters>
and --service-evidence JSON with status=passed, database=real PostgreSQL,
cleanup=verified (or a verified cleanup description), and a matching tag in
runtime_tag, runtime_image_tag or deploymentTag. Conflicting tags are refused.
Evidence is validated before production imports or any task update. This
script records supplied evidence; it does not deploy or run acceptance itself.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re


TASK_CODE = 'TRD-BE-014'
TAG_FIELDS = ('runtime_tag', 'runtime_image_tag', 'deploymentTag')
LIMITS = ['Partial OIDC backend milestone only; never marks BE-014 done',
          'Browser login bridge, BE-015 and QA-006 remain incomplete',
          'No other development task is updated']
START_NOTE = ('OIDC backend core work started. Partial milestone only: browser login bridge, '
              'BE-015 and QA-006 are not complete.')
FINISH_NOTE = ('OIDC backend core service acceptance passed on real PostgreSQL with verified '
               'scoped cleanup and matching deployed runtime tag. Partial milestone only: '
               'browser login bridge, BE-015 and QA-006 remain incomplete; BE-014 is not done.')


def validate_service_evidence(evidence, runtime_tag):
    """Return a small, credential-free attestation or refuse the checkpoint."""
    if not isinstance(runtime_tag, str) or not re.fullmatch(r'\d{14}-[0-9a-f]{7,40}', runtime_tag):
        raise ValueError('Formatted runtime tag required')
    try:
        datetime.strptime(runtime_tag[:14], '%Y%m%d%H%M%S')
    except ValueError:
        raise ValueError('Runtime tag timestamp is invalid') from None
    if not isinstance(evidence, dict):
        raise ValueError('Service evidence must be a JSON object')
    if evidence.get('database') != 'real PostgreSQL' or evidence.get('status') != 'passed':
        raise ValueError('Passed real PostgreSQL service evidence required')
    cleanup = evidence.get('cleanup')
    if (not isinstance(cleanup, str) or not re.fullmatch(r'verified(?:[ :;][^\r\n]+)?', cleanup)
            or re.search(r'\b(failed|failure|error|pending|deferred|unverified|incomplete)\b|not\s+verified',
                         cleanup, re.IGNORECASE)
            or evidence.get('cleanup_error_type') or evidence.get('cleanup_check_failed')):
        raise ValueError('Verified completed service cleanup required')
    tags = [evidence[key] for key in TAG_FIELDS if key in evidence]
    if not tags or any(tag != runtime_tag for tag in tags):
        raise ValueError('Service evidence runtime tag must match --runtime-tag')
    return {'status': 'passed', 'database': 'real PostgreSQL', 'cleanup': cleanup,
            'runtime_image_tag': runtime_tag}


def checkpoint_update(phase, *, evidence=None, runtime_tag=''):
    """Pure validation and update construction; only BE-014 can be selected."""
    if phase == 'start':
        return {'status': 'in_progress', 'progress': 15, 'note': START_NOTE}, None
    if phase != 'finish':
        raise ValueError('Unknown checkpoint phase')
    verified = validate_service_evidence(evidence, runtime_tag)
    return {'status': 'in_progress', 'progress': 65, 'note': FINISH_NOTE}, verified


def record_checkpoint(m, client_factory, update, verified):
    from sqlalchemy import select

    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if admin is None:
            raise ValueError('Checkpoint administrator missing')
        headers = {'Authorization': 'Bearer ' + m.issue_token(admin)}
    client = client_factory(m.app)  # No context manager: do not start lifespan jobs.
    try:
        response = client.patch('/api/development/tasks/' + TASK_CODE, headers=headers, json=update)
        if response.status_code != 200:
            raise RuntimeError('Task update failed: ' + TASK_CODE)
        response = client.get('/api/development/tasks', headers=headers)
        if response.status_code != 200:
            raise RuntimeError('Task snapshot failed')
        snapshot = response.json()
    finally:
        client.close()
    snapshot['batch_verification'] = {'task_code': TASK_CODE, 'milestone': 'partial',
        'runtime_image_tag': verified['runtime_image_tag'] if verified else '',
        'oidc_service': verified, 'limits': list(LIMITS)}
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('start', 'finish'))
    parser.add_argument('--output')
    parser.add_argument('--runtime-tag', default='')
    parser.add_argument('--service-evidence')
    args = parser.parse_args(argv)
    if args.phase == 'finish' and not (args.service_evidence and args.runtime_tag):
        parser.error('finish requires --service-evidence and --runtime-tag; this remains partial')
    evidence = None
    if args.phase == 'finish':
        evidence = json.loads(Path(args.service_evidence).read_text(encoding='utf-8-sig'))
    update, verified = checkpoint_update(args.phase, evidence=evidence, runtime_tag=args.runtime_tag)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from fastapi.testclient import TestClient

    snapshot = record_checkpoint(m, TestClient, update, verified)
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': args.phase, 'task_code': TASK_CODE, 'milestone': 'partial',
                      'status': update['status'], 'progress': update['progress'],
                      'counts': snapshot['counts']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
