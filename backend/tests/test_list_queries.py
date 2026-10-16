"""List contracts against real routes/models, using isolated SQLite and no services."""
from datetime import datetime, timezone
import unittest

import test_message_business_events as fixture


class ListQueriesTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixture.BusinessRoutesHTTPTests.setUp
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def seed(self):
        m = self.m
        instant = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        with m.SessionLocal() as db:
            db.add(m.ProductCategory(id="root", name="Root"))
            db.flush()
            db.add_all([m.ProductCategory(id="child", parent_id="root", name="Child"),
                m.ProductCategory(id="sibling", parent_id="root", name="Sibling")])
            db.flush()
            for i in range(25):
                suffix = f"{i:02}"
                db.add(m.Product(id="list-product-" + suffix, enterprise_id="provider_tenant",
                    name="Needle " + suffix, product_type="dataset", status="published",
                    category_id="child" if i < 24 else "sibling", catalog_name="Root/Child" if i < 24 else "Root/Sibling",
                    provider_name="", updated_at=instant))
            db.flush()
            for i in range(25):
                suffix = f"{i:02}"
                db.add(m.Order(id="list-order-" + suffix, order_no="READABLE-" + suffix,
                    product_id="list-product-" + suffix, product_name="Historical needle " + suffix,
                    buyer_enterprise_id="buyer_tenant" if i < 24 else "other_tenant",
                    buyer_user_id="buyer" if i < 24 else "outsider", provider_enterprise_id="provider_tenant",
                    buyer_name="Snapshot buyer", main_status="completed", payment_status="paid",
                    delivery_status="accepted", after_sales_status="none", updated_at=instant,
                    total_cost_snapshot=37, economic_snapshot_json='{"secret":"PRIVATE"}'))
            db.flush()
            for i in range(25):
                suffix = f"{i:02}"
                db.add(m.DeliveryTask(id="list-task-" + suffix, order_id="list-order-" + suffix,
                    assignee="provider", method="file", delivery_mode="automatic", status="in_delivery",
                    sla_due_at=datetime(2026, 10, 15, 16, tzinfo=timezone.utc) if i == 23 else instant,
                    created_at=instant, note="PRIVATE", last_error="PRIVATE"))
            db.commit()

    def test_products_combined_filters_category_hierarchy_and_legacy_all(self):
        self.seed()
        params = dict(product_name=" Needle ", provider_name="provider_tenant", product_type="dataset",
            status="published", category_id="root")
        legacy = self.request("GET", "/api/products", actor="provider", params=params)
        self.assertEqual(set(legacy), {"items"})
        self.assertEqual(len(legacy["items"]), 25)
        for category, total in (("root", 25), ("child", 24), ("sibling", 1)):
            response = self.request("GET", "/api/products", actor="provider",
                params={**params, "category_id": category, "page_size": 10})
            self.assertEqual((response["total"], response["page"], response["page_size"]), (total, 1, 10))
        response = self.request("GET", "/api/products", actor="provider",
            params={**params, "catalog_name": "Root/Child", "page": 1})
        self.assertEqual(response["total"], 24)
        self.assertEqual(response["page_size"], 20)
        self.assertEqual(self.request("GET", "/api/products", actor="outsider",
            params={**params, "page": 1})["total"], 0)
        self.request("GET", "/api/products", actor="finance", params={"page": 1}, expected=403)

    def test_stable_pages_and_totals_all_three_routes(self):
        self.seed()
        for route, actor, query, prefix, count in (
            ("products", "provider", {"product_name": "Needle"}, "product", 25),
            ("orders", "buyer_admin", {"order_no": "READABLE"}, "order", 24),
            ("delivery-tasks", "buyer_admin", {"task_id": "list-task"}, "task", 24)):
            ids = []
            for page in (1, 2, 3):
                response = self.request("GET", "/api/" + route, actor=actor,
                    params={**query, "page": page, "page_size": 10})
                self.assertEqual(response["total"], count)
                ids.extend(item["id"] for item in response["items"])
            self.assertEqual(ids, [f"list-{prefix}-{i:02}" for i in reversed(range(count))])
            beyond = self.request("GET", "/api/" + route, actor=actor,
                params={**query, "page": 4, "page_size": 10})
            self.assertEqual(beyond["page"], 3)
            self.assertEqual([item["id"] for item in beyond["items"]], ids[20:])
            legacy = self.request("GET", "/api/" + route, actor=actor, params=query)
            self.assertEqual(len(legacy["items"]), count)
            self.assertNotIn("total", legacy)

    def test_orders_eight_filters_snapshot_names_id_and_privacy(self):
        self.seed()
        params = dict(order_no="READABLE-03", product_name="Historical needle", provider_name="provider_tenant",
            buyer_name=" Snapshot buyer ", main_status="completed", payment_status="paid",
            delivery_status="accepted", after_sales_status="none", page=1)
        response = self.request("GET", "/api/orders", actor="buyer_admin", params=params)
        self.assertEqual(response["total"], 1)
        item = response["items"][0]
        self.assertEqual((item["provider_name"], item["buyer_name"]), ("provider_tenant", "Snapshot buyer"))
        for secret in ("total_cost_snapshot", "economic_snapshot_json", "PRIVATE"):
            self.assertNotIn(secret, str(response))
        for key in ("order_no", "q"):
            response = self.request("GET", "/api/orders", params={key: "list-order-03", "page": 1})
            self.assertEqual(response["total"], 1)
        for key, value in (("payment_status", "unpaid"), ("provider_name", "Snapshot buyer"),
                ("buyer_name", "provider_tenant"), ("status", "created")):
            self.assertEqual(self.request("GET", "/api/orders", params={**params, key: value})["total"], 0)

    def test_order_name_fallbacks_do_not_rewrite_snapshots(self):
        self.seed()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, "list-order-00")
            order.buyer_name = ""
            db.commit()
        item = self.request("GET", "/api/orders", params={"order_no": "list-order-00", "buyer_name": "buyer_tenant"})["items"][0]
        self.assertEqual(item["buyer_name"], "buyer_tenant")
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, item["id"]).buyer_name, "")
        # Historical personal display remains the saved personal name.
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, item["id"]).buyer_name = "Personal historical name"
            db.commit()
        response = self.request("GET", "/api/orders", params={"buyer_name": "Personal historical", "page": 1})
        self.assertEqual(response["total"], 1)

    def test_order_and_delivery_business_scope_and_authorization(self):
        self.seed()
        for route in ("orders", "delivery-tasks"):
            for view, total in (("buyer", 24), ("provider", 0)):
                response = self.request("GET", "/api/" + route, actor="buyer_admin",
                    params={"business_view": view, "page": 1})
                self.assertEqual(response["total"], total)
            self.request("GET", "/api/" + route, actor="buyer_admin",
                params={"enterprise_id": "other_tenant", "page": 1}, expected=403)
        with self.m.SessionLocal() as db:
            db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer")).role = "member"
            db.commit()
        self.assertEqual(self.request("GET", "/api/orders", params={"page": 1})["total"], 24)
        self.assertEqual(self.request("GET", "/api/delivery-tasks", params={"page": 1})["total"], 0)
        self.request("GET", "/api/orders/list-order-24", actor="buyer_admin", expected=403)

    def test_delivery_combined_filters_and_beijing_end_boundary(self):
        self.seed()
        params = dict(task_id="list-task", order_id="READABLE", assignee="provider@example.invalid",
            method="file", delivery_mode="automatic", status="in_delivery", sla_start="2026-10-15",
            sla_end="2026-10-15", page=1, page_size=100)
        response = self.request("GET", "/api/delivery-tasks", actor="buyer_admin", params=params)
        self.assertEqual(response["total"], 23)
        for item in response["items"]:
            self.assertTrue(item["order_no"].startswith("READABLE"))
            self.assertEqual(item["assignee"], "provider")
            self.assertNotIn("note", item)
            self.assertNotIn("last_error", item)
        response = self.request("GET", "/api/delivery-tasks", actor="platform",
            params={"order_id": "list-order-23", "sla_start": "2026-10-16", "sla_end": "2026-10-16", "page": 1})
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["order_no"], "READABLE-23")
        self.assertIn("note", response["items"][0])

    def test_invalid_ranges_states_and_lengths_return_422(self):
        for route in ("products", "orders", "delivery-tasks"):
            for params in ({"page": 0}, {"page": -1}, {"page": "bad"}, {"page": 100001},
                    {"page": 100000, "page_size": 100}, {"page_size": 12}, {"page_size": 0},
                    {"page_size": 101}, {"status": "invalid"}):
                self.request("GET", "/api/" + route, actor="platform", params=params, expected=422)
        for params in ({"sla_start": "2026-02-30"}, {"sla_end": "20261015"},
                {"sla_start": "2026-10-16", "sla_end": "2026-10-15"}, {"sla_end": "9999-12-31"},
                {"method": "bad"}, {"delivery_mode": "bad"}, {"assignee": "x" * 221}):
            self.request("GET", "/api/delivery-tasks", actor="platform", params=params, expected=422)
        for name in ("main_status", "payment_status", "delivery_status", "after_sales_status"):
            self.request("GET", "/api/orders", params={name: "invalid"}, expected=422)
        self.request("GET", "/api/products", actor="platform", params={"product_type": "invalid"}, expected=422)

    def test_literal_text_search_and_no_results(self):
        self.seed()
        for route, key in (("products", "product_name"), ("orders", "order_no"), ("delivery-tasks", "task_id")):
            for value in ("%", "_", "' OR 1=1 --"):
                response = self.request("GET", "/api/" + route, actor="platform", params={key: value, "page": 9})
                self.assertEqual(response["total"], 0)
                self.assertEqual(response["items"], [])
                self.assertEqual(response["page"], 1)

    def test_real_legacy_types_and_offline_states_remain_searchable(self):
        self.seed()
        for product_type in ("report", "application"):
            with self.m.SessionLocal() as db:
                db.get(self.m.Product, "list-product-00").product_type = product_type
                db.commit()
            self.assertEqual(self.request("GET", "/api/products", actor="provider",
                params={"product_type": product_type, "page": 1})["total"], 1)
        for status in ("awaiting_start", "pending_acceptance", "rectifying"):
            with self.m.SessionLocal() as db:
                db.get(self.m.DeliveryTask, "list-task-00").status = status
                db.get(self.m.Order, "list-order-00").main_status = status
                db.commit()
            for path, key in (("orders", "main_status"), ("delivery-tasks", "status")):
                self.assertEqual(self.request("GET", "/api/" + path,
                    params={key: status, "page": 1})["total"], 1)


if __name__ == "__main__":
    unittest.main()
