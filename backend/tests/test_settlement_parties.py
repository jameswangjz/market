"""Isolated participant routes and hooks; no application startup or production DB."""
import json
import unittest
from decimal import Decimal

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import Boolean, Column, Integer, Numeric, String, Text, create_engine, event, select
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool

from app.settlement_parties import install


class SettlementPartiesTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()

        def model(class_name, table, **columns):
            return type(class_name, (Base,), dict(__tablename__=table,
                        id=Column(String(36), primary_key=True), **columns))

        User = model('User', 'users', name=Column(String), platform_role=Column(String),
                     verified_status=Column(String), activation_status=Column(String), is_active=Column(Boolean))
        Enterprise = model('Enterprise', 'enterprises', name=Column(String), verification_status=Column(String))
        Membership = model('Membership', 'memberships', user_id=Column(String), enterprise_id=Column(String),
                           role=Column(String), status=Column(String), business_roles=Column(String), created_at=Column(Integer))
        Product = model('Product', 'products', enterprise_id=Column(String))
        Order = model('Order', 'orders', provider_enterprise_id=Column(String), product_id=Column(String),
                      economic_snapshot_json=Column(Text))
        Refund = model('Refund', 'refunds', order_id=Column(String), status=Column(String), amount=Column(Numeric(14, 2)))
        Baseline = type('Baseline', (Base,), dict(__tablename__='baselines',
            settlement_id=Column(String, primary_key=True), order_id=Column(String), currency=Column(String),
            revenue=Column(Numeric(14, 2)), cost=Column(Numeric(14, 2)), reference_settlement_id=Column(String)))
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})

        @event.listens_for(self.engine, 'connect')
        def foreign_keys(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')

        self.audits = []
        app = FastAPI()

        def db_session():
            with Session(self.engine) as db:
                yield db

        def current_user(x_actor: str = Header('ops')):
            with Session(self.engine) as db:
                return db.get(User, x_actor)

        self.ns = dict(Base=Base, User=User, Enterprise=Enterprise, Membership=Membership,
                       Product=Product, Order=Order, Refund=Refund, app=app,
                       current_user=current_user, db_session=db_session,
                       audit=lambda *args, **kw: self.audits.append((args[2], kw)),
                       settlement_finance=dict(Baseline=Baseline))
        self.h = install(self.ns)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            for uid, role in [('ops', 'platform_operator'), ('finance', 'finance_settlement'),
                              ('root', 'super_admin'), ('provider', ''), ('other', ''),
                              ('member', ''), ('auditor', 'security_compliance')]:
                db.add(User(id=uid, name=uid, platform_role=role, verified_status='verified',
                            activation_status='active', is_active=True))
            for eid in ('e1', 'e2'):
                db.add(Enterprise(id=eid, name=eid, verification_status='verified'))
            for uid, eid, role in [('provider', 'e1', 'super_admin'), ('other', 'e2', 'enterprise_admin'),
                                   ('member', 'e1', 'member')]:
                db.add(Membership(id=uid, user_id=uid, enterprise_id=eid, role=role,
                                  status='active', business_roles='provider,user', created_at=1))
            db.add_all([Product(id='p1', enterprise_id='e1'), Product(id='p2', enterprise_id='e2')])
            db.add_all([Order(id='o1', product_id='p1', provider_enterprise_id='e1', economic_snapshot_json='{"currency":"CNY"}'),
                        Order(id='o2', product_id='p2', provider_enterprise_id='e2', economic_snapshot_json='{}')])
            db.flush()
            db.add_all([Refund(id='r1', order_id='o1', status='completed', amount=50),
                        Refund(id='r2', order_id='o1', status='approved', amount=50),
                        Refund(id='r3', order_id='o2', status='completed', amount=50)])
            db.add(Baseline(settlement_id='b1', order_id='o1', currency='CNY', revenue=150, cost=100,
                            reference_settlement_id=''))
            db.commit()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def request(self, method, path, actor='ops', expected=200, **kwargs):
        response = self.client.request(method, path, headers={'X-Actor': actor}, **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def party(self, key='a', actor='ops', **values):
        return self.request('POST', '/api/settlement-parties', actor, json=dict(
            request_id=key, name='Same Name', enterprise_id='e1', kind='expert', currency='CNY', **values))

    def config(self, rows, key='config', actor='ops', product='p1', expected=200):
        return self.request('POST', f'/api/settlement-parties/products/{product}/allocations', actor,
                            expected, json=dict(request_id=key, allocations=rows))

    def exception(self, key='exception', actor='provider', expected=200, **values):
        body = dict(request_id=key, refund_id='r1', retained_cost='20.00', reason='Nonreturnable work',
                    evidence_refs=['invoice:1'], bearer_participant_id=self.bearer)
        body.update(values)
        return self.request('POST', '/api/settlement-cost-exceptions', actor, expected, json=body)

    def decision(self, eid, actor='ops', expected=200, **values):
        body = dict(request_id='decision', decision='approve', expected_version=1, reason='Checked evidence')
        body.update(values)
        return self.request('POST', f'/api/settlement-cost-exceptions/{eid}/decision', actor, expected, json=body)

    def test_install_idempotency_registry_identity_audit_and_scope(self):
        self.assertIs(install(self.ns), self.h)
        self.assertEqual(len(self.ns['app'].routes), 12)
        first = self.party()
        self.assertEqual(first, self.party())
        second = self.party('b')
        self.assertNotEqual(first['id'], second['id'])
        body = dict(request_id='a', name='changed', enterprise_id='e1', kind='expert', currency='CNY')
        self.request('POST', '/api/settlement-parties', expected=409, json=body)
        own = self.party('own', 'provider')
        self.assertEqual(own['creator_id'], 'provider')
        self.assertEqual(self.request('GET', '/api/settlement-parties', 'provider')['total'], 1)
        self.assertEqual(self.request('GET', '/api/settlement-parties', 'other')['total'], 0)
        self.assertEqual(self.request('GET', '/api/settlement-parties')['total'], 3)
        for actor in ('member', 'auditor'):
            self.request('GET', '/api/settlement-parties', actor, 403)
        body.update(request_id='other', enterprise_id='e2')
        self.request('POST', '/api/settlement-parties', 'provider', 403, json=body)
        body.update(enterprise_id='missing')
        self.request('POST', '/api/settlement-parties', expected=422, json=body)
        self.assertEqual(len(self.audits), 3)
        self.assertTrue(all('request_id' in a[1]['after'] for a in self.audits))

    def test_active_verified_membership_and_business_scope(self):
        for model, identity, field, value in [('User', 'provider', 'is_active', False),
                ('User', 'provider', 'verified_status', 'pending'),
                ('Enterprise', 'e1', 'verification_status', 'pending'),
                ('Membership', 'provider', 'status', 'inactive'),
                ('Membership', 'provider', 'business_roles', 'user')]:
            with self.subTest(field=field):
                with Session(self.engine) as db:
                    row = db.get(self.ns[model], identity)
                    old = getattr(row, field)
                    setattr(row, field, value)
                    db.commit()
                self.request('GET', '/api/settlement-parties', 'provider', 403)
                with Session(self.engine) as db:
                    setattr(db.get(self.ns[model], identity), field, old)
                    db.commit()
        self.request('GET', '/api/settlement-parties?enterprise_id=e2', 'provider', 403)
        self.request('GET', '/api/settlement-parties?business_view=buyer', 'provider', 403)

    def test_versions_weights_multiple_recipients_and_frozen_cents(self):
        a, b, c = [self.party(k)['id'] for k in ('a', 'b', 'c')]
        rows = [dict(participant_id=p, participant_type='expert', weight=w)
                for p, w in [(a, '33.33'), (b, '33.33'), (c, '33.34')]]
        v1 = self.config(rows)
        self.assertEqual(v1, self.config(rows))
        self.config(rows, 'foreign', 'other', expected=404)
        self.config([dict(rows[0], weight='99.999999999')], 'bad', expected=422)
        self.config([dict(rows[0], weight='NaN')], 'nan', expected=422)
        self.config([dict(rows[0], participant_type='channel', weight=100)], 'kind', expected=422)
        self.config([dict(rows[0], participant_id='unknown', weight=100)], 'id', expected=422)
        self.config([dict(rows[0], weight=50)] * 2, 'duplicate', expected=422)
        self.config([dict(rows[0], weight=100)], 'v2')
        with Session(self.engine) as db:
            order = db.get(self.ns['Order'], 'o1')
            order.economic_snapshot_json = json.dumps(dict(currency='CNY', settlement_parties=v1))
            for amount in ('0.01', '0.02', '100.01', '-100.01'):
                result = self.h['allocations'](db, order, 'expert', Decimal(amount))
                self.assertEqual(len(result), 3)
                self.assertEqual(sum(Decimal(r['amount']) for r in result), Decimal(amount))
                self.assertEqual(result, self.h['allocations'](db, order, 'expert', Decimal(amount)))
            cent = {r['participant_id']: Decimal(r['amount']) for r in self.h['allocations'](db, order, 'expert', '.01')}
            self.assertEqual(cent, {a: Decimal(0), b: Decimal(0), c: Decimal('.01')})
            whole = {r['participant_id']: Decimal(r['amount']) for r in self.h['allocations'](db, order, 'expert', '100.00')}
            self.assertEqual(whole, {a: Decimal('33.33'), b: Decimal('33.33'), c: Decimal('33.34')})
            self.assertEqual(self.h['allocations'](db, order, 'provider', 100)[0]['participant_id'], 'e1')
            self.assertTrue(self.h['allocations'](db, order, 'platform', 10)[0]['internal'])
            self.assertTrue(self.h['allocations'](db, order, 'channel', 5)[0]['unassigned'])
            order.economic_snapshot_json = '{}'
            self.assertTrue(self.h['allocations'](db, order, 'expert', 5)[0]['unassigned'])
            self.assertEqual(db.scalars(select(self.h['Configuration'])).all().__len__(), 2)

    def test_immutable_configuration_and_participant(self):
        a = self.party()['id']
        v = self.config([dict(participant_id=a, participant_type='expert', weight=100)])
        for model, identity, field, value in [('Participant', a, 'name', 'Changed'),
                                              ('Configuration', v['id'], 'version', 99)]:
            with Session(self.engine) as db:
                setattr(db.get(self.h[model], identity), field, value)
                with self.assertRaises(ValueError):
                    db.flush()

    def test_exception_approval_replay_cross_scope_and_no_money_mutation(self):
        self.bearer = self.party()['id']
        e = self.exception()
        self.assertEqual(e, self.exception())
        self.assertEqual(e['status'], 'pending')
        self.assertEqual(self.request('GET', '/api/settlement-cost-exceptions', 'other')['total'], 0)
        self.decision(e['id'], 'other', 404)
        self.decision(e['id'], 'provider', 403)
        with Session(self.engine) as db:
            self.assertIsNone(self.h['cost_exception'](db, 'r1'))
        approved = self.decision(e['id'])
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(approved, self.decision(e['id']))
        self.decision(e['id'], expected=409, request_id='again')
        self.assertEqual(e, self.exception())
        with Session(self.engine) as db:
            result = self.h['cost_exception'](db, 'r1')
            self.assertEqual(result['retained_cost'], '20.00')
            self.assertEqual(result['bearer_participant_id'], self.bearer)
            self.assertEqual(result['evidence_refs'], ['invoice:1'])
            self.assertEqual(db.get(self.ns['Refund'], 'r1').amount, 50)
            row = db.get(self.h['Decision'], e['id'])
            row.reason = 'change'
            with self.assertRaises(ValueError):
                db.flush()

    def test_opposite_party_platform_proposal_rejection_and_stale_version(self):
        self.bearer = self.party()['id']
        e = self.exception(actor='ops')
        self.decision(e['id'], 'finance', 403)
        self.decision(e['id'], 'provider', 409, expected_version=9)
        rejected = self.decision(e['id'], 'provider', decision='reject')
        self.assertEqual(rejected['status'], 'rejected')
        with Session(self.engine) as db:
            self.assertIsNone(self.h['cost_exception'](db, 'r1'))

    def test_exception_evidence_cap_baseline_and_existing_posting(self):
        self.bearer = self.party()['id']
        self.exception('large', expected=409, retained_cost='100.01')
        self.exception('precision', expected=422, retained_cost='1.001')
        self.exception('zero', expected=422, retained_cost='0')
        self.exception('evidence', expected=422, evidence_refs=[])
        self.exception('foreign', expected=404, refund_id='r3')
        self.exception('no-baseline', actor='ops', expected=409, refund_id='r3')
        e = self.exception(retained_cost='80')
        self.decision(e['id'])
        self.exception('aggregate', expected=409, refund_id='r2', retained_cost='21')
        pending = self.exception('pending', refund_id='r2', retained_cost='20')
        model = self.ns['settlement_finance']['Baseline']
        with Session(self.engine) as db:
            db.add(model(settlement_id='negative', order_id='o1', currency='CNY', revenue=-10,
                         cost=-5, reference_settlement_id='b1'))
            db.commit()
        self.decision(pending['id'], expected=409, request_id='late')

    def test_missing_finance_fails_closed_and_pending_refund_denied(self):
        self.bearer = self.party()['id']
        with Session(self.engine) as db:
            db.get(self.ns['Refund'], 'r1').status = 'pending'
            db.commit()
        self.exception(expected=409)
        self.ns.pop('settlement_finance')
        self.exception('missing', expected=409, refund_id='r2')

    def test_cost_cap_is_rechecked_when_deciding_pending_requests(self):
        self.bearer = self.party()['id']
        first = self.exception('first', retained_cost='80')
        second = self.exception('second', refund_id='r2', retained_cost='30')
        self.decision(first['id'], request_id='first-approve')
        self.decision(second['id'], expected=409, request_id='second-approve')
        self.decision(second['id'], decision='reject', request_id='second-reject')


if __name__ == '__main__':
    unittest.main()
