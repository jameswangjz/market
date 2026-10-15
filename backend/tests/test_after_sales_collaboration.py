"""HTTP permission/evidence/transaction contracts in disposable SQLite only."""
import io
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

import test_after_sales_workflow as fixture


class AfterSalesCollaborationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.AfterSalesWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.AfterSalesWorkflowTests.tearDownClass.__func__)
    tearDown = fixture.AfterSalesWorkflowTests.tearDown
    request = fixture.AfterSalesWorkflowTests.request
    create_order = fixture.AfterSalesWorkflowTests.create_order
    create = fixture.AfterSalesWorkflowTests.create
    act = fixture.AfterSalesWorkflowTests.act

    def setUp(self):
        fixture.AfterSalesWorkflowTests.setUp(self)
        self.hooks = self.m.after_sales_workflow['collaboration']
        self.Comment, self.Attachment = self.hooks['Comment'], self.hooks['Attachment']
        with self.m.SessionLocal() as db:
            db.add(self.m.User(id='monitor', name='monitor', email='monitor@example.invalid',
                password_hash='unused', platform_role='delivery_monitor', verified_status='verified',
                activation_status='active', is_active=True))
            db.commit()
            self.tokens['monitor'] = self.m.issue_token(db.get(self.m.User, 'monitor'))
        self.ticket = self.create()
        self.scan_stream = self.m.clamav_scan_stream
        self.storage = MagicMock()
        def content(*args):
            response = MagicMock()
            response.read.side_effect = io.BytesIO(b'evidence').read
            return response
        self.storage.get_object.side_effect = content
        for p in (patch.object(self.m, 'MINIO_ENDPOINT', 'isolated-storage'),
                  patch.object(self.m, 'Minio', return_value=self.storage),
                  patch.object(self.m, 'clamav_scan_stream', return_value=('clean', 'synthetic clean scan'))):
            p.start()
            self.addCleanup(p.stop)

    def comment(self, ticket=None, actor='buyer', expected=200, **changes):
        ticket = ticket or self.ticket
        return self.request('POST', f"/api/after-sales/{ticket['id']}/comments", actor=actor, expected=expected,
            json={'content': 'Evidence review comment', 'expected_version': ticket['version'],
                  'idempotency_key': 'comment-key-0001', **changes})

    def upload(self, ticket=None, actor='buyer', expected=200, content=b'evidence', **changes):
        ticket = ticket or self.ticket
        return self.request('POST', f"/api/after-sales/{ticket['id']}/attachments", actor=actor, expected=expected,
            files={'upload': ('evidence.txt', content, 'text/plain')},
            data={'expected_version': ticket['version'], 'idempotency_key': 'attachment-key-0001', **changes})

    def test_allowed_actions_match_roles_and_monitor_cannot_force_close(self):
        path = f"/api/after-sales/{self.ticket['id']}"
        expected = {'buyer': {'withdraw', 'request_intervention'},
                    'provider': {'respond', 'request_intervention'},
                    'ops': {'force_close'}, 'platform': {'force_close'}, 'monitor': set()}
        for actor, actions in expected.items():
            row = self.request('GET', path, actor=actor)
            self.assertEqual(set(row['allowed_actions']), actions)
        self.act(self.ticket, 'force_close', 'monitor', 403)
        ticket = self.act(self.ticket, 'request_intervention', 'buyer')
        row = self.request('GET', path, actor='monitor')
        self.assertEqual(set(row['allowed_actions']), {'respond_intervention', 'review_intervention'})
        self.act(ticket, 'force_close', 'monitor', 403)
        self.assertEqual(self.act(ticket, 'review_intervention', 'monitor')['status'], 'awaiting_response')

    def test_comment_is_not_business_response_or_state_mutation_and_replay_is_unique(self):
        with self.m.SessionLocal() as db:
            before = db.get(self.m.AfterSalesTicket, self.ticket['id'])
            frozen = before.sla_snapshot_json
        first = self.comment(actor='provider')
        replay = self.comment(actor='provider')
        self.assertEqual(first['id'], replay['id'])
        self.comment(actor='provider', expected=409, content='Changed payload')
        with self.m.SessionLocal() as db:
            ticket = db.get(self.m.AfterSalesTicket, self.ticket['id'])
            self.assertEqual((ticket.version, ticket.status, ticket.first_response_at), (1, 'awaiting_response', None))
            self.assertEqual(ticket.sla_snapshot_json, frozen)
            self.assertEqual(db.query(self.m.AfterSalesEvent).count(), 1)
            self.assertEqual(db.query(self.Comment).count(), 1)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='after_sales_comment').count(), 1)
            Message, Outbox = self.m.message_center['Message'], self.m.message_center['Outbox']
            messages = db.query(Message).filter(Message.event_key.like('after-sales-evidence:comment:%')).all()
            self.assertEqual(len(messages), 1)
            jobs = db.query(Outbox).filter_by(message_id=messages[0].id).all()
            keys = [(job.user_id, job.channel) for job in jobs]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertTrue(any(channel == 'email' for _, channel in keys))

    def test_terminal_comments_do_not_corrupt_closed_event_chain(self):
        ticket = self.act(self.ticket, 'force_close', 'ops')
        self.upload(ticket, expected=409)
        self.upload(ticket, purpose='audit_comment')
        self.comment(ticket, actor='monitor')
        self.comment(ticket, idempotency_key='buyer-terminal-comment')
        with self.m.SessionLocal() as db:
            row = db.get(self.m.AfterSalesTicket, ticket['id'])
            events = list(db.scalars(self.select(self.m.AfterSalesEvent)))
            self.assertEqual(row.version, 2)
            self.assertEqual([e.event_type for e in events], ['create', 'force_close'])
        self.assertEqual(self.request('GET', f"/api/after-sales/{ticket['id']}")['allowed_actions'], [])

    def test_cross_tenant_inactive_regular_and_revoked_members_cannot_read_or_append(self):
        attachment = self.upload()
        for actor in ('outsider', 'inactive', 'finance', 'reviewer', 'disabled_ops'):
            for suffix in ('comments', 'attachments', f"attachments/{attachment['id']}/download"):
                self.request('GET', f"/api/after-sales/{self.ticket['id']}/{suffix}", actor=actor,
                    expected=401 if actor == 'disabled_ops' else 403 if actor == 'inactive' else 404)
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == 'buyer_admin'))
            member.role = 'member'
            db.commit()
        self.comment(actor='buyer_admin', expected=404)
        self.upload(actor='buyer_admin', expected=404)
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == 'buyer'))
            member.status = 'disabled'
            db.commit()
        self.comment(expected=404)
        self.request('GET', f"/api/files/{attachment['file_id']}/download", expected=404)

    def test_explicit_scope_is_enforced_on_all_evidence_routes(self):
        attachment = self.upload()
        base = f"/api/after-sales/{self.ticket['id']}"
        for suffix in ('comments', 'attachments', f"attachments/{attachment['id']}/download"):
            self.request('GET', base + '/' + suffix + '?enterprise_id=other_tenant', expected=403)
        self.request('POST', base + '/comments?enterprise_id=other_tenant', expected=403, json={
            'content': 'cross scope', 'expected_version': 1, 'idempotency_key': 'scope-comment-key'})

    def test_attachment_scan_gate_idempotency_metadata_and_controlled_download(self):
        with patch.object(self.m, 'clamav_scan_stream', return_value=('infected', 'unsafe')):
            self.upload(expected=400)
        with patch.object(self.m, 'clamav_scan_stream', return_value=('unavailable', 'offline')):
            self.upload(expected=503)
        self.storage.put_object.assert_not_called()
        row = self.upload()
        self.assertEqual(self.upload()['id'], row['id'])
        self.storage.put_object.assert_called_once()
        self.upload(expected=409, content=b'changed')
        serialized = json.dumps(row)
        for secret in ('object_name', 'scan_report', 'checksum', 'secret_key', 'isolated-storage'):
            self.assertNotIn(secret, serialized)
        for path in (f"/api/after-sales/{self.ticket['id']}/attachments/{row['id']}/download",
                     f"/api/files/{row['file_id']}/download"):
            response = self.client.get(path, headers={'Authorization': 'Bearer ' + self.tokens['provider']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b'evidence')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))
        self.request('GET', f"/api/files/{row['file_id']}/download", actor='reviewer', expected=404)
        with self.m.SessionLocal() as db:
            db.get(self.m.FileObject, row['file_id']).scan_status = 'infected'
            db.commit()
        self.request('GET', f"/api/files/{row['file_id']}/download", expected=404)

    def test_action_evidence_cannot_reference_foreign_files_or_other_ticket(self):
        attachment = self.upload(actor='provider')
        self.act(self.ticket, 'respond', 'provider', 404, attachments=['product_data'])
        self.act(self.ticket, 'respond', 'provider', 422, attachments=[attachment['id'], attachment['id']])
        ticket = self.act(self.ticket, 'respond', 'provider', attachments=[attachment['id']])
        self.assertEqual(ticket['version'], 2)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.hooks['EventAttachment']).count(), 1)
        timeline = self.request('GET', f"/api/after-sales/{ticket['id']}/timeline")['items']
        self.assertEqual(timeline[-1]['attachments'][0]['id'], attachment['id'])
        self.assertNotIn('object_name', json.dumps(timeline))
        self.comment(ticket, expected=404, attachments=[attachment['id']])
        self.act(ticket, 'withdraw', 'buyer')
        other = self.create(idempotency_key='create-second-ticket')
        self.comment(other, actor='provider', expected=404, attachments=[attachment['id']])

    def test_stale_versions_and_whitespace_reject_without_evidence_rows(self):
        ticket = self.act(self.ticket, 'respond', 'provider')
        self.comment(expected=409)
        self.upload(expected=409)
        self.comment(ticket, expected=422, content='   ')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Comment).count(), 0)
            self.assertEqual(db.query(self.Attachment).count(), 0)

    def test_upload_rechecks_version_after_external_io_and_cleans_only_new_object(self):
        first = self.upload()
        self.storage.remove_object.assert_not_called()
        self.upload()
        self.storage.remove_object.assert_not_called()
        self.assertEqual(self.storage.put_object.call_count, 1)
        def concurrent_action(*args, **kwargs):
            self.act(self.ticket, 'respond', 'provider')
        self.storage.put_object.side_effect = concurrent_action
        self.upload(expected=409, idempotency_key='upload-race-conflict')
        self.storage.remove_object.assert_called_once()
        created = self.storage.put_object.call_args.args[1]
        self.assertEqual(self.storage.remove_object.call_args.args[1], created)
        with self.m.SessionLocal() as db:
            preserved = db.get(self.m.FileObject, first['file_id'])
            self.assertNotEqual(created, preserved.object_name)
            self.assertEqual(db.query(self.Attachment).count(), 1)

    def test_storage_failure_and_unconfigured_storage_never_create_file_metadata(self):
        with patch.object(self.m, 'MINIO_ENDPOINT', ''):
            self.upload(expected=503)
        self.storage.put_object.side_effect = RuntimeError('storage unavailable')
        self.upload(expected=500)
        self.storage.remove_object.assert_called_once()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Attachment).count(), 0)
            self.assertEqual(db.query(self.m.FileObject).filter_by(file_role='after_sales_attachment').count(), 0)

    def test_scan_and_storage_take_no_business_locks_and_use_bounded_clients(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        locks = []
        def observe(state):
            if getattr(state.statement, '_for_update_arg', None) is not None:
                locks.append([table.name for table in state.statement.get_final_froms()])
        event.listen(Session, 'do_orm_execute', observe)
        self.addCleanup(event.remove, Session, 'do_orm_execute', observe)
        def scan(*args, **kwargs):
            self.assertEqual(locks, [])
            self.assertEqual(kwargs['timeout_seconds'], 15)
            return 'clean', 'synthetic scan'
        def store(*args, **kwargs):
            self.assertEqual(locks, [])
        self.storage.put_object.side_effect = store
        with patch.object(self.m, 'clamav_scan_stream', side_effect=scan):
            self.upload()
        self.assertEqual(locks[:2], [['orders'], ['after_sales_tickets']])
        http_client = self.m.Minio.call_args.kwargs['http_client']
        self.assertIs(http_client.connection_pool_kw['retries'].total, False)

    def test_generic_file_upload_and_scan_report_cannot_bypass_ticket_access(self):
        row = self.upload()
        self.request('GET', f"/api/files/{row['file_id']}/scan-report.pdf", actor='reviewer', expected=404)
        self.request('POST', '/api/files/upload', actor='outsider', expected=422,
            data={'file_role': 'after_sales_attachment'}, files={'upload': ('bypass.txt', b'x', 'text/plain')})

    def test_stream_scan_total_deadline_fails_closed(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch.object(self.m, 'CLAMAV_ENABLED', True), \
                patch.object(self.m.socket, 'create_connection', return_value=connection), \
                patch.object(self.m.time, 'monotonic', side_effect=[0, 16]):
            status, _ = self.scan_stream(io.BytesIO(b'evidence'), 8, timeout_seconds=15)
        self.assertEqual(status, 'unavailable')
        connection.sendall.assert_not_called()

    def test_comment_and_upload_audit_outbox_failures_are_atomic(self):
        for failure in ('audit', 'notify_business_event'):
            with patch.object(self.m, failure, side_effect=RuntimeError('isolated failure')):
                self.comment(expected=500)
                self.upload(expected=500)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.query(self.Comment).count(), 0)
                self.assertEqual(db.query(self.Attachment).count(), 0)
                self.assertEqual(db.query(self.m.FileObject).filter_by(file_role='after_sales_attachment').count(), 0)
                self.assertEqual(db.query(self.m.AfterSalesEvent).count(), 1)
        self.assertEqual(self.storage.remove_object.call_count, 2)

    def test_denied_actions_have_safe_security_audit_without_success_event(self):
        self.act(self.ticket, 'force_close', 'monitor', 403, reason='PRIVATE untrusted input')
        with self.m.SessionLocal() as db:
            audits = db.query(self.m.AuditLog).filter_by(action='after_sales_denied').all()
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0].result, 'denied')
            self.assertNotIn('PRIVATE', audits[0].detail + audits[0].after_json)
            self.assertTrue(audits[0].request_id)
            self.assertEqual(db.query(self.m.AfterSalesEvent).count(), 1)

    def test_pre_attachment_action_hash_replays_without_new_events_or_notifications(self):
        body = {'action': 'respond', 'reason': 'Legacy action', 'expected_version': 1,
                'idempotency_key': 'legacy-action-key'}
        path = f"/api/after-sales/{self.ticket['id']}/actions"
        self.request('POST', path, actor='provider', json=body)
        old_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        with self.m.SessionLocal() as db:
            event = db.scalar(self.select(self.m.AfterSalesEvent).where(self.m.AfterSalesEvent.event_type == 'respond'))
            event.payload_hash = old_hash
            db.commit()
            counts = (db.query(self.m.AfterSalesEvent).count(), db.query(self.m.message_center['Message']).count())
        for payload in (body, {**body, 'attachments': []}):
            self.assertEqual(self.request('POST', path, actor='provider', json=payload)['version'], 2)
        with self.m.SessionLocal() as db:
            self.assertEqual(counts, (db.query(self.m.AfterSalesEvent).count(),
                                     db.query(self.m.message_center['Message']).count()))

    def test_allowed_actions_is_read_only_even_for_intervention(self):
        ticket = self.act(self.ticket, 'request_intervention', 'buyer')
        with patch.object(self.m, 'ticket_sla_event', side_effect=AssertionError('Read must not refresh clocks')):
            with patch.dict(self.m.ticket_sla_clocks, refresh=lambda *a: self.fail('Read must not materialize')):
                row = self.request('GET', f"/api/after-sales/{ticket['id']}", actor='monitor')
        self.assertIn('respond_intervention', row['allowed_actions'])

    def test_safe_display_names_and_public_order_number_after_scope_filtering(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'buyer').name = 'Buyer display name'
            db.get(self.m.User, 'provider').name = 'Provider display name'
            order_no = db.get(self.m.Order, self.order_id).order_no
            db.commit()
        self.assertEqual(self.comment()['author_name'], 'Buyer display name')
        attachment = self.upload(actor='provider')
        self.assertEqual(attachment['author_name'], 'Provider display name')
        self.act(self.ticket, 'respond', 'provider', attachments=[attachment['id']])
        base = f"/api/after-sales/{self.ticket['id']}"
        detail = self.request('GET', base)
        self.assertEqual(detail['order_no'], order_no)
        self.assertEqual(self.request('GET', '/api/after-sales')['items'][0]['order_no'], order_no)
        timeline = self.request('GET', base + '/timeline')['items']
        self.assertEqual([row['actor_name'] for row in timeline], ['Buyer display name', 'Provider display name'])
        self.assertEqual(timeline[-1]['attachments'][0]['author_name'], 'Provider display name')
        self.assertEqual(self.request('GET', base + '/comments')['items'][0]['author_name'], 'Buyer display name')
        self.assertEqual(self.request('GET', base + '/attachments')['items'][0]['author_name'], 'Provider display name')
        serialized = json.dumps([detail, timeline, attachment])
        for private in ('password_hash', '@example.invalid', 'object_name', 'unit_cost'):
            self.assertNotIn(private, serialized)
        for suffix in ('', '/timeline', '/comments', '/attachments'):
            self.request('GET', base + suffix, actor='outsider', expected=404)
        with self.m.SessionLocal() as db:
            event = db.scalar(self.select(self.m.AfterSalesEvent).where(self.m.AfterSalesEvent.event_type == 'respond'))
            event.actor_id = 'historical-removed-user'
            db.commit()
        self.assertEqual(self.request('GET', base + '/timeline')['items'][-1]['actor_name'], '')

    def test_buyer_outputs_and_notifications_do_not_include_order_cost_or_secret(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.economic_snapshot_json = '{"cost":"SECRET_COST_SENTINEL"}'
            db.commit()
        self.comment()
        attachment = self.upload()
        paths = ['/api/after-sales', f"/api/after-sales/{self.ticket['id']}",
                 f"/api/after-sales/{self.ticket['id']}/timeline",
                 f"/api/after-sales/{self.ticket['id']}/comments", f"/api/after-sales/{self.ticket['id']}/attachments"]
        for path in paths:
            serialized = json.dumps(self.request('GET', path))
            self.assertNotIn('SECRET_COST_SENTINEL', serialized)
            self.assertNotIn('unit_cost', serialized)
            self.assertNotIn('object_name', serialized)
        with self.m.SessionLocal() as db:
            Message = self.m.message_center['Message']
            for message in db.scalars(self.select(Message)):
                self.assertNotIn('SECRET_COST_SENTINEL', message.body)


if __name__ == '__main__':
    unittest.main()
