"""Private SQLite state shared by machine operations and the OIDC RP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time


class MachineOperationError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


OPERATION_SCOPES = {**dict.fromkeys(('OPEN', 'CLOSE', 'RENEW', 'CHANGE'), 'saas.tenant'),
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
        for key in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id'):
            if key in body and (not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > 180):
                raise MachineOperationError(400, 'invalid_identity')
        if body.get('product_id', product_id) != product_id:
            raise MachineOperationError(403, 'machine_product_mismatch')
        tenant_id = body.get('tenant_id')
        if not tenant_id:
            if operation != 'OPEN' or not body.get('subscription_id'):
                raise MachineOperationError(400, 'tenant_required')
            tenant_id = 'tenant-' + body['subscription_id'][:12]
        field = 'user_id' if required == 'saas.user' else 'department_id' if required == 'saas.department' else None
        if field and (not isinstance(body.get(field), str) or not body[field].strip() or len(body[field]) > 180):
            raise MachineOperationError(400, 'invalid_operation_identity')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM tenants WHERE tenant_id = ?', (tenant_id,)).fetchone()
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
            elif operation != 'OPEN':
                raise MachineOperationError(404, 'tenant_not_found')
            elif any(not body.get(key) for key in ('product_id', 'enterprise_id', 'subscription_id')):
                raise MachineOperationError(400, 'open_identity_required')
            tenant = json.loads(row['payload']) if row else {'tenant_id': tenant_id, 'login_open': False}
            if operation == 'OPEN':
                tenant.update({key: body[key] for key in ('product_id', 'subscription_id', 'enterprise_id',
                              'enterprise_name', 'tenant_name', 'version') if key in body})
                tenant.update(status='opened', login_open=True, app_id=tenant.get('app_id', 'app-' + tenant_id[7:]))
                result = {'ret': 0, 'msg': 'ok', 'tenant_id': tenant_id, 'app_id': tenant['app_id'],
                          'version': tenant.get('version'), 'status': 'opened',
                          'url': f'https://mock-saas.local/t/{tenant_id}'}
            elif operation == 'CLOSE':
                tenant.update(status='closed', login_open=False)
                result = {'ret': 0, 'tenant_id': tenant_id, 'status': 'closed'}
            elif operation == 'CHANGE':
                tenant.update(version=body.get('to_version'), status='active')
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
                        return {'ret': 0, 'tenant_id': tenant_id, 'user_id': uid, 'status': status}
                payload = {'user_id': uid, 'external_user_id': external, 'status': status}
                db.execute('INSERT INTO users VALUES (?, ?, ?) ON CONFLICT(tenant_id, user_id) '
                           'DO UPDATE SET payload = excluded.payload', (tenant_id, uid, json.dumps(payload)))
                return {'ret': 0, 'tenant_id': tenant_id, 'user_id': external if status == 'active'
                        else body.get('user_id'), 'status': status}
            elif operation in {'DEPT_CREATE', 'DEPT_REMOVE'}:
                did = body.get('department_id', '')
                status = 'active' if operation == 'DEPT_CREATE' else 'deleted'
                db.execute('INSERT INTO departments VALUES (?, ?, ?) ON CONFLICT(tenant_id, department_id) '
                           'DO UPDATE SET payload = excluded.payload',
                           (tenant_id, did, json.dumps({**body, 'status': status})))
                return {'ret': 0, 'tenant_id': tenant_id, 'department_id': 'mock-dept-' + did[:12]
                        if status == 'active' else did, 'status': status}
            elif operation == 'RENEW':
                from datetime import datetime, timezone

                tenant.update(status='active', expires_at=datetime.now(timezone.utc).isoformat())
                result = {'ret': 0, 'tenant_id': tenant_id, 'status': 'active', 'expires_at': tenant['expires_at']}
            else:
                return {'ret': 1, 'msg': f'unsupported operation: {operation}', 'retryable': False}
            db.execute('INSERT INTO tenants VALUES (?, ?) ON CONFLICT(tenant_id) '
                       'DO UPDATE SET payload = excluded.payload', (tenant_id, json.dumps(tenant)))
            return result
