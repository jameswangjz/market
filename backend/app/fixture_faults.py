"""Explicit, immutable QA scope and non-forwarding fixture fault endpoint."""
from dataclasses import dataclass
import hmac
import json
import os
import re
import stat
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTECTED_ORDER = 'ORD-261012005144E1B3'


@dataclass(frozen=True)
class FixtureScope:
    run_id: str
    change_id: str
    order_id: str
    product_id: str
    enterprise_id: str
    combination_id: str

    def __post_init__(self):
        if not re.fullmatch(r'qa-vcs-[0-9a-f]{12}', self.run_id):
            raise ValueError('invalid QA ownership root')
        if not re.fullmatch(r'[0-9a-f]{32}', self.change_id):
            raise ValueError('invalid exact change ID')
        if self.product_id not in {self.run_id + '-p-api', self.run_id + '-p-model'}:
            raise ValueError('product is outside exact fixture allowlist')
        if self.enterprise_id != self.run_id + '-e-buyer':
            raise ValueError('enterprise is outside exact fixture allowlist')
        for value in (self.order_id, self.combination_id):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
                raise ValueError('invalid exact database ID')
        if self.order_id == PROTECTED_ORDER:
            raise ValueError('protected order cannot be selected')

    @classmethod
    def load(cls, path):
        # Walk via directory descriptors so symlinked parents cannot redirect reads.
        parts = os.path.abspath(path).split(os.sep)
        directory = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.geteuid() or before.st_nlink != 1
                    or before.st_size > 4096):
                raise ValueError('manifest must be an owned regular 0600 file without links')
            raw = handle.read(4097)
            after = os.fstat(handle.fileno())
            stable_fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink',
                             'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (any(getattr(before, field) != getattr(after, field) for field in stable_fields)
                    or len(raw) > 4096):
                raise ValueError('manifest changed while loading')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('scope requires exactly the declared fields')
        return cls(**value)

    def predicate(self, ns, Request):
        from sqlalchemy import select, or_
        Order = ns['Order']
        Combo = ns['subscription_terms']['Combination']
        Term = ns['subscription_terms']['Term']
        protected_products = select(Order.product_id).where(or_(
            Order.id == PROTECTED_ORDER, Order.order_no == PROTECTED_ORDER))
        orders = select(Order.id).where(
            Order.id == self.order_id, Order.order_no != PROTECTED_ORDER,
            Order.product_id == self.product_id,
            Order.buyer_enterprise_id == self.enterprise_id,
            Order.product_id.not_in(protected_products))
        combinations = select(Combo.id).where(
            Combo.id == self.combination_id, Combo.product_id == self.product_id,
            Combo.enterprise_id == self.enterprise_id,
            select(Term.id).where(Term.order_id == self.order_id,
                                  Term.combination_id == Combo.id,
                                  Term.generation == Combo.generation).exists())
        return (Request.id == self.change_id) & (Request.source_order_id.in_(orders)) & (
            Request.enterprise_id == self.enterprise_id) & (Request.combination_id.in_(combinations))


def fault_server(scope, secret, *, mode='unavailable', delay=0, port=0):
    """Loopback only; no forwarding, URL parameters, admin API, or secret logging."""
    if not isinstance(scope, FixtureScope):
        raise ValueError('explicit fixture scope required')
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError('fixture auth secret must contain at least 32 characters')
    if mode not in {'healthy', 'unavailable', 'timeout'} or not 0 <= delay <= 30:
        raise ValueError('invalid bounded fault mode')
    if mode == 'timeout' and delay == 0:
        raise ValueError('timeout requires a positive delay')
    expected_path = '/fixtures/' + scope.run_id + '/' + scope.change_id + '/health'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not hmac.compare_digest(
                    auth[0].encode(), ('Bearer ' + secret).encode()):
                self.send_error(401)
                return
            if self.path != expected_path:
                self.send_error(404)
                return
            if mode == 'timeout':
                time.sleep(delay)
            payload = json.dumps({'fixture': scope.run_id, 'change_id': scope.change_id,
                                  'synthetic': True, 'mode': mode}).encode()
            self.send_response(200 if mode == 'healthy' else 503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
