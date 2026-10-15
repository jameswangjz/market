"""Installed mock Store smoke with a temporary DB; no real tenants or OAuth calls."""
from datetime import datetime, timedelta, timezone
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

        expiry = datetime.now(timezone.utc) + timedelta(days=20, microseconds=123)
        call('OPEN', version='v1', expires_at=expiry.isoformat())
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
        shortened = expiry - timedelta(days=2, microseconds=123)
        shortening = dict(action='refund_shorten', protocol='refund_shorten_v1',
            refund_id='isolated-refund', idempotency_key='refund-shorten:isolated-refund',
            expected_expires_at=expiry.isoformat(), expires_at=shortened.isoformat())
        receipt = call('RENEW', **shortening)
        assert call('RENEW', **shortening) == receipt
        store = Store(path)
        assert call('RENEW', **shortening) == receipt
        status = call('STATUS')
        assert status['refund_shorten_supported'] is True and status['refund_id'] == 'isolated-refund'
        assert datetime.fromisoformat(status['expires_at']) == shortened
        assert {k: status[k] for k in fields} == {k: restored[k] for k in fields}
        try:
            call('RENEW', **dict(shortening, expires_at=expiry.isoformat()))
        except MachineOperationError as exc:
            assert exc.status == 409
        else:
            raise AssertionError('Conflicting refund shortening accepted')
    print(json.dumps({'status': 'passed', 'cases': 10,
        'scope': 'installed mock Store and temporary SQLite only',
        'live_tenant_writes': 0, 'platform_database_writes': 0,
        'limits': 'Synthetic trusted claims; not HTTP OAuth, real supplier or bank validation'}))


if __name__ == '__main__':
    main()
