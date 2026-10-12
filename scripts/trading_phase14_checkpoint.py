"""Record the limited unified-access batch without closing unfinished fulfillment."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--phase', choices=('start', 'verified'), default='start')
    parser.add_argument('--contract-proof')
    parser.add_argument('--cleanup-proof')
    parser.add_argument('--browser-proof')
    parser.add_argument('--base-url', default='http://192.168.10.10:30080')
    args = parser.parse_args()
    if args.phase == 'verified':
        for flag in ('contract_proof', 'cleanup_proof', 'browser_proof'):
            path = getattr(args, flag)
            if not path:
                raise RuntimeError('Missing verification proof: ' + flag)
            proof = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if proof.get('status') != 'passed':
                raise RuntimeError('Verification proof not passed: ' + flag)
            if flag == 'cleanup_proof' and not proof.get('cleanup_verified'):
                raise RuntimeError('Cleanup not verified')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from httpx import Client
    from sqlalchemy import select
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not admin:
            raise RuntimeError('Platform administrator unavailable')
        headers = {'Authorization': 'Bearer ' + m.issue_token(admin)}
    updates = {
        'TRD-BE-012': (40, 'Unified buyer delivery-access API implementation started; API/SaaS automatic fulfillment verification remains incomplete.'),
        'TRD-FE-006': (40, 'Order access entry implementation: file, API/model metadata and SaaS launch; coding model gpt-6-luna medium, current-model design/review/verification.'),
        'TRD-QA-003': (30, 'Scoped access acceptance in preparation; private draft fixtures only. Full four-delivery real fulfillment acceptance remains incomplete.'),
    }
    if args.phase == 'verified':
        updates = {
            'TRD-BE-012': (60, 'Unified read-only buyer access and scoped PostgreSQL/HTTP acceptance passed. Full API/SaaS automatic fulfillment remains incomplete.'),
            'TRD-FE-006': (100, 'File/API/model/SaaS typed order entry implemented; desktop/mobile browser metadata acceptance passed. Awaiting user acceptance; native credential management and positive SSO are not covered by this batch.'),
            'TRD-QA-003': (40, 'Scoped PostgreSQL/HTTP and desktop/mobile access UI passed; exact fixture cleanup verified. Full four-delivery real provisioning/payment/download acceptance remains incomplete.'),
        }
    with Client(base_url=args.base_url, trust_env=False, follow_redirects=False, timeout=30) as client:
        for code, (progress, note) in updates.items():
            response = client.patch('/api/development/tasks/' + code, headers=headers,
                json={'status': 'review' if args.phase == 'verified' and code == 'TRD-FE-006' else 'in_progress',
                      'progress': progress, 'note': note})
            if response.status_code != 200:
                raise RuntimeError('Task update failed: ' + code)
        snapshot = client.get('/api/development/tasks', headers=headers)
        if snapshot.status_code != 200:
            raise RuntimeError('Task snapshot failed')
        result = snapshot.json()
    result['batch_scope'] = {'coding_model': 'gpt-6-luna', 'reasoning_effort': 'medium',
        'task_codes': list(updates), 'phase': args.phase, 'token_usage': None,
        'token_usage_note': 'No model-token usage exposed by available agent tools'}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'counts': result['counts'], 'tasks': list(updates)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
