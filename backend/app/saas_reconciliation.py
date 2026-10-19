"""Additive outbound context. Call only while freezing a new request or plan."""
from copy import deepcopy
from datetime import datetime, timezone


def timestamp(value):
    """Normalize UTC; naive values are internal SQLite DateTime round trips."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def reconciliation_context(*, order=None, starts_at=None, ends_at=None,
                           subscription_expires_at=None, effective_at=None,
                           effective_at_semantics, provisional=False, affected_orders=None,
                           event_id=None, event_type=None, product_id=None, enterprise_id=None,
                           subscription_id=None, tenant_id=None):
    return {
        'schema_version': 1,
        'event_id': event_id,
        'event_type': event_type,
        'product_id': product_id,
        'enterprise_id': enterprise_id,
        'subscription_id': subscription_id,
        'tenant_id': tenant_id,
        'order_id': getattr(order, 'id', None),
        'order_no': getattr(order, 'order_no', None),
        'order_starts_at': timestamp(starts_at),
        'order_ends_at': timestamp(ends_at),
        'subscription_expires_at': timestamp(subscription_expires_at),
        'effective_at': timestamp(effective_at),
        'effective_at_semantics': effective_at_semantics,
        'period_status': 'provisional' if provisional else 'confirmed',
        'affected_orders': deepcopy(affected_orders or []),
    }


def change_context(quote, source_order, expires_at, segments=()):
    # Only project reconciliation fields; never expose quote prices or costs.
    fields = ('order_id', 'order_no', 'starts_at', 'ends_at',
              'remaining_starts_at', 'remaining_ends_at')
    segment_ids = {row['order_id']: row['term_id'] for row in segments}
    affected = [{('order_' + key if key in ('starts_at', 'ends_at') else key): row.get(key)
                 for key in fields} | {'term_id': segment_ids.get(row.get('order_id'))}
                for row in quote.get('segments', [])]
    source = next((row for row in affected if row['order_id'] == source_order.id), {})
    result = reconciliation_context(order=source_order,
        starts_at=source.get('order_starts_at'), ends_at=source.get('order_ends_at'),
        subscription_expires_at=expires_at, affected_orders=affected,
        effective_at_semantics='provider_receipt', provisional=expires_at is None)
    if source:
        result['order_no'] = source['order_no']
    return result


def event_context(context, *, event_id, event_type, product_id, enterprise_id,
                  subscription_id, tenant_id):
    return dict(deepcopy(context), event_id=event_id, event_type=event_type,
        product_id=product_id, enterprise_id=enterprise_id,
        subscription_id=subscription_id, tenant_id=tenant_id or None)
