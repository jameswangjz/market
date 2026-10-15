"""Installed mock Store smoke with a temporary DB; no real tenants or OAuth calls."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile

from app.store import MachineOperationError, Store


def main():
    with tempfile.TemporaryDirectory(prefix='receipt-smoke-') as directory:
        path = Path(directory) / 'state.sqlite3'
        store = Store(path)
        claims = {'active': True, 'product_id': 'isolated-product',
                  'scope': 'saas.tenant saas.user saas.department'}
        identity = dict(tenant_id='isolated-tenant', product_id='isolated-product',
                        enterprise_id='isolated-enterprise', subscription_id='isolated-subscription')

        def call(operation, **values):
            return store.machine_operation(dict(type=operation, **identity, **values), claims)

        call('OPEN', version='v1')
        body = dict(to_version='v2', change_id='isolated-change', direction='target',
                    idempotency_key='isolated-target')
        first = call('CHANGE', **body)
        assert datetime.fromisoformat(first['effective_at']).utcoffset() == timedelta(0)
        assert call('CHANGE', **body) == first
        store = Store(path)
        assert call('CHANGE', **body) == first
        for values in (dict(body, to_version='v3'), dict(body, idempotency_key='other-key')):
            try:
                call('CHANGE', **values)
            except MachineOperationError as exc:
                assert exc.status == 409
            else:
                raise AssertionError('Conflicting version change was accepted')
        fields = ('change_id', 'direction', 'effective_at')
        call('USER_ASSIGN', user_id='isolated-user')
        assert {k: call('STATUS')[k] for k in fields} == {k: first[k] for k in fields}
        restored = call('CHANGE', **dict(body, to_version='v1', direction='original',
                                       idempotency_key='isolated-original'))
        assert call('CHANGE', **body) == first
        assert {k: call('STATUS')[k] for k in fields} == {k: restored[k] for k in fields}
    print(json.dumps({'status': 'passed', 'cases': 7,
        'scope': 'installed mock Store and temporary SQLite only',
        'live_tenant_writes': 0, 'platform_database_writes': 0,
        'limits': 'Synthetic trusted claims; not HTTP OAuth, real supplier or bank validation'}))


if __name__ == '__main__':
    main()
