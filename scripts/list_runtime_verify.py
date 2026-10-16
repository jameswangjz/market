"""Read-only installed-list smoke checks and protected-record fingerprints."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('before', 'after'), required=True)
    p.add_argument('--baseline', type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--browser-token', type=Path)
    args = p.parse_args()
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    import httpx
    from sqlalchemy import select
    with os.fdopen(fd, 'w') as out, m.SessionLocal() as db:
        order = db.scalar(select(m.Order).where(m.Order.order_no == 'ORD-261012005144E1B3'))
        if order is None:
            raise ValueError('Protected order missing')
        protected = {}
        for row in (order, db.get(m.Product, order.product_id)):
            if row is None:
                raise ValueError('Protected product missing')
            value = {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
            protected[row.__tablename__] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        result = {'captured_at': m.now().isoformat(), 'mode': args.mode, 'protected': protected,
                  'checks': [], 'skipped': [], 'status': 'passed'}
        if args.mode == 'after':
            if not args.baseline or json.loads(args.baseline.read_text())['protected'] != protected:
                raise ValueError('Protected records differ')
            admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
            if not admin or not admin.is_active or admin.platform_role != 'super_admin':
                raise ValueError('Active platform administrator required')
            if args.browser_token:
                token_fd = os.open(args.browser_token, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(token_fd, 'w') as secret:
                    secret.write(m.issue_token(admin))
            with httpx.Client(base_url='http://127.0.0.1:8000/api', timeout=30,
                              headers={'Authorization': 'Bearer ' + m.issue_token(admin)}) as client:
                def get(path, **params):
                    r = client.get(path, params=params)
                    r.raise_for_status()
                    data = r.json()
                    if not isinstance(data.get('items'), list) or type(data.get('total')) is not int:
                        raise ValueError('Invalid deployed pagination contract')
                    if len(data['items']) > data['page_size'] or data['page'] < 1:
                        raise ValueError('Invalid page bounds')
                    return data
                cases = [('/products', 'product_name'), ('/orders', 'order_no'),
                         ('/delivery-tasks', 'task_id'), ('/after-sales', 'ticket_id'), ('/refunds', 'refund_id')]
                for path, name in cases:
                    first = get(path, page=1, page_size=10)
                    empty = get(path, page=1, page_size=10, **{name: '__LIST_NO_MATCH_20261016__'})
                    if empty['total'] != 0 or empty['items']:
                        raise ValueError('Filter was ignored: ' + path)
                    result['checks'].append({'path': path, 'page_size': 10, 'total': first['total'], 'empty_filter': True})
                published = get('/gateway/routes', page=1, page_size=10)
                if any(row['product_status'] != 'published' for row in published['items']):
                    raise ValueError('Gateway published default invalid')
                history = get('/gateway/routes', page=1, page_size=10, product_status='all')
                if history['total'] < published['total']:
                    raise ValueError('Gateway history scope invalid')
                result['checks'].append({'path': '/gateway/routes', 'published': published['total'], 'history': history['total']})
                for route in history['items'][:2]:
                    for suffix in ('stats', 'revisions', 'alerts'):
                        get('/gateway/routes/' + route['id'] + '/' + suffix, page=1, page_size=10)
                        result['checks'].append({'route_id': route['id'], 'detail': suffix, 'http': 200})
                tasks = client.get('/development/tasks')
                tasks.raise_for_status()
                rows = [r for r in tasks.json()['items'] if r['code'].startswith('LIST-')]
                if len(rows) != 12:
                    raise ValueError('Task import incomplete')
                result['checks'].append({'development_list_tasks': len(rows)})
            result['checks'].append({'protected_records_unchanged': True})
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'status': result['status'], 'checks': len(result['checks'])}))


if __name__ == '__main__':
    main()
