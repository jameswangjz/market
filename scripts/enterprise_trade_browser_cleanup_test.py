"""Offline API cleanup graph tests against an isolated SQLite backend."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import api_auto_delivery_runtime_acceptance as api


class HealthMessageGuards(unittest.TestCase):
    def setUp(self):
        self.ids = api.fixture_ids('qa-api-auto-012345abcdef')
        self.instant = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.row = SimpleNamespace(target_type='product', target_id=self.ids['products'][1],
            category='system', tenant_id=self.ids['enterprises'][1],
            event_key=f"application-health:{self.ids['products'][1]}:{self.ids['versions'][1]}:incident:20261014120000")
        self.observation = SimpleNamespace(id='observation', product_id=self.row.target_id,
            version_id=self.ids['versions'][1], last_slot=100)
        self.slot = SimpleNamespace(id='slot', product_id=self.row.target_id, version_id=self.ids['versions'][1],
            status='failed', completed_at=self.instant, claimed_at=self.instant, slot=100)

    def validate(self):
        api.validate_health_message(self.row, self.ids, self.observation, [self.slot], {'observation'}, {'slot'})

    def test_exact_incident_and_recovery_slot_proof(self):
        self.validate()
        self.row.event_key = self.row.event_key.replace(':incident:', ':recovered:')
        with self.assertRaises(api.AcceptanceFailure):
            self.validate()
        healthy = SimpleNamespace(**(vars(self.slot) | {'id':'healthy', 'status':'healthy', 'slot':101}))
        api.validate_health_message(self.row, self.ids, self.observation, [self.slot, healthy], {'observation'}, {'slot','healthy'})

    def test_wrong_target_tenant_version_event_and_observation_rejected(self):
        for field, value in [('target_id','qa-vcq-a044ea9ea181-p2'), ('tenant_id',self.ids['enterprises'][0]),
                             ('category','order'), ('event_key',self.row.event_key+'extra')]:
            original = getattr(self.row, field)
            setattr(self.row, field, value)
            with self.subTest(field=field), self.assertRaises(api.AcceptanceFailure):
                self.validate()
            setattr(self.row, field, original)
        self.observation.version_id='foreign'
        with self.assertRaises(api.AcceptanceFailure):
            self.validate()

    def test_missing_or_wrong_slot_and_unjournaled_observation_rejected(self):
        for slots, observations, slot_ids in [([], {'observation'}, {'slot'}),
                                             ([self.slot], set(), {'slot'}),
                                             ([self.slot], {'observation'}, set())]:
            with self.assertRaises(api.AcceptanceFailure):
                api.validate_health_message(self.row,self.ids,self.observation,slots,observations,slot_ids)
        self.slot.claimed_at += timedelta(seconds=1)
        with self.assertRaises(api.AcceptanceFailure):
            self.validate()

    def test_transport_two_ownership_paths(self):
        self.assertEqual(api.message_edge_ownership('owned','outside',{'owned'},{'fixture'}),'owned_message')
        self.assertEqual(api.message_edge_ownership('outside','fixture',{'owned'},{'fixture'}),'fixture_user_external_message')
        with self.assertRaises(api.AcceptanceFailure):
            api.message_edge_ownership('outside','outside',{'owned'},{'fixture'})


class CleanupGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / 'backend/tests/test_message_business_events.py'
        spec = importlib.util.spec_from_file_location('enterprise_trade_cleanup_isolated_backend', path)
        cls.fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture_module)
        cls.fixture_module.BusinessRoutesHTTPTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_module.BusinessRoutesHTTPTests.tearDownClass()

    def setUp(self):
        self.fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        self.fixture.setUp()
        self.m = self.fixture.m
        self.ids = api.fixture_ids()
        api.prepare(self.m, self.ids)

    def tearDown(self):
        self.fixture.tearDown()

    def test_scoped_delete_preserves_external_message_and_other_recipients(self):
        from sqlalchemy import delete, select
        m, ids = self.m, self.ids
        Message, Receipt, Delivery, Attempt, Expired = (m.message_center[k] for k in
            ('Message','Receipt','Delivery','DeliveryAttempt','ExpiredUrgent'))
        instant = datetime(2026,10,14,12,tzinfo=timezone.utc)
        Observation, Slot = m.application_health['Observation'], m.application_health['Slot']
        with m.SessionLocal() as db:
            db.add(Observation(product_id=ids['products'][1],version_id=ids['versions'][1],last_slot=100,
                last_checked_at=instant,last_status='failed',failure_streak=3,incident_active=True,
                incident_started_at=instant,updated_at=instant))
            db.add(Slot(product_id=ids['products'][1],version_id=ids['versions'][1],slot=100,
                        claimed_at=instant,completed_at=instant,status='failed'))
            owned = m.message_center['create'](db,[ids['users']['seller'],'ops'],'Owned health','unit',
                'product',ids['products'][1],category='system',tenant_id=ids['enterprises'][1],
                event_key=f"application-health:{ids['products'][1]}:{ids['versions'][1]}:incident:20261014120000")
            external = m.message_center['create'](db,[ids['users']['buyer'],'ops'],'External global','unit',
                'development_task','',category='ops',event_key='external-global-unit')
            db.flush()
            receipt=db.scalar(select(Receipt).where(Receipt.message_id==external.id,Receipt.user_id==ids['users']['buyer']))
            delivery=db.scalar(select(Delivery).where(Delivery.message_id==external.id,Delivery.user_id==ids['users']['buyer']).limit(1))
            db.add(Attempt(delivery_id=delivery.id,lease_token='offline',attempt_no=1))
            db.add(Expired(receipt_id=receipt.id,message_id=external.id,user_id=ids['users']['buyer'],expired_at=instant))
            db.commit()
            external_id, owned_id = external.id, owned.id
            before={c.name:getattr(external,c.name) for c in Message.__table__.columns}
            other_receipts=set(db.scalars(select(Receipt.id).where(Receipt.message_id==external.id,Receipt.user_id=='ops')))
            other_deliveries=set(db.scalars(select(Delivery.id).where(Delivery.message_id==external.id,Delivery.user_id=='ops')))
            predicates=api.owned_scopes(m,db,ids)
            self.assertNotIn(external_id,set(db.scalars(select(Message.id).where(predicates[Message.__tablename__]))))
            for table in reversed(m.Base.metadata.sorted_tables):
                if table.name in predicates:
                    db.execute(delete(table).where(predicates[table.name]))
            db.commit()
            external=db.get(Message,external_id)
            self.assertEqual({c.name:getattr(external,c.name) for c in Message.__table__.columns},before)
            self.assertIsNone(db.get(Message,owned_id))
            self.assertEqual(set(db.scalars(select(Receipt.id).where(Receipt.message_id==external_id))),other_receipts)
            self.assertEqual(set(db.scalars(select(Delivery.id).where(Delivery.message_id==external_id))),other_deliveries)
            self.assertIsNotNone(db.get(m.User,'ops'))
            self.assertIsNotNone(db.get(m.Product,'product'))
            self.assertTrue(all(db.get(m.User,uid) is None for uid in ids['users'].values()))

    def test_start_payment_requires_exact_buyer_transition(self):
        m, ids=self.m,self.ids
        with m.SessionLocal() as db:
            order=m.Order(product_id=ids['products'][0],product_version_id=ids['versions'][0],
                buyer_user_id=ids['users']['buyer'],buyer_enterprise_id=ids['enterprises'][0],
                provider_enterprise_id=ids['enterprises'][1],order_no='OFFLINE-OWNED',
                buyer_name=ids['enterprises'][0],product_name=ids['products'][0])
            db.add(order);db.flush()
            m.message_center['create'](db,[ids['users']['buyer']],'Owned start','unit','order',order.id,
                category='order',tenant_id=ids['enterprises'][0],event_key=f'business:order:{order.id}:start_payment:paying')
            db.flush()
            with self.assertRaisesRegex(api.AcceptanceFailure,'owned buyer transition'):
                api.owned_scopes(m,db,ids)
            log=m.OrderStateLog(order_id=order.id,domain='payment',from_status='unpaid',to_status='paying',
                action='start_payment',operator=ids['users']['buyer']+'@example.invalid')
            db.add(log);db.flush()
            api.owned_scopes(m,db,ids)
            log.operator='outsider@example.invalid';db.flush()
            with self.assertRaisesRegex(api.AcceptanceFailure,'owned buyer transition'):
                api.owned_scopes(m,db,ids)


if __name__ == '__main__':
    unittest.main()
