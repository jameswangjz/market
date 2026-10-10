"""New-order economic and delivery snapshots; legacy orders remain identifiable."""
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import Depends, HTTPException
from sqlalchemy import select

RATE_KEYS = ("platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")
CENT = Decimal("0.01")
MAX_MONEY = Decimal("999999999999.99")


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > MAX_MONEY:
            raise ValueError("Invalid money")
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(400, "产品价格或成本无效") from exc


def install(ns):
    Product, Version, SaaSVersion = ns["Product"], ns["ProductReleaseVersion"], ns["SaaSProductVersion"]
    Rule, File = ns["SettlementRule"], ns["FileObject"]

    def settlement_snapshot(db, product, lock=False):
        rule = None
        if product.settlement_rule_mode == "custom":
            try:
                raw = json.loads(product.settlement_rule_json or "{}")
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "产品清算规则无效") from exc
            version = f"product:{product.id}"
        else:
            stmt = select(Rule)
            if product.settlement_rule_id:
                stmt = stmt.where(Rule.id == product.settlement_rule_id, Rule.status.in_(["active", "disabled"]))
            else:
                stmt = stmt.where(Rule.status == "active")
            stmt = stmt.order_by(Rule.created_at.desc(), Rule.id.desc())
            rule = db.scalar(stmt.with_for_update() if lock else stmt)
            if not rule:
                raise HTTPException(409, "产品尚未配置可用的清算规则")
            raw = {key: getattr(rule, key) for key in RATE_KEYS}
            version = f"global:{rule.version}"
        try:
            rates = {key: Decimal(str(raw.get(key, 0))) for key in RATE_KEYS}
            if any(not value.is_finite() or value < 0 or value > 100 for value in rates.values()) or sum(rates.values()) > 100:
                raise ValueError("Invalid rates")
        except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(400, "产品清算比例无效") from exc
        return {"mode": product.settlement_rule_mode, "id": rule.id if rule else "",
                "version": version, "rates": {key: str(value) for key, value in rates.items()}}

    def quote(db, user, body, lock=False):
        ns["trading_policy"]["require_buyer"](db, user, body.buyer_enterprise_id)
        stmt = select(Product).where(Product.id == body.product_id)
        product = db.scalar(stmt.with_for_update() if lock else stmt)
        if not product or product.status != "published":
            raise HTTPException(400, "产品不存在或尚未发布")
        category = ns["trading_policy"]["delivery_category"](product.delivery_method)
        monthly = product.delivery_method in {"api", "model_api", "tenant_access"}
        months = body.subscription_months
        if not monthly and months != 1:
            raise HTTPException(400, "文件及线下服务为一次性购买，不支持购买月数")
        model = SaaSVersion if product.product_type == "saas" else Version
        stmt = select(model).where(model.product_id == product.id, model.status == "active")
        if body.product_version_id:
            stmt = stmt.where(model.id == body.product_version_id)
        stmt = stmt.order_by(model.created_at, model.id)
        version = db.scalar(stmt.with_for_update() if lock else stmt)
        if not version:
            raise HTTPException(400, "产品版本不存在或未启用")
        available = ns["storefront"]["versions_for"](db, product)
        if not any(v["id"] == version.id for v in available):
            raise HTTPException(409, "所选版本尚未完成接入验证或不可购买")
        unit_price = money(version.monthly_price if model is SaaSVersion else version.price)
        unit_cost = money(version.cost)
        if unit_price < unit_cost:
            raise HTTPException(400, "产品版本售价不得低于成本")
        amount, cost = money(unit_price * months), money(unit_cost * months)
        economic = {"schema_version": 1, "product_id": product.id, "product_version_id": version.id,
                    "version_code": version.version_code, "billing_unit": "month" if monthly else "order",
                    "subscription_months": months, "unit_price": str(unit_price), "unit_cost": str(unit_cost),
                    "amount": str(amount), "total_cost": str(cost), "currency": product.currency,
                    "settlement_rule": settlement_snapshot(db, product, lock)}
        file_stmt = select(File).where(File.product_id == product.id, File.version_id == version.id,
                                      File.file_role == "product_data", File.status != "deleted").order_by(File.id)
        files = db.scalars(file_stmt.with_for_update() if lock else file_stmt).all()
        delivery = {"method": product.delivery_method, "category": category,
                    "download_limit": int(product.download_limit or 0), "application_url": product.application_url,
                    "upstream_url": product.upstream_url, "integration_api_url": product.integration_api_url,
                    "files": [{"id": f.id, "checksum": f.checksum or ""} for f in files],
                    "quota": {key: int(getattr(version, key, 0) or 0) for key in (
                        "rate_limit_per_minute", "daily_quota", "monthly_quota", "quota_amount")}}
        digest = hashlib.sha256(json.dumps({"user_id": user.id, "buyer": body.buyer_enterprise_id,
            "economic": economic, "delivery": delivery}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        public = {"quote_id": digest, "product_id": product.id, "product_version_id": version.id,
                  "product_version_code": version.version_code, "delivery_method": product.delivery_method,
                  "delivery_category": category, "billing_unit": economic["billing_unit"],
                  "subscription_months": months, "unit_price": float(unit_price), "amount": float(amount),
                  "currency": product.currency}
        return {"public": public, "economic": economic, "delivery": delivery, "amount": amount,
                "cost": cost, "unit_price": unit_price, "unit_cost": unit_cost, "product": product, "version": version}

    def apply_snapshot(order, quoted):
        order.snapshot_version = 1
        order.subscription_months = quoted["public"]["subscription_months"]
        order.unit_price_snapshot, order.unit_cost_snapshot = quoted["unit_price"], quoted["unit_cost"]
        order.total_cost_snapshot = quoted["cost"]
        order.delivery_method_snapshot = quoted["delivery"]["method"]
        order.economic_snapshot_json = json.dumps(quoted["economic"], ensure_ascii=False, sort_keys=True)
        order.delivery_snapshot_json = json.dumps(quoted["delivery"], ensure_ascii=False, sort_keys=True)

    def economic_snapshot(order):
        try:
            result = json.loads(order.economic_snapshot_json)
            rule = result["settlement_rule"]
            assert result["schema_version"] == 1 and isinstance(rule["version"], str) and rule["version"]
            assert result["product_id"] == order.product_id and result["product_version_id"] == order.product_version_id
            months = result["subscription_months"]
            assert type(months) is int and 1 <= months <= 36 and months == order.subscription_months
            assert result["billing_unit"] in {"month", "order"}
            assert result["billing_unit"] == "month" or months == 1
            unit_price, unit_cost = money(result["unit_price"]), money(result["unit_cost"])
            amount, total_cost = money(result["amount"]), money(result["total_cost"])
            assert unit_price >= unit_cost
            assert amount == money(unit_price * months) == money(order.amount)
            assert total_cost == money(unit_cost * months) == money(order.total_cost_snapshot)
            assert unit_price == money(order.unit_price_snapshot) and unit_cost == money(order.unit_cost_snapshot)
            rates = [Decimal(str(rule["rates"][key])) for key in RATE_KEYS]
            assert all(value.is_finite() and 0 <= value <= 100 for value in rates) and sum(rates) <= 100
            return result
        except (ValueError, TypeError, KeyError, AssertionError, InvalidOperation, HTTPException) as exc:
            raise HTTPException(409, "订单经济快照不完整，不能清算") from exc

    def delivery_snapshot(order):
        try:
            result = json.loads(order.delivery_snapshot_json)
            assert result["method"] == order.delivery_method_snapshot
            assert result["method"] in {"file", "api", "model_api", "tenant_access", "training", "consulting", "custom"}
            assert type(result["download_limit"]) is int and result["download_limit"] >= 0
            return result
        except (ValueError, TypeError, KeyError, AssertionError) as exc:
            raise HTTPException(409, "订单交付快照不完整，不能交付") from exc

    @ns["app"].post("/api/orders/quote")
    def quote_order(body: ns["OrderBody"], user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        return quote(db, user, body)["public"]

    return {"quote": quote, "apply_snapshot": apply_snapshot, "economic_snapshot": economic_snapshot,
            "delivery_snapshot": delivery_snapshot,
            "rate_keys": RATE_KEYS}
