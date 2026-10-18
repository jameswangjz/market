"""Narrow tests of the production CSV function without application startup."""
import ast
import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import HTTPException, Response


SHARE_HEADERS = ['\u5e73\u53f0\u8fd0\u8425\u65b9', '\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9',
                 '\u6570\u636e\u670d\u52a1\u65b9', '\u4e13\u5bb6', '\u6e20\u9053']
AMOUNT_HEADERS = ['\u8ba2\u5355\u6570\u91cf', '\u8ba2\u5355\u91d1\u989d',
                  '\u8ba2\u5355\u6210\u672c', '\u8ba2\u5355\u5229\u6da6']


class LegacySettlementExportTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'app' / 'main.py'
        function = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'export_settlement_report')
        function.decorator_list = []
        tree = ast.Module(body=[ast.ImportFrom(module='__future__',
                          names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
        self.report = Mock()
        self.audit = Mock()
        self.db = Mock()
        self.user = SimpleNamespace(email='finance@example.invalid')
        namespace = dict(csv=csv, StringIO=StringIO, Response=Response, HTTPException=HTTPException,
                         Depends=lambda _: None, current_user=None, db_session=None,
                         settlement_reports=self.report, audit=self.audit)
        exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), namespace)
        self.export = namespace['export_settlement_report']
        amounts = dict(gross_amount=Decimal('-12.50'), cost_amount=Decimal('5.00'),
                       profit_amount=Decimal('-17.50'), platform_fee=Decimal('-1.25'),
                       provider_share=Decimal('-16.25'), service_share=0, expert_fee=0, channel_fee=0)
        name = 'ACME, "Research"\r\nLine 2'
        self.data = dict(
            summary=dict(amounts, participants=[dict(participant_type='provider', participant_name=name,
                                                    order_count=1, amount=Decimal('-16.25'))]),
            monthly=[dict(amounts, label='2026-10', order_count=1)],
            products=[dict(amounts, product_id='p1', product_name=name, order_count=1)],
            items=[dict(amounts, settlement_no='SET-1', order_id='o1', order_no='ORD-1', product_name=name,
                        status='locked', refund_amount=Decimal('-2.00'), net_amount=Decimal('-12.50'),
                        participants=[dict(participant_type='provider', amount=Decimal('-16.25'))],
                        created_at='2026-10-18T00:00:00+00:00', source_kind='ordinary_refund',
                        refund_id='r1', refund_no='REF-1', is_refund=True)])
        self.report.return_value = self.data

    def rows(self, kind='details'):
        response = self.export(kind=kind, status='locked', start='2026-10-01', end='2026-10-31',
                               user=self.user, db=self.db)
        self.assertTrue(response.body.startswith(b'\xef\xbb\xbf'))
        self.assertTrue(response.headers['content-type'].startswith('text/csv'))
        self.assertEqual(response.headers['content-disposition'],
                         f'attachment; filename=settlement-{kind}.csv')
        self.report.assert_called_with(status='locked', start='2026-10-01', end='2026-10-31',
                                       user=self.user, db=self.db)
        return list(csv.reader(StringIO(response.body.decode('utf-8-sig'), newline='')))

    def test_details_headers_quoted_names_and_native_negative_numbers(self):
        rows = self.rows()
        self.assertEqual(rows[0], ['settlement_no', 'order_id', 'order_no', 'product_name', 'status',
            'order_amount', 'refund_amount', 'net_amount', 'cost_amount', 'profit_amount'] + SHARE_HEADERS +
            ['created_at', 'source_kind', 'refund_id', 'refund_no', 'is_refund'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][3], self.data['items'][0]['product_name'])
        self.assertEqual(rows[1][5:10], ['-12.50', '-2.00', '-12.50', '5.00', '-17.50'])
        self.assertEqual(rows[1][11], '-16.25')
        self.assertEqual(rows[1][-1], 'True')
        self.db.commit.assert_called_once()
        self.audit.assert_called_once()

    def test_products_and_summary_preserve_headers_sections_and_cells(self):
        products = self.rows('products')
        self.assertEqual(products[0], ['\u4ea7\u54c1\u7ef4\u5ea6\u6e05\u7b97\u6c47\u603b'])
        self.assertEqual(products[1], ['product_id', 'product_name'] + AMOUNT_HEADERS + SHARE_HEADERS)
        self.assertEqual(products[2][1], self.data['products'][0]['product_name'])
        self.assertEqual(products[2][3], '-12.50')
        summary = self.rows('summary')
        self.assertEqual(summary[0], ['\u603b\u4f53\u8ba2\u5355\u6e05\u7b97\u6c47\u603b'])
        self.assertEqual(summary[1], ['\u6307\u6807', '\u91d1\u989d'])
        self.assertEqual(summary[2:5], [[AMOUNT_HEADERS[1], '-12.50'], [AMOUNT_HEADERS[2], '5.00'],
                                      [AMOUNT_HEADERS[3], '-17.50']])
        self.assertEqual(summary[5], [])
        self.assertEqual(summary[6], ['\u603b\u4f53\u53c2\u4e0e\u65b9\u6c47\u603b'])
        self.assertEqual(summary[7], ['participant_type', 'participant_name', 'order_count', 'settlement_amount'])
        self.assertEqual(summary[8][1], self.data['summary']['participants'][0]['participant_name'])
        self.assertEqual(summary[8][-1], '-16.25')
        self.assertEqual(summary[9], [])
        self.assertEqual(summary[10], ['\u81ea\u7136\u6708\u6c47\u603b'])
        self.assertEqual(summary[11], ['\u6708\u4efd'] + AMOUNT_HEADERS + SHARE_HEADERS)
        self.assertEqual(summary[12][2], '-12.50')

    def test_text_formula_and_leading_control_prefixes_are_escaped_in_every_export(self):
        for text in ('=1+1', '+SUM(1,2)', '-12.50', '@SUM(A1)', '  =1', '\tplain',
                     '  \rplain', '\nplain', '\u2003\tplain', '\t @SUM(A1)'):
            with self.subTest(text=text):
                self.data['items'][0]['product_name'] = text
                self.data['products'][0]['product_name'] = text
                self.data['summary']['participants'][0]['participant_name'] = text
                self.data['monthly'][0]['label'] = text
                self.assertEqual(self.rows()[1][3], "'" + text)
                self.assertEqual(self.rows('products')[2][1], "'" + text)
                summary = self.rows('summary')
                self.assertEqual(summary[8][1], "'" + text)
                self.assertEqual(summary[12][0], "'" + text)
                self.assertEqual(summary[12][2], '-12.50')

    def test_unknown_kind_is_422_without_query_audit_or_commit(self):
        for kind in ('unknown', '', 'DETAILS'):
            with self.subTest(kind=kind), self.assertRaises(HTTPException) as error:
                self.export(kind=kind, user=self.user, db=self.db)
            self.assertEqual(error.exception.status_code, 422)
        self.report.assert_not_called()
        self.audit.assert_not_called()
        self.db.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
