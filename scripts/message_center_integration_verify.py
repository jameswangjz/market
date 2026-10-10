"""Real notification services integration, isolated PG schema and MinIO prefix."""
import argparse
import ast
import html
import json
import os
import secrets
import signal
import smtplib
import socket
import struct
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from unittest.mock import patch

import httpx
import redis
from fastapi.testclient import TestClient
from minio import Minio
from minio.error import S3Error
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from message_center_pg_verify import disposable_schema, load_center, require

SUBJECT_TEMPLATE = '\u6a21\u677f\u4e3b\u9898 ${title}'
BODY_TEMPLATE = '\u6a21\u677f\u6b63\u6587 ${title}\n${content}\n${platform_url}\n<footer> & end'


def scan_function(main_path, states):
    source = ast.parse(main_path.read_text(encoding='utf-8'))
    node = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                and node.name == 'clamav_scan_stream')
    namespace = dict(socket=socket, struct=struct, CLAMAV_ENABLED=True,
        CLAMAV_HOST=os.environ.get('CLAMAV_HOST', 'market-clamav'),
        CLAMAV_PORT=int(os.environ.get('CLAMAV_PORT', '3310')))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(main_path), 'exec'), namespace)
    def scan(stream, size):
        result = namespace['clamav_scan_stream'](stream, size)
        states.append(result[0])
        return result
    return scan


class PrefixStore:
    def __init__(self, client, bucket, prefix):
        self.client, self.bucket, self.prefix = client, bucket, prefix

    def key(self, bucket, key):
        require(bucket == self.bucket and key.startswith('notifications/'), 'Unscoped object operation')
        return self.prefix + key

    def bucket_exists(self, bucket):
        require(bucket == self.bucket, 'Unexpected bucket')
        return self.client.bucket_exists(bucket)

    def put_object(self, bucket, key, *args, **kwargs):
        return self.client.put_object(bucket, self.key(bucket, key), *args, **kwargs)

    def get_object(self, bucket, key):
        return self.client.get_object(bucket, self.key(bucket, key))

    def remove_object(self, bucket, key):
        return self.client.remove_object(bucket, self.key(bucket, key))

    def cleanup(self):
        for obj in self.client.list_objects(self.bucket, prefix=self.prefix, recursive=True):
            require(obj.object_name.startswith(self.prefix), 'Object escaped the test prefix')
            self.client.remove_object(self.bucket, obj.object_name)
        require(not list(self.client.list_objects(self.bucket, prefix=self.prefix, recursive=True)),
                'Test objects remain after cleanup')
        print(json.dumps({'minio_prefix': self.prefix, 'removed': True}), flush=True)


def ok(response):
    require(response.status_code in (200, 201), f'HTTP {response.status_code}: {response.text[:400]}')
    return response.json()


def capture_mail(mailpit, subject, ids):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = mailpit.get('/api/v1/messages', params={'limit': 200})
        response.raise_for_status()
        matches = [item for item in response.json().get('messages', []) if item.get('Subject') == subject]
        ids.update(item['ID'] for item in matches)
        if len(matches) == 2:
            return matches
        require(len(matches) <= 2, 'Duplicate test emails captured')
        time.sleep(0.2)
    raise AssertionError('Mailpit did not capture both test emails')


def cleanup_mail(mailpit, subject, ids):
    response = mailpit.get('/api/v1/messages', params={'limit': 200})
    response.raise_for_status()
    ids.update(item['ID'] for item in response.json().get('messages', []) if item.get('Subject') == subject)
    if ids:
        response = mailpit.request('DELETE', '/api/v1/messages', json={'IDs': sorted(ids)})
        response.raise_for_status()
        response = mailpit.get('/api/v1/messages', params={'limit': 200})
        response.raise_for_status()
        require(not any(item.get('ID') in ids for item in response.json().get('messages', [])),
                'Captured test emails remain after cleanup')
    print(json.dumps({'mailpit_test_messages_removed': len(ids)}), flush=True)


def verify(context, engine, store, mailpit, subject, mail_ids, states, clock):
    center, actor = context.center, context.actor
    with Session(engine) as db:
        for uid in ('a', 'b'):
            user = db.get(context.User, uid)
            user.email, user.email_verified = f'temp{uid}@example.invalid', True
            user.platform_role = 'super_admin' if uid == 'a' else ''
        db.add(context.User(id='c', email='tempc@example.invalid', email_verified=True))
        db.commit()
    actor.platform_role, actor.email = 'super_admin', 'tempa@example.invalid'
    body = '\u4e2d\u6587\u6b63\u6587 <script>alert("test")</script> & integration'
    expected_subject = Template(SUBJECT_TEMPLATE).substitute(title=subject)
    expected_body = Template(BODY_TEMPLATE).substitute(title=subject, content=body,
        platform_url=os.environ.get('PUBLIC_BASE_URL', 'http://192.168.10.10:30080'))
    payload = '\u771f\u5b9e\u9644\u4ef6\u9a8c\u6536\n'.encode('utf-8') + b'isolated integration bytes\n'
    subscriber = redis.Redis.from_url(os.environ.get('REDIS_URL', 'redis://market-redis:6379/0'),
                                     socket_connect_timeout=5, socket_timeout=5, decode_responses=True)
    with ExitStack() as stack:
        stack.callback(subscriber.close)
        pubsub = subscriber.pubsub()
        stack.callback(pubsub.close)
        pubsub.subscribe('market:notifications:a')
        subscribed = pubsub.get_message(timeout=5)
        require(subscribed and subscribed['type'] == 'subscribe', 'Redis subscription was not ready')
        client = TestClient(context.app)
        stack.callback(client.close)
        defaults = ok(client.get('/api/notifications/settings'))
        require(all(defaults[key] == value for key, value in dict(retry_count=3,
            retry_interval_seconds=10, poll_interval_seconds=30).items()), 'Unexpected retry/poll defaults')
        settings = dict(retention_days=180, attachment_max_mb=20, email_enabled=True,
            retry_count=3, retry_interval_seconds=10, poll_interval_seconds=30,
            email_subject_template=SUBJECT_TEMPLATE, email_body_template=BODY_TEMPLATE)
        ok(client.put('/api/notifications/settings', json=settings))
        actual_settings = ok(client.get('/api/notifications/settings'))
        require(all(actual_settings[key] == value for key, value in settings.items()), 'Template/settings did not persist')
        rejected = client.put('/api/notifications/settings', json=settings | {'email_body_template': '${smtp_password}'})
        require(rejected.status_code == 422, 'Non-whitelisted template variable was accepted')
        attachment = ok(client.post('/api/notifications/attachments',
            files={'upload': ('integration.txt', payload, 'text/plain')}))
        require(states == ['clean'], 'Real ClamAV scan did not return clean')
        with Session(engine) as db:
            row = db.get(center['Attachment'], attachment['id'])
            object_key = row.object_name
            require(row.scan_result == 'clean', 'Clean scan was not persisted')
        require(store.client.stat_object(store.bucket, store.prefix + object_key).size == len(payload),
                'MinIO object size differs from upload')
        too_many = client.post('/api/notifications/send', json=dict(title=subject, content=body,
            recipient_ids=['b'], attachment_ids=[attachment['id']] * 6))
        require(too_many.status_code == 422, 'Six attachments were accepted')
        sent = ok(client.post('/api/notifications/send', json=dict(title=subject, content=body,
            severity='important', category='announcement', tenant_id='', recipient_ids=['a', 'b'],
            attachment_ids=[attachment['id']], draft=False, event_key=secrets.token_hex(16))))
        actor.id, actor.email, actor.platform_role = 'b', 'tempb@example.invalid', ''
        download = client.get(f'/api/notifications/attachments/{attachment["id"]}/download')
        require(download.status_code == 200 and download.content == payload, 'Recipient download differs from upload')
        actor.id, actor.email = 'c', 'tempc@example.invalid'
        denied = client.get(f'/api/notifications/attachments/{attachment["id"]}/download')
        require(denied.status_code == 404, 'Unauthorized attachment download was not hidden')
        for _ in range(20):
            if not center['process_once']():
                break
        else:
            raise AssertionError('Outbox did not drain')
        hint = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
            if item and json.loads(item['data']).get('message_id') == sent['id']:
                hint = item
                break
        require(hint is not None, 'Actual Redis notification hint was not received')
        messages = capture_mail(mailpit, expected_subject, mail_ids)
        recipients = set()
        for item in messages:
            response = mailpit.get('/api/v1/message/' + item['ID'])
            response.raise_for_status()
            message = response.json()
            require(message['Subject'] == expected_subject, 'Chinese template subject differs')
            plain = message['Text'].replace('\r\n', '\n')
            markup = message['HTML'].replace('\r\n', '\n')
            require(plain.rstrip('\n') == expected_body, 'Chinese template plaintext body differs after CRLF normalization')
            require(html.escape(expected_subject) in markup and html.escape(expected_body) in markup,
                    'HTML email did not escape the subject/body')
            require('<script>' not in markup, 'Raw script tag in HTML email')
            recipients.update(address['Address'] for address in message['To'])
        require(recipients == {'tempa@example.invalid', 'tempb@example.invalid'}, 'Unexpected SMTP recipients')
        with Session(engine) as db:
            jobs = db.scalars(select(center['Delivery'])).all()
            require(len(jobs) == 4 and all(job.status == 'accepted' and job.attempts == 1 for job in jobs),
                    'Real SMTP/Redis deliveries were not accepted once')
            require(db.scalar(select(func.count()).select_from(center['DeliveryAttempt'])) == 4,
                    'Real delivery attempts were not persisted')
        clock[0] += timedelta(days=179)
        first_cleanup = center['cleanup']()
        require(first_cleanup['messages_removed'] == 0, 'Cleanup removed a message before 180 days')
        clock[0] += timedelta(days=2)
        with Session(engine) as db:
            live = center['create'](db, [], 'live retention control', 'body', status='draft')
            live_id = live.id
            db.commit()
        cleanup = center['cleanup']()
        require(cleanup == {'messages_removed': 1, 'attachments_removed': 1}, 'Retention cleanup counts differ')
        with Session(engine) as db:
            require(db.get(center['Message'], sent['id']) is None, 'Expired message remains')
            require(db.get(center['Message'], live_id) is not None, 'Cleanup removed the unexpired control')
            for name in ('Receipt', 'Delivery', 'DeliveryAttempt', 'Attachment'):
                require(db.scalar(select(func.count()).select_from(center[name])) == 0, f'Expired {name} remains')
        try:
            store.client.stat_object(store.bucket, store.prefix + object_key)
        except S3Error as exc:
            require(exc.code in ('NoSuchKey', 'NoSuchObject'), 'Unexpected MinIO deletion result')
        else:
            raise AssertionError('Cleanup did not delete the actual attachment object')
        actor.id, actor.email, actor.platform_role = 'a', 'tempa@example.invalid', 'super_admin'
        with Session(engine) as db:
            urgent = center['create'](db, ['a'], 'expired urgent', 'test', severity='urgent', delivery=False)
            acknowledged = center['create'](db, ['a'], 'acknowledged urgent', 'test', severity='urgent', delivery=False)
            db.flush()
            pending_rid = db.scalar(select(center['Receipt'].id).where(center['Receipt'].message_id == urgent.id))
            acknowledged_rid = db.scalar(select(center['Receipt'].id).where(center['Receipt'].message_id == acknowledged.id))
            db.commit()
        ok(client.post(f'/api/notifications/{acknowledged_rid}/acknowledge'))
        clock[0] += timedelta(days=181)
        center['cleanup']()
        health = ok(client.get('/api/notifications/delivery-health'))
        require(len(health['expired_urgent']) == 1 and health['expired_urgent'][0]['id'] == pending_rid,
                'Expired urgent pending record differs')
        actor.id, actor.email, actor.platform_role = 'b', 'tempb@example.invalid', ''
        denied = client.post(f'/api/notifications/expired-urgent/{pending_rid}/resolve', json={'reason': 'test'})
        require(denied.status_code == 403, 'Ordinary user resolved expired urgent')
        actor.id, actor.email, actor.platform_role = 'a', 'tempa@example.invalid', 'super_admin'
        denied = client.post(f'/api/notifications/expired-urgent/{pending_rid}/resolve', json={'reason': ' '})
        require(denied.status_code == 422, 'Blank expired urgent resolution accepted')
        ok(client.post(f'/api/notifications/expired-urgent/{pending_rid}/resolve', json={'reason': 'integration verified'}))
        require(ok(client.get('/api/notifications/delivery-health'))['expired_urgent'] == [],
                'Expired urgent pending was not resolved')
    return dict(clamav='clean', minio_upload_download='passed', unauthorized_download=404,
        smtp_chinese_and_escaped_html='passed', smtp_messages=2, redis_hint='passed',
        accepted_deliveries=4, retention_179_days='preserved', retention_181_days='cleaned',
        unexpired_control='preserved', settings_templates='passed', template_variable_whitelist='passed',
        attachment_count_limit=5, retry_count=3, retry_interval_seconds=10, poll_interval_seconds=30,
        expired_urgent_pending_and_resolution='passed', acknowledged_urgent_no_pending='passed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', type=Path, default=Path('/app/app/message_center.py'))
    parser.add_argument('--main-source', type=Path, default=Path('/app/app/main.py'))
    args = parser.parse_args()
    require(args.module.is_file() and args.main_source.is_file(), 'Module/source path does not exist')
    require(bool(os.environ.get('DATABASE_URL')), 'DATABASE_URL is missing')
    endpoint, bucket = os.environ.get('MINIO_ENDPOINT', ''), os.environ.get('MINIO_BUCKET', 'market-files')
    require(bool(endpoint), 'MINIO_ENDPOINT is missing')
    minio = Minio(endpoint, access_key=os.environ['MINIO_ACCESS_KEY'],
                  secret_key=os.environ['MINIO_SECRET_KEY'], secure=False)
    require(minio.bucket_exists(bucket), 'Existing attachment bucket is unavailable')
    run_id = secrets.token_hex(12)
    prefix = 'message-center-integration/' + run_id + '/'
    subject = '\u4e2d\u6587\u6d88\u606f\u9a8c\u6536 <unsafe> & ' + run_id
    store, states, clock = PrefixStore(minio, bucket, prefix), [], [datetime.now(timezone.utc)]
    original_smtp = smtplib.SMTP
    def safe_smtp(host, port, *args, **kwargs):
        require(host == 'market-mailpit' and int(port) == 1025, 'SMTP destination is not the isolated Mailpit')
        return original_smtp(host, port, *args, **kwargs)
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(*_):
        raise KeyboardInterrupt('Integration terminated')
    signal.signal(signal.SIGTERM, terminate)
    try:
        with ExitStack() as stack:
            stack.callback(signal.signal, signal.SIGTERM, previous_handler)
            mailpit = httpx.Client(base_url='http://market-mailpit:8025', timeout=10, trust_env=False)
            stack.callback(mailpit.close)
            info = mailpit.get('/api/v1/info')
            info.raise_for_status()
            require(bool(info.json().get('Version')), 'Capture server is not Mailpit')
            mail_ids = set()
            stack.callback(cleanup_mail, mailpit, Template(SUBJECT_TEMPLATE).substitute(title=subject), mail_ids)
            stack.callback(store.cleanup)
            stack.enter_context(patch.dict(os.environ, NOTIFICATION_SMTP_HOST='market-mailpit',
                NOTIFICATION_SMTP_PORT='1025', NOTIFICATION_SMTP_FROM='integration@example.invalid'))
            stack.enter_context(patch('smtplib.SMTP', safe_smtp))
            stack.enter_context(patch('smtplib.SMTP_SSL', side_effect=AssertionError('Real SMTP SSL forbidden')))
            engine, schema = stack.enter_context(disposable_schema(os.environ['DATABASE_URL']))
            context = load_center(args.module, engine, schema, return_context=True, services=dict(
                now=lambda: clock[0], Minio=lambda *args, **kwargs: store, MINIO_ENDPOINT=endpoint,
                MINIO_BUCKET=bucket, MINIO_ACCESS_KEY='scoped-adapter', MINIO_SECRET_KEY='scoped-adapter',
                clamav_scan_stream=scan_function(args.main_source, states)))
            checks = verify(context, engine, store, mailpit, subject, mail_ids, states, clock)
            print(json.dumps({'schema': schema, 'minio_prefix': prefix, 'checks': checks}, sort_keys=True), flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == '__main__':
    main()
