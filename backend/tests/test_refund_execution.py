"""SQLite HTTP + real isolated Redis + installed resource/execution adapters.

Only the financial charge-summary adapter is fixture evidence. No provider or
resource adapter is mocked in the successful execution path. Not PG concurrency.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from app.quota_cutover import token

import test_refund_planning as fixture
import test_refund_segment_boundary as redis_fixture


class RefundExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.RefundPlanningHTTPTests.setUpClass.__func__(cls)

        class RedisOwner:
            pass

        cls.redis_owner = RedisOwner
        redis_fixture.BoundaryTests.setUpClass.__func__(cls.redis_owner)

    @classmethod
    def tearDownClass(cls):
        redis_fixture.BoundaryTests.tearDownClass.__func__(cls.redis_owner)
        fixture.RefundPlanningHTTPTests.tearDownClass.__func__(cls)

    tearDown = fixture.RefundPlanningHTTPTests.tearDown
    request = fixture.RefundPlanningHTTPTests.request
    create_order = fixture.RefundPlanningHTTPTests.create_order
    monthly = fixture.RefundPlanningHTTPTests.monthly
    create = fixture.RefundPlanningHTTPTests.create
    review = fixture.RefundPlanningHTTPTests.review
    ledger = fixture.RefundPlanningHTTPTests.ledger

    def setUp(self):
        fixture.RefundPlanningHTTPTests.setUp(self)
        self.redis = self.redis_owner.client
        self.redis.flushdb()
        self.monthly()
        self.at = datetime.fromtimestamp(self.redis.time()[0], timezone.utc)
        self.m.now.return_value = self.at
        self.h = self.m.refund_execution
        self.Execution, self.Leg, self.Completion = (
            self.h[k] for k in ("Execution", "PaymentLeg", "Completion")
        )
        self.worker = importlib.import_module(self.m.__package__ + ".refund_execution_worker")
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms["Term"], "refund-segment")
            db.get(self.m.Order, self.order_id).delivery_status = "accepted"
            db.get(self.m.Payment, "paid-source").paid_at = self.at - timedelta(hours=1)
            db.add(
                self.m.ApiGatewayRoute(
                    id="execution-route",
                    product_id="product",
                    route_key="execution-route",
                    upstream_url="https://example.invalid",
                )
            )
            db.flush()
            db.add(
                self.m.ApiCredential(
                    id="execution-credential",
                    route_id="execution-route",
                    enterprise_id="buyer_tenant",
                    order_id=self.order_id,
                    key_hash="x" * 64,
                    apisix_consumer_name="exec-test",
                )
            )
            db.add(
                self.m.api_charge_ledger["Cursor"](
                    stream="market:apisix:charges:v1",
                    redis_run_id=self.redis.info("server")["run_id"],
                    activated_at=term.starts_at - timedelta(days=1),
                )
            )
            periods = [
                dict(
                    start_at=int(term.starts_at.timestamp()),
                    end_at=int(term.ends_at.timestamp()),
                    period_key="refund-combo:1:0",
                    order_id=self.order_id,
                    segment_id=term.id,
                    route_key="execution-route",
                    monthly_quota=100,
                    daily_quota=0,
                    rate_limit_per_minute=60,
                )
            ]
            db.commit()
        self.key = "market:apisix:credential:exec-test"
        self.redis.hset(
            self.key,
            mapping=dict(
                mode="subscription",
                status="active",
                quota_scope="refund-combo",
                subscription_periods=json.dumps(periods, separators=(",", ":")),
            ),
        )
        self.redis.hset(
            "market:apisix:charges:source", mapping={"run_id": self.redis.info("server")["run_id"]}
        )
        suffix = "refund-combo:" + token("refund-combo:1:0")
        self.redis.set("market:apisix:quota:subscription:month:" + suffix, "25")
        self.redis.hset("market:apisix:charges:coverage:" + suffix, mapping={"baseline": "0"})
        # Use the real isolated Redis client; close() only releases its pool.
        settings = patch.dict(
            vars(self.m), refund_charge_summary=self.ledger, apisix_policy_redis=lambda: self.redis
        )
        settings.start()
        self.addCleanup(settings.stop)

    def approve(self):
        row = self.create()
        return self.review(row, decision="approve")

    def execute(self, row, operation="execute", expected=200, actor="ops", key=None):
        return self.request(
            "POST",
            f"/api/refunds/{row['id']}/{operation}",
            actor=actor,
            expected=expected,
            json={
                "expected_version": row["version"],
                "idempotency_key": key or "execution-operation-" + operation,
                "reason": "Isolated simulated refund execution",
            },
        )

    def step(self):
        return self.worker.process_once(self.m.SessionLocal, ns=vars(self.m))

    def test_real_resource_prepare_apply_simulated_funds_and_be013_guard(self):
        approved = self.approve()
        self.assertTrue(approved["execution_available"])
        self.assertFalse(self.step())  # Approval alone never starts execution.
        queued = self.execute(approved)
        self.assertEqual(queued["status"], "executing")
        self.assertEqual(self.execute(approved)["id"], approved["id"])
        self.assertTrue(self.step())
        self.assertTrue(self.step())
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.Execution))
            self.assertEqual(execution.status, "resources_applied")
            self.assertTrue(execution.proof_json)
            recovery = db.scalar(select(self.m.refund_execution_resources["Recovery"]))
            self.assertEqual(recovery.execution_id, execution.id)
            self.assertEqual(db.get(self.m.Refund, approved["id"]).status, "executing")
            self.assertEqual(db.scalar(select(self.Leg)).status, "prepared")
        target = json.loads(self.redis.hget(self.key, "subscription_periods"))
        self.assertEqual(target[0]["monthly_quota"], 80)
        for _ in range(3):
            self.assertTrue(self.step())
        self.assertFalse(self.step())
        for actor in ("buyer", "provider", "ops"):
            view = self.request("GET", "/api/refunds/" + approved["id"], actor=actor)["execution"]
            self.assertEqual(view["funds_mode"], "simulated")
            self.assertEqual(view["ledger_status"], "pending_be013")
            self.assertEqual(
                view["payment_legs"], [dict(amount="20.00", status="completed", error_code=None)]
            )
            self.assertNotIn("paid-source", json.dumps(view))
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved["id"])
            self.assertEqual(refund.status, "completed")
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, Decimal("20"))
            self.assertEqual(db.get(self.Completion, refund.id).settlement_status, "pending_be013")
            leg = db.scalar(select(self.Leg))
            self.assertEqual(leg.receipt, "simulated:" + refund.id + ":paid-source")
            with self.assertRaises(HTTPException):
                self.m.create_refund_negative_settlement(db, refund)
            self.assertEqual(list(db.scalars(select(self.m.Settlement))), [])

    def test_roles_manual_and_request_fields_fail_closed(self):
        approved = self.approve()
        for actor in ("buyer", "provider", "finance"):
            self.execute(approved, actor=actor, expected=403)
        self.request(
            "POST",
            f"/api/refunds/{approved['id']}/execute",
            actor="ops",
            expected=422,
            json={
                "expected_version": approved["version"],
                "idempotency_key": "execution-test-extra",
                "reason": "test",
                "payment_mode": "real",
            },
        )

    def test_worker_failure_operator_retry_and_completed_leg_never_repeated(self):
        approved = self.approve()
        self.execute(approved)
        with self.m.SessionLocal() as db:
            leg = db.scalar(select(self.Leg))
            self.assertEqual(leg.status, "prepared")
            db.delete(leg)
            with self.assertRaisesRegex(ValueError, "immutable evidence"):
                db.flush()
            db.rollback()
        self.step()
        resources = self.m.refund_execution_resources
        apply = resources["apply"]
        with patch.dict(
            resources, apply=lambda *a: (_ for _ in ()).throw(RuntimeError("https://secret.invalid/token"))
        ):
            self.step()
        self.assertFalse(self.step())
        row = self.request("GET", "/api/refunds/" + approved["id"], actor="ops")
        self.assertEqual(row["execution_error_code"], "execution_phase_failed")
        self.assertNotIn("secret.invalid", json.dumps(row))
        self.execute(row, operation="retry")
        self.assertIs(resources["apply"], apply)
        for _ in range(4):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, approved["id"]).status, "completed")
            leg = db.scalar(select(self.Leg))
            leg.receipt = "changed"
            with self.assertRaises(ValueError):
                db.flush()
            db.rollback()
            leg = db.scalar(select(self.Leg))
            self.assertEqual(leg.status, "completed")
            receipt = leg.receipt
            db.delete(leg)
            with self.assertRaisesRegex(ValueError, "immutable evidence"):
                db.flush()
            db.rollback()
            self.assertEqual(db.scalar(select(self.Leg)).receipt, receipt)

    def test_get_is_lock_and_network_free_and_old_approval_can_be_revalidated(self):
        approved = self.approve()
        with patch.object(self.m, "apisix_policy_redis", side_effect=AssertionError("GET network")):
            with patch.dict(self.m.refund_workflow, lock_order=lambda *args: (_ for _ in ()).throw(AssertionError('GET lock'))):
                row = self.request("GET", "/api/refunds/" + approved["id"], actor="ops")
                self.assertTrue(row["execution_available"])
        self.m.now.return_value = self.at + timedelta(minutes=10)
        self.execute(approved)

    def test_completion_audit_failure_rolls_back_economics_and_retry_does_not_repay(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(4):
            self.step()
        audit = self.m.audit

        def fail_completion(db, actor, action, *args, **kwargs):
            if action == "ordinary_refund_completed":
                raise RuntimeError("completion audit unavailable")
            return audit(db, actor, action, *args, **kwargs)

        with patch.object(self.m, "audit", side_effect=fail_completion):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, 0)
            self.assertIsNone(db.get(self.Completion, approved["id"]))
            self.assertEqual(db.scalar(select(self.Leg)).status, "completed")
            receipt = db.scalar(select(self.Leg)).receipt
        failed = self.request("GET", "/api/refunds/" + approved["id"], actor="ops")
        self.execute(failed, operation="retry")
        self.step()
        self.assertFalse(self.step())
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, Decimal("20"))
            self.assertEqual(db.scalar(select(self.Leg)).receipt, receipt)
            self.assertIsNotNone(db.get(self.Completion, approved["id"]))

    def test_crash_after_remote_recovery_replays_real_receipt_after_lease_expiry(self):
        approved = self.approve()
        self.execute(approved)
        self.step()
        resources = self.m.refund_execution_resources
        apply = resources["apply"]

        def crash(db, execution):
            apply(db, execution)
            raise SystemExit("simulated process death after external recovery")

        with patch.dict(resources, apply=crash), self.assertRaises(SystemExit):
            self.step()
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.Execution))
            self.assertEqual(execution.status, "resources_pending")
            self.assertIsNone(execution.proof_json)
            self.assertIsNotNone(db.scalar(select(resources["Recovery"])))
            self.assertEqual(db.scalar(select(self.Leg)).status, "prepared")
            execution.lease_expires_at = self.at - timedelta(seconds=1)
            db.commit()
        self.worker = importlib.reload(self.worker)
        self.step()
        for _ in range(3):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, approved["id"]).status, "completed")
            self.assertEqual(len(list(db.scalars(select(resources["Recovery"])))), 1)
        self.assertEqual(
            json.loads(self.redis.hget(self.key, "subscription_periods"))[0]["monthly_quota"], 80
        )

    def test_changed_balance_or_unknown_leg_state_prevents_simulated_debit(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(3):
            self.step()
        with self.m.SessionLocal() as db:
            db.add(
                self.m.Refund(
                    id="legacy-hold",
                    refund_no="LEGACY-HOLD",
                    order_id=self.order_id,
                    payment_id="paid-source",
                    status="pending",
                    amount=90,
                )
            )
            db.commit()
        self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.Leg)).status, "prepared")
            self.assertEqual(db.scalar(select(self.Execution)).status, "failed")
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, 0)

    def test_unknown_leg_state_is_not_treated_as_prepared(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(3):
            self.step()
        with self.m.SessionLocal() as db:
            db.scalar(select(self.Leg)).status = "unknown"
            db.commit()
        self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.Leg)).status, "unknown")
            self.assertIsNone(db.scalar(select(self.Leg)).receipt)
            self.assertEqual(db.scalar(select(self.Execution)).status, "failed")

    def test_completed_outbox_failure_preserves_successful_legs_and_rolls_back_completion(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(4):
            self.step()
        notify = self.m.notify_business_event
        def fail_completion(db, *args, **kwargs):
            if db.get(self.m.Refund, approved['id']).status == 'completed':
                raise RuntimeError('completion outbox failed')
            return notify(db, *args, **kwargs)
        with patch.object(self.m, 'notify_business_event', side_effect=fail_completion):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.Completion, approved['id']))
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, 0)
            self.assertEqual(db.scalar(select(self.Leg)).status, 'completed')
        failed = self.request('GET', '/api/refunds/' + approved['id'], actor='ops')
        self.execute(failed, operation='retry')
        self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, Decimal('20'))

    def test_fencing_rejects_old_resource_worker_after_new_lease_owner(self):
        approved = self.approve()
        self.execute(approved)
        self.step()
        resources = self.m.refund_execution_resources
        apply = resources['apply']
        def replaced_lease(db, execution):
            proof = apply(db, execution)
            with self.m.SessionLocal() as session:
                persisted = session.get(self.Execution, execution.id)
                persisted.lease_token = 'replacement-owner'
                persisted.lease_expires_at = self.at + timedelta(seconds=60)
                session.commit()
            return proof
        with patch.dict(resources, apply=replaced_lease):
            self.step()
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.Execution))
            self.assertEqual(execution.lease_token, 'replacement-owner')
            self.assertIsNone(execution.proof_json)
            self.assertEqual(db.scalar(select(self.Leg)).status, 'prepared')
            execution.lease_expires_at = self.at - timedelta(seconds=1)
            db.commit()
        for _ in range(4):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, approved['id']).status, 'completed')


if __name__ == "__main__":
    unittest.main()
