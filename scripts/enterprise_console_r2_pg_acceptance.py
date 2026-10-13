"""Run selected R2 route tests on private PostgreSQL copies of synthetic fixtures.

Each test first builds its existing synthetic SQLite fixture, then copies it
into a new PostgreSQL schema before exercising the actual production handlers.
No startup seed or public-schema business mutation is performed.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import unittest

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    url = make_url(os.environ['DATABASE_URL'])
    if url.get_backend_name() != 'postgresql':
        raise ValueError('PostgreSQL required')
    admin = create_engine(url)
    sys.path.insert(0, str(args.tests_dir))
    created, outcomes = set(), []
    modules = ['test_enterprise_console_scope', 'test_product_categories', 'test_application_health',
               'test_console_business_scope', 'test_saas_sync_management']

    def pg_case(original):
        class PostgreSQLCase(original):
            def setUp(self):
                super().setUp()
                source = getattr(self, 'engine', self.m.engine)
                schema = 'qa_r2_' + secrets.token_hex(10)
                with admin.begin() as connection:
                    connection.execute(CreateSchema(schema))
                created.add(schema)
                private_url = url.update_query_dict({'options': '-csearch_path=' + schema})
                target = create_engine(private_url)
                self.addCleanup(target.dispose)

                def cleanup():
                    target.dispose()
                    with admin.begin() as connection:
                        connection.execute(DropSchema(schema, cascade=True))
                    created.remove(schema)
                self.addCleanup(cleanup)
                self.m.Base.metadata.create_all(target)
                self.m.product_categories['ensure_schema'](target)
                with source.connect() as connection:
                    rows = [(table, [dict(row) for row in connection.execute(select(table)).mappings()])
                            for table in self.m.Base.metadata.sorted_tables]
                with target.begin() as connection:
                    if connection.scalar(text('select current_schema()')) != schema:
                        raise AssertionError('Fixture connection escaped private schema')
                    for table, values in rows:
                        if values:
                            if table.name == 'product_categories':
                                values.sort(key=lambda row: bool(row.get('parent_id')))
                            connection.execute(table.insert(), values)
                sessions = sessionmaker(bind=target, autoflush=False)
                self.m.engine, self.m.SessionLocal = target, sessions
                if hasattr(self, 'engine'):
                    self.engine = target
                if hasattr(self, 'Session'):
                    self.Session = sessions
                self.addCleanup(source.dispose)
                outcomes.append({'test': self.id(), 'schema_created': True})
        return PostgreSQLCase

    suite = unittest.TestSuite()
    for module_name in modules:
        module = importlib.import_module(module_name)
        classes = [value for value in vars(module).values()
                   if isinstance(value, type) and issubclass(value, unittest.TestCase)
                   and value.__module__ == module_name]
        for test_class in classes:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(pg_case(test_class)))
    try:
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        evidence = {'status': 'passed' if result.wasSuccessful() else 'failed',
                    'scope': 'actual R2 handlers and constraints; synthetic fixtures in private PostgreSQL',
                    'tests': result.testsRun, 'failures': len(result.failures),
                    'errors': len(result.errors), 'cleanup_verified': not created,
                    'limits': ['no external SaaS/APISIX fault injection', 'no concurrent PostgreSQL lock stress'],
                    'cases': outcomes}
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in evidence.items() if key != 'cases'}))
        if not result.wasSuccessful() or created:
            raise SystemExit(1)
    finally:
        for schema in list(created):
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


if __name__ == '__main__':
    main()
