#!/usr/bin/env python3
"""Run an authenticated, loopback-only synthetic fault for one exact QA fixture."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.fixture_faults import (FixtureScope, fault_server, reservation_metadata,
                               reserve_fixture, release_fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--mode', choices=('healthy', 'unavailable', 'timeout'), default='unavailable')
    parser.add_argument('--delay', type=float, default=0)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--action', choices=('serve', 'reserve', 'renew', 'release', 'provision'),
                        default='serve')
    parser.add_argument('--reservation-seconds', type=int, default=120)
    args = parser.parse_args()
    scope = FixtureScope.load(args.manifest)
    if args.action != 'serve':
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        from app import main as runtime
        with runtime.SessionLocal() as db:
            if args.action == 'provision':
                reservation_metadata.create_all(db.connection())
            else:
                token = os.environ.get('QA_FIXTURE_RESERVATION_TOKEN')
                if not token:
                    raise ValueError('QA_FIXTURE_RESERVATION_TOKEN required')
                if args.action == 'release':
                    release_fixture(db, runtime.__dict__, scope, token)
                else:
                    reserve_fixture(db, runtime.__dict__, scope, token=token,
                                    seconds=args.reservation_seconds,
                                    require_existing=args.action == 'renew')
            db.commit()
        return
    server = fault_server(scope, os.environ.get('QA_FIXTURE_FAULT_SECRET'),
                          mode=args.mode, delay=args.delay, port=args.port)
    print(f'http://127.0.0.1:{server.server_port}/fixtures/{scope.run_id}/{scope.change_id}/health',
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
