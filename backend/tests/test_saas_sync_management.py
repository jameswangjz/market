"""HTTP coverage for enterprise scoped SaaS synchronization trees and actions."""
from datetime import timedelta
from unittest.mock import patch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qa_acceptance_harness as harness
from sqlalchemy import func, select


class SaaSSyncManagementTests(harness.AcceptanceHTTPCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, "product")
            product.product_type, product.delivery_method = "saas", "tenant_access"
            db.add(self.m.SaaSProductVersion(id="sync-version", product_id="product", version_code="v1",
                name="v1", monthly_price=10, cost=1, status="active"))
            db.add(self.m.SaaSIntegrationConfig(product_id="product", base_url="https://saas.invalid",
                operation_path="/isv.php", client_id="sync-client", client_secret="secret",
                status="active", auth_mode="oauth2"))
            db.flush()
            db.add(self.m.SaaSSubscription(id="sync-sub", enterprise_id="buyer_tenant", product_id="product",
                version_id="sync-version", status="active", external_tenant_id="remote-tenant",
                starts_at=self.m.now() - timedelta(days=1), expires_at=self.m.now() + timedelta(days=30)))
            db.add_all([
                self.m.User(id="member-a", name="Member A", email="a@example.invalid", password_hash="unused",
                    verified_status="verified"),
                self.m.User(id="member-b", name="Member B", email="b@example.invalid", password_hash="unused",
                    verified_status="verified"),
                self.m.EnterpriseDepartment(id="dept-a", enterprise_id="buyer_tenant", name="Parent", parent_id="", status="active"),
                self.m.EnterpriseDepartment(id="dept-b", enterprise_id="buyer_tenant", name="Child", parent_id="dept-a", status="active"),
                self.m.EnterpriseDepartment(id="other-dept", enterprise_id="other_tenant", name="Private", status="active"),
            ])
            db.flush()
            db.add_all([
                self.m.Membership(user_id="member-a", enterprise_id="buyer_tenant", role="member", department_id="dept-a"),
                self.m.Membership(user_id="member-b", enterprise_id="buyer_tenant", role="member", department_id="dept-a"),
            ])
            db.flush()
            member_a = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == "member-a"))
            db.add(self.m.MembershipDepartment(membership_id=member_a.id, department_id="dept-b"))
            db.commit()

        def remote(config, operation, payload):
            self.calls.append((operation, dict(payload)))
            if operation == "DEPT_CREATE":
                return {"ret": 0, "department_id": "remote-" + payload["department_id"]}
            if operation == "USER_ASSIGN":
                return {"ret": 0, "user_id": "remote-user-" + payload["user_id"],
                        "external_user_id": "remote-user-" + payload["user_id"]}
            return {"ret": 0, "status": "deleted"}

        self.provider = patch.object(self.m, "saas_call", side_effect=remote)
        self.provider.start()
        self.addCleanup(self.provider.stop)
        self.addCleanup(patch.stopall)

    def test_tree_is_buyer_enterprise_scoped_and_admin_only(self):
        self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin")
        tree = self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree")
        self.assertEqual([item["id"] for item in tree["departments"]], ["dept-a"])
        self.assertEqual(tree["departments"][0]["children"][0]["id"], "dept-b")
        self.assertNotIn("other-dept", str(tree))
        self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="outsider", expected=403)
        self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="platform", expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer_admin").verified_status = "pending"
            db.commit()
        self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin", expected=403)

    def test_department_then_multi_department_user_sync_and_replay(self):
        created = self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "add", "kind": "departments", "all": True})
        self.assertEqual(created["succeeded"], 2)
        self.assertEqual([call[1]["name"] for call in self.calls], ["Parent", "Child"])
        added = self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "add", "kind": "users", "ids": ["member-a"]})
        self.assertEqual(added["succeeded"], 1)
        payload = next(payload for operation, payload in self.calls if operation == "USER_ASSIGN")
        self.assertEqual(payload["department_ids"], self.ids("dept-a", "dept-b"))
        replay = self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "add", "kind": "users", "ids": ["member-a"]})
        self.assertEqual(replay["succeeded"], 1)
        self.assertEqual(sum(operation == "USER_ASSIGN" for operation, _ in self.calls), 1)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSUserMapping).where(
                self.m.SaaSUserMapping.subscription_id == "sync-sub", self.m.SaaSUserMapping.status == "active")), 1)

    def ids(self, *ids):
        with self.m.SessionLocal() as db:
            rows = db.scalars(select(self.m.SaaSDepartment).where(
                self.m.SaaSDepartment.subscription_id == "sync-sub",
                self.m.SaaSDepartment.platform_department_id.in_(ids)).order_by(self.m.SaaSDepartment.platform_department_id)).all()
            by_platform = {row.platform_department_id: row.external_department_id for row in rows}
            return [by_platform[item] for item in ids]

    def test_foreign_selection_rejected_and_failed_items_audited(self):
        self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin", expected=404,
            json={"action": "add", "kind": "departments", "ids": ["other-dept"]})
        failure = patch.object(self.m, "saas_call", side_effect=self.m.HTTPException(502, "provider unavailable"))
        no_wait = patch.object(self.m.time, "sleep")
        with failure as remote_failure, no_wait as retry_wait:
            result = self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
                json={"action": "add", "kind": "users", "ids": ["member-a"]})
        self.assertEqual(remote_failure.call_count, 3)
        self.assertEqual([call.args for call in retry_wait.call_args_list], [(10,), (10,)])
        self.assertEqual(result["failed"], 1)
        with self.m.SessionLocal() as db:
            audit = db.scalar(select(self.m.AuditLog).where(self.m.AuditLog.action == "saas_sync_add_user"))
            self.assertIsNotNone(audit)
            self.assertEqual(audit.result, "failure")
            operation = db.scalar(select(self.m.SaaSOperation).where(self.m.SaaSOperation.subscription_id == "sync-sub"))
            self.assertEqual(operation.status, "failed")

    def test_department_removal_checks_child_and_synced_user_dependencies(self):
        self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "add", "kind": "departments", "all": True})
        self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "add", "kind": "users", "ids": ["member-a"]})
        blocked = self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            json={"action": "remove", "kind": "departments", "ids": ["dept-a"]})
        self.assertEqual(blocked["failed"], 1)
        self.assertFalse(any(operation == "DEPT_REMOVE" for operation, _ in self.calls))

    def act(self, action, kind, ids=None, expected=200):
        return self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin",
            expected=expected, json={"action": action, "kind": kind, **({"ids": ids} if ids is not None else {"all": True})})

    def test_changed_departments_refresh_and_stale_links_block_removal(self):
        self.act("add", "departments")
        self.act("add", "users", ["member-a"])
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == "member-a"))
            for link in db.scalars(select(self.m.MembershipDepartment).where(self.m.MembershipDepartment.membership_id == member.id)):
                db.delete(link)
            member.department_id = ""
            db.commit()
        self.assertEqual(self.act("remove", "departments", ["dept-b"])["failed"], 1)
        self.assertEqual(self.act("add", "users", ["member-a"])["succeeded"], 1)
        self.assertEqual([payload for op, payload in self.calls if op == "USER_ASSIGN"][-1]["department_ids"], [])
        self.assertEqual(self.act("remove", "departments", ["dept-b"])["succeeded"], 1)

    def test_assignment_can_return_to_previous_departments_without_cached_completion(self):
        self.act("add", "departments")
        self.act("add", "users", ["member-a"])
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == "member-a"))
            member.department_id = "dept-b"
            db.commit()
        self.act("add", "users", ["member-a"])
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == "member-a"))
            member.department_id = "dept-a"
            db.commit()
        self.act("add", "users", ["member-a"])
        self.act("add", "users", ["member-a"])
        self.assertEqual(sum(op == "USER_ASSIGN" for op, _ in self.calls), 3)
        tree = self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin")
        self.assertEqual(tree["mapped_users"][0]["department_ids"], self.ids("dept-a", "dept-b"))

    def test_stale_department_mappings_and_recreated_departments(self):
        self.act("add", "departments")
        with self.m.SessionLocal() as db:
            db.get(self.m.EnterpriseDepartment, "dept-a").status = "deleted"
            db.get(self.m.EnterpriseDepartment, "dept-b").status = "deleted"
            db.commit()
        tree = self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin")
        self.assertEqual(len(tree["mapped_departments"]), 2)
        self.assertEqual(tree["departments"], [])
        self.assertEqual(self.act("remove", "departments")["succeeded"], 2)
        with self.m.SessionLocal() as db:
            db.get(self.m.EnterpriseDepartment, "dept-a").status = "active"
            db.get(self.m.EnterpriseDepartment, "dept-b").status = "active"
            db.commit()
        self.assertEqual(self.act("add", "departments")["succeeded"], 2)
        self.assertEqual(sum(op == "DEPT_CREATE" for op, _ in self.calls), 4)

    def test_enterprise_subscription_and_actor_must_be_active(self):
        for model, item_id, field, value, expected in [
            (self.m.Enterprise, "buyer_tenant", "verification_status", "pending", 403),
            (self.m.SaaSSubscription, "sync-sub", "status", "closed", 409),
            (self.m.SaaSSubscription, "sync-sub", "starts_at", self.m.now() + timedelta(days=1), 409),
            (self.m.SaaSSubscription, "sync-sub", "expires_at", self.m.now() - timedelta(days=1), 409),
            (self.m.Membership, None, "role", "member", 403),
        ]:
            with self.m.SessionLocal() as db:
                item = db.get(model, item_id) if item_id else db.scalar(select(model).where(model.user_id == "buyer_admin"))
                original = getattr(item, field)
                setattr(item, field, value)
                db.commit()
            self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin", expected=expected)
            with self.m.SessionLocal() as db:
                item = db.get(model, item_id) if item_id else db.scalar(select(model).where(model.user_id == "buyer_admin"))
                setattr(item, field, original)
                db.commit()

    def test_inactive_mapping_leftovers_removable_and_lifecycle_replay(self):
        self.act("add", "users", ["member-a"])
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "member-a").is_active = False
            db.commit()
        tree = self.request("GET", "/api/saas-subscriptions/sync-sub/sync-tree", actor="buyer_admin")
        mapping_id = tree["mapped_users"][0]["id"]
        self.assertEqual(self.act("remove", "users", [mapping_id])["succeeded"], 1)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "member-a").is_active = True
            db.commit()
        self.act("add", "users", ["member-a"])
        self.act("remove", "users", ["member-a"])
        self.assertEqual(sum(op == "USER_ASSIGN" for op, _ in self.calls), 2)
        self.assertEqual(sum(op == "USER_UNASSIGN" for op, _ in self.calls), 2)

    def test_primary_admin_and_last_admin_protected_in_bulk(self):
        self.act("add", "users", ["buyer", "buyer_admin"])
        with self.m.SessionLocal() as db:
            db.add(self.m.SaaSOperation(subscription_id="sync-sub", operation="TENANT_CREATE",
                idempotency_key="primary-admin-fixture", status="succeeded",
                request_payload=json.dumps({"admin_user_id": "buyer"})))
            db.commit()
        result = self.act("remove", "users")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertFalse(any(op == "USER_UNASSIGN" and payload["user_id"] == "remote-user-buyer" for op, payload in self.calls))

    def test_child_before_parent_removal_by_platform_ids(self):
        self.act("add", "departments", ["dept-b", "dept-a"])
        result = self.act("remove", "departments", ["dept-a", "dept-b"])
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual([item["id"] for item in result["items"]], ["dept-b", "dept-a"])

    def test_strict_body_and_bounds_and_platform_accounts(self):
        for body in [
            {"action": "bad", "kind": "users", "all": True},
            {"action": "add", "kind": "bad", "all": True},
            {"action": "add", "kind": "users", "all": "true"},
            {"action": "add", "kind": "users", "ids": [1]},
            {"action": "add", "kind": "users", "ids": [""]},
            {"action": "add", "kind": "users", "ids": ["x"] * 11},
            {"action": "add", "kind": "users", "all": True, "extra": 1},
        ]:
            self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin", expected=422, json=body)
        self.act("add", "users", ["platform"], expected=404)
        self.request("POST", "/api/saas-subscriptions/sync-sub/sync-actions", actor="buyer_admin", expected=400,
            json={"action": "add", "kind": "users", "ids": ["member-a"], "all": True})
        self.act("add", "users", [], expected=400)
        self.assertEqual(self.calls, [])


    def test_sync_failed_request_replays_frozen_context_after_expiry_changes(self):
        from fastapi import HTTPException
        with patch.object(self.m, 'saas_call', side_effect=HTTPException(502, 'temporary')), \
                patch.object(self.m.time, 'sleep'):
            result = self.act('add', 'users', ['member-a'])
        self.assertEqual(result['failed'], 1)
        with self.m.SessionLocal() as db:
            record = db.scalar(select(self.m.SaaSOperation).where(
                self.m.SaaSOperation.operation == 'USER_ASSIGN'))
            raw = record.request_payload
            frozen = json.loads(raw)
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            sub.expires_at += timedelta(days=10)
            db.commit()
        self.assertEqual(self.act('add', 'users', ['member-a'])['succeeded'], 1)
        self.assertEqual(self.calls[-1][1], frozen)
        with self.m.SessionLocal() as db:
            record = db.scalar(select(self.m.SaaSOperation).where(
                self.m.SaaSOperation.operation == 'USER_ASSIGN'))
            self.assertEqual(record.request_payload, raw)

    def test_legacy_executor_preserves_pre_context_bytes_and_enriches_new_operations(self):
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            config = db.scalar(select(self.m.SaaSIntegrationConfig))
            raw = '{ "tenant_id": "old-tenant", "user_id": "old-user" }'
            db.add(self.m.SaaSOperation(subscription_id=sub.id, operation='USER_UNASSIGN',
                idempotency_key='legacy-failed', request_payload=raw, status='failed'))
            db.commit()
            self.m.execute_saas_operation(db, sub, 'USER_UNASSIGN', {'user_id': 'changed'},
                config, 'legacy-failed')
            self.assertEqual(self.calls[-1][1], json.loads(raw))
            record = db.scalar(select(self.m.SaaSOperation).where(
                self.m.SaaSOperation.idempotency_key == 'legacy-failed'))
            self.assertEqual(record.request_payload, raw)
            old_renew = '{ "billing_cycle": "monthly" }'
            db.add(self.m.SaaSOperation(subscription_id=sub.id, operation='RENEW',
                idempotency_key='legacy-renew-without-dates', request_payload=old_renew, status='failed'))
            db.commit()
            self.m.execute_saas_operation(db, sub, 'RENEW', {'billing_cycle': 'annual'},
                config, 'legacy-renew-without-dates')
            self.assertEqual(self.calls[-1][1], json.loads(old_renew))
            old_record = db.scalar(select(self.m.SaaSOperation).where(
                self.m.SaaSOperation.idempotency_key == 'legacy-renew-without-dates'))
            self.assertEqual(old_record.request_payload, old_renew)
            for operation in ('CLOSE', 'OPEN', 'USER_ASSIGN', 'DEPT_CREATE', 'DEPT_REMOVE', 'RENEW'):
                with self.subTest(operation=operation):
                    self.m.execute_saas_operation(db, sub, operation,
                        {'tenant_id': sub.external_tenant_id, 'user_id': 'u', 'department_id': 'd'},
                        config, 'new:' + operation)
                    context = self.calls[-1][1]['reconciliation_context']
                    self.assertEqual(context['event_id'], 'new:' + operation)
                    self.assertEqual(context['event_type'], operation)
                    self.assertEqual(context['subscription_id'], sub.id)
                    self.assertIsNone(context['order_id'])
                    self.assertEqual(context['period_status'], 'confirmed')

    def test_legacy_renew_freezes_calendar_and_retries_without_rewriting_payload(self):
        from datetime import datetime, timezone
        from fastapi import HTTPException
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            sub.expires_at = None
            db.commit()
            config = db.scalar(select(self.m.SaaSIntegrationConfig))
            with patch.object(self.m, 'saas_call', side_effect=HTTPException(502, 'temporary')), \
                    patch.object(self.m.time, 'sleep'), \
                    patch.object(self.m, 'now', return_value=datetime(2026, 1, 31, tzinfo=timezone.utc)):
                with self.assertRaises(HTTPException):
                    self.m.execute_saas_operation(db, sub, 'RENEW', {'billing_cycle': 'monthly'},
                        config, 'legacy-renew')
            record = db.scalar(select(self.m.SaaSOperation).where(
                self.m.SaaSOperation.idempotency_key == 'legacy-renew'))
            raw = record.request_payload
            frozen = json.loads(raw)
            self.assertEqual(frozen['expires_at'], '2026-02-28T00:00:00+00:00')
            self.assertEqual(frozen['reconciliation_context']['subscription_expires_at'], frozen['expires_at'])
            self.assertIsNone(frozen['reconciliation_context']['order_starts_at'])
            self.assertIsNone(frozen['reconciliation_context']['order_ends_at'])
            with patch.object(self.m, 'now', return_value=datetime(2026, 2, 2, tzinfo=timezone.utc)):
                self.m.execute_saas_operation(db, sub, 'RENEW', {'billing_cycle': 'annual'},
                    config, 'legacy-renew')
            self.assertEqual(record.request_payload, raw)
            self.assertEqual(self.calls[-1][1], frozen)

    def test_legacy_unknown_expiry_is_provisional(self):
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            sub.expires_at = None
            db.commit()
            config = db.scalar(select(self.m.SaaSIntegrationConfig))
            self.m.execute_saas_operation(db, sub, 'CLOSE', {'tenant_id': sub.external_tenant_id},
                config, 'unknown-close')
            context = self.calls[-1][1]['reconciliation_context']
            self.assertEqual(context['period_status'], 'provisional')
            self.assertIsNone(context['subscription_expires_at'])

    def test_legacy_expired_and_perpetual_renew_have_no_invented_order_dates(self):
        from datetime import datetime, timezone
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            sub.expires_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.commit()
            config = db.scalar(select(self.m.SaaSIntegrationConfig))
            for cycle in ('monthly', 'perpetual'):
                with self.subTest(cycle=cycle), patch.object(self.m, 'now',
                        return_value=datetime(2026, 1, 31, tzinfo=timezone.utc)):
                    self.m.execute_saas_operation(db, sub, 'RENEW', {'billing_cycle': cycle},
                        config, 'expired-renew:' + cycle)
                    payload = self.calls[-1][1]
                    expected = None if cycle == 'perpetual' else '2026-02-28T00:00:00+00:00'
                    self.assertEqual(payload['expires_at'], expected)
                    context = payload['reconciliation_context']
                    self.assertEqual(context['subscription_expires_at'], expected)
                    self.assertEqual(context['period_status'], 'confirmed')
                    self.assertIsNone(context['order_starts_at'])
                    self.assertIsNone(context['order_ends_at'])
                    self.assertIsNone(context['order_id'])

    def test_legacy_renew_route_applies_frozen_cutoff_even_if_clock_advances(self):
        from datetime import datetime, timezone
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            sub.expires_at = None
            db.commit()
        original = self.m.execute_saas_operation
        earlier = datetime(2026, 1, 31, tzinfo=timezone.utc)
        later = datetime(2026, 2, 2, tzinfo=timezone.utc)

        def execute(*args, **kwargs):
            with patch.object(self.m, 'now', return_value=earlier):
                return original(*args, **kwargs)

        with patch.object(self.m, 'execute_saas_operation', side_effect=execute), \
                patch.object(self.m, 'now', return_value=later):
            self.request('POST', '/api/saas-subscriptions/sync-sub/renew',
                json={'billing_cycle': 'monthly'})
        sent = next(payload for operation, payload in self.calls if operation == 'RENEW')
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, 'sync-sub')
            self.assertEqual(sub.expires_at.replace(tzinfo=timezone.utc),
                             datetime.fromisoformat(sent['expires_at']))
        self.assertEqual(sent['expires_at'], '2026-02-28T00:00:00+00:00')


if __name__ == "__main__":
    import unittest
    unittest.main()
