"""Back up and remove only the explicitly retired default SLA profile and results."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select

NAME = '平台默认服务等级'


def inventory(m, db, *, lock=False):
    query = select(m.SLAProfile).where(m.SLAProfile.name == NAME).order_by(m.SLAProfile.id)
    profiles = list(db.scalars(query.with_for_update() if lock else query))
    ids = [row.id for row in profiles]
    query = select(m.SLAResult).where(m.SLAResult.profile_id.in_(ids)).order_by(m.SLAResult.id)
    results = list(db.scalars(query.with_for_update() if lock else query))
    return profiles, results


def remove(m, db, backup, expected_ids, expected_count, actor):
    profiles, results = inventory(m, db, lock=True)
    if {row.id for row in profiles} != set(expected_ids) or len(results) != expected_count:
        raise ValueError('Inventory changed; review a fresh plan before deletion')
    if not profiles:
        raise ValueError('No matching legacy default profile')
    def record(row):
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}
    saved = {'profile_name': NAME, 'profiles': [record(row) for row in profiles],
             'results': [record(row) for row in results], 'captured_at': m.now().isoformat()}
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(saved, out, ensure_ascii=False, indent=2, default=str)
        out.flush()
        os.fsync(out.fileno())
    ids = [row.id for row in profiles]
    result_ids = [row.id for row in results]
    for row in results:
        db.delete(row)
    db.flush()
    for row in profiles:
        db.delete(row)
    db.flush()
    m.audit(db, actor, 'retire_legacy_default_sla', 'sla_profile', ','.join(ids),
            'User requested deletion of the retired default profile and associated assessments',
            category='sla', business_domain='service_level',
            before={'profile_name': NAME, 'profile_ids': ids, 'assessment_ids': result_ids},
            after={'deleted_profiles': len(ids), 'deleted_assessments': len(result_ids)})
    return {'deleted_profiles': len(ids), 'deleted_assessments': len(result_ids), 'profile_ids': ids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--expected-profile-id', action='append', default=[])
    parser.add_argument('--expected-result-count', type=int)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    with m.SessionLocal() as db:
        if not args.apply:
            profiles, results = inventory(m, db)
            print(json.dumps({'profile_name': NAME, 'profile_ids': [row.id for row in profiles],
                              'assessment_count': len(results)}, ensure_ascii=False))
            return
        if not args.backup or not args.expected_profile_id or args.expected_result_count is None:
            raise ValueError('Apply requires an exclusive backup path and exact inventory')
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if actor is None or actor.platform_role != 'super_admin' or not actor.is_active:
            raise ValueError('Active platform administrator required')
        result = remove(m, db, args.backup, args.expected_profile_id, args.expected_result_count, actor.email)
        db.commit()
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
