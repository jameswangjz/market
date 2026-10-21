"""Direct immutable-reference guards, using only disposable SQLite fixtures."""
import hashlib
import json
import unittest

from fastapi import HTTPException
from sqlalchemy import Column, String

from app.settlement_finance_adapter import protect, protect_rebuild
import test_settlement_finance as finance_fixture


class ReferenceProtectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = finance_fixture.FinanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db, self.ns = self.fixture.db, self.fixture.ns

    def blocked(self, ids):
        for guard in (protect, protect_rebuild):
            with self.subTest(guard=guard.__name__, ids=ids):
                with self.assertRaises(HTTPException) as raised:
                    guard(self.ns, self.db, ids)
                self.assertEqual(raised.exception.status_code, 409)

    def test_unused_baseline_still_rebuildable(self):
        self.fixture.capture()
        protect_rebuild(self.ns, self.db, ['s'])

    def test_each_child_mapping_directly_protects_both_ends(self):
        for name in ('RefundSource', 'RefundPositionSource'):
            with self.subTest(model=name), self.db.begin_nested():
                model = self.fixture.h[name]
                values = dict(settlement_id='negative', source_settlement_id='second-source', segment_id='term2')
                if name == 'RefundSource':
                    values.update(source_hash='a'*64, evidence_json='{}', evidence_hash='b'*64)
                else:
                    values['position_id'] = 'mapped-position'
                self.db.add(model(**values))
                self.db.flush()
                self.blocked(['negative'])
                self.blocked(['second-source'])

    def manifest(self, payload):
        Base = self.ns['Base']
        class Manifest(Base):
            __tablename__ = 'fixture_frozen_manifests'
            settlement_id = Column(String, primary_key=True)
            payload_json = Column(String)
            payload_hash = Column(String)
        Base.metadata.create_all(self.fixture.engine)
        self.ns['refund_settlement'] = {'MultiSourceManifest': Manifest}
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        self.db.add(Manifest(settlement_id='not-captured', payload_json=raw,
                             payload_hash=hashlib.sha256(raw.encode()).hexdigest()))
        self.db.flush()

    def test_frozen_manifest_without_baseline_protects_target_and_every_source(self):
        self.manifest({'references': [{'reference_settlement_id': 'first'},
                                      {'reference_settlement_id': 'second'}]})
        for identity in ('not-captured', 'first', 'second'):
            self.blocked([identity])
        protect_rebuild(self.ns, self.db, ['unrelated'])

    def test_invalid_frozen_manifest_fails_closed(self):
        self.manifest({'references': ['invalid']})
        self.blocked(['potential-source'])

    def test_independent_funding_facts_protect_rebuild(self):
        for name in ('FundingProof', 'FundingAbsorption', 'CostExceptionPosting'):
            model = self.fixture.h[name]
            # Guard reads direct settlement identity, not optional indirect links.
            values = {column.name: 'x' for column in model.__table__.columns}
            values['settlement_id'] = name
            if 'amount' in values:
                values['amount'] = 1
            self.db.execute(model.__table__.insert().values(**values))
            self.blocked([name])


if __name__ == '__main__':
    unittest.main()
