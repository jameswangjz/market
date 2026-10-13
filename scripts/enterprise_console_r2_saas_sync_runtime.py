"""Exercise real mock-SaaS sync using the guarded version-change fixture."""
import argparse
import json
from pathlib import Path

from sqlalchemy import delete, select

import version_change_saas_runtime_acceptance as guarded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mock-public-base-url', required=True)
    args = parser.parse_args()
    if args.mock_public_base_url not in {'http://127.0.0.1:30082', 'http://192.168.10.10:30083'}:
        raise ValueError('Only known local mock SaaS origins are permitted')
    guarded.OIDC_REDIRECT = args.mock_public_base_url + '/sso/callback'
    original = guarded.worker_acceptance

    def verify(m, ids, path, client, tokens):
        result = original(m, ids, path, client, tokens)
        subscription_id = ids['database_ids']['subscription_id']
        endpoint = '/saas-subscriptions/' + subscription_id
        department_ids = [ids['run_id'] + '-dept-' + str(index) for index in range(13)]
        with m.SessionLocal() as db:
            db.add(m.EnterpriseDepartment(id=department_ids[0],
                enterprise_id=ids['enterprises']['buyer'], name='QA sync root', status='active'))
            db.flush()
            for index, department_id in enumerate(department_ids[1:], 1):
                db.add(m.EnterpriseDepartment(id=department_id,
                    enterprise_id=ids['enterprises']['buyer'], name='QA sync child ' + str(index),
                    parent_id=department_ids[0], status='active'))
            db.commit()
        proofs = []

        def action(kind, action, targets):
            payload = guarded.request(client, tokens['buyer'], 'POST', endpoint + '/sync-actions',
                body={'kind': kind, 'action': action, 'ids': targets})
            guarded.check(payload['failed'] == 0 and payload['succeeded'] == len(targets),
                'Real SaaS sync failed for ' + action + ' ' + kind)
            proofs.append({'kind': kind, 'action': action, 'items': len(targets),
                'succeeded': payload['succeeded']})
            return payload

        action('departments', 'add', department_ids[:10])
        action('departments', 'add', department_ids[10:])
        tree = guarded.request(client, tokens['buyer'], 'GET', endpoint + '/sync-tree')
        guarded.check(len(tree['mapped_departments']) == 13, 'Real department mappings missing')
        with m.SessionLocal() as db:
            membership = db.scalar(select(m.Membership).where(
                m.Membership.user_id == ids['users']['buyer'],
                m.Membership.enterprise_id == ids['enterprises']['buyer']))
            for department_id in department_ids[-2:]:
                db.add(m.MembershipDepartment(membership_id=membership.id, department_id=department_id))
            db.commit()
        action('users', 'add', [ids['users']['buyer']])
        tree = guarded.request(client, tokens['buyer'], 'GET', endpoint + '/sync-tree')
        buyer = next(item for item in tree['mapped_users'] if item['platform_user_id'] == ids['users']['buyer'])
        guarded.check(len(buyer['department_ids']) == 2, 'Multi-department assignment not persisted')
        guarded.request(client, tokens['provider'], 'GET', endpoint + '/sync-tree', expected=403)
        rejected = guarded.request(client, tokens['buyer'], 'POST', endpoint + '/sync-actions',
            body={'kind': 'users', 'action': 'remove', 'ids': [buyer['id']]})
        guarded.check(rejected['failed'] == 1, 'Primary administrator removal was not blocked')
        with m.SessionLocal() as db:
            membership = db.scalar(select(m.Membership).where(
                m.Membership.user_id == ids['users']['buyer'],
                m.Membership.enterprise_id == ids['enterprises']['buyer']))
            db.execute(delete(m.MembershipDepartment).where(
                m.MembershipDepartment.membership_id == membership.id))
            membership.department_id = ''
            db.commit()
        action('users', 'add', [ids['users']['buyer']])
        tree = guarded.request(client, tokens['buyer'], 'GET', endpoint + '/sync-tree')
        mappings = {row['platform_department_id']: row['id'] for row in tree['mapped_departments']}
        remove_ids = [mappings[value] for value in reversed(department_ids)]
        action('departments', 'remove', remove_ids[:10])
        action('departments', 'remove', remove_ids[10:])
        tree = guarded.request(client, tokens['buyer'], 'GET', endpoint + '/sync-tree')
        guarded.check(not tree['mapped_departments'] and len(tree['mapped_users']) == 1,
            'Real sync final state incorrect')
        result['sync_runtime'] = {'proofs': proofs, 'multi_department_assignment': True,
            'primary_admin_removal_blocked': True, 'cross_enterprise_denied': True,
            'provider': 'actual market-mock-saas HTTP, no response mocks',
            'limits': ['explicit backend batches, not a browser bulk-operation test']}
        return result

    guarded.worker_acceptance = verify
    result = guarded.run()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'scope': 'real mock SaaS sync and version changes',
        'cleanup': result['cleanup']}))


if __name__ == '__main__':
    main()
