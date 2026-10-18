"""Opt-in actual HTTP funded combinations on a private PostgreSQL schema."""
import os
import unittest


import test_settlement_batch_pg_http as pg_fixture
import test_settlement_funded_combinations_http as funded
import test_settlement_funded_segment_bindings_http as segments
import test_settlement_version_funding_http as version_funding


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class FundedCombinationPostgresHTTPTests(pg_fixture.PrivatePostgresFixtureMixin,
                                        funded.FundedCombinationHTTPTests):
    def test_existing_varchar12_migrates_idempotently_then_actual_return_capture(self):
        from sqlalchemy import inspect, text
        engine = self.m.engine
        with engine.begin() as connection:
            connection.execute(text('ALTER TABLE settlement_finance_positions ALTER COLUMN kind TYPE VARCHAR(12)'))
        kind_length = lambda: next(c for c in inspect(engine).get_columns('settlement_finance_positions')
                                   if c['name'] == 'kind')['type'].length
        self.assertEqual(kind_length(), 12)
        for _ in range(2):
            self.m.settlement_finance['ensure_schema'](engine)
            self.assertEqual(kind_length(), 30)
        self.test_actual_external_refund_posts_unpaid_exact_payer_return_and_preserves_completion()

    def test_existing_transaction_varchar12_migrates_then_actual_absorption_capture(self):
        from sqlalchemy import inspect, text
        engine = self.m.engine
        with engine.begin() as connection:
            connection.execute(text('ALTER TABLE settlement_finance_transactions ALTER COLUMN kind TYPE VARCHAR(12)'))
        for expected in (12, 30, 30):
            kind = next(c for c in inspect(engine).get_columns('settlement_finance_transactions') if c['name'] == 'kind')
            self.assertEqual(kind['type'].length, expected)
            self.m.settlement_finance['ensure_schema'](engine)
        self.test_actual_absorbed_refund_cost_exception_posts_independent_loss()


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class FundedSegmentBindingPostgresHTTPTests(pg_fixture.PrivatePostgresFixtureMixin,
                                           segments.FundedSegmentBindingHTTPTests):
    pass


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class VersionFundingPostgresHTTPTests(pg_fixture.PrivatePostgresFixtureMixin,
                                      version_funding.VersionFundingHTTPTests):
    pass


if __name__ == '__main__':
    unittest.main()
