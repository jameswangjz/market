"""Pure funding tests: no database, HTTP sources or payment execution."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import unittest

from app.refund_funding import FundingSource, FundingUnavailable, allocate_refund


class RefundFundingTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.source = FundingSource('original', 'original-order', self.at,
                                    Decimal('100'), Decimal('0'), Decimal('0'), 'CNY')

    def allocate(self, amount, sources):
        return allocate_refund(Decimal(amount), sources, currency='CNY')

    def test_multiple_topups_newest_first_and_original_route(self):
        middle = replace(self.source, payment_id='topup-1', source_order_id='change-1',
                         paid_at=self.at + timedelta(days=1), paid_amount=Decimal('30'))
        newest = replace(middle, payment_id='topup-2', source_order_id='change-2',
                         paid_at=self.at + timedelta(days=2), paid_amount=Decimal('20'))
        result = self.allocate('75', [middle, self.source, newest])
        self.assertEqual([(row.payment_id, row.source_order_id, row.amount) for row in result],
                         [('topup-2', 'change-2', Decimal('20.00')),
                          ('topup-1', 'change-1', Decimal('30.00')),
                          ('original', 'original-order', Decimal('25.00'))])
        self.assertEqual(sum(row.amount for row in result), Decimal('75'))

    def test_refunded_and_reserved_reduce_each_source_and_exhausted_is_skipped(self):
        newest = replace(self.source, payment_id='new', paid_at=self.at + timedelta(days=1),
                         refunded=Decimal('60'), reserved=Decimal('30'))
        exhausted = replace(newest, payment_id='exhausted', reserved=Decimal('40'))
        result = self.allocate('30', [self.source, newest, exhausted])
        self.assertEqual([(row.payment_id, row.amount) for row in result],
                         [('new', Decimal('10')), ('original', Decimal('20'))])

    def test_equal_instants_have_stable_payment_id_order(self):
        a = replace(self.source, payment_id='a')
        b = replace(self.source, payment_id='b',
                    paid_at=self.at.astimezone(timezone(timedelta(hours=8))))
        self.assertEqual(self.allocate('150', [b, a]), self.allocate('150', [a, b]))
        self.assertEqual([row.payment_id for row in self.allocate('150', [b, a])], ['a', 'b'])

    def test_insufficient_balance_rejects_whole_request(self):
        source = replace(self.source, refunded=Decimal('20'), reserved=Decimal('30'))
        with self.assertRaisesRegex(FundingUnavailable, 'insufficient_refundable_funding'):
            self.allocate('50.01', [source])
        self.assertEqual(self.allocate('50', [source])[0].amount, Decimal('50'))

    def test_inconsistent_occupancy_rejects_even_unused_source(self):
        for refunded, reserved in [('100.01', '0'), ('60', '40.01')]:
            with self.subTest(refunded=refunded, reserved=reserved):
                bad = replace(self.source, payment_id='bad', refunded=Decimal(refunded), reserved=Decimal(reserved))
                with self.assertRaisesRegex(FundingUnavailable, 'funding_balance_inconsistent'):
                    self.allocate('1', [self.source, bad])

    def test_missing_unknown_duplicate_and_currency_fail_closed(self):
        for sources in (None, [], [None], [self.source, self.source],
                        [replace(self.source, payment_id='')], [replace(self.source, source_order_id=' ')],
                        [replace(self.source, currency='USD')],
                        [self.source, replace(self.source, payment_id='other', currency='USD')],
                        [replace(self.source, currency='cny')],
                        [replace(self.source, paid_at=self.at.replace(tzinfo=None))],
                        [replace(self.source, paid_at=None)]):
            with self.subTest(sources=sources), self.assertRaises(FundingUnavailable):
                self.allocate('1', sources)
        with self.assertRaises(FundingUnavailable):
            allocate_refund(Decimal('1'), [self.source], currency='USD')

    def test_money_requires_known_nonnegative_finite_exact_cents(self):
        invalid = [None, '1', 1, True, Decimal('-0.01'), Decimal('NaN'),
                   Decimal('Infinity'), Decimal('-Infinity'), Decimal('0.001')]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(FundingUnavailable):
                    allocate_refund(value, [self.source], currency='CNY')
                for field in ('paid_amount', 'refunded', 'reserved'):
                    with self.subTest(field=field), self.assertRaises(FundingUnavailable):
                        self.allocate('1', [replace(self.source, **{field: value})])
        with self.assertRaises(FundingUnavailable):
            self.allocate('0', [self.source])

    def test_input_is_unchanged_and_allocations_are_immutable(self):
        sources = [self.source, replace(self.source, payment_id='new', paid_at=self.at + timedelta(days=1))]
        before = tuple(sources)
        result = self.allocate('125', sources)
        self.assertEqual(tuple(sources), before)
        self.assertIsInstance(result, tuple)
        with self.assertRaises(FrozenInstanceError):
            result[0].amount = Decimal('0')
        with self.assertRaises(FrozenInstanceError):
            self.source.reserved = Decimal('1')

    def test_large_exact_amounts_do_not_round_under_decimal_context(self):
        amount = Decimal('123456789012345678901234567890.12')
        source = replace(self.source, paid_amount=amount)
        with localcontext() as context:
            context.prec = 6
            result = allocate_refund(amount, [source], currency='CNY')
        self.assertEqual(result[0].amount, amount)
        self.assertEqual(result[0].amount.as_tuple().exponent, -2)


if __name__ == '__main__':
    unittest.main()
