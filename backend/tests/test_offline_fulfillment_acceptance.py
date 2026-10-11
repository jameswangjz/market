"""TRD-QA-004 all-method quotation/payment rollback; isolated SQLite HTTP routes.

These tests prove atomic failure recovery, not PostgreSQL concurrent locking or
real MinIO/ClamAV behavior. The acceptance script supplies the service checks.
"""
import unittest

import test_order_workflow as workflow


class OfflineAcceptanceRollbackTests(unittest.TestCase):
    setUpClass = classmethod(workflow.OrderWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(workflow.OrderWorkflowTests.tearDownClass.__func__)
    setUp = workflow.OrderWorkflowTests.setUp
    tearDown = workflow.OrderWorkflowTests.tearDown
    request = workflow.OrderWorkflowTests.request
    fail_after_create = workflow.OrderWorkflowTests.fail_after_create
    create = workflow.OrderWorkflowTests.create
    review = workflow.OrderWorkflowTests.review
    transition = workflow.OrderWorkflowTests.transition
    counts = workflow.OrderWorkflowTests.counts

    def frozen_state(self, oid):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            payments = db.scalars(self.select(self.m.Payment).where(self.m.Payment.order_id == oid)).all()
            return (order.main_status, order.delivery_status, order.payment_status,
                    order.amount, order.paid_amount, order.updated_at,
                    order.unit_price_snapshot, order.unit_cost_snapshot, order.total_cost_snapshot,
                    order.economic_snapshot_json, order.delivery_snapshot_json,
                    tuple(sorted((row.id, row.status, row.amount, row.paid_at) for row in payments)),
                    self.counts())

    def test_all_methods_review_failure_rolls_back_and_retry_succeeds(self):
        self.assertEqual(self.workflow_module.OFFLINE, {"training", "consulting", "custom"})
        for method in sorted(self.workflow_module.OFFLINE):
            with self.subTest(method=method):
                order = self.create(method)
                before = self.frozen_state(order["id"])
                with self.fail_after_create("order"):
                    self.review(order, expected=500, amount=120, cost=60, reason="Revised scope")
                self.assertEqual(self.frozen_state(order["id"]), before)
                reviewed = self.review(order, amount=120, cost=60, reason="Revised scope")
                self.assertEqual((reviewed["main_status"], reviewed["amount"]), ("pending_payment", 120))

    def test_all_methods_payment_failure_rolls_back_and_retry_creates_one_task(self):
        for method in sorted(self.workflow_module.OFFLINE):
            with self.subTest(method=method):
                order = self.create(method)
                self.review(order, amount=120, cost=60, reason="Revised scope")
                before = self.frozen_state(order["id"])
                with self.fail_after_create("order"):
                    self.transition(order, expected=500)
                self.assertEqual(self.frozen_state(order["id"]), before)
                self.transition(order)
                paid = self.frozen_state(order["id"])
                self.transition(order)
                self.assertEqual(self.frozen_state(order["id"]), paid)
                with self.m.SessionLocal() as db:
                    saved = db.get(self.m.Order, order["id"])
                    self.assertEqual((saved.main_status, saved.payment_status, saved.paid_amount),
                                     ("awaiting_start", "paid", 120))
                    self.assertEqual((saved.economic_snapshot_json, saved.delivery_snapshot_json), before[9:11])
                    tasks = db.scalars(self.select(self.m.DeliveryTask).where(
                        self.m.DeliveryTask.order_id == saved.id)).all()
                    self.assertEqual(len(tasks), 1)
                    self.assertEqual((tasks[0].method, tasks[0].delivery_mode), (method, "manual"))
                    self.assertEqual(self.m.order_cost(db, saved), self.m.Decimal("60.00"))


if __name__ == "__main__":
    unittest.main()
