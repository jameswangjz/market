"""Run only the opt-in clearing race suite in a newly created disposable DB."""
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def main():
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
                                    'test_settlement_finance_pg', '-v'], env=environment,
                                   cwd=Path(__file__).resolve().parent)
        if completed.returncode:
            raise RuntimeError('Isolated PostgreSQL race suite failed')
    finally:
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql('DROP DATABASE "' + name + '" WITH (FORCE)')
            print('Disposable clearing database removed', flush=True)
        admin.dispose()


if __name__ == '__main__':
    main()
