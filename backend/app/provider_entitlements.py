"""Read-only, product-scoped provider reconciliation over persisted terms."""

import json
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, func, or_, select

from .subscription_calendar import CalendarAnchor
from .saas_reconciliation import reconciliation_context


def install(ns):
    if 'provider_entitlements' in ns:
        return ns['provider_entitlements']
    Order, Product, Enterprise = ns['Order'], ns['Product'], ns['Enterprise']
    Term, Combination = ns['Term'], ns['Combination']

    def authorize(product_id, request, db):
        authorization = request.headers.get('authorization', '').split()
        if len(authorization) != 2 or authorization[0].lower() != 'bearer':
            raise HTTPException(401, 'Bearer authentication required')
        credentials = ns['HTTPAuthorizationCredentials'](scheme='Bearer', credentials=authorization[1])
        # current_user rejects OAuth's market-resource audience and buyer API keys.
        user = ns['current_user'](credentials, db)
        if user.activation_status != 'active':
            raise HTTPException(403, 'Active account required')
        product = db.get(Product, product_id)
        if user.platform_role:
            ns['require_platform_admin'](user)
        else:
            if product is None:
                raise HTTPException(404, 'Product not found')
            membership = ns['require_enterprise_admin'](db, user, product.enterprise_id)
            if 'provider' not in (membership.business_roles or '').split(','):
                raise HTTPException(403, 'Provider role required')
            enterprise = db.get(Enterprise, product.enterprise_id)
            if user.verified_status != 'verified' or not enterprise or enterprise.verification_status != 'verified':
                raise HTTPException(403, 'Verified provider user and enterprise required')
        if product is None:
            raise HTTPException(404, 'Product not found')
        return product

    def aware(value):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def overlay_for(db, order, term):
        reader = ns['refund_execution_resources']['entitlement']
        overlay = reader(db, order, term)
        if overlay is not None:
            if overlay['payment_compatible'] is not True:
                raise HTTPException(409, 'Refund entitlement payment evidence is incompatible')
            for row in overlay['periods']:
                start = aware(row['starts_at']) if row.get('starts_at') else datetime.fromtimestamp(row['start_at'], timezone.utc)
                end = aware(row['ends_at']) if row.get('ends_at') else datetime.fromtimestamp(row['end_at'], timezone.utc)
                if (row['order_id'] != order.id or row['segment_id'] != term.id
                        or start < term.starts_at.replace(microsecond=0) or end > term.ends_at or start >= end):
                    raise HTTPException(409, 'Refund entitlement interval is invalid')
        return overlay

    def quota_projection(row):
        # These are gateway quota periods, not financial ledger segments.
        return {key: row[key] for key in (
            'order_id', 'segment_id', 'period_key', 'start_at', 'end_at',
            'rate_limit_per_minute', 'daily_quota', 'monthly_quota')} | {
            'starts_at': aware(row['starts_at']) if row.get('starts_at') else datetime.fromtimestamp(row['start_at'], timezone.utc),
            'ends_at': aware(row['ends_at']) if row.get('ends_at') else datetime.fromtimestamp(row['end_at'], timezone.utc)}

    def combination_rights(db, combination, product, cache):
        if combination.id not in cache:
            # Validate every recovery first: quota_periods deliberately skips rejected
            # rights for enforcement, while reconciliation must report the rejection.
            candidates = db.execute(select(Order, Term).join(Term, Term.order_id == Order.id).where(
                Term.combination_id == combination.id, Term.generation == combination.generation,
                Term.status == 'active', Order.product_id == product.id,
                Order.buyer_enterprise_id == combination.enterprise_id,
                Order.provider_enterprise_id == product.enterprise_id)).all()
            for order, term in candidates:
                overlay_for(db, order, term)
            periods = ns['subscription_terms']['quota_periods'](db, combination.enterprise_id, product.id)
            allowed = {term.id for order, term in candidates}
            cache[combination.id] = [quota_projection(row) for row in periods if row['segment_id'] in allowed]
        return cache[combination.id]

    @ns['app'].get('/api/provider/products/{product_id}/entitlements')
    def entitlements(
        product_id: str, request: Request, response: Response,
        enterprise_id: str = Query(default='', max_length=36),
        order_id: str = Query(default='', max_length=36),
        order_no: str = Query(default='', max_length=50),
        starts_before: datetime | None = None,
        ends_after: datetime | None = None,
        page: int = Query(default=1, ge=1, le=100000),
        page_size: int = Query(default=50, ge=1, le=100),
        db=Depends(ns['db_session']),
    ):
        """Read-only provider user/admin Bearer API; internal machine tokens are unsupported."""
        product = authorize(product_id, request, db)
        response.headers['Cache-Control'] = 'no-store'
        for value in (starts_before, ends_after):
            if value is not None and value.utcoffset() is None:
                raise HTTPException(422, 'Date filters must include a timezone')
        if starts_before and ends_after and ends_after >= starts_before:
            raise HTTPException(422, 'ends_after must precede starts_before')
        stmt = (select(Order, Term, Combination, Enterprise.name)
            .outerjoin(Term, Term.order_id == Order.id)
            .outerjoin(Combination, and_(Combination.id == Term.combination_id,
                Combination.product_id == Order.product_id,
                Combination.enterprise_id == Order.buyer_enterprise_id))
            .join(Enterprise, Enterprise.id == Order.buyer_enterprise_id)
            .where(Order.product_id == product.id,
                Order.provider_enterprise_id == product.enterprise_id,
                Order.delivery_method_snapshot.in_(('api', 'model_api', 'tenant_access'))))
        for field, value in ((Order.buyer_enterprise_id, enterprise_id),
                             (Order.id, order_id), (Order.order_no, order_no)):
            if value:
                stmt = stmt.where(field == value)
        if starts_before:
            stmt = stmt.where(Term.starts_at < starts_before)
        if ends_after:
            stmt = stmt.where(Term.ends_at > ends_after)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = db.execute(stmt.order_by(Order.created_at, Order.id)
                          .offset((page - 1) * page_size).limit(page_size)).all()
        items = []
        rights_cache = {}
        now = aware(ns['now']())
        for order, term, combination, enterprise_name in rows:
            # Order versions are purchase snapshots; terms carry revised versions.
            item = {
                'order_id': order.id, 'order_no': order.order_no,
                'enterprise_id': order.buyer_enterprise_id, 'enterprise_name': enterprise_name,
                'product_id': order.product_id, 'delivery_method': order.delivery_method_snapshot,
                'original_version_id': order.product_version_id,
                'original_version_code': order.product_version_code,
                'order_status': order.main_status, 'payment_status': order.payment_status,
                'delivery_status': order.delivery_status,
                'business_type': order.business_type,
                'related_order_id': order.related_order_id or None,
                'term_id': term.id if term else None,
                'version_id': term.version_id if term else None,
                'version_code': term.version_code if term else None,
                'starts_at': term.starts_at if term else None,
                'ends_at': term.ends_at if term else None,
                'term_status': term.status if term else None,
                'term_generation': term.generation if term else None,
                'combination_id': combination.id if combination else None,
                'combination_generation': combination.generation if combination else None,
                'combination_status': combination.status if combination else None,
                'recorded_combination_expires_at': CalendarAnchor(combination.anchor_at).boundary(
                    combination.total_months) if combination else None,
            }
            item['date_semantics'] = 'recorded_term_bounds'
            item['recorded_terms'] = ([{'order_id': order.id, 'segment_id': term.id,
                    'version_id': term.version_id, 'version_code': term.version_code,
                    'starts_at': term.starts_at, 'ends_at': term.ends_at,
                    'status': term.status, 'generation': term.generation}] if term else [])
            subscription = None
            if order.delivery_method_snapshot == 'tenant_access' and order.subscription_id:
                subscription = db.get(ns['SaaSSubscription'], order.subscription_id)
                if (not subscription or subscription.product_id != product.id
                        or subscription.enterprise_id != order.buyer_enterprise_id):
                    raise HTTPException(409, 'SaaS subscription identity is inconsistent')
            item.update(subscription_id=subscription.id if subscription else None,
                        tenant_id=subscription.external_tenant_id if subscription else None,
                        subscription_status=subscription.status if subscription else None,
                        subscription_expires_at=aware(subscription.expires_at) if subscription else None)
            lifecycle = ns.get('api_provider_lifecycle')
            item['provider_management'] = {'managed': False, 'status': 'unmanaged'}
            if lifecycle and order.delivery_method_snapshot in {'api', 'model_api'}:
                binding = db.scalar(select(lifecycle['Tenant']).where(
                    lifecycle['Tenant'].product_id == product.id,
                    lifecycle['Tenant'].enterprise_id == order.buyer_enterprise_id))
                if binding and combination and binding.combination_id == combination.id:
                    item['tenant_id'] = binding.tenant_id
                    receipts = db.execute(select(lifecycle['Plan'], lifecycle['Receipt']).join(
                        lifecycle['Receipt'], lifecycle['Receipt'].plan_id == lifecycle['Plan'].id).where(
                            lifecycle['Plan'].product_id == product.id,
                            lifecycle['Plan'].enterprise_id == order.buyer_enterprise_id)
                        .order_by(lifecycle['Receipt'].recorded_at.desc())).all()
                    latest = next((json.loads(receipt.evidence_json) for plan, receipt in receipts
                        if plan.payload['combination_id'] == combination.id), None)
                    if latest:
                        item['provider_management'] = {
                            'managed': True, 'status': latest['status'],
                            'event_id': latest['event_id'], 'operation': latest['type'],
                            'generation': latest['generation'],
                            'starts_at': latest['starts_at'], 'expires_at': latest['expires_at'],
                            'effective_at': latest['effective_at'], 'tenant_id': latest['tenant_id']}
            try:
                overlay = overlay_for(db, order, term) if term else None
                periods = combination_rights(db, combination, product, rights_cache) if combination else []
                own_periods = [row for row in periods if row['order_id'] == order.id]
                final_expiry = max((row['ends_at'] for row in periods), default=None)
                if subscription:
                    final_expiry = aware(subscription.expires_at)
                    # A tenant's recorded expiry is authoritative; it can shorten
                    # the calendar, but does not by itself prove paid order rights.
                    own_periods = [dict(row, ends_at=min(row['ends_at'], final_expiry),
                        end_at=int(min(row['ends_at'], final_expiry).timestamp()))
                        for row in own_periods if final_expiry and row['starts_at'] < final_expiry]
                item['quota_periods'] = ([quota_projection(row) for row in overlay['periods']]
                    if overlay is not None else own_periods)
                item['quota_periods_semantics'] = 'recorded_refund_recovery' if overlay is not None else 'validated_current_generation'
            except (HTTPException, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise HTTPException(409, 'Entitlement reconciliation evidence is unavailable or rejected') from exc
            effective_start = min((row['starts_at'] for row in own_periods), default=None)
            effective_end = max((row['ends_at'] for row in own_periods), default=None)
            active = (combination is not None and combination.status == 'active'
                      and (subscription is None or subscription.status == 'active')
                      and any(row['starts_at'] <= now < row['ends_at'] for row in own_periods))
            item.update(combination_expires_at=final_expiry,
                combination_expiry_source='subscription_record' if subscription else 'validated_quota_periods',
                effective_rights={'starts_at': effective_start, 'ends_at': effective_end,
                    'active_now': bool(active), 'current_generation': bool(term and combination
                        and term.generation == combination.generation),
                    'periods': own_periods},
                rights_checked_at=now)
            item['segments_semantics'] = 'effective_entitlement_intervals_not_financial_ledger'
            item['segments'] = [dict(order_id=row['order_id'], segment_id=row['segment_id'],
                version_id=term.version_id, version_code=term.version_code,
                starts_at=row['starts_at'], ends_at=row['ends_at']) for row in own_periods]
            Refund = ns['Refund']
            item['refunds'] = [dict(refund_id=rid, refund_no=number, status=status)
                for rid, number, status in db.execute(select(Refund.id, Refund.refund_no, Refund.status)
                    .where(Refund.order_id == order.id).order_by(Refund.created_at, Refund.id))]
            Revision = ns['SubscriptionTermRevision']
            item['change_revisions'] = [dict(change_id=change_id, revision_number=number,
                version_id=version_id, version_code=code, recorded_at=at)
                for change_id, number, version_id, code, at in db.execute(select(
                    Revision.change_id, Revision.revision_number, Revision.version_id,
                    Revision.version_code, Revision.created_at).where(Revision.term_id == term.id)
                    .order_by(Revision.revision_number))] if term else []
            Change = ns['version_change_saga']['VersionChangeRequest']
            item['changes'] = [dict(change_id=cid, source_order_id=source,
                funding_order_id=funding, kind=kind, status=status)
                for cid, source, funding, kind, status in db.execute(select(
                    Change.id, Change.source_order_id, Change.funding_order_id, Change.kind, Change.status)
                    .where(Change.enterprise_id == order.buyer_enterprise_id,
                        or_(Change.source_order_id == order.id, Change.funding_order_id == order.id,
                            Change.id.in_([row['change_id'] for row in item['change_revisions']])))
                    .order_by(Change.created_at, Change.id))]
            item['reconciliation_context'] = reconciliation_context(
                order=order, starts_at=item['starts_at'], ends_at=item['ends_at'],
                subscription_expires_at=final_expiry,
                effective_at_semantics='recorded_term_bounds', provisional=term is None,
                product_id=order.product_id, enterprise_id=order.buyer_enterprise_id,
                subscription_id=item['subscription_id'], tenant_id=item['tenant_id'])
            items.append(item)
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size,
                'has_more': page * page_size < total}

    hooks = {'authorize': authorize}
    ns['provider_entitlements'] = hooks
    return hooks
