"""Explicit privileged source transitions; no quota resets or synthetic usage."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'open', 'seal', 'resume'))
    parser.add_argument('--actor', default='admin@market.local')
    parser.add_argument('--reason', default='')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.action != 'status' and not args.reason.strip():
        raise ValueError('Transition reason required')
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from app.api_charge_ledger import bounded_client
    from sqlalchemy import select
    with os.fdopen(fd, 'w') as output, m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == args.actor))
        if not actor or actor.platform_role != 'super_admin' or not actor.is_active:
            raise ValueError('Active platform system administrator required')
        hooks = m.api_charge_ledger
        Generation, Gap = hooks['SourceGeneration'], hooks['Gap']
        def snapshot():
            return [{'source_id': r.id, 'stream': r.stream, 'status': r.status,
                     'cutover': r.cutover.isoformat(), 'old_stream': r.old_stream,
                     'old_cursor': r.old_cursor}
                    for r in db.scalars(select(Generation).order_by(Generation.cutover))]
        before = snapshot()
        client = bounded_client(vars(m))
        try:
            source = None
            if args.action != 'status':
                source = hooks[args.action + '_epoch'](db, client, actor.email, args.reason)
                m.audit(db, actor.email, 'redis_source_' + args.action, 'api_charge_source',
                        source['source_id'], args.reason, before={'generations': before}, after=source)
                db.commit()
            after = snapshot()
            result = {'status': 'passed', 'action': args.action, 'captured_at': m.now().isoformat(),
                      'generations': after, 'gap_count': db.query(Gap).count(),
                      'source': source, 'quotas_reset': False, 'usage_fabricated': False}
            json.dump(result, output, ensure_ascii=False, indent=2)
        finally:
            client.close()
    print(json.dumps({'status': result['status'], 'action': args.action, 'gap_count': result['gap_count']}))


if __name__ == '__main__':
    main()
