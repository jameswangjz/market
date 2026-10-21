"""Refund cash records only; callers own flushing, transactions and settlement.

Install after Order/Refund are declared and before metadata creation. Main may call
scan_pending(session) from its centralized before_flush hook, or sync(session,
refund, flush=False) explicitly before flushing. No listeners are registered;
only an explicit sync(..., flush=True) requests a flush outside flush events.
There is deliberately no historical scan or economic settlement hook.
"""

import secrets
from decimal import Decimal

from sqlalchemy import Column, ForeignKey, inspect
from sqlalchemy.orm import relationship


PENDING_STATUSES = frozenset({
    'pending', 'requested', 'pending_review', 'approved', 'executing', 'failed',
})
COPY_FIELDS = (
    'buyer_enterprise_id', 'buyer_user_id', 'provider_enterprise_id',
    'product_id', 'buyer_name', 'product_name', 'product_version_id',
    'product_version_code', 'product_version_name', 'billing_cycle',
    'subscription_id', 'subscription_months', 'snapshot_version',
    'unit_price_snapshot', 'unit_cost_snapshot', 'total_cost_snapshot',
    'economic_snapshot_json', 'service_starts_at', 'service_ends_at',
    'service_effective_at',
)


def install(ns):
    if 'refund_order_records' in ns:
        return ns['refund_order_records']
    Order, Refund = ns['Order'], ns['Refund']

    class RefundOrderLink(ns['Base']):
        __tablename__ = 'refund_order_links'
        refund_id = Column(ForeignKey('refunds.id'), primary_key=True)
        order_id = Column(ForeignKey('orders.id'), nullable=False, unique=True)
        source_order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        refund = relationship(Refund, foreign_keys=[refund_id])
        order = relationship(Order, foreign_keys=[order_id])
        source_order = relationship(Order, foreign_keys=[source_order_id])

    def find(db, model, key):
        return next((row for row in db.new if isinstance(row, model)
                     and row.id == key), None) or db.get(model, key)

    def sync(db, refund, *, flush=False, enabled=True):
        """Stage one cash record; flush=False is safe inside before_flush.

        The source is Refund.order_id, including version downgrade refunds. This
        adapter never invokes ordinary or version-change economic settlement.
        """
        if not enabled:
            return None
        if flush and db._flushing:
            raise ValueError('flush=True is not allowed during a flush')
        if not isinstance(refund, Refund) or refund in db.deleted:
            raise ValueError('a live Refund is required')
        state = inspect(refund)
        if state.session is not db:
            raise ValueError('refund must belong to the supplied session')
        amount = Decimal(str(refund.amount))
        if (not amount.is_finite() or amount <= 0 or amount > Decimal('999999999999.99')
                or amount != amount.quantize(Decimal('0.01'))):
            raise ValueError('refund amount must be positive cents')
        status = refund.status or ('pending' if state.pending else None)
        if status not in PENDING_STATUSES | {'completed', 'rejected'}:
            raise ValueError('unsupported refund status')
        if state.attrs.order_id.history.has_changes() and state.persistent:
            raise ValueError('refund source order is immutable')
        with db.no_autoflush:
            source = find(db, Order, refund.order_id)
            if (source is None or source in db.deleted
                    or getattr(source, 'order_type', '') == 'refund'
                    or source.business_type == 'refund_record'
                    or Decimal(str(source.amount)) <= 0):
                raise ValueError('refund requires a positive source order')
            if not refund.id:
                refund.id = secrets.token_hex(16)
            link = next((row for row in db.new if isinstance(row, RefundOrderLink)
                         and row.refund_id == refund.id), None)
            if link is None:
                link = db.get(RefundOrderLink, refund.id)
            if link is not None:
                if (link in db.deleted or link.source_order_id != source.id
                        or any(inspect(link).attrs[name].history.has_changes()
                               for name in ('refund_id', 'order_id', 'source_order_id'))
                        and inspect(link).persistent):
                    raise ValueError('refund order linkage is immutable')
                record = find(db, Order, link.order_id)
                if (record is None or record in db.deleted
                        or record.related_order_id != source.id
                        or record.order_type != 'refund'
                        or record.business_type != 'refund_record'):
                    raise ValueError('refund order identity changed')
            else:
                values = {name: getattr(source, name) for name in COPY_FIELDS
                          if hasattr(Order, name)}
                record = Order(
                    id=secrets.token_hex(16),
                    order_no='REFORD-' + secrets.token_hex(16).upper(),
                    order_type='refund', business_type='refund_record',
                    related_order_id=source.id, **values,
                )
                db.add(record)
                link = RefundOrderLink(refund_id=refund.id, order_id=record.id,
                                       source_order_id=source.id, refund=refund,
                                       order=record, source_order=source)
                db.add(link)
            record.amount = -amount
            record.paid_amount = Decimal('0.00')
            record.refunded_amount = Decimal('0.00')
            record.payment_status = 'refunded' if status == 'completed' else 'not_applicable'
            record.main_status = {
                'completed': 'refund_completed', 'rejected': 'refund_rejected',
            }.get(status, 'refund_pending')
            record.delivery_status = 'not_applicable'
            record.after_sales_status = 'none'
        if flush:
            db.flush()
        return record

    def scan_pending(db, *, enabled=True):
        """Sync only new/modified Refund objects; never materialize history."""
        if not enabled:
            return []
        candidates = [row for row in list(db.new) + list(db.dirty)
                      if isinstance(row, Refund) and row not in db.deleted
                      and (row in db.new or db.is_modified(row, include_collections=False))]
        return [sync(db, row) for row in candidates]

    hooks = dict(RefundOrderLink=RefundOrderLink, sync=sync, scan_pending=scan_pending)
    ns['RefundOrderLink'] = RefundOrderLink
    ns['refund_order_records'] = hooks
    return hooks
