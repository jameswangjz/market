"""Private SQLite state shared by machine operations and the OIDC RP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from datetime import datetime, timezone
import calendar


class MachineOperationError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


OPERATION_SCOPES = {**dict.fromkeys(('OPEN', 'CLOSE', 'RENEW', 'CHANGE'), 'saas.tenant'),
                    'STATUS': 'saas.tenant',
                    **dict.fromkeys(('USER_ASSIGN', 'USER_UNASSIGN'), 'saas.user'),
                    **dict.fromkeys(('DEPT_CREATE', 'DEPT_REMOVE'), 'saas.department')}


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Private database must be a regular file')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id));
            CREATE TABLE IF NOT EXISTS departments (
                tenant_id TEXT NOT NULL, department_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (tenant_id, department_id));
            CREATE TABLE IF NOT EXISTS flows (
                state_hash TEXT PRIMARY KEY, browser_hash TEXT UNIQUE NOT NULL,
                client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
                enterprise_id TEXT NOT NULL, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
                expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS mappings (
                client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
                sub TEXT NOT NULL, tenant_id TEXT NOT NULL, enterprise_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL, PRIMARY KEY (client_id, sub, tenant_id));
            CREATE TABLE IF NOT EXISTS sessions (
                session_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
                identity TEXT NOT NULL, access_token TEXT NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS machine_operations (
                product_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                response TEXT NOT NULL, PRIMARY KEY(product_id, idempotency_key));
        ''')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def client(self, client_id):
        with self.connect() as db:
            row = db.execute('SELECT config FROM clients WHERE client_id = ?', (client_id,)).fetchone()
            return json.loads(row['config']) if row else None

    def consume_flow(self, state, cookie):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM flows WHERE state_hash = ? AND browser_hash = ?',
                             (hashed(state), hashed(cookie))).fetchone()
            if not row or row['consumed'] or row['expires_at'] <= int(time.time()):
                return None
            db.execute('UPDATE flows SET consumed = 1 WHERE state_hash = ?', (row['state_hash'],))
            return dict(row)

    def tenant(self, tenant_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM tenants WHERE tenant_id = ?', (tenant_id,)).fetchone()
            return json.loads(row['payload']) if row else None

    def machine_operation(self, body, token):
        operation = body.get('type')
        if not isinstance(operation, str):
            raise MachineOperationError(400, 'invalid_operation')
        product_id = token.get('product_id')
        scope = token.get('scope')
        if token.get('active') is not True or not isinstance(product_id, str) or not product_id:
            raise MachineOperationError(403, 'machine_product_required')
        required = OPERATION_SCOPES.get(operation)
        if required and (not isinstance(scope, str) or required not in scope.split()):
            raise MachineOperationError(403, 'insufficient_scope')
        if not required:
            return {'ret': 1, 'msg': f'unsupported operation: {operation}', 'retryable': False}
        for key in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id', 'oidc_client_id', 'admin_user_id'):
            if key in body and (not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > 180):
                raise MachineOperationError(400, 'invalid_identity')
        if body.get('product_id', product_id) != product_id:
            raise MachineOperationError(403, 'machine_product_mismatch')
        if operation == 'USER_ASSIGN':
            for key in ('name', 'email', 'external_user_id'):
                if key in body and (not isinstance(body[key], str) or len(body[key]) > 500):
                    raise MachineOperationError(400, 'invalid_user_field')
            if 'department_ids' in body and (not isinstance(body['department_ids'], list) or
                    any(not isinstance(value, str) or not value or len(value) > 180 for value in body['department_ids'])):
                raise MachineOperationError(400, 'invalid_department_ids')
        idem = body.get('idempotency_key')
        if idem is not None and (not isinstance(idem, str) or not idem.strip() or len(idem) > 180):
            raise MachineOperationError(400, 'invalid_idempotency_key')
        if operation == 'STATUS':
            for key in ('tenant_id', 'enterprise_id', 'subscription_id'):
                if not isinstance(body.get(key), str) or not body[key].strip():
                    raise MachineOperationError(400, 'invalid_identity')
            with self.connect() as db:
                row = db.execute('SELECT payload FROM tenants WHERE tenant_id = ?', (body['tenant_id'],)).fetchone()
                if not row:
                    raise MachineOperationError(404, 'tenant_not_found')
                tenant = json.loads(row['payload'])
                if tenant.get('product_id') != product_id:
                    raise MachineOperationError(403, 'tenant_product_mismatch')
                if any(tenant.get(k) != body[k] for k in ('enterprise_id', 'subscription_id')):
                    raise MachineOperationError(409, 'tenant_identity_mismatch')
                client_id = body.get('oidc_client_id')
                client = db.execute('SELECT config FROM clients WHERE client_id = ?', (client_id,)).fetchone() if client_id else None
                matches = bool(client and json.loads(client['config']).get('product_id') == product_id)
                admin_id = body.get('admin_user_id')
                user = db.execute('SELECT payload FROM users WHERE tenant_id = ? AND user_id = ?',
                                  (body['tenant_id'], admin_id)).fetchone() if admin_id else None
                admin_active = bool(user and json.loads(user['payload']).get('status') == 'active')
                return {'ret': 0, 'tenant_id': tenant['tenant_id'], 'product_id': product_id,
                        'enterprise_id': tenant['enterprise_id'], 'subscription_id': tenant['subscription_id'],
                        'version': tenant.get('version'), 'status': tenant.get('status'),
                        'expires_at': tenant.get('expires_at'), 'admin_user_id': admin_id if admin_active else None,
                        'admin_active': admin_active, 'application_ready': bool(tenant.get('login_open')
                        and tenant.get('status') != 'closed' and matches),
                        'oidc_client_id': client_id if matches else None}
        tenant_id = body.get('tenant_id')
        if not tenant_id:
            if operation != 'OPEN' or not body.get('subscription_id'):
                raise MachineOperationError(400, 'tenant_required')
            tenant_id = 'tenant-' + body['subscription_id'][:12]
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        field = 'user_id' if required == 'saas.user' else 'department_id' if required == 'saas.department' else None
        if field and (not isinstance(body.get(field), str) or not body[field].strip() or len(body[field]) > 180):
            raise MachineOperationError(400, 'invalid_operation_identity')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM tenants WHERE tenant_id = ?', (tenant_id,)).fetchone()
            if not row and operation != 'OPEN':
                raise MachineOperationError(404, 'tenant_not_found')
            if row:
                existing = json.loads(row['payload'])
                if existing.get('product_id') != product_id:
                    raise MachineOperationError(403, 'tenant_product_mismatch')
                if operation == 'OPEN' and any(not existing.get(key)
                        for key in ('product_id', 'enterprise_id', 'subscription_id')):
                    raise MachineOperationError(400, 'open_identity_required')
                if any(key in body and existing.get(key) != body[key]
                       for key in ('enterprise_id', 'subscription_id')):
                    raise MachineOperationError(409, 'tenant_identity_mismatch')
            if idem:
                cached = db.execute('SELECT request_hash, response FROM machine_operations WHERE product_id=? AND idempotency_key=?',
                                    (product_id, idem)).fetchone()
                if cached:
                    if cached['request_hash'] != request_hash:
                        raise MachineOperationError(409, 'idempotency_key_reused')
                    return json.loads(cached['response'])
            if not row and any(not body.get(key) for key in ('product_id', 'enterprise_id', 'subscription_id')):
                raise MachineOperationError(400, 'open_identity_required')
            tenant = json.loads(row['payload']) if row else {'tenant_id': tenant_id, 'login_open': False}
            if row and tenant.get('status') == 'closed' and operation != 'OPEN':
                raise MachineOperationError(409, 'tenant_closed')
            if operation == 'OPEN':
                if row and existing.get('status') == 'closed':
                    until = existing.get('retention_until')
                    if not until or datetime.fromisoformat(until) <= datetime.now(timezone.utc):
                        raise MachineOperationError(410, 'tenant_retention_expired')
                if not row or existing.get('status') == 'closed':
                    tenant.update({key: body[key] for key in ('product_id', 'subscription_id', 'enterprise_id',
                                  'enterprise_name', 'tenant_name', 'version') if key in body})
                if not row or 'expires_at' in body or 'subscription_months' in body:
                    requested = body.get('expires_at')
                    if requested is not None:
                        if not isinstance(requested, str):
                            raise MachineOperationError(400, 'invalid_expires_at')
                        try:
                            expiry = datetime.fromisoformat(requested.replace('Z', '+00:00'))
                        except ValueError:
                            raise MachineOperationError(400, 'invalid_expires_at') from None
                        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                            raise MachineOperationError(400, 'invalid_expires_at')
                        tenant['expires_at'] = expiry.astimezone(timezone.utc).isoformat()
                    elif 'subscription_months' in body:
                        months = body['subscription_months']
                        if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 36:
                            raise MachineOperationError(400, 'invalid_subscription_months')
                        base = datetime.now(timezone.utc)
                        month = base.month - 1 + months
                        year = base.year + month // 12
                        month = month % 12 + 1
                        expiry = base.replace(year=year, month=month, day=min(base.day, calendar.monthrange(year, month)[1]))
                        tenant['expires_at'] = expiry.isoformat()
                if not row or existing.get('status') == 'closed':
                    tenant.update(status='opened', login_open=True)
                tenant['app_id'] = tenant.get('app_id', 'app-' + tenant_id[7:])
                tenant.pop('retention_until', None)
                result = {'ret': 0, 'msg': 'ok', 'tenant_id': tenant_id, 'product_id': tenant['product_id'],
                          'enterprise_id': tenant['enterprise_id'], 'subscription_id': tenant['subscription_id'],
                          'app_id': tenant['app_id'], 'version': tenant.get('version'), 'status': tenant.get('status'),
                          'url': f'https://mock-saas.local/t/{tenant_id}'}
            elif operation == 'CLOSE':
                from datetime import timedelta
                tenant.update(status='closed', login_open=False,
                              closed_at=datetime.now(timezone.utc).isoformat(),
                              retention_until=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
                result = {'ret': 0, 'tenant_id': tenant_id, 'status': 'closed'}
            elif operation == 'CHANGE':
                version = body.get('to_version')
                if (not isinstance(version, (str, int)) or isinstance(version, bool) or
                        not str(version).strip() or isinstance(version, int) and version <= 0):
                    raise MachineOperationError(400, 'invalid_version')
                tenant.update(version=version)
                result = {'ret': 0, 'tenant_id': tenant_id, 'version': tenant['version'], 'status': 'active'}
            elif operation in {'USER_ASSIGN', 'USER_UNASSIGN'}:
                uid = body.get('user_id', '')
                status = 'active' if operation == 'USER_ASSIGN' else 'deleted'
                external = 'mock-user-' + uid[:12]
                if operation == 'USER_UNASSIGN':
                    matches = db.execute('SELECT user_id, payload FROM users WHERE tenant_id = ?',
                                         (tenant_id,)).fetchall()
                    resolved = next((r for r in matches if r['user_id'] == uid
                        or json.loads(r['payload']).get('external_user_id') == uid), None)
                    if resolved:
                        uid = resolved['user_id']
                        external = json.loads(resolved['payload'])['external_user_id']
                    elif uid.startswith('mock-user-'):
                        result = {'ret': 0, 'tenant_id': tenant_id, 'user_id': uid, 'status': status}
                        if idem:
                            db.execute('INSERT INTO machine_operations VALUES (?, ?, ?, ?)',
                                       (product_id, idem, request_hash, json.dumps(result, separators=(',', ':'))))
                        return result
                if operation == 'USER_ASSIGN':
                    external = body.get('external_user_id', external)
                payload = {**body, 'user_id': uid, 'external_user_id': external, 'status': status}
                db.execute('INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(tenant_id, user_id) '
                           'DO UPDATE SET payload = excluded.payload', (tenant_id, uid, json.dumps(payload)))
                result = {'ret': 0, 'tenant_id': tenant_id, 'product_id': product_id,
                          'enterprise_id': tenant.get('enterprise_id'),
                          'subscription_id': tenant.get('subscription_id'),
                          'user_id': external if status == 'active' else body.get('user_id'),
                          'external_user_id': external if status == 'active' else None,
                          'platform_user_id': body.get('user_id'), 'status': status}
                if idem:
                    db.execute('INSERT INTO machine_operations VALUES (?, ?, ?, ?)',
                               (product_id, idem, request_hash, json.dumps(result, separators=(',', ':'))))
                return result
            elif operation in {'DEPT_CREATE', 'DEPT_REMOVE'}:
                did = body.get('department_id', '')
                status = 'active' if operation == 'DEPT_CREATE' else 'deleted'
                if operation == 'DEPT_REMOVE':
                    found = db.execute('SELECT department_id, payload FROM departments WHERE tenant_id=?', (tenant_id,)).fetchall()
                    resolved = next((r for r in found if r['department_id'] == did or
                                     json.loads(r['payload']).get('external_department_id') == did), None)
                    if resolved:
                        did = resolved['department_id']
                    elif did.startswith('mock-dept-'):
                        result = {'ret': 0, 'tenant_id': tenant_id, 'department_id': body['department_id'], 'status': status}
                        if idem:
                            db.execute('INSERT INTO machine_operations VALUES (?, ?, ?, ?)',
                                       (product_id, idem, request_hash, json.dumps(result, separators=(',', ':'))))
                        return result
                department_payload = {**body, 'status': status}
                if operation == 'DEPT_CREATE':
                    department_payload['external_department_id'] = body.get(
                        'external_department_id', 'mock-dept-' + did[:12])
                db.execute('INSERT INTO departments VALUES (?, ?, ?) ON CONFLICT(tenant_id, department_id) '
                           'DO UPDATE SET payload = excluded.payload',
                           (tenant_id, did, json.dumps(department_payload)))
                result = {'ret': 0, 'tenant_id': tenant_id, 'department_id': 'mock-dept-' + did[:12]
                          if status == 'active' else body['department_id'], 'status': status}
                if idem:
                    db.execute('INSERT INTO machine_operations VALUES (?, ?, ?, ?)',
                               (product_id, idem, request_hash, json.dumps(result, separators=(',', ':'))))
                return result
            elif operation == 'RENEW':
                if tenant.get('status') == 'closed':
                    raise MachineOperationError(409, 'tenant_closed')
                requested = body.get('expires_at')
                if requested is not None:
                    if not isinstance(requested, str):
                        raise MachineOperationError(400, 'invalid_expires_at')
                    try:
                        expiry = datetime.fromisoformat(requested.replace('Z', '+00:00'))
                    except ValueError:
                        raise MachineOperationError(400, 'invalid_expires_at') from None
                    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                        raise MachineOperationError(400, 'invalid_expires_at')
                    current = tenant.get('expires_at')
                    if current:
                        old = datetime.fromisoformat(current.replace('Z', '+00:00'))
                        if old.tzinfo is None:
                            old = old.replace(tzinfo=timezone.utc)
                        if expiry < old:
                            raise MachineOperationError(409, 'renewal_would_shorten')
                else:
                    months = body.get('subscription_months')
                    if months is not None and (not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 36):
                        raise MachineOperationError(400, 'invalid_subscription_months')
                    current = tenant.get('expires_at')
                    try:
                        base = datetime.fromisoformat(current.replace('Z', '+00:00')) if current else datetime.now(timezone.utc)
                    except ValueError:
                        base = datetime.now(timezone.utc)
                    if base.tzinfo is None:
                        base = base.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if base <= now:
                        base = now
                    if months is None:
                        cycle = body.get('billing_cycle', 'monthly')
                        months = {'monthly': 1, 'quarterly': 3, 'annual': 12, 'yearly': 12}.get(cycle)
                        if cycle == 'perpetual':
                            expiry = None
                        elif months is None:
                            raise MachineOperationError(400, 'invalid_billing_cycle')
                    if months is not None:
                        month = base.month - 1 + months
                        year = base.year + month // 12
                        month = month % 12 + 1
                        expiry = base.replace(year=year, month=month, day=min(base.day, calendar.monthrange(year, month)[1]))
                tenant['expires_at'] = expiry.astimezone(timezone.utc).isoformat() if expiry is not None else None
                tenant['status'] = 'active'
                result = {'ret': 0, 'tenant_id': tenant_id, 'product_id': product_id,
                          'enterprise_id': tenant.get('enterprise_id'),
                          'subscription_id': tenant.get('subscription_id'), 'status': 'active',
                          'starts_at': body.get('starts_at'), 'expires_at': tenant['expires_at'],
                          'platform_delivery_at': body.get('platform_delivery_at')}
            else:
                return {'ret': 1, 'msg': f'unsupported operation: {operation}', 'retryable': False}
            db.execute('INSERT INTO tenants VALUES (?, ?) ON CONFLICT(tenant_id) '
                       'DO UPDATE SET payload = excluded.payload', (tenant_id, json.dumps(tenant)))
            if idem:
                result['idempotency_key'] = idem
                db.execute('INSERT INTO machine_operations VALUES (?, ?, ?, ?)',
                           (product_id, idem, request_hash, json.dumps(result, separators=(',', ':'))))
            return result
