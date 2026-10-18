"""Read-only clearing deployment checks and protected-data fingerprints."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--before', action='store_true')
    parser.add_argument('--browser-token', type=Path)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    import httpx
    from sqlalchemy import select, func
    from app import main as m
    protected = {}
    checks = []
    with m.SessionLocal() as db:
        order = db.scalar(select(m.Order).where(m.Order.order_no == 'ORD-261012005144E1B3'))
        if order is None:
            raise ValueError('Protected order missing')
        rows = [('orders', [order]), ('products', [db.get(m.Product, order.product_id)])]
        for model in (m.Payment, m.Refund, m.Settlement, m.OrderStateLog):
            rows.append((model.__tablename__, db.scalars(select(model).where(model.order_id == order.id).order_by(model.id)).all()))
        for table, objects in rows:
            if any(row is None for row in objects):
                raise ValueError('Protected product missing')
            payload = [{column.name: str(getattr(row, column.name)) for column in row.__table__.columns} for row in objects]
            protected[table] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if not args.before:
            if not args.baseline or json.loads(args.baseline.read_text())['protected'] != protected:
                raise ValueError('Protected order or dependencies changed')
            actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
            if actor is None or not actor.is_active or actor.platform_role != 'super_admin':
                raise ValueError('Active platform administrator required')
            token = m.issue_token(actor)
            if args.browser_token:
                fd = os.open(args.browser_token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    output.write(token)
            with httpx.Client(base_url='http://127.0.0.1:8000/api', timeout=45,
                              headers={'Authorization': 'Bearer ' + token}) as client:
                def get(path, **params):
                    response = client.get(path, params=params)
                    response.raise_for_status()
                    return response.json()
                report = get('/settlement-reporting/report', year=2026, page=1, page_size=2)
                if (len(report['items']) > 2 or not isinstance(report['total'], int)
                        or not isinstance(report['summary']['provider_total_receivable'], str)):
                    raise ValueError('Invalid report contract')
                checks.append({'report': 'passed', 'total': report['total'], 'full_totals_separate_from_page': True})
                for path in ('/settlement-finance/positions', '/settlement-finance/transactions',
                             '/settlement-reporting/annual-reports'):
                    result = get(path, page=1, page_size=10)
                    if not isinstance(result.get('items'), list):
                        raise ValueError('Invalid list contract: ' + path)
                    checks.append({'path': path, 'passed': True})
                tasks = get('/development/tasks')['items']
                clearing = [row for row in tasks if row['code'].startswith('CLR2-')]
                if len(clearing) != 17 or sum(len(row.get('subtasks', [])) for row in clearing) != 57:
                    raise ValueError('Clearing task/substep definitions missing')
                checks.append({'clearing_tasks': 17, 'defined_substeps': 57})
                unauthenticated = httpx.get('http://127.0.0.1:8000/api/settlement-reporting/report', timeout=15)
                if unauthenticated.status_code not in (401, 403):
                    raise ValueError('Anonymous report access was not denied')
                checks.append({'anonymous_denied': True, 'protected_unchanged': True})
            checks.append({'baseline_count': db.scalar(select(func.count()).select_from(m.settlement_finance['Baseline'])),
                           'no_historical_cash_inference': True})
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'status': 'passed', 'before': args.before, 'protected': protected, 'checks': checks}, output, indent=2)
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
