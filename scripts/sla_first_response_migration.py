"""Explicit, audited correction of the exact historical strategic seed only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def migrate(db, ns, *, apply=False, actor=''):
    from sqlalchemy import select
    from app.enterprise_sla import STRATEGIC_DESCRIPTION, is_legacy_strategic_seed

    if apply and not actor.strip():
        raise ValueError('An audit actor is required for apply')
    level_type = ns['ServiceLevel']
    statement = select(level_type).where(level_type.code == 'strategic').execution_options(populate_existing=True)
    if apply:
        statement = statement.with_for_update()
    levels = list(db.scalars(statement))
    ticket = ns['AfterSalesTicket']
    snapshots = dict(db.execute(select(ticket.id, ticket.sla_snapshot_json)).all())

    def digest(values):
        return hashlib.sha256(json.dumps(sorted(values.items()), ensure_ascii=False).encode()).hexdigest()

    before_hash = digest(snapshots)
    matched = [level for level in levels if is_legacy_strategic_seed(level)]
    report = {'apply': apply, 'matched_ids': [level.id for level in matched],
              'skipped_ids': [level.id for level in levels if level not in matched],
              'snapshot_count': len(snapshots), 'snapshot_hash_before': before_hash}
    for level in matched:
        if not apply:
            continue
        before = {key: getattr(level, key) for key in (
            'initial_response_minutes', 'urgent_initial_response_minutes', 'description')}
        level.initial_response_minutes = 720
        level.urgent_initial_response_minutes = 360
        level.description = STRATEGIC_DESCRIPTION
        ns['audit'](db, actor, 'migrate_strategic_first_response', 'service_level', level.id,
                    detail='Correct exact historical seed; preserve frozen ticket promises',
                    category='sla', business_domain='service_level',
                    before=before, after={**{key: getattr(level, key) for key in before},
                                         'snapshot_hash_before': before_hash,
                                         'snapshot_hash_after': before_hash})
    db.flush()
    # Compare only the original IDs: concurrently created tickets are not historical mutations.
    current = {key: value for key, value in db.execute(select(ticket.id, ticket.sla_snapshot_json))
               if key in snapshots}
    report['snapshot_hash_after'] = digest(current)
    if report['snapshot_hash_after'] != before_hash:
        raise RuntimeError('Historical ticket snapshot changed; transaction must be rolled back')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--actor', default='')
    args = parser.parse_args()
    from app import main as runtime
    with runtime.SessionLocal() as db:
        try:
            report = migrate(db, vars(runtime), apply=args.apply, actor=args.actor)
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps(report, ensure_ascii=False))
        except Exception:
            db.rollback()
            raise


if __name__ == '__main__':
    main()
