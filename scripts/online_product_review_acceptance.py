"""QA003 companion: actual API/model-serving API/SaaS reviews before delivery.

No daemon/global queue, lifespan, migrations, seeds or shared policy changes.
Run only in the API image after parent review. Frozen four-delivery code is read,
not edited. Private journals precede owned DB/storage/mock mutations.
"""
import argparse
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import traceback
from unittest.mock import patch

import four_type_online_delivery_acceptance as f

a, s, g = f.api, f.saas, f.guarded
check = a.check
ROLES = {'business': 'product_manager', 'quality': 'quality_reviewer',
         'security': 'security_compliance', 'release': 'platform_operator'}
STEPS = (('business', 'review', 'pending_review', 'quality_review', 'business_approve_product'),
         ('quality', 'review', 'quality_review', 'security_review', 'quality_approve_product'),
         ('security', 'security-review', 'security_review', 'operation_review', 'approve_product_security'),
         ('release', 'review', 'operation_review', 'pending_integration', 'operation_approve_product'))
STAGE = 'startup'


def roots(marker=None):
    marker = marker or 'qa-review-' + secrets.token_hex(6)
    return {'run_id': marker,
            'reviewers': {label: {role: marker + '-' + label + '-' + role for role in ROLES}
                          for label in ('a0', 'a1', 's')},
            'canonical_versions': {side: marker + '-sv-' + side for side in ('source', 'upgrade', 'downgrade')}}


def write(path, saved, create=False):
    a.write_manifest(path, saved, create=create)


def validate(saved, path):
    marker = saved.get('run_id', '')
    check(isinstance(marker, str) and re.fullmatch(r'qa-review-[0-9a-f]{12}', marker), 'Invalid review marker')
    check(all(saved.get(key) == value for key, value in roots(marker).items()), 'Review roots changed')
    check(set(saved) <= set(roots(marker)) | {'api', 'saas', 'assets', 'keys', 'environment',
          'protected_state', 'authorized', 'cleaned', 'database_ids'}, 'Unknown review journal fields')
    for kind in ('api', 'saas'):
        check(saved[kind] == str(path.with_name(path.name + '.' + kind + '.json')), 'Child path changed')
    f.validate_protected_state(saved['protected_state'])
    check(isinstance(saved.get('environment'), str) and re.fullmatch(r'[0-9a-f]{64}', saved['environment']),
          'Invalid environment fingerprint')
    check(type(saved.get('authorized')) is bool, 'Invalid cleanup authorization flag')
    check(isinstance(saved.get('keys'), list) and isinstance(saved.get('assets'), dict)
          and len(saved['keys']) <= 6 and len(saved['assets']) <= 6, 'Invalid asset journal')
    check(len({item['key'] for item in saved['keys']}) == len(saved['keys']), 'Duplicate asset key')
    for asset in saved['assets'].values():
        check(asset in saved['keys'], 'Asset lacks pre-PUT journal')
    for fid in saved['assets']:
        check(re.fullmatch(r'[0-9a-f]{32}', fid), 'Invalid generated asset ID')


def load_children(saved):
    return a.read_manifest(saved['api']), s.read_manifest(saved['saas'])


def targets(saved, api_ids, saas_ids):
    return {**{label: {'pid': api_ids['products'][index], 'provider': api_ids['users']['seller'],
                      'enterprise': api_ids['enterprises'][1], 'kind': 'api',
                      'version': api_ids['versions'][index], 'tokens_role': 'seller'}
               for index, label in enumerate(('a0', 'a1'))},
            's': {'pid': saas_ids['product'], 'provider': saas_ids['users']['provider'],
                  'enterprise': saas_ids['enterprises']['provider'], 'kind': 'saas',
                  'version': saas_ids['versions']['source'], 'tokens_role': 'provider'}}


def validate_asset_records(saved, target_map):
    for item in saved['keys']:
        check(set(item) == {'key', 'product', 'owner', 'role'}, 'Invalid asset fields')
        label = next((label for label, target in target_map.items() if target['pid'] == item['product']), None)
        check(label is not None and item['owner'] == target_map[label]['provider'] and
              item['role'] in {'product_logo', 'product_logo_thumbnail'}, 'Asset outside review fixture')
        suffix = '.logo.png' + ('.thumb.png' if item['role'] == 'product_logo_thumbnail' else '')
        check(re.fullmatch(re.escape(item['owner']) + r'/[0-9]{8}/[0-9a-f]{12}-' +
              re.escape(saved['run_id'] + '-' + label + suffix), item['key']), 'Unsafe asset key')


def scopes(m, db, saved, api_ids, saas_ids):
    """Reuse FK ownership discovery, then verify every business and inbox edge."""
    from sqlalchemy import select
    target_map = targets(saved, api_ids, saas_ids)
    validate_asset_records(saved, target_map)
    configurations = [
        {'run_id': api_ids['run_id'], 'users': {**api_ids['users'], **{
             label + '_' + role: uid for label in ('a0', 'a1') for role, uid in saved['reviewers'][label].items()}},
         'enterprises': api_ids['enterprises'], 'products': {'a0': api_ids['products'][0], 'a1': api_ids['products'][1]},
         'versions': {'a0': {'v': api_ids['versions'][0]}, 'a1': {'v': api_ids['versions'][1]}},
         'request_ids': [api_ids['request_id']]},
        {'run_id': saas_ids['run_id'], 'users': {**saas_ids['users'], **saved['reviewers']['s']},
         'enterprises': [saas_ids['enterprises']['buyer'], saas_ids['enterprises']['provider']],
         'products': {'s': saas_ids['product']},
         'versions': {'s': {**saas_ids['versions'], **{'canonical_' + k: v for k, v in saved['canonical_versions'].items()}}},
         'request_ids': [saas_ids['request_id']]},
    ]
    owned = {}
    for scope in configurations:
        for name, rows in g.owned_rows(m, db, scope).items():
            owned.setdefault(name, set()).update(rows)
    predicates = {name: g.row_predicate(m.Base.metadata.tables[name], rows) for name, rows in owned.items()}
    users = set(api_ids['users'].values()) | set(saas_ids['users'].values()) | {
        uid for group in saved['reviewers'].values() for uid in group.values()}
    products = {item['pid'] for item in target_map.values()}
    orders = set()
    if m.Order.__tablename__ in predicates:
        for row in db.scalars(select(m.Order).where(predicates[m.Order.__tablename__])):
            check(row.id != s.PROTECTED_ORDER and row.order_no != s.PROTECTED_ORDER and row.product_id in products,
                  'Protected/external order reached review cleanup')
            orders.add(row.id)
    if m.FileObject.__tablename__ in predicates:
        for row in db.scalars(select(m.FileObject).where(predicates[m.FileObject.__tablename__])):
            asset = saved['assets'].get(row.id)
            check(asset is not None and row.product_id == asset['product'] and row.owner_id == asset['owner']
                  and row.object_name == asset['key'] and row.file_role == asset['role'] and row.version_id is None,
                  'Unplanned review file object')
    Message = m.message_center['Message']
    messages = set()
    task_ids = {row[0] for row in owned.get(m.DeliveryTask.__tablename__, set())}
    if Message.__tablename__ in predicates:
        for row in db.scalars(select(Message).where(predicates[Message.__tablename__])):
            if row.target_type == 'product':
                target = next((item for item in target_map.values() if item['pid'] == row.target_id), None)
                check(target and row.category == 'review' and row.tenant_id == target['enterprise'] and
                      row.event_key.startswith('review:product:' + row.target_id + ':'), 'Unowned review message')
            else:
                check(row.target_type in {'order', 'delivery_task'} and row.target_id in
                      (orders if row.target_type == 'order' else task_ids) and row.category == 'order' and
                      row.tenant_id in {api_ids['enterprises'][0], saas_ids['enterprises']['buyer']},
                      'Unowned delivery message')
            messages.add(row.id)
    recipient_tables = {m.message_center[key].__tablename__ for key in ('Receipt', 'Delivery', 'ExpiredUrgent')}
    allowed = {m.User.__tablename__: users,
               m.Enterprise.__tablename__: set(api_ids['enterprises']) | set(saas_ids['enterprises'].values()),
               m.Product.__tablename__: products, m.Order.__tablename__: orders,
               m.FileObject.__tablename__: set(saved['assets']),
               m.ProductReleaseVersion.__tablename__: set(api_ids['versions']) | set(saved['canonical_versions'].values()),
               m.SaaSProductVersion.__tablename__: set(saas_ids['versions'].values())}
    for name, predicate in predicates.items():
        table = m.Base.metadata.tables[name]
        if name in recipient_tables:
            for row in db.execute(select(table).where(predicate)).mappings():
                f.message_edge_ownership(row['message_id'], row['user_id'], messages, users)
                if name == m.message_center['ExpiredUrgent'].__tablename__:
                    receipt = db.get(m.message_center['Receipt'], row['receipt_id'])
                    check(receipt is None or (receipt.message_id == row['message_id'] and receipt.user_id == row['user_id']),
                          'Expired receipt metadata mismatch')
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent not in allowed or (name in recipient_tables and parent == m.User.__tablename__ and fk.parent.name == 'user_id'):
                continue
            check(set(db.scalars(select(fk.parent).where(predicate))) - {None} <= allowed[parent],
                  'Review descendant references unowned business root')
    return predicates


def request(client, token, method, route, expected=200, **kwargs):
    response = client.request(method, '/api' + route, headers={'Authorization': 'Bearer ' + token}, **kwargs)
    check(response.status_code == expected, f'Review {method} {route}: expected {expected}, got {response.status_code}')
    return response


def audit_step(m, db, pid, actor, action, before, after):
    from sqlalchemy import select
    rows = db.scalars(select(m.AuditLog).where(m.AuditLog.target_id == pid, m.AuditLog.action == action)).all()
    check(len(rows) == 1 and rows[0].actor == actor and json.loads(rows[0].before_json).get('status') == before
          and json.loads(rows[0].after_json).get('status') == after, 'Review audit count/actor/state mismatch')
    return {'audit_id': rows[0].id, 'action': action, 'before': before, 'after': after, 'actor': actor}


def review_chain(m, client, saved, label, target, provider_token, reviewer_tokens, category_id):
    from sqlalchemy import func, select
    pid = target['pid']
    with m.SessionLocal() as db:
        product = db.get(m.Product, pid)
        check(db.scalar(select(func.count()).select_from(m.Order).where(m.Order.product_id == pid)) == 0,
              'Refusing draft reset on a purchased product')
        product.status, product.reviewed_at, product.reviewed_by = 'draft', None, ''
        callbacks = json.loads(product.oidc_redirect_uris_json or '[]')
        application_url, integration_api_url = product.application_url, product.integration_api_url
        db.commit()
    metadata = {'name': pid, 'product_type': target['kind'], 'category_id': category_id,
        'provider_name': target['enterprise'], 'description': 'Owned ' + label + ' actual review acceptance',
        'usage_scenarios': 'Isolated API application or SaaS access verification', 'pricing_strategy': 'Monthly service',
        'authorization_conditions': 'Fixture buyer enterprise only', 'data_source_statement': 'Synthetic QA metadata',
        'compliance_statement': 'No real customer records', 'delivery_method': 'api' if target['kind'] == 'api' else 'tenant_access',
        'upstream_url': a.MOCK_URL if target['kind'] == 'api' else '',
        'application_url': application_url, 'integration_api_url': integration_api_url,
        'oidc_redirect_uris': callbacks, 'settlement_rule_mode': 'custom',
        'settlement_rule': {'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0}}
    request(client, provider_token, 'PUT', '/products/' + pid, json=metadata)
    request(client, provider_token, 'POST', '/products/' + pid + '/submit', expected=400)
    from PIL import Image
    image = BytesIO()
    Image.new('RGB', (380, 280), (32, 130, 76)).save(image, format='PNG')
    uploaded = request(client, provider_token, 'POST', '/files/upload', data={'product_id': pid, 'file_role': 'product_logo'},
        files={'upload': (saved['run_id'] + '-' + label + '.logo.png', image.getvalue(), 'image/png')}).json()
    check(uploaded['clamav_status'] == 'clean', 'Real review-logo ClamAV scan failed')
    submitted = request(client, provider_token, 'POST', '/products/' + pid + '/submit').json()
    check(submitted['status'] == 'pending_review', 'Submit did not enter business review')
    request(client, provider_token, 'POST', '/products/' + pid + '/review', expected=403,
            json={'decision': 'approve', 'comment': 'Provider cannot approve'})
    evidence = []
    for role, route, before, after, action in STEPS:
        body = request(client, reviewer_tokens[role], 'POST', f'/products/{pid}/{route}',
                       json={'decision': 'approve', 'comment': 'Owned actual ' + role + ' approval'}).json()
        check(body['status'] == after, 'Review stage bypassed pending-integration gate')
        with m.SessionLocal() as db:
            evidence.append(audit_step(m, db, pid, saved['reviewers'][label][role] + '@example.invalid', action, before, after))
    with m.SessionLocal() as db:
        check(db.get(m.Product, pid).status == 'pending_integration', 'Review published before health verification')
        check(db.scalar(select(m.Order.id).where(m.Order.product_id == pid)) is None, 'Delivery/order preceded complete review')
        scan = db.scalar(select(m.ProductSecurityScan).where(m.ProductSecurityScan.product_id == pid))
        check(scan and scan.status == 'approved' and scan.reviewed_by == saved['reviewers'][label]['security'] + '@example.invalid'
              and json.loads(scan.report_json)['presidio_status'] == 'available', 'Actual quality/security scan proof missing')
        Message, Receipt = m.message_center['Message'], m.message_center['Receipt']
        for state, role in (('pending_review', 'business'), ('quality_review', 'quality'),
                            ('security_review', 'security'), ('operation_review', 'release')):
            messages = db.scalars(select(Message).where(Message.target_id == pid, Message.target_type == 'product',
                Message.category == 'review', Message.event_key.startswith(f'review:product:{pid}:{state}:'))).all()
            check(len(messages) == 1 and db.scalar(select(Receipt.id).where(Receipt.message_id == messages[0].id,
                Receipt.user_id == saved['reviewers'][label][role])) is not None, 'Reviewer inbox stage proof missing')
    return {'reviews': evidence, 'reviewer_inbox_stages': 4, 'before_health': 'pending_integration',
            'registration': 'provider PUT on owned draft identity roots', 'logo': 'real multipart 380x280 PNG + ClamAV'}


@contextmanager
def journal_uploads(m, saved, path, target_map):
    from sqlalchemy import event
    original = m.Minio
    class Storage(original):
        def make_bucket(self, *args, **kwargs):
            raise a.AcceptanceFailure('Existing bucket required; no bucket creation')
        def put_object(self, bucket, key, *args, **kwargs):
            label = next((label for label, target in target_map.items() if key.startswith(target['provider'] + '/')
                          and key.endswith(saved['run_id'] + '-' + label + '.logo.png')), None)
            if label is None:
                label = next((label for label, target in target_map.items() if key.startswith(target['provider'] + '/')
                              and key.endswith(saved['run_id'] + '-' + label + '.logo.png.thumb.png')), None)
            check(label is not None and bucket == m.MINIO_BUCKET and f.online.object_absent(self, bucket, key),
                  'Upload key is unowned or collides')
            item = {'key': key, 'product': target_map[label]['pid'], 'owner': target_map[label]['provider'],
                    'role': 'product_logo_thumbnail' if key.endswith('.thumb.png') else 'product_logo'}
            saved['keys'].append(item)
            validate_asset_records(saved, target_map)
            check(len(saved['keys']) <= 6, 'Too many review assets')
            write(path, saved)
            return super().put_object(bucket, key, *args, **kwargs)
    def capture(db, *_):
        for row in db.new:
            if isinstance(row, m.FileObject) and row.product_id in {item['pid'] for item in target_map.values()}:
                item = next((item for item in saved['keys'] if item['key'] == row.object_name), None)
                check(item is not None and row.owner_id == item['owner'] and row.file_role == item['role']
                      and row.version_id is None, 'File flush lacks exact pre-PUT journal')
                saved['assets'][row.id] = item
                write(path, saved)
    event.listen(m.SessionLocal.class_, 'after_flush', capture)
    try:
        with patch.object(m, 'Minio', Storage):
            yield
    finally:
        event.remove(m.SessionLocal.class_, 'after_flush', capture)


def prepare(m, saved, path, api_ids, saas_ids):
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    with m.SessionLocal() as db:
        parent = aliased(m.ProductCategory)
        category = db.scalar(select(m.ProductCategory).join(parent, m.ProductCategory.parent_id == parent.id)
                             .where(parent.parent_id.is_(None)).order_by(m.ProductCategory.id).limit(1))
        check(category is not None, 'Existing second-level category required')
        category_id = category.id
        user_ids = [uid for group in saved['reviewers'].values() for uid in group.values()]
        check(db.scalar(select(m.User.id).where((m.User.id.in_(user_ids)) | (m.User.email.in_(
            [uid + '@example.invalid' for uid in user_ids]))).limit(1)) is None, 'Reviewer identity collision')
    a.preflight(m, api_ids)
    s.preflight(m, saas_ids)
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY, secret_key=m.MINIO_SECRET_KEY, secure=False)
    check(m.MINIO_ENDPOINT and storage.bucket_exists(m.MINIO_BUCKET), 'Existing real MinIO bucket required')
    saved['authorized'] = True
    api_ids.update(authorized=True, environment=a.environment(m), consumers=[], quota_windows=[])
    saas_ids['authorized'] = True
    a.write_manifest(Path(saved['api']), api_ids, create=True)
    s.write_manifest(saved['saas'], saas_ids, create=True)
    write(path, saved)
    api_tokens = a.prepare(m, api_ids)
    saas_ids['_manifest_writer'] = lambda value, create=False: s.write_manifest(saved['saas'], value, create)
    saas_tokens, oidc_secret = s.prepare_fixture(m, saas_ids)
    saas_ids['_manifest_writer'](saas_ids)
    with m.SessionLocal() as db:
        for label, group in saved['reviewers'].items():
            for role, uid in group.items():
                db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid', password_hash='unused',
                    platform_role=ROLES[role], verified_status='verified', activation_status='active', is_active=True))
        db.flush()
        for group in saved['reviewers'].values():
            for uid in group.values():
                db.add(m.message_center['Preference'](user_id=uid, email_enabled=False))
        for side, vid in saved['canonical_versions'].items():
            special = db.get(m.SaaSProductVersion, saas_ids['versions'][side])
            db.add(m.ProductReleaseVersion(id=vid, product_id=saas_ids['product'], version_code=side,
                price=special.monthly_price, cost=special.cost, status='active'))
        db.commit()
        reviewers = {label: {role: m.issue_token(db.get(m.User, uid)) for role, uid in group.items()}
                     for label, group in saved['reviewers'].items()}
    return category_id, api_tokens, saas_tokens, reviewers, oidc_secret


def exercise(m, saved, path, api_ids, saas_ids):
    global STAGE
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, event, func, select
    category, api_tokens, saas_tokens, reviewers, oidc_secret = prepare(m, saved, path, api_ids, saas_ids)
    target_map = targets(saved, api_ids, saas_ids)
    client = TestClient(m.app)
    consumer_writer = a.consumer_journal(m, api_ids, Path(saved['api']))
    event.listen(m.SessionLocal.class_, 'after_flush', consumer_writer)
    evidence = {}
    try:
        # Configure only exact owned route keys before review, with no verify/publication.
        for index, label in enumerate(('a0', 'a1')):
            pid, vid = api_ids['products'][index], api_ids['versions'][index]
            base = f'/products/{pid}/versions/{vid}/integration'
            request(client, api_tokens['ops'], 'PUT', base, json={'upstream_url': a.MOCK_URL,
                'route_key': api_ids['routes'][index], 'version': 'v1', 'upstream_auth_mode': 'oauth2',
                'health_path': '/' + saved['run_id'] + '-missing', 'rate_limit_per_minute': 60,
                'daily_quota': 100, 'monthly_quota': 1000})
            with m.SessionLocal() as db:
                check(db.scalar(select(m.Order.id).where(m.Order.product_id == pid)) is None,
                      'Unused OAuth removal refuses a purchased product')
                clients = db.scalars(select(m.OAuthClient).where(m.OAuthClient.product_id == pid)).all()
                check(len(clients) == 1 and clients[0].client_id == api_ids['run_id'] + '-oauth-' + str(index),
                      'Unused OAuth removal refuses unknown client')
                db.execute(delete(m.OAuthClient).where(m.OAuthClient.id == clients[0].id))
                db.commit()
        with journal_uploads(m, saved, path, target_map):
            for label, target in target_map.items():
                STAGE = 'actual-' + label + '-registration-four-review'
                tokens = saas_tokens if label == 's' else api_tokens
                evidence[label] = review_chain(m, client, saved, label, target, tokens[target['tokens_role']], reviewers[label], category)
                with m.SessionLocal() as db:
                    oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == target['pid']))
                    check(oauth is not None, 'Operation approval lacks platform OAuth client')
                    audit = db.scalar(select(m.AuditLog).where(m.AuditLog.target_id == oauth.id,
                        m.AuditLog.action == 'ensure_platform_oauth_client',
                        m.AuditLog.actor == saved['reviewers'][label]['release'] + '@example.invalid'))
                    check(audit is not None, 'Operation approval OAuth audit missing')
                    evidence[label]['oauth_boundary'] = 'retained seeded machine client; not fresh creation proof' if label == 's' else 'unused seeded client removed; operation approval created client'
        for index, label in enumerate(('a0', 'a1')):
            STAGE = 'actual-' + label + '-health-gate-and-initial-delivery'
            target = target_map[label]
            base = f"/products/{target['pid']}/versions/{target['version']}/integration"
            check(a.admin(m, 'GET', '/routes/' + api_ids['routes'][index], absent=True) is None,
                  'Route published before health verification')
            failed = request(client, api_tokens['ops'], 'POST', base + '/verify').json()
            check(failed['status'] == 'failed' and failed['health_status_code'] == 404, 'Real unhealthy integration did not fail')
            with m.SessionLocal() as db:
                check(db.get(m.Product, target['pid']).status == 'pending_integration', 'Failed health published product')
            request(client, api_tokens['ops'], 'PUT', base, json={'upstream_url': a.MOCK_URL,
                'route_key': api_ids['routes'][index], 'version': 'v1', 'upstream_auth_mode': 'oauth2',
                'health_path': '/health', 'rate_limit_per_minute': 60, 'daily_quota': 100, 'monthly_quota': 1000})
            verified = request(client, api_tokens['ops'], 'POST', base + '/verify').json()
            check(verified['status'] == 'verified' and verified['healthy'], 'Real API health verification failed')
            with m.SessionLocal() as db:
                check(db.get(m.Product, target['pid']).status == 'published', 'Verified API not published')
            oid = request(client, api_tokens['buyer'], 'POST', '/orders', json={'product_id': target['pid'],
                'product_version_id': target['version'], 'buyer_enterprise_id': api_ids['enterprises'][0],
                'subscription_months': 1}).json()['id']
            with f.held_payment_tasks(m, oid):
                request(client, api_tokens['buyer'], 'POST', f'/orders/{oid}/transition', json={
                    'action': 'confirm_payment', 'reason': 'Reviewed service simulated payment'})
            delivered = f.process_online_delivery_once(m, api_ids, oid, 0, release=True)
            check(delivered['outcome'] is True and delivered['status'] == 'completed', 'Reviewed API initial delivery failed')
            check(request(client, api_tokens['buyer'], 'GET', f'/orders/{oid}/delivery-access').json()['ready'] is True,
                  'Reviewed API access not ready')
            evidence[label].update(health='real 404 rejected then healthy verify published', delivery=delivered)
        STAGE = 'actual-saas-health-gate-and-initial-delivery'
        s.register_mock_client(saas_ids, oidc_secret)
        verified = request(client, saas_tokens['provider'], 'POST', f"/products/{saas_ids['product']}/saas-integration/verify").json()
        check(verified['verification_status'] == 'verified' and verified['product']['status'] == 'published', 'Real SaaS health/OIDC verification failed')
        oid = request(client, saas_tokens['buyer'], 'POST', '/orders', json={'product_id': saas_ids['product'],
            'product_version_id': saas_ids['versions']['source'], 'buyer_enterprise_id': saas_ids['enterprises']['buyer'],
            'subscription_months': 1}).json()['id']
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            check(order.subscription_id, 'SaaS order lacks subscription')
            saas_ids['database_ids']['orders'].append(oid)
            saas_ids['database_ids']['subscription_id'] = order.subscription_id
            saas_ids['external']['tenant'] = 'tenant-' + order.subscription_id[:12]
        saas_ids['_manifest_writer'](saas_ids)
        request(client, saas_tokens['finance'], 'POST', f'/orders/{oid}/transition', json={
            'action': 'confirm_payment', 'reason': 'Reviewed SaaS simulated payment'})
        with m.SessionLocal() as db:
            task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
            check(task is not None, 'Reviewed SaaS task missing')
            tid = task.id
        s.deliver_staged(m, saas_ids, saved['saas'], client, saas_tokens, tid)
        ready, _ = s.delivery_ready_proof(m, saas_ids, oid, tid)
        check(ready, 'Reviewed SaaS OPEN/STATUS readiness proof failed')
        evidence['s'].update(health='real OAuth/health/OIDC verify published', delivery='actual initial OPEN/STATUS proof')
        return evidence
    finally:
        event.remove(m.SessionLocal.class_, 'after_flush', consumer_writer)
        client.close()


def cleanup(m, saved, path):
    from sqlalchemy import delete, event, func, select, text
    api_ids, saas_ids = load_children(saved)
    check(saved.get('authorized') is True and saved['environment'] == f.file_environment(m), 'Cleanup not authorized for environment')
    f.assert_protected_core(saved['protected_state'], f.protected_snapshot(m))
    with m.SessionLocal() as db:
        predicates = scopes(m, db, saved, api_ids, saas_ids)
        f.reject_protected_candidates(m, db, predicates, f.protected_snapshot(m))
        route_ids = set(db.scalars(select(m.ApiGatewayRoute.id).where(m.ApiGatewayRoute.product_id.in_(api_ids['products']))))
        credentials = db.scalars(select(m.ApiCredential).where(m.ApiCredential.route_id.in_(route_ids))).all()
        check({row.id for row in credentials} <= {item['id'] for item in api_ids.get('consumers', [])}, 'Unjournaled API consumer')
        for item in api_ids.get('consumers', []):
            row = db.get(m.ApiCredential, item['id'])
            check(row is not None and row.route_id in route_ids and row.apisix_consumer_name == item['name']
                  and row.enterprise_id == api_ids['enterprises'][0], 'Consumer ownership mismatch')
        saved['database_ids'] = {name: [list(row) for row in db.execute(
            select(*m.Base.metadata.tables[name].primary_key.columns).where(predicate))]
            for name, predicate in predicates.items()}
        write(path, saved)
    # Close only the journaled tenant using exact persisted identity and independent STATUS.
    tenant = saas_ids['external']['tenant']
    if tenant:
        with m.SessionLocal() as db:
            sub = db.get(m.SaaSSubscription, saas_ids['database_ids']['subscription_id'])
            config = db.scalar(select(m.SaaSIntegrationConfig).where(m.SaaSIntegrationConfig.product_id == saas_ids['product']))
            check(sub and config and sub.product_id == saas_ids['product'] and
                  sub.enterprise_id == saas_ids['enterprises']['buyer'] and tenant == 'tenant-' + sub.id[:12],
                  'Tenant ownership mismatch')
            identity = {'product_id': sub.product_id, 'enterprise_id': sub.enterprise_id,
                        'subscription_id': sub.id, 'tenant_id': tenant}
        code, live = s.mock_saas_status(m, config, identity)
        if code == 404:
            check(live.get('detail') == 'tenant_not_found' and not saas_ids['external']['tenant_created'], 'Unexpected tenant absence')
        else:
            check(code == 200 and live.get('ret') == 0 and all(live.get(k) == v for k, v in identity.items()), 'Tenant STATUS ownership failed')
            if live.get('status') != 'closed':
                closed = m.saas_call(config, 'CLOSE', dict(identity, idempotency_key=saved['run_id'] + '-close'))
                check(closed.get('ret') == 0 and closed.get('status') == 'closed', 'Tenant CLOSE failed')
            code, live = s.mock_saas_status(m, config, identity)
            check(code == 200 and live.get('status') == 'closed' and all(live.get(k) == v for k, v in identity.items()), 'Tenant close STATUS failed')
    if saas_ids['external']['oidc_client_created']:
        import httpx
        cid = saas_ids['external']['oidc_client_id']
        with m.SessionLocal() as db:
            check(db.scalar(select(m.OIDCClient.id).where(m.OIDCClient.client_id == cid,
                m.OIDCClient.product_id == saas_ids['product'])) is not None, 'Mock OIDC client ownership failed')
        response = httpx.delete(s.MOCK_SAAS + '/_test/oidc/clients/' + cid,
            headers={'X-Mock-Admin-Secret': s.mock_admin_secret()}, timeout=10)
        check(response.status_code == 200 and response.json().get('client_id') == cid and
              type(response.json().get('deleted')) is bool, 'Exact mock OIDC deletion failed')
    for key in api_ids['routes']:
        live = a.admin(m, 'GET', '/routes/' + key, absent=True)
        if live is not None:
            check(live.get('labels', {}).get('market_product_id') in api_ids['products'], 'External route ownership failed')
            a.admin(m, 'DELETE', '/routes/' + key, absent=True)
        check(a.admin(m, 'GET', '/routes/' + key, absent=True) is None, 'Owned route remains')
    for item in api_ids.get('consumers', []):
        a.admin(m, 'DELETE', '/consumers/' + item['name'], absent=True)
        check(a.admin(m, 'GET', '/consumers/' + item['name'], absent=True) is None, 'Owned consumer remains')
    redis = m.apisix_policy_redis()
    try:
        keys = a.redis_keys(api_ids)
        if keys:
            redis.delete(*keys)
        check(not any(redis.exists(key) for key in keys), 'Owned Redis key remains')
    finally:
        redis.close()
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY, secret_key=m.MINIO_SECRET_KEY, secure=False)
    protected = f.protected_snapshot(m)
    def protect_delete(execution):
        if execution.is_delete:
            check(execution.statement.whereclause is not None, 'Unscoped delete refused')
            f.reject_protected_candidates(m, execution.session,
                {execution.statement.table.name: execution.statement.whereclause}, protected)
    event.listen(m.SessionLocal.class_, 'do_orm_execute', protect_delete)
    try:
        with m.SessionLocal() as db:
            db.execute(text("SET LOCAL lock_timeout = '15s'"))
            all_products = api_ids['products'] + [saas_ids['product']]
            db.scalars(select(m.Product).where(m.Product.id.in_(all_products)).order_by(m.Product.id).with_for_update()).all()
            db.scalars(select(m.Order).where(m.Order.product_id.in_(all_products)).order_by(m.Order.id).with_for_update()).all()
            predicates = scopes(m, db, saved, api_ids, saas_ids)
            f.reject_protected_candidates(m, db, predicates, protected)
            for item in saved['keys']:
                storage.remove_object(m.MINIO_BUCKET, item['key'])
                check(f.online.object_absent(storage, m.MINIO_BUCKET, item['key']), 'Owned logo object remains')
            for table in reversed(m.Base.metadata.sorted_tables):
                if table.name in predicates:
                    db.execute(delete(table).where(predicates[table.name]))
            db.commit()
        with m.SessionLocal() as db:
            for name, predicate in predicates.items():
                check(db.scalar(select(func.count()).select_from(m.Base.metadata.tables[name]).where(predicate)) == 0,
                      'Owned review row/report remains')
    finally:
        event.remove(m.SessionLocal.class_, 'do_orm_execute', protect_delete)
    f.assert_protected_core(saved['protected_state'], f.protected_snapshot(m))
    saved['cleaned'] = True
    write(path, saved)


def run(args):
    global STAGE
    check(getattr(args, 'parent_reviewed', False) is True, 'Parent review required before runtime mutation')
    path = Path(args.manifest).absolute()
    if args.cleanup:
        saved = f.read_private(path)
        validate(saved, path)
        m = a.load_main()
        cleanup(m, saved, path)
        return {'status': 'passed', 'scope': 'exact companion cleanup only'}
    m = a.load_main()
    saved = roots()
    saved.update(api=str(path.with_name(path.name + '.api.json')), saas=str(path.with_name(path.name + '.saas.json')),
                 assets={}, keys=[], environment=f.file_environment(m), protected_state=f.protected_snapshot(m), authorized=False)
    check(all(not Path(saved[key]).exists() and not Path(saved[key]).is_symlink() for key in ('api', 'saas')), 'Child journal collision')
    write(path, saved, create=True)
    api_ids, saas_ids = a.fixture_ids(), s.fixture_ids()
    try:
        STAGE = 'actual-review-and-delivery'
        evidence = exercise(m, saved, path, api_ids, saas_ids)
        STAGE = 'exact-owned-review-cleanup'
        cleanup(m, saved, path)
        return {'status': 'passed', 'cases': evidence, 'protected_business_unchanged': True,
                'cleanup': 'exact DB/assets/scans/inbox edges/routes/consumers/Redis; mock tenant closed and client removed',
                'boundary': 'actual three service review chains -> health gate -> initial production process delivery; '
                    'SaaS OAuth/OIDC credentials seeded; no daemon, browser SSO or bank evidence'}
    finally:
        f.assert_protected_core(saved['protected_state'], f.protected_snapshot(m))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--parent-reviewed', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        result = {'status': 'failed', 'stage': STAGE, 'error_type': type(exc).__name__,
                  'file': Path(frame.filename).name, 'line': frame.lineno,
                  'recovery': 'Retain private journals; exact --cleanup after parent review'}
        if isinstance(exc, (a.AcceptanceFailure, g.AcceptanceFailure)):
            result['check_failed'] = str(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
