"""Read-only installed SLA-console smoke checks; writes evidence, never business data."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--baseline', type=Path)
    p.add_argument('--before', action='store_true')
    p.add_argument('--browser-token', type=Path)
    args = p.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    import httpx
    from sqlalchemy import select
    from app import main as m
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out, m.SessionLocal() as db:
        order = db.scalar(select(m.Order).where(m.Order.order_no == 'ORD-261012005144E1B3'))
        if order is None:
            raise ValueError('Protected order missing')
        protected = {}
        for row in (order, db.get(m.Product, order.product_id)):
            if row is None:
                raise ValueError('Protected product missing')
            fields = {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
            protected[row.__tablename__] = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        checks = []
        if not args.before:
            if not args.baseline or json.loads(args.baseline.read_text())['protected'] != protected:
                raise ValueError('Protected business data changed')
            actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
            if actor is None or not actor.is_active or actor.platform_role != 'super_admin':
                raise ValueError('Active platform administrator required')
            if args.browser_token:
                token_fd = os.open(args.browser_token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(token_fd, 'w') as secret:
                    secret.write(m.issue_token(actor))
            with httpx.Client(base_url='http://127.0.0.1:8000/api', timeout=30,
                              headers={'Authorization': 'Bearer ' + m.issue_token(actor)}) as client:
                def get(path, **params):
                    response = client.get(path, params=params)
                    response.raise_for_status()
                    return response.json()

                matrices = get('/sla/matrices')['items']
                if [row['code'] for row in matrices[:3]] != ['standard', 'enterprise', 'strategic']:
                    raise ValueError('Built-in matrix order incorrect')
                for row in matrices:
                    if len(row['metrics']) < 14 or row['editable_fields'] != ['target', 'warning']:
                        raise ValueError('Incomplete matrix contract')
                checks.append({'matrix_count': len(matrices), 'complete': True})
                for path, field in [('/users', 'q'), ('/admin/enterprises', 'q'),
                                    ('/sla/enterprise-bindings', 'q')]:
                    data = get(path, page=1, page_size=10)
                    empty = get(path, page=1, page_size=10, **{field: '__SLC_NO_MATCH_20261016__'})
                    if not isinstance(data.get('total'), int) or len(data['items']) > 10:
                        raise ValueError('Invalid page contract: ' + path)
                    if empty['total'] != 0 or empty['items']:
                        raise ValueError('Filter ignored: ' + path)
                    checks.append({'path': path, 'total': data['total'], 'empty_filter': True})
                get('/sla/overview')
                checks.append({'overview': 200, 'protected_unchanged': True})
        result = {'captured_at': m.now().isoformat(), 'before': args.before,
                  'protected': protected, 'checks': checks, 'status': 'passed'}
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
