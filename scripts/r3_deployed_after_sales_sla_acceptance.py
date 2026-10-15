"""Post-rollout BE005/008 HTTP proof using the installed app and isolated PG.

WAIT for explicit parent rollout confirmation before running this script.
Place this script beside r3_backend_acceptance_pg.py in the API container's
temporary scripts directory. Keep PYTHONPATH=/app (never a workingtree backend).
  python -B r3_deployed_after_sales_sla_acceptance.py \
      --expected-source-sha256 "$EXPECTED_SOURCE_JSON"
The JSON object must map every SOURCE_FILES name to its frozen release SHA256.
Mismatch aborts before schema creation. No app source files are transferred.
Uses the existing probe's PG/schema cleanup and blocked external integrations.
TestClient exercises actual deployed HTTP routes in-process, not ingress.
Happy path is create plus seven actions: eight HTTP writes/eight events.
Print sanitized results only; stdout may be archived as new evidence.
"""
import argparse
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
from datetime import timedelta
from unittest.mock import patch

import r3_backend_acceptance_pg as base


SOURCE_FILES = ('main.py', 'after_sales_workflow.py', 'enterprise_sla.py',
                'holiday_calendar.py', 'ticket_calendar.py', 'sla_clock.py', 'sla_metrics.py')


def installed_sources(expected):
    base.check(isinstance(expected, dict) and set(expected) == set(SOURCE_FILES),
               'Expected manifest must contain exactly the seven release source files')
    base.check(all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                   for value in expected.values()), 'Expected source SHA256 invalid')
    spec = importlib.util.find_spec('app')
    base.check(spec is not None and spec.origin is not None, 'Installed app package unavailable')
    root = Path(spec.origin).resolve().parent
    actual = {}
    for name in SOURCE_FILES:
        path = root/name
        base.check(path.is_file() and not path.is_symlink(), 'Installed release source missing or linked')
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    base.check(actual == expected, 'Installed source differs from expected rollout; no DB action performed')
    return root, actual


def seed(m):
    actors = {'buyer': '', 'provider': '', 'member': '', 'outsider': '',
              'admin': 'super_admin', 'ops': 'platform_operator', 'finance': 'finance_settlement',
              'reviewer': 'product_manager', 'quality': 'quality_reviewer',
              'security': 'security_compliance', 'monitor': 'delivery_monitor'}
    with m.SessionLocal() as db:
        for tenant in ('buyer', 'provider', 'other'):
            db.add(m.Enterprise(id='owned-'+tenant, name='Owned '+tenant,
                credit_code='owned-'+tenant, verification_status='verified'))
        for name, role in actors.items():
            db.add(m.User(id='owned-'+name, name='Owned acceptance actor',
                email=name+'@example.invalid', password_hash='unused', platform_role=role,
                verified_status='verified', activation_status='active', is_active=True))
        db.flush()
        for name, tenant, role in [('buyer', 'buyer', 'super_admin'),
                                  ('provider', 'provider', 'super_admin'),
                                  ('member', 'buyer', 'member'), ('outsider', 'other', 'super_admin')]:
            db.add(m.Membership(user_id='owned-'+name, enterprise_id='owned-'+tenant,
                                role=role, status='active'))
        db.add(m.Product(id='owned-product', enterprise_id='owned-provider', name='Owned acceptance',
                         product_type='dataset', delivery_method='file', status='published'))
        for level in ('standard', 'enterprise', 'strategic'):
            db.add(m.ServiceLevel(id='owned-'+level, code=level, name='Owned '+level, status='active'))
        db.flush()
        for name in ('happy', 'negative', 'new-sla', 'urgent'):
            db.add(m.Order(id='owned-'+name, order_no='OWNED-DEPLOYED-'+name,
                buyer_enterprise_id='owned-buyer', provider_enterprise_id='owned-provider',
                buyer_user_id='owned-buyer', product_id='owned-product', buyer_name='Owned buyer',
                product_name='Owned acceptance', payment_status='paid', paid_amount=10, amount=10,
                main_status='completed', delivery_status='accepted'))
        db.commit()
        return {name: {'Authorization': 'Bearer '+m.issue_token(db.get(m.User, 'owned-'+name))}
                for name in actors}


def exercise(m, client, headers):
    from sqlalchemy import func, select
    results, shared = {}, {}

    def call(method, path, actor='buyer', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        base.check(response.status_code == expected,
            f'{method} {path} actor={actor} expected={expected} actual={response.status_code}')
        return response.json()

    def case(name, fn):
        try:
            results[name] = {'status': 'passed', **fn()}
        except Exception as exc:
            results[name] = base.safe_error(exc)

    def create_body(order, key, **extra):
        return {'order_id': 'owned-'+order, 'description': 'Owned HTTP acceptance',
                'type': 'owned-quality', 'idempotency_key': key, **extra}

    def act_body(ticket, action, key):
        return {'action': action, 'reason': 'Owned action reason', 'expected_version': ticket['version'],
                'idempotency_key': key}

    def happy():
        ticket = call('POST', '/api/after-sales', json=create_body('happy', 'owned-happy-create'))
        base.check(ticket['status'] == 'awaiting_response' and ticket['version'] == 1, 'Initial ticket state invalid')
        tid = ticket['id']; shared['old_ticket'] = tid
        with m.SessionLocal() as db:
            shared['old_snapshot'] = db.get(m.AfterSalesTicket, tid).sla_snapshot_json
        sequence = [('respond', 'provider', 'processing'),
                    ('request_info', 'provider', 'awaiting_buyer_info'),
                    ('supplement', 'buyer', 'processing'),
                    ('submit_resolution', 'provider', 'awaiting_buyer_confirmation'),
                    ('reject_resolution', 'buyer', 'processing'),
                    ('submit_resolution', 'provider', 'awaiting_buyer_confirmation'),
                    ('confirm', 'buyer', 'completed')]
        for index, (action, actor, status) in enumerate(sequence, 1):
            ticket = call('POST', '/api/after-sales/'+tid+'/actions', actor,
                          json=act_body(ticket, action, 'owned-happy-action-'+str(index)))
            base.check(ticket['status'] == status and ticket['version'] == index+1,
                       'Happy-path state/version mismatch')
        timeline = call('GET', '/api/after-sales/'+tid+'/timeline')['items']
        base.check(len(timeline) == 8 and [e['after']['version'] for e in timeline] == list(range(1, 9)),
                   'Happy-path event count or versions invalid')
        base.check(ticket['first_response_at'] and ticket['closed_at'], 'Response/closure evidence missing')
        with m.SessionLocal() as db:
            base.check(db.get(m.Order, 'owned-happy').after_sales_status == 'resolved', 'Order state not resolved')
            audits = db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.target_id == tid, m.AuditLog.action.like('after_sales_%')))
            base.check(audits == 8, 'Expected one audit per happy-path event')
        return {'http_writes': 8, 'create_requests': 1, 'action_requests': 7, 'events': 8,
                'audits': audits, 'final_version': 8, 'final_status': 'completed',
                'actions': [entry[0] for entry in sequence]}
    case('BE005_HTTP_eight_step_happy_path', happy)

    def denials():
        body = create_body('negative', 'owned-negative-create')
        for actor in ('provider', 'admin', 'ops', 'finance', 'member'):
            call('POST', '/api/after-sales', actor, 403, json=body)
        call('POST', '/api/after-sales', 'outsider', 404, json=body)
        ticket = call('POST', '/api/after-sales', json=body); tid = ticket['id']
        repeat = call('POST', '/api/after-sales', json=body)
        base.check(repeat['id'] == tid and repeat['version'] == 1, 'Create replay not idempotent')
        call('POST', '/api/after-sales', expected=409, json={**body, 'description': 'Changed owned payload'})
        call('POST', '/api/after-sales', expected=409, json={**body, 'idempotency_key': 'owned-other-create'})
        for actor in ('buyer', 'admin'):
            call('POST', '/api/after-sales/'+tid+'/actions', actor, 403,
                 json=act_body(ticket, 'respond', 'owned-forbidden-'+actor))
        for actor in ('member', 'outsider', 'finance'):
            call('GET', '/api/after-sales/'+tid, actor, 404)
        call('GET', '/api/after-sales/'+tid+'?enterprise_id=owned-other', expected=403)
        action = act_body(ticket, 'respond', 'owned-respond-replay')
        processed = call('POST', '/api/after-sales/'+tid+'/actions', 'provider', json=action)
        replay = call('POST', '/api/after-sales/'+tid+'/actions', 'provider', json=action)
        base.check(processed['version'] == replay['version'] == 2, 'Action replay changes version')
        call('POST', '/api/after-sales/'+tid+'/actions', 'provider', 409,
             json={**action, 'reason': 'Changed replay payload'})
        call('POST', '/api/after-sales/'+tid+'/actions', 'provider', 409,
             json=act_body(ticket, 'request_info', 'owned-stale-version'))
        call('POST', '/api/after-sales/'+tid+'/actions', 'provider', 403,
             json=act_body(processed, 'confirm', 'owned-provider-confirm'))
        timeline = call('GET', '/api/after-sales/'+tid+'/timeline')['items']
        base.check(len(timeline) == 2, 'Rejected/replayed operations created extra events')
        return {'unauthorized_create': 6, 'unauthorized_actions': 3, 'detail_scope_denials': 4,
                'create_and_action_replay_idempotent': True, 'payload_conflicts': 2,
                'duplicate_open_ticket_conflict': True, 'stale_version_conflict': True,
                'events_after_replays_and_denials': 2}
    case('BE005_HTTP_permissions_replay_and_version', denials)

    def sla_assignment():
        path = '/api/sla/service-level-assignments'
        body = {'enterprise_id': 'owned-buyer', 'service_level_id': 'owned-enterprise',
                'reason': 'Owned approved upgrade'}
        for actor in ('buyer', 'provider', 'member', 'outsider', 'finance', 'reviewer',
                      'quality', 'security', 'monitor'):
            call('POST', path, actor, 403, json=body)
        for actor in ('admin', 'ops'):
            for reason in ('', '   '):
                call('POST', path, actor, 400, json={**body, 'reason': reason})
        call('POST', path, 'admin', 400, json={**body, 'user_id': 'owned-buyer'})
        with m.SessionLocal() as db:
            base.check(db.scalar(select(func.count()).select_from(m.ServiceLevelAssignment)) == 0,
                       'Rejected SLA assignments persisted')
        call('POST', path, 'admin', json=body)
        call('POST', path, 'ops', json={**body, 'service_level_id': 'owned-strategic'})
        ticket = call('POST', '/api/after-sales', json=create_body('new-sla', 'owned-new-sla-create'))
        with m.SessionLocal() as db:
            old = db.get(m.AfterSalesTicket, shared['old_ticket'])
            base.check(old.sla_snapshot_json == shared['old_snapshot'], 'Existing SLA snapshot changed')
            new = db.get(m.AfterSalesTicket, ticket['id'])
            snapshot = json.loads(new.sla_snapshot_json)
            base.check(snapshot['service_level_code'] == 'strategic', 'New ticket did not inherit final SLA')
            base.check(json.loads(old.sla_snapshot_json)['service_level_code'] == 'standard',
                       'Old ticket did not preserve standard SLA')
            audits = db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action == 'assign_service_level'))
            base.check(audits == 2, 'Successful SLA assignment audit mismatch')
            logs = db.scalars(select(m.AuditLog).where(m.AuditLog.action == 'assign_service_level')).all()
            base.check({json.loads(log.after_json)['actor_role'] for log in logs} ==
                       {'super_admin', 'platform_operator'}, 'Assignment audit actor roles missing')
            base.check(all(log.detail == body['reason'] and log.tenant_id == 'owned-buyer'
                           for log in logs), 'Assignment reason or tenant audit mismatch')
        # Return buyer to a holiday-dependent policy for the unconfigured-calendar HTTP check.
        call('POST', path, 'ops', json={**body, 'service_level_id': 'owned-standard'})
        return {'unauthorized_actors': 9, 'blank_reason_denials': 4, 'personal_override_denied': True,
                'admin_and_ops_assignments': True, 'successful_assignment_audits_before_reset': 2,
                'assignment_reason_and_actor_roles_audited': True,
                'new_ticket_level': 'strategic', 'old_ticket_snapshot_byte_identical': True,
                'owned_policy_reset_to_standard': True}
    case('BE008_HTTP_assignment_boundaries_and_snapshot', sla_assignment)

    def sla_get():
        ticket = call('POST', '/api/after-sales', json=create_body('urgent', 'owned-urgent-create', priority='urgent'))
        tid = ticket['id']
        with m.SessionLocal() as db:
            row = db.get(m.AfterSalesTicket, tid)
            # Owned fixture only: ensure the interval includes a support weekday at any run time.
            row.created_at = m.now()-timedelta(days=8)
            before = row.sla_snapshot_json
            db.commit()
        result = call('GET', '/api/after-sales/'+tid+'/sla')
        base.check(result['calendar_status'] == 'not_ready', 'Missing annual cache unexpectedly ready')
        first = next(row for row in result['items'] if row['metric'] == 'urgent_first_response')
        resolution = next(row for row in result['items'] if row['metric'] == 'urgent_resolution')
        base.check(first['clock'] == 'natural' and first['elapsed_seconds'] is not None,
                   'Natural urgent response hidden by absent support calendar')
        base.check(resolution['status'] == 'insufficient_data' and resolution['elapsed_seconds'] is None,
                   'Missing support calendar generated fake resolution compliance')
        call('GET', '/api/after-sales/'+tid+'/sla', 'outsider', 404)
        with m.SessionLocal() as db:
            base.check(db.get(m.AfterSalesTicket, tid).sla_snapshot_json == before, 'GET SLA rewrote snapshot')
        return {'calendar_status': 'not_ready', 'urgent_natural_response_measured': True,
                'support_resolution_status': 'insufficient_data', 'foreign_sla_denied': True,
                'snapshot_byte_identical': True}
    case('BE005_BE008_HTTP_sla_missing_calendar_and_natural_response', sla_get)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-source-sha256', required=True, help='JSON mapping frozen filenames to SHA256')
    args = parser.parse_args()
    expected = json.loads(args.expected_source_sha256)
    root, before = installed_sources(expected)
    with patch.object(base, 'seed', seed), patch.object(base, 'exercise', exercise):
        result = base.run()
    final_hashes = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    cases = result.get('cases', {})
    result['status'] = 'passed' if (len(cases) == 4 and all(row['status'] == 'passed' for row in cases.values())
        and before == final_hashes and result.get('protected_order_count_before') == 1
        and result.get('protected_order_count_after') == 1) else 'failed'
    result.update(kind='post_rollout_deployed_http_isolated_pg', source_kind='deployed_app',
        workingtree_package_used=False, source_sha256=before, expected_source_sha256=expected,
        source_unchanged_during_probe=before == final_hashes,
        coverage=['TRD-R3-BE-005', 'TRD-R3-BE-008'],
        scope='Installed app.main HTTP routes through TestClient plus actual isolated PG; not ingress',
        gaps=['Intervention/remediation SLA materialization and external notification delivery not covered'],
        fixture_note='Only owned schema synthetic rows; no original business or secret rows read.')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': base.safe_error(exc)}))
        sys.exit(1)
