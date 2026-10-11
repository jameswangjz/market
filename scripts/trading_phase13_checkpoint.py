"""Track browser SSO work; completion requires matching runtime evidence."""
import argparse
import json
import os
from pathlib import Path

from trading_phase12_checkpoint import validate_service_evidence

CODES = ('TRD-BE-014', 'TRD-BE-015', 'TRD-FE-010', 'TRD-QA-006')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('start', 'finish'))
    parser.add_argument('--runtime-tag', default='')
    parser.add_argument('--evidence')
    parser.add_argument('--output')
    args = parser.parse_args()
    evidence = None
    if args.phase == 'finish':
        if not args.evidence:
            parser.error('finish requires --evidence')
        evidence = json.loads(Path(args.evidence).read_text(encoding='utf-8-sig'))
        validate_service_evidence(evidence, args.runtime_tag)
        required = ('browser_sso', 'mock_session', 'application_access', 'negative_cases')
        if any(evidence.get(key) != 'passed' for key in required):
            parser.error('All browser/session/application/security gates must pass')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        headers = {'Authorization': 'Bearer ' + m.issue_token(admin)}
    with_client = TestClient(m.app)
    try:
        for code in CODES:
            update = {'status': 'in_progress', 'progress': 70 if code == 'TRD-BE-014' else 15,
                'note': 'Browser login bridge, isolated mock SaaS session and buyer application access are under implementation and verification.'}
            if args.phase == 'finish':
                update = {'status': 'done', 'progress': 100,
                    'note': 'Browser SSO/session/application and negative-case acceptance passed; scoped cleanup verified; runtime=' + args.runtime_tag}
            response = with_client.patch('/api/development/tasks/' + code, headers=headers, json=update)
            if response.status_code != 200:
                raise RuntimeError('Task update failed: ' + code)
        snapshot = with_client.get('/api/development/tasks', headers=headers).json()
    finally:
        with_client.close()
    snapshot['batch_verification'] = {'phase': args.phase, 'task_codes': CODES, 'evidence': evidence}
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': args.phase, 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
