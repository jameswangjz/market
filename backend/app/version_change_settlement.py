"""Monthly settlement integration for posted version-change adjustments."""
import json
import secrets
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, UniqueConstraint, event, select

from .refund_execution import canonical, digest
from .refund_execution_resources import strict_json
from .settlement_finance_adapter import proportional_values


CENT = Decimal("0.01")
ACTIVE_BATCH = {"generated", "pending_confirm", "confirmed", "payment_processing", "partial_paid", "paid", "archived"}
FINAL_BATCH = {"confirmed", "payment_processing", "partial_paid", "paid", "archived"}


def install(ns):
    if "version_change_settlement" in ns:
        return ns["version_change_settlement"]
    Base = ns["Base"]

    class VersionChangeSettlementLink(Base):
        __tablename__ = "version_change_settlement_links"
        __table_args__ = (UniqueConstraint("adjustment_id", name="uq_vc_settlement_adjustment"),
                          UniqueConstraint("settlement_id", name="uq_vc_settlement_settlement"))
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        adjustment_id = Column(ForeignKey("version_change_adjustments.id"), nullable=False)
        settlement_id = Column(ForeignKey("settlements.id"), nullable=False)

    class SegmentBaselineLink(Base):
        __tablename__ = 'version_change_segment_baseline_links'
        window_id = Column(ForeignKey('refund_segment_windows.id'), primary_key=True)
        settlement_id = Column(ForeignKey('settlements.id'), nullable=False, unique=True)
        source_settlement_id = Column(ForeignKey('settlements.id'), nullable=False, index=True)
        baseline_hash = Column(String(64), nullable=False)
        context_hash = Column(String(64), nullable=False)
        evidence_json = Column(Text, nullable=False)
        actor = Column(String(180), nullable=False)

    def immutable(*args):
        raise ValueError('version change financial linkage is immutable')

    event.listen(SegmentBaselineLink, 'before_update', immutable)
    event.listen(SegmentBaselineLink, 'before_delete', immutable)

    def fail(message):
        raise HTTPException(409, message)

    def cents(value):
        try:
            value = Decimal(str(value))
            if not value.is_finite() or abs(value) > Decimal('999999999999.99') or value != value.quantize(CENT):
                raise ValueError()
            return value.quantize(CENT)
        except (ValueError, TypeError, ArithmeticError):
            fail('invalid_segment_money')

    def final_baseline(db, settlement_id, order, currency):
        model = ns.get('settlement_finance', {}).get('Baseline')
        if model is None:
            fail('settlement_finance_unavailable')
        b = db.scalar(select(model).where(model.settlement_id == settlement_id).with_for_update())
        settlement = db.get(ns['Settlement'], settlement_id)
        if not b or not settlement or settlement.status not in ('locked', 'paid'):
            fail('exact_segment_final_baseline_required')
        snapshot = strict_json(b.snapshot_json)
        if (b.order_id != order.id or b.provider_enterprise_id != order.provider_enterprise_id or b.currency != currency
                or b.revenue <= 0 or b.cost < 0 or b.profit < 0 or b.revenue - b.cost != b.profit
                or hashlib.sha256(b.snapshot_json.encode()).hexdigest() != b.snapshot_hash
                or sum(cents(v) for v in snapshot['shares'].values()) != b.profit):
            fail('segment_final_baseline_mismatch')
        return b

    def window_context(db, window):
        """Validate immutable identities and hashes without consulting current catalog rules."""
        terms = ns['subscription_terms']
        revision = db.get(terms['Revision'], window.revision_id)
        term = db.get(terms['Term'], window.term_id)
        order = db.get(ns['Order'], window.order_id)
        change = db.get(ns['VersionChangeRequest'], window.change_id)
        receipt = db.get(ns['refund_segment_store']['Receipt'], window.receipt_id)
        combination = db.get(terms['Combination'], term.combination_id) if term else None
        if (not revision or not term or not order or not change or not receipt
                or not combination or combination.enterprise_id != order.buyer_enterprise_id
                or combination.product_id != order.product_id
                or revision.change_id != change.id or revision.term_id != term.id or term.order_id != order.id
                or term.combination_id != change.combination_id or change.enterprise_id != order.buyer_enterprise_id
                or change.status != 'completed' or not change.resource_applied or not change.completed_at
                or receipt.change_id != change.id or receipt.direction != 'target'
                or hashlib.sha256(receipt.receipt_json.encode()).hexdigest() != receipt.receipt_hash):
            fail('segment_window_ownership_or_completion_mismatch')
        economic = strict_json(window.economic_snapshot_json)
        delivery = strict_json(window.delivery_snapshot_json)
        frozen = strict_json(change.target_snapshots_json).get(term.id)
        proof = strict_json(change.evidence_json)
        evidence = strict_json(window.evidence_json)
        if (not frozen or frozen['economic'] != economic or frozen['delivery'] != delivery
                or revision.economic_snapshot_json != window.economic_snapshot_json
                or revision.delivery_snapshot_json != window.delivery_snapshot_json
                or revision.version_id != change.target_version_id or economic['product_version_id'] != revision.version_id
                or economic['product_id'] != order.product_id or economic['currency'] != window.currency
                or proof.get('request_change_id') != change.id or proof.get('resource_change_id') != change.id
                or evidence.get('proof') != proof or evidence.get('receipt_hash') != receipt.receipt_hash
                or evidence.get('economic_hash') != digest(economic) or evidence.get('delivery_hash') != digest(delivery)):
            fail('segment_window_frozen_evidence_mismatch')
        fields = ('order_id', 'term_id', 'change_id', 'receipt_id', 'revision_id', 'revenue', 'cost', 'currency',
                  'starts_at', 'ends_at', 'available_quota', 'economic_snapshot_json', 'delivery_snapshot_json', 'evidence_json')
        payload = {k: getattr(window, k) for k in fields}
        payload = {k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v
                   for k, v in payload.items()}
        if digest(payload) != window.payload_hash:
            fail('segment_window_hash_mismatch')
        adjustments = db.scalars(select(ns['VersionChangeAdjustment']).where(
            ns['VersionChangeAdjustment'].change_id == change.id,
            ns['VersionChangeAdjustment'].source_order_id == order.id)).all()
        by_side = {a.side: a for a in adjustments}
        if len(adjustments) != 2 or set(by_side) != {'old', 'target'}:
            fail('exact_posted_adjustment_pair_required')
        prepared = [s for s in strict_json(change.prepared_json) if s['order_id'] == order.id and s['term_id'] == term.id]
        if len(prepared) != 1:
            fail('exact_prepared_term_required')
        for side, a in by_side.items():
            item = prepared[0][side]
            if (a.status != 'posted' or not a.posted_at or a.request_change_id != change.id
                    or a.resource_change_id != change.id or a.resource_evidence_fingerprint != proof['evidence_fingerprint']
                    or a.combination_id != term.combination_id or a.currency != window.currency
                    or a.revenue != cents(item['revenue']) or a.cost != cents(item['cost'])
                    or a.profit != cents(item['profit']) or a.revenue - a.cost != a.profit
                    or a.original_version_id != prepared[0]['old']['version_id']
                    or a.target_version_id != prepared[0]['target']['version_id']
                    or strict_json(a.rule_json) != item['rule']):
                fail('posted_adjustment_evidence_mismatch')
        target = by_side['target']
        if (target.target_version_id != revision.version_id or strict_json(target.rule_json) != economic['settlement_rule']
                or not 0 < window.revenue <= target.revenue or not 0 <= window.cost <= target.cost
                or window.ends_at != term.ends_at or window.starts_at != max(term.starts_at, receipt.cutover_at)
                or window.starts_at >= window.ends_at):
            fail('target_window_economics_mismatch')
        raw = strict_json(receipt.receipt_json)
        resource = strict_json(change.resource_plan_json)
        if raw.get('scope') != change.combination_id or raw.get('change_id') != change.id or raw.get('direction') != 'target':
            fail('segment_receipt_identity_mismatch')
        if receipt.kind == 'api':
            desc = resource.get('charge_boundary', {})
            expected_proof = dict(change_id=change.id, direction='target', kind='api', evidence=dict(
                route_id=resource.get('target_route_id'), route_key=resource.get('target_route_key'),
                credential_ids=[c['id'] for c in resource.get('credentials', []) if c['status'] == 'active'],
                receipt_ids=[receipt.id]))
            attributed = [p for p in raw.get('periods', []) if p.get('order_id') == order.id
                          and p.get('term_id', p.get('segment_id')) == term.id]
            available = None if any(p['available_quota'] is None for p in attributed) else sum(p['available_quota'] for p in attributed)
            if (raw.get('source_run_id') != receipt.source_run_id or raw.get('boundary_id') != receipt.boundary_id
                    or window.revenue != target.revenue or window.cost != target.cost or not attributed
                    or available != window.available_quota or resource.get('kind') != 'api' or desc.get('mode') != 'strict'
                    or desc.get('credential_id') != receipt.credential_id or receipt.target_fingerprint != digest(desc.get('target'))
                    or raw.get('target_fingerprint') != receipt.target_fingerprint
                    or proof['evidence_fingerprint'] != digest(expected_proof)):
                fail('segment_api_receipt_or_amount_mismatch')
        elif receipt.kind == 'saas':
            expected_proof = dict(change_id=change.id, direction='target', kind='saas', evidence=dict(
                subscription_id=receipt.subscription_id, tenant_id=resource.get('tenant_id'),
                version_id=change.target_version_id, receipt_ids=[receipt.id]))
            start = max(term.starts_at, target.effective_at)
            ratio = Decimal((term.ends_at - window.starts_at) // timedelta(microseconds=1)) / Decimal(
                (term.ends_at - start) // timedelta(microseconds=1))
            if (window.revenue != (target.revenue * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
                    or window.cost != (target.cost * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
                    or resource.get('kind') != 'saas' or resource.get('subscription_id') != receipt.subscription_id
                    or raw.get('subscription_id') != receipt.subscription_id or raw.get('tenant_id') != resource.get('tenant_id')
                    or raw.get('version_id') != revision.version_id or raw.get('enterprise_id') != order.buyer_enterprise_id
                    or raw.get('product_id') != order.product_id or proof['evidence_fingerprint'] != digest(expected_proof)):
                fail('segment_saas_window_amount_mismatch')
        else:
            fail('unsupported_segment_receipt_kind')
        return order, term, revision, change, by_side, economic, receipt

    def linked_settlement(db, adjustment):
        link = db.scalar(select(VersionChangeSettlementLink).where(
            VersionChangeSettlementLink.adjustment_id == adjustment.id))
        if not link:
            fail('posted_adjustment_settlement_missing')
        return db.get(ns['Settlement'], link.settlement_id)

    def paired_target(db, adjustment_id):
        a = db.get(ns['VersionChangeAdjustment'], adjustment_id)
        if not a or a.side != 'old':
            return None
        rows = db.scalars(select(ns['VersionChangeAdjustment']).where(
            ns['VersionChangeAdjustment'].change_id == a.change_id,
            ns['VersionChangeAdjustment'].source_order_id == a.source_order_id,
            ns['VersionChangeAdjustment'].side == 'target')).all()
        return linked_settlement(db, rows[0]) if len(rows) == 1 else None

    def source_baseline(db, window, order, term, revision, old):
        if revision.revision_number > 1:
            prior = db.scalar(select(ns['subscription_terms']['Revision']).where(
                ns['subscription_terms']['Revision'].term_id == term.id,
                ns['subscription_terms']['Revision'].revision_number == revision.revision_number - 1))
            if not prior or prior.version_id != old.original_version_id:
                fail('exact_previous_segment_required')
            previous = db.scalar(select(ns['refund_segment_store']['Window']).where(
                ns['refund_segment_store']['Window'].revision_id == prior.id))
            binding = db.get(SegmentBaselineLink, previous.id) if previous else None
            if not binding:
                fail('previous_segment_final_binding_required')
            b = final_baseline(db, binding.settlement_id, order, window.currency)
            if b.snapshot_hash != binding.baseline_hash:
                fail('previous_segment_baseline_hash_mismatch')
            raw_revenue = previous.revenue
        else:
            model = ns.get('settlement_finance', {}).get('Baseline')
            if model is None:
                fail('settlement_finance_unavailable')
            ids = db.scalars(select(model.settlement_id).join(ns['Settlement'], ns['Settlement'].id == model.settlement_id)
                .where(model.order_id == order.id, model.revenue > 0, ns['Settlement'].status.in_(('locked', 'paid')),
                       ~model.settlement_id.in_(select(VersionChangeSettlementLink.settlement_id)))).all()
            if len(ids) != 1 or order.product_version_id != old.original_version_id:
                fail('exact_original_segment_final_baseline_required')
            b = final_baseline(db, ids[0], order, window.currency)
            original = strict_json(order.economic_snapshot_json)
            if original['product_version_id'] != old.original_version_id:
                fail('original_segment_version_mismatch')
            raw_revenue = cents(original['amount'])
        if abs(old.revenue) > raw_revenue or old.revenue >= 0:
            fail('source_segment_revenue_requires_explicit_funding_plan')
        if b.revenue < raw_revenue:
            fail('source_segment_revenue_requires_explicit_funding_plan')
        if b.revenue > raw_revenue:
            proof = ns.get('settlement_version_funding', {}).get('source_proof')
            if not callable(proof):
                fail(dict(code='source_funding_projection_required', reason='source_funding_contract_unavailable'))
            try:
                proof(db, b, raw_revenue)
            except HTTPException as exc:
                fail(dict(code='source_funding_projection_required', reason=exc.detail))
            except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                fail(dict(code='source_funding_projection_required', reason='source_funding_evidence_invalid'))
        return b

    def funding_context(db, change, source_order_id):
        """Return only verified net cash legs, never treat target gross as a payment."""
        adjustments = db.scalars(select(ns['VersionChangeAdjustment']).where(
            ns['VersionChangeAdjustment'].change_id == change.id)).all()
        net = sum((cents(a.revenue) for a in adjustments), Decimal(0))
        if any(a.status != 'posted' or a.request_change_id != change.id or a.resource_change_id != change.id
               for a in adjustments):
            fail('posted_funding_economic_group_required')
        if len({a.payload_fingerprint for a in adjustments}) != 1:
            fail('posted_change_fingerprint_mismatch')
        groups = {}
        for a in adjustments:
            groups.setdefault(a.source_order_id, []).append(a)
        prepared = strict_json(change.prepared_json)
        if len(prepared) != len(groups) or {s['order_id'] for s in prepared} != set(groups):
            fail('complete_change_segment_group_required')
        segments = []
        for oid, rows in sorted(groups.items()):
            sides = {a.side: a for a in rows}
            frozen = [s for s in prepared if s['order_id'] == oid]
            if len(rows) != 2 or set(sides) != {'old', 'target'} or len(frozen) != 1:
                fail('exact_change_adjustment_pairs_required')
            source = db.get(ns['Order'], oid)
            if not source or source.buyer_enterprise_id != change.enterprise_id:
                fail('change_segment_buyer_mismatch')
            for side, row in sides.items():
                if row.revenue != cents(frozen[0][side]['revenue']):
                    fail('change_segment_funding_proof_mismatch')
            if sides['old'].revenue >= 0 or sides['target'].revenue <= 0:
                fail('unsupported_zero_or_mixed_segment_transfer')
            segments.append(dict(order_id=oid, term_id=frozen[0]['term_id'],
                old_adjustment_id=sides['old'].id, target_adjustment_id=sides['target'].id,
                old_amount=str(sides['old'].revenue), target_amount=str(sides['target'].revenue),
                segment_net_amount=str((sides['old'].revenue + sides['target'].revenue).quantize(CENT))))
        selected = [s for s in segments if s['order_id'] == source_order_id]
        if len(selected) != 1 or sum(cents(s['segment_net_amount']) for s in segments) != net:
            fail('segment_net_change_conservation_failed')
        allocations = []
        for segment in segments:
            windows = db.scalars(select(ns['refund_segment_store']['Window']).where(
                ns['refund_segment_store']['Window'].change_id == change.id,
                ns['refund_segment_store']['Window'].order_id == segment['order_id'])).all()
            if len(windows) != 1:
                fail('exact_change_funding_windows_required')
            window_context(db, windows[0])
            allocations.append(dict(window_id=windows[0].id, net_amount=segment['segment_net_amount'],
                old_adjustment_id=segment['old_adjustment_id'], target_adjustment_id=segment['target_adjustment_id']))
        if ((net > 0 and change.kind != 'upgrade') or (net < 0 and change.kind != 'downgrade')
                or (net == 0 and change.kind != 'no_payment')):
            fail('version_change_kind_funding_mismatch')
        funds = db.scalars(select(ns['VersionChangeFund']).where(ns['VersionChangeFund'].change_id == change.id)
                           .order_by(ns['VersionChangeFund'].id).with_for_update()).all()
        result, actual, seen = [], Decimal(0), set()
        for fund in funds:
            identity = (fund.kind, fund.payment_id if fund.kind == 'payment' else fund.refund_id)
            if identity in seen:
                fail('duplicate_version_change_cash_proof')
            seen.add(identity)
            if fund.amount <= 0:
                fail('invalid_version_change_cash_leg')
            if fund.kind == 'payment':
                payment = db.get(ns['Payment'], fund.payment_id)
                order = db.get(ns['Order'], payment.order_id) if payment else None
                if (net <= 0 or not payment or payment.status not in ('paid', 'refunding', 'refunded') or not payment.paid_at
                        or not order or order.id != change.funding_order_id or order.business_type != 'version_change_payment'
                        or order.buyer_enterprise_id != change.enterprise_id or payment.amount != fund.amount):
                    fail('version_change_payment_proof_mismatch')
                actual += fund.amount
                result.append(dict(fund_id=fund.id, kind='payment', payment_id=payment.id, amount=str(fund.amount)))
            elif fund.kind == 'refund':
                refund = db.get(ns['Refund'], fund.refund_id)
                payment = db.get(ns['Payment'], fund.payment_id)
                source_order = db.get(ns['Order'], refund.order_id) if refund else None
                if (net >= 0 or not refund or refund.status != 'completed' or not refund.completed_at
                        or refund.payment_id != fund.payment_id or refund.amount != fund.amount
                        or not payment or payment.order_id != refund.order_id or not source_order
                        or source_order.buyer_enterprise_id != change.enterprise_id
                        or refund.order_id not in {a.source_order_id for a in adjustments}):
                    fail('version_change_refund_proof_mismatch')
                actual -= fund.amount
                result.append(dict(fund_id=fund.id, kind='refund', refund_id=refund.id,
                                   payment_id=refund.payment_id, amount=str(fund.amount)))
            else:
                fail('invalid_version_change_cash_kind')
        if actual != net or cents(strict_json(change.quote_json)['net_difference']) != net:
            fail('version_change_net_funding_mismatch')
        return dict(change_id=change.id, net_amount=str(net.quantize(CENT)),
                    change_total_net_amount=str(net.quantize(CENT)), segment_net_amount=selected[0]['segment_net_amount'],
                    segments=segments, segment_allocations=allocations, legs=result,
                    basis='verified_net_change_cash_not_target_gross')

    def financial_context(db, settlement):
        """Read-only exact context for finance.capture_segment; no financial posting."""
        try:
            link = db.scalar(select(VersionChangeSettlementLink).where(
                VersionChangeSettlementLink.settlement_id == settlement.id))
            if not link:
                fail('version_change_settlement_link_required')
            a = db.get(ns['VersionChangeAdjustment'], link.adjustment_id)
            windows = db.scalars(select(ns['refund_segment_store']['Window']).where(
                ns['refund_segment_store']['Window'].change_id == a.change_id,
                ns['refund_segment_store']['Window'].order_id == a.source_order_id)).all()
            if len(windows) != 1:
                fail('exact_verified_segment_window_required')
            w = windows[0]
            order, term, revision, change, pair, economic, receipt = window_context(db, w)
            source = source_baseline(db, w, order, term, revision, pair['old'])
            old_values = proportional_values(source, abs(pair['old'].revenue))
            projection = None
            source_snapshot = strict_json(source.snapshot_json)
            if source_snapshot.get('actual_receipts') is not None and source.revenue > cents(source_snapshot['actual_receipts']):
                project = ns.get('settlement_version_funding', {}).get('project')
                if not callable(project):
                    fail(dict(code='source_funding_projection_required', reason='source_funding_contract_unavailable'))
                negative = linked_settlement(db, pair['old'])
                try:
                    projection = project(db, source, cents(source_snapshot['actual_receipts']), abs(pair['old'].revenue),
                                         w, change, receipt, negative.id)
                except HTTPException as exc:
                    fail(dict(code='source_funding_projection_required', reason=exc.detail))
                except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                    fail(dict(code='source_funding_projection_required', reason='source_funding_projection_invalid'))
            if a.side == 'old':
                values = dict(gross_amount=str(-abs(a.revenue)), cost_amount=str(-old_values['cost']),
                    profit_amount=str(-old_values['profit']), shares={k: str(-v) for k, v in old_values['shares'].items()})
                if projection:
                    values = projection['values']
            else:
                revenue, cost, profit = (cents(getattr(settlement, k)) for k in ('gross_amount', 'cost_amount', 'profit_amount'))
                shares = {k: cents(getattr(settlement, k)) for k in old_values['shares']}
                if revenue != a.revenue or cost < 0 or profit < 0 or revenue - cost != profit or sum(shares.values()) != profit:
                    fail('target_final_values_require_explicit_funding_plan')
                values = dict(gross_amount=str(revenue), cost_amount=str(cost), profit_amount=str(profit),
                              shares={k: str(v) for k, v in shares.items()})
            context = dict(schema_version=1, kind='version_change', settlement_id=settlement.id, side=a.side,
                order_id=order.id, product_id=order.product_id, provider_enterprise_id=order.provider_enterprise_id,
                currency=w.currency, adjustment_id=a.id, adjustment_fingerprint=a.payload_fingerprint,
                change_id=change.id, window_id=w.id, window_hash=w.payload_hash, term_id=term.id,
                revision_id=revision.id, receipt_id=receipt.id, receipt_hash=receipt.receipt_hash,
                source_settlement_id=source.settlement_id, source_baseline_hash=source.snapshot_hash,
                source_reversal= dict(gross_amount=str(-abs(pair['old'].revenue)), cost_amount=str(-old_values['cost']),
                    profit_amount=str(-old_values['profit']), shares={k: str(-v) for k, v in old_values['shares'].items()}),
                economic_snapshot=economic, funding=funding_context(db, change, order.id), values=values)
            context['participant_allocations'] = {}
            allocate = ns.get('settlement_parties', {}).get('allocations')
            if a.side == 'target' and callable(allocate):
                frozen_order = SimpleNamespace(id=order.id, product_id=order.product_id,
                    provider_enterprise_id=order.provider_enterprise_id, economic_snapshot_json=canonical(economic))
                for role, field in (('service', 'service_share'), ('expert', 'expert_fee'), ('channel', 'channel_fee')):
                    context['participant_allocations'][role] = allocate(db, frozen_order, role, cents(values['shares'][field]))
            context['segment_net_amount'] = context['funding']['segment_net_amount']
            context['change_total_net_amount'] = context['funding']['change_total_net_amount']
            if projection:
                context.update(schema_version=2, version_funding_projection=projection,
                    returned_unused_supplement=projection['returned_unused_supplement'],
                    source_reversal=projection['values'])
            context['context_hash'] = digest(context)
            return dict(status='ready', **context)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='unsupported', reason='segment_financial_evidence_invalid')

    def bind_baseline(db, settlement, actor='system'):
        context = financial_context(db, settlement)
        if context['status'] != 'ready' or context['side'] != 'target':
            fail(context.get('reason', 'target_segment_context_required'))
        order = db.get(ns['Order'], settlement.order_id)
        baseline = final_baseline(db, settlement.id, order, context['currency'])
        snapshot = strict_json(baseline.snapshot_json)
        values = context['values']
        if (snapshot.get('segment_context_hash') != context['context_hash']
                or snapshot.get('economic_snapshot') != context['economic_snapshot']
                or any(cents(getattr(baseline, k)) != cents(values[v]) for k, v in
                       (('revenue', 'gross_amount'), ('cost', 'cost_amount'), ('profit', 'profit_amount')))
                or {k: cents(v) for k, v in snapshot['shares'].items()} != {k: cents(v) for k, v in values['shares'].items()}):
            fail('finance_segment_context_not_captured')
        evidence = canonical(dict(context_hash=context['context_hash'], window_hash=context['window_hash'],
                                  baseline_hash=baseline.snapshot_hash, source_baseline_hash=context['source_baseline_hash']))
        old = db.get(SegmentBaselineLink, context['window_id'])
        if old:
            if old.settlement_id != settlement.id or old.evidence_json != evidence:
                fail('segment_baseline_binding_conflict')
            return old
        binding = SegmentBaselineLink(window_id=context['window_id'], settlement_id=settlement.id,
            source_settlement_id=context['source_settlement_id'], baseline_hash=baseline.snapshot_hash,
            context_hash=context['context_hash'], evidence_json=evidence, actor=str(actor))
        db.add(binding)
        db.flush()
        ns['audit'](db, actor, 'bind_version_change_final_baseline', 'settlement', settlement.settlement_no,
                    category='settlement', business_domain='settlement', order_id=order.id,
                    after=dict(window_id=binding.window_id, baseline_hash=binding.baseline_hash, context_hash=binding.context_hash))
        db.flush()
        return binding

    def prepare_lock(db, settlement, actor='system'):
        """Explicitly project final old-side values without editing posted economic evidence."""
        context = financial_context(db, settlement)
        if context['status'] != 'ready':
            reason = context.get('reason')
            if isinstance(reason, dict) and reason.get('code') == 'source_funding_projection_required':
                fail(reason)
            return context
        if context.get('version_funding_projection') and context['side'] == 'target':
            projection = context['version_funding_projection']
            if ns['settlement_version_funding']['describe'](db, projection['settlement_id']) is None:
                fail('source_funding_old_capture_required')
        if settlement.status in ('locked', 'paid', 'superseded'):
            fail('segment_projection_requires_mutable_settlement')
        before = dict(cost_amount=str(settlement.cost_amount), profit_amount=str(settlement.profit_amount))
        values = context['values']
        for k in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(settlement, k, cents(values[k]))
        settlement.net_amount = settlement.gross_amount
        settlement.reference_settlement_id = context['source_settlement_id'] if context['side'] == 'old' else ''
        for k, v in values['shares'].items():
            setattr(settlement, k, cents(v))
        fields = dict(platform='platform_fee', provider='provider_share', service='service_share', expert='expert_fee', channel='channel_fee')
        Line = ns['SettlementLine']
        lines = db.scalars(select(Line).where(Line.settlement_id == settlement.id, Line.status != 'superseded')).all()
        if len(lines) != len({line.participant_type for line in lines}):
            fail('multi_subject_segment_line_projection_requires_plan')
        for line in lines:
            line.amount = getattr(settlement, fields[line.participant_type])
        for batch_id in {line.batch_id for line in lines}:
            batch = db.get(ns['SettlementBatch'], batch_id)
            batch.total_profit = cents(batch.total_profit) + settlement.profit_amount - cents(before['profit_amount'])
        ns['audit'](db, actor, 'project_version_change_final_baseline', 'settlement', settlement.settlement_no,
                    category='settlement', business_domain='settlement', order_id=settlement.order_id,
                    before=before, after=context)
        db.flush()
        return context

    def refund_basis(db, refund, plan, *, apply_exception=True):
        try:
            window = db.get(ns['refund_segment_store']['Window'], plan['segment_id'])
            if not window or window.order_id != refund.order_id or window.payload_hash != plan.get('window_hash'):
                fail('exact_refund_segment_window_required')
            order, term, revision, change, pair, economic, receipt = window_context(db, window)
            if (plan.get('change_id') != change.id or plan.get('receipt_hash') != receipt.receipt_hash
                    or plan.get('source_order_id') != order.id
                    or plan.get('starts_at') != window.starts_at.isoformat() or plan.get('ends_at') != window.ends_at.isoformat()
                    or plan['economic_snapshot'] != economic or cents(plan['original_amount']) != window.revenue
                    or cents(plan['revenue']) != window.revenue or cents(plan['cost']) != window.cost):
                fail('refund_segment_frozen_basis_mismatch')
            settlement = linked_settlement(db, pair['target'])
            context = financial_context(db, settlement)
            if context['status'] != 'ready':
                return context
            b = final_baseline(db, settlement.id, order, window.currency)
            binding = db.get(SegmentBaselineLink, window.id)
            if (not binding or binding.settlement_id != b.settlement_id or binding.baseline_hash != b.snapshot_hash
                    or binding.context_hash != context['context_hash']):
                fail('exact_refund_segment_final_binding_required')
            amount, cumulative = cents(plan['refund_amount']), cents(plan['cumulative_refund_amount'])
            if amount <= 0 or amount != cents(refund.amount) or not amount <= cumulative <= window.revenue:
                fail('invalid_segment_refund_cumulative_amount')
            # A delayed SaaS switch can narrow the window; project the final
            # target baseline once, then use cumulative cent differences.
            window_values = proportional_values(b, window.revenue)
            current = proportional_values(b, cumulative, basis_revenue=window.revenue, basis_values=window_values)
            previous = proportional_values(b, cumulative - amount, basis_revenue=window.revenue, basis_values=window_values)
            shares = {k: previous['shares'][k] - current['shares'][k] for k in current['shares']}
            if any(v > 0 for v in shares.values()):
                fail('subject_rounding_requires_explicit_reversal_plan')
            result = dict(status='ready', reference_settlement_id=b.settlement_id, baseline_hash=b.snapshot_hash,
                currency=window.currency, gross_amount=str(-amount), cost_amount=str(previous['cost'] - current['cost']),
                profit_amount=str(previous['profit'] - current['profit']), shares={k: str(v) for k, v in shares.items()},
                segment_id=window.id, window_hash=window.payload_hash, segment_context_hash=binding.context_hash,
                cumulative_refund_amount=str(cumulative), original_amount=str(window.revenue))
            if not apply_exception:
                return result
            hook = ns.get('settlement_finance', {}).get('apply_cost_exception')
            if not callable(hook):
                fail('segment_cost_exception_application_contract_required')
            return hook(db, refund, b, result)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='unsupported', reason='segment_refund_final_evidence_invalid')

    def refund_segment_basis(db, refund, plan):
        """Ordinary exact slice for a multi-source manifest; no exception writes."""
        try:
            amount = cents(plan['refund_amount'])
            if (plan['source_order_id'] != refund.order_id or amount <= 0
                    or amount > cents(refund.amount)):
                fail('refund_segment_scope_or_amount_mismatch')
            subject = SimpleNamespace(id=refund.id, order_id=refund.order_id, amount=amount)
            result = refund_basis(db, subject, plan, apply_exception=False)
            if result['status'] != 'ready':
                return result
            window = db.get(ns['refund_segment_store']['Window'], plan['segment_id'])
            receipt = db.get(ns['refund_segment_store']['Receipt'], window.receipt_id)
            return dict(result, receipt_id=receipt.id, receipt_hash=receipt.receipt_hash,
                        revision_id=window.revision_id, term_id=window.term_id, change_id=window.change_id)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='segment_refund_final_evidence_invalid')

    def is_funding_order(order):
        return bool(order and getattr(order, "business_type", "") == "version_change_payment")

    def is_change_refund(db, refund):
        Fund = ns.get("VersionChangeFund")
        if Fund is None or refund is None:
            return False
        return db.scalar(select(Fund.refund_id).where(
            Fund.refund_id == refund.id).limit(1)) is not None

    def describe(db, settlement_id):
        link = db.scalar(select(VersionChangeSettlementLink).where(
            VersionChangeSettlementLink.settlement_id == settlement_id))
        if link is None:
            return None
        Adjustment = ns.get("VersionChangeAdjustment")
        if Adjustment is None:
            return None
        adjustment = db.get(Adjustment, link.adjustment_id)
        if adjustment is None:
            return None
        try:
            rule_version = json.loads(adjustment.rule_json or "{}").get("version", "")
        except (TypeError, ValueError):
            rule_version = ""
        version_id = (adjustment.original_version_id if adjustment.side == "old"
                      else adjustment.target_version_id)
        return {
            "kind": "version_change",
            "change_id": adjustment.change_id,
            "change_no": "VC-" + adjustment.change_id[:12].upper(),
            "adjustment_id": adjustment.id,
            "side": adjustment.side,
            "version_id": version_id,
            "rule_version": rule_version,
            "effective_at": adjustment.effective_at,
            "posted_at": adjustment.posted_at,
        }

    def include_into_batch(db, batch, body, actor):
        Adjustment = ns.get("VersionChangeAdjustment")
        if Adjustment is None or body.cycle != "monthly":
            return {"amount": Decimal("0.00"), "profit": Decimal("0.00"), "count": 0}
        stmt = select(Adjustment).where(Adjustment.status == "posted", Adjustment.posted_at.is_not(None))
        if body.period_start:
            stmt = stmt.where(Adjustment.posted_at >= body.period_start)
        if body.period_end:
            stmt = stmt.where(Adjustment.posted_at < body.period_end)
        if body.order_ids:
            stmt = stmt.where(Adjustment.source_order_id.in_(body.order_ids))
        rows = db.scalars(stmt.order_by(Adjustment.posted_at, Adjustment.change_id,
                                        Adjustment.source_order_id, Adjustment.side).with_for_update()).all()
        amount_total = Decimal("0.00")
        profit_total = Decimal("0.00")
        count = 0
        names = {"platform": "平台运营方", "provider": "数据/服务提供方",
                 "service": "数据服务方", "expert": "专家", "channel": "渠道"}
        for row in rows:
            link = db.scalar(select(VersionChangeSettlementLink).where(
                VersionChangeSettlementLink.adjustment_id == row.id))
            settlement = db.get(ns["Settlement"], link.settlement_id) if link else None
            reused = settlement is not None
            previous_status = settlement.status if settlement else "none"
            existing_lines = db.scalars(select(ns["SettlementLine"]).where(
                ns["SettlementLine"].settlement_id == settlement.id,
                ns["SettlementLine"].status != "superseded")).all() if settlement else []
            finance_baseline = ns.get('settlement_finance', {}).get('Baseline')
            if settlement and finance_baseline is not None and db.get(finance_baseline, settlement.id):
                fail('version_change_final_baseline_requires_explicit_adjustment')
            for line in existing_lines:
                other_batch = db.get(ns["SettlementBatch"], line.batch_id)
                if other_batch and other_batch.id != batch.id and other_batch.status in ACTIVE_BATCH:
                    fail("版本变更调整已属于其他有效清算批次")
                if other_batch and other_batch.status in FINAL_BATCH:
                    fail("已确认或付款的版本变更清算不可重写")

            try:
                snapshot = json.loads(row.rule_json)
                rates = snapshot["rates"]
                values = {key: Decimal(str(rates[key])) for key in (
                    "platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")}
                revenue, cost, profit = (Decimal(str(value)) for value in (row.revenue, row.cost, row.profit))
                if (any(not value.is_finite() or value.quantize(CENT) != value for value in (revenue, cost, profit))
                        or any(not rate.is_finite() or rate < 0 or rate > 100 for rate in values.values())
                        or sum(values.values()) > 100 or revenue - cost != profit):
                    raise ValueError("profit mismatch")
                shares = {}
                for participant, rate_key in (("platform", "platform_rate"), ("provider", "provider_rate"),
                                               ("service", "service_rate"), ("expert", "expert_rate"),
                                               ("channel", "channel_rate")):
                    shares[participant] = (profit * values[rate_key] / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
                shares["provider"] += profit - sum(shares.values())
            except (KeyError, TypeError, ValueError, InvalidOperation):
                fail("版本变更冻结清算规则或金额无效")

            if settlement is None:
                settlement = ns["Settlement"](
                    settlement_no="SET-VC-" + secrets.token_hex(6).upper(), order_id=row.source_order_id,
                    gross_amount=revenue, refund_amount=Decimal("0.00"), net_amount=revenue,
                    cost_amount=cost, profit_amount=profit, refund_recovery=Decimal("0.00"),
                    platform_fee=shares["platform"], provider_share=shares["provider"],
                    service_share=shares["service"], expert_fee=shares["expert"],
                    channel_fee=shares["channel"], tax_amount=Decimal("0.00"), status="pending")
                db.add(settlement)
                db.flush()
                db.add(VersionChangeSettlementLink(adjustment_id=row.id, settlement_id=settlement.id))
            else:
                settlement.gross_amount, settlement.net_amount = revenue, revenue
                settlement.cost_amount, settlement.profit_amount = cost, profit
                settlement.platform_fee, settlement.provider_share = shares["platform"], shares["provider"]
                settlement.service_share, settlement.expert_fee = shares["service"], shares["expert"]
                settlement.channel_fee, settlement.status = shares["channel"], "pending"

            for line in db.scalars(select(ns["SettlementLine"]).where(
                    ns["SettlementLine"].settlement_id == settlement.id,
                    ns["SettlementLine"].status != "superseded")).all():
                line.status = "superseded"
            order = db.get(ns["Order"], row.source_order_id)
            db.add_all(ns["SettlementLine"](batch_id=batch.id, settlement_id=settlement.id,
                participant_type=participant, participant_id=(order.provider_enterprise_id or "") if participant == "provider" and order else "",
                participant_name=names[participant], amount=shares[participant])
                for participant in names)
            ns["audit"](db, actor, "settle_version_change_adjustment", "settlement",
                settlement.settlement_no, "版本变更调整纳入月度清算", category="settlement_adjustment",
                business_domain="settlement", order_id=row.source_order_id, batch_no=batch.batch_no,
                rule_version=str(snapshot.get("version", "")),
                before={"status": previous_status},
                after={"status": settlement.status, "created_or_reused": "reused" if reused else "created",
                    "change_id": row.change_id, "adjustment_id": row.id, "side": row.side,
                    "rule_id": snapshot.get("id", ""), "rule_version": snapshot.get("version", ""),
                    "rule_mode": snapshot.get("mode", ""), "gross_amount": str(revenue),
                    "cost_amount": str(cost), "net_amount": str(revenue), "profit_amount": str(profit),
                    "platform_fee": str(shares["platform"]), "provider_share": str(shares["provider"]),
                    "service_share": str(shares["service"]), "expert_fee": str(shares["expert"]),
                    "channel_fee": str(shares["channel"])})
            amount_total += revenue
            profit_total += profit
            count += 1
        db.flush()
        return {"amount": amount_total, "profit": profit_total, "count": count}

    hooks = {"Model": VersionChangeSettlementLink, "include_into_batch": include_into_batch,
             "describe": describe,
             'SegmentBaselineLink': SegmentBaselineLink, 'financial_context': financial_context,
             'bind_baseline': bind_baseline, 'refund_basis': refund_basis, 'prepare_lock': prepare_lock,
             'paired_target': paired_target, 'refund_segment_basis': refund_segment_basis,
             "is_funding_order": is_funding_order, "is_change_refund": is_change_refund}
    ns["VersionChangeSettlementLink"] = VersionChangeSettlementLink
    ns["version_change_settlement"] = hooks
    return hooks
