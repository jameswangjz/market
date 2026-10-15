"""Payment-time immutable SLA contracts and first verified online deliveries.

Hooks flush only. Callers own Order -> Enterprise lock order and transaction.
No historical backfill, external I/O, economic snapshots, or public endpoint.
"""
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
import secrets

from fastapi import HTTPException
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import mapped_column

from .order_economics import money


ONLINE = frozenset({'file', 'api', 'model_api', 'tenant_access'})
FUNDING = frozenset({'version_change_payment', 'upgrade', 'downgrade'})


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def install(ns):
    if ns.get('order_sla_contracts'):
        return ns['order_sla_contracts']
    Base, Order = ns['Base'], ns['Order']

    class OrderSlaContract(Base):
        __tablename__ = 'order_sla_contracts'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        order_id = mapped_column(ForeignKey('orders.id'), nullable=False, unique=True)
        source_payment_id = mapped_column(ForeignKey('payments.id'), nullable=False, unique=True)
        snapshot_json = mapped_column(Text, nullable=False)
        paid_at = mapped_column(DateTime(timezone=True), nullable=False)
        deadline_at = mapped_column(DateTime(timezone=True), nullable=False, index=True)
        verified_at = mapped_column(DateTime(timezone=True), nullable=True)
        source_delivery_task_id = mapped_column(ForeignKey('delivery_tasks.id'), nullable=True)
        delivery_method = mapped_column(String(40), nullable=False)
        delivery_eligible = mapped_column(Boolean, nullable=False, default=True)

    Contract = OrderSlaContract

    def get_contract(db, order_id):
        return db.scalar(select(Contract).where(Contract.order_id == order_id)
                         .execution_options(populate_existing=True))

    def freeze_payment(db, order, payment, *, previously_paid, legacy_saas=False):
        """Explicit transition evidence required; never infer it from updated_at.

        An existing contract is returned unchanged. Already-paid orders without
        one stay unknown, including when a client repeats payment confirmation.
        """
        existing = get_contract(db, order.id)
        if existing is not None:
            return existing
        if type(previously_paid) is not bool:
            raise ValueError('Explicit prior payment evidence required')
        if previously_paid or order.business_type in FUNDING:
            return None
        method = order.delivery_method_snapshot
        if legacy_saas and order.business_type in {'purchase', 'renew'}:
            method = 'tenant_access'
        elif order.snapshot_version != 1:
            return None
        if method not in ONLINE or money(order.amount) <= 0:
            return None
        if (payment.order_id != order.id or order.payment_status != 'paid' or payment.status != 'paid'
                or money(order.paid_amount) != money(order.amount) or money(payment.amount) != money(order.amount)
                or money(order.refunded_amount) != 0 or payment.paid_at is None):
            raise HTTPException(409, 'A new complete payment fact is required to freeze the SLA')
        # New insert/update locks belong to the caller before Enterprise locking.
        db.flush()
        frozen = ns['freeze_sla_snapshot'](db, order.buyer_enterprise_id)
        # Preserve the shared helper's signed content, including legacy units.
        # This internal SLA-only document is never added to order_out/public APIs.
        snapshot = json.loads(json.dumps(frozen, ensure_ascii=False))
        delivery = snapshot['metrics'].get('delivery', {})
        budget = delivery.get('target_seconds')
        if (isinstance(budget, bool) or not isinstance(budget, (int, float))
                or not isfinite(budget) or budget <= 0 or delivery.get('clock') != 'natural'
                or delivery.get('direction') != 'upper'):
            raise HTTPException(409, 'Confirmed natural-time delivery SLA is missing')
        paid_at = utc(payment.paid_at)
        row = Contract(order_id=order.id, source_payment_id=payment.id,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            paid_at=paid_at, deadline_at=paid_at + timedelta(seconds=budget), delivery_method=method)
        db.add(row)
        db.flush()
        ns['audit'](db, 'system', 'freeze_order_sla_contract', 'order_sla_contract', row.id,
            'Freeze new payment promise; no historical inference', category='sla', business_domain='sla',
            order_id=order.id, after={'source_payment_id': payment.id,
                'service_level_code': snapshot.get('service_level_code'),
                'paid_at': paid_at.isoformat(), 'deadline_at': row.deadline_at.isoformat()})
        db.flush()
        return row

    def record_verified_delivery(db, order, task):
        """Called only after the adapter's actual verification succeeds."""
        row = get_contract(db, order.id)
        if row is None or row.verified_at is not None:
            return row
        if (not row.delivery_eligible or task.order_id != order.id or task.status != 'completed'
                or task.method != row.delivery_method or order.delivery_status != 'accepted'
                or order.main_status != 'completed'):
            raise HTTPException(409, 'Verified delivery fact does not match the paid contract')
        at = utc(ns['now']())
        if at < utc(row.paid_at):
            raise HTTPException(409, 'Delivery verification precedes payment')
        row.verified_at, row.source_delivery_task_id = at, task.id
        ns['audit'](db, 'system', 'verify_order_sla_delivery', 'order_sla_contract', row.id,
            'First successful online adapter verification', category='sla', business_domain='sla',
            order_id=order.id, after={'verified_at': at.isoformat(), 'source_delivery_task_id': task.id})
        db.flush()
        return row

    def evaluation_query(*, period_start, period_end, product_id=None, buyer_enterprise_id=None):
        """Deadline-month cohort, including uncompleted/overdue orders, not paid month."""
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
               for value in (period_start, period_end)):
            raise ValueError('Evaluation bounds must have explicit timezones')
        start, end = utc(period_start), utc(period_end)
        if start >= end:
            raise ValueError('Evaluation period must be increasing')
        query = select(Contract).join(Order, Order.id == Contract.order_id).where(
            Contract.delivery_eligible.is_(True), Contract.deadline_at >= start, Contract.deadline_at < end)
        if product_id is not None:
            query = query.where(Order.product_id == product_id)
        if buyer_enterprise_id is not None:
            query = query.where(Order.buyer_enterprise_id == buyer_enterprise_id)
        return query.order_by(Contract.deadline_at, Contract.id)

    hooks = dict(Contract=Contract, freeze_payment=freeze_payment, record_verified_delivery=record_verified_delivery,
                 get_contract=get_contract, evaluation_query=evaluation_query)
    ns['OrderSlaContract'] = Contract
    ns['order_sla_contracts'] = hooks
    return hooks
