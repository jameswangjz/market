"""Run an allowlisted clearing suite in a newly created disposable DB.

Use PYTHONPATH to select the isolated backend/app and tests snapshot.
"""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=(
        'test_settlement_finance_pg', 'test_settlement_batch_pg_http',
        'test_settlement_batch_pg_http.CrossVersionCostPostgresHTTPTests.'
        'test_cross_version_cost_exception_http_approval_refresh_and_lock'),
        default='test_settlement_finance_pg')
    args = parser.parse_args(argv)
    # The deployed connection is used only for CREATE/DROP DATABASE on postgres.
    # The tests receive a different database, then create private schemas in it.
    source = make_url(os.environ['DATABASE_URL'])
    name = 'market_clr2_test_' + secrets.token_hex(8)
    if not re.fullmatch(r'market_clr2_test_[a-f0-9]{16}', name):
        raise ValueError('Invalid disposable database identifier')
    admin = create_engine(source.set(database='postgres'), isolation_level='AUTOCOMMIT')
    created = False
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql('CREATE DATABASE "' + name + '"')
            created = True
        environment = dict(os.environ, CLEARING_TEST_DATABASE_URL=source.set(database=name).render_as_string(hide_password=False))
        completed = subprocess.run([sys.executable, '-W', 'ignore', '-m', 'unittest',
                                    args.module, '-v'], env=environment,
                                   cwd=Path(__file__).resolve().parent)
        if completed.returncode:
            raise RuntimeError('Isolated PostgreSQL suite failed: ' + args.module)
    finally:
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql('DROP DATABASE "' + name + '" WITH (FORCE)')
            print('Disposable clearing database removed', flush=True)
        admin.dispose()


if __name__ == '__main__':
    main()
