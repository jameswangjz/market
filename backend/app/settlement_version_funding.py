"""Immutable supplementary-funding reversal for VC old legs; no buyer cash I/O."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, event, select

from .settlement_finance import SHARES, canonical, money
from .settlement_funded_refund import allocation


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cumulative_target(source, snapshot, raw_amount, amount):
    gross = (source.revenue * amount / raw_amount).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if source.cost > raw_amount:
        parts = allocation([source.cost] + [snapshot['shares'][k] for k in SHARES], gross)
        cost, shares = parts[0], dict(zip(SHARES, parts[1:]))
    else:
        cost = (source.cost * amount / raw_amount).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        shares = dict(zip(SHARES, allocation([snapshot['shares'][k] for k in SHARES], gross - cost)))
    return gross, cost, gross - cost, shares


def install(ns, finance):
    if 'settlement_version_funding' in ns:
        return ns['settlement_version_funding']
    Base, Baseline, Position = ns['Base'], finance['Baseline'], finance['Position']

    class Projection(Base):
        __tablename__ = 'settlement_version_funding_projections'
        settlement_id = Column(ForeignKey('settlements.id'), primary_key=True)
        source_settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        change_id = Column(String(36), nullable=False)
        window_id = Column(String(120), nullable=False)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        __table_args__ = (UniqueConstraint('change_id', 'window_id'),)

    class Credit(Base):
        __tablename__ = 'settlement_version_funding_credits'
        position_id = Column(ForeignKey(Position.id), primary_key=True)
        settlement_id = Column(ForeignKey(Projection.settlement_id), nullable=False, unique=True)
        source_settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        projection_hash = Column(String(64), nullable=False)
        amount = Column(Numeric(14, 2), nullable=False)
        __table_args__ = (CheckConstraint('amount > 0'),)

    def fail(reason):
        raise HTTPException(409, reason)

    def parsed(raw, expected):
        value = json.loads(raw)
        if digest(value) != expected:
            fail('version_funding_evidence_hash_mismatch')
        return value

    def immutable(*_):
        raise ValueError('version funding projection is immutable')

    for model in (Projection, Credit):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def source_proof(db, source, raw_amount):
        raw_amount = money(raw_amount, True)
        hook = ns.get('settlement_funded_refund', {}).get('source_evidence')
        if not callable(hook):
            fail('version_funding_source_proof_contract_required')
        snapshot, funding = hook(db, source, raw_amount)
        if (source.revenue <= raw_amount or funding.get('source') not in ('external_receipt', 'platform_absorption')
                or money(funding['amount']) != source.revenue - raw_amount
                or not funding.get('proof_hash')):
            fail('version_funding_verified_source_required')
        if funding['source'] == 'external_receipt':
            if not funding.get('manual_evidence_hash') or funding.get('bearer', {}).get('participant_type') != 'enterprise':
                fail('version_funding_verified_external_source_required')
        elif (not funding.get('absorption_hash') or not funding.get('transaction_id')
              or (funding.get('bearer', {}).get('participant_type'), funding.get('bearer', {}).get('participant_id'))
                 != ('platform', 'platform')):
            fail('version_funding_verified_platform_source_required')
        return snapshot, funding

    def project(db, source, raw_amount, old_amount, window, change, receipt, negative_id):
        raw_amount, old_amount = money(raw_amount, True), money(old_amount, True)
        snapshot, funding = source_proof(db, source, raw_amount)
        external = funding['source'] == 'external_receipt'
        identity = dict(version='external_unused_return_v1' if external else 'platform_unused_restoration_v1', settlement_id=negative_id,
            source_settlement_id=source.settlement_id, source_hash=source.snapshot_hash,
            order_id=source.order_id, provider_enterprise_id=source.provider_enterprise_id, currency=source.currency,
            raw_source_amount=str(raw_amount), raw_amount=str(old_amount), change_id=change.id,
            window_id=window.id, window_hash=window.payload_hash, receipt_id=receipt.id, receipt_hash=receipt.receipt_hash,
            funding=funding, policy=('return_unused_to_original_payer_not_target' if external
                                    else 'restore_unused_platform_absorption_not_target'))
        old = db.get(Projection, negative_id)
        if old:
            payload = parsed(old.payload_json, old.payload_hash)
            if any(payload.get(k) != v for k, v in identity.items()):
                fail('frozen_version_funding_projection_changed')
            return dict(payload, projection_hash=old.payload_hash)
        prior, evidence, known = Decimal(0), [], set()
        for row in db.scalars(select(Projection).where(Projection.source_settlement_id == source.settlement_id)):
            item = parsed(row.payload_json, row.payload_hash)
            if item['source_hash'] != source.snapshot_hash or item['raw_source_amount'] != str(raw_amount):
                fail('version_funding_prior_projection_scope_mismatch')
            prior += money(item['raw_amount'])
            known.add(row.settlement_id)
            evidence.append(dict(kind='version_change', id=row.settlement_id, hash=row.payload_hash))
        funded = ns['settlement_funded_refund']
        Slice, Plan, Posting = funded['Slice'], funded['Plan'], funded['Posting']
        for row in db.scalars(select(Slice).where(Slice.source_settlement_id == source.settlement_id)):
            item = parsed(row.payload_json, row.payload_hash)
            parent = db.get(Plan, row.refund_id)
            content = parsed(parent.payload_json, parent.payload_hash) if parent else {}
            if (row.source_hash != source.snapshot_hash or item not in content.get('references', [])
                    or item['baseline_hash'] != source.snapshot_hash):
                fail('version_funding_prior_refund_scope_mismatch')
            prior += money(item['buyer_cash_amount'])
            posting = db.get(Posting, row.refund_id)
            if posting:
                known.add(posting.settlement_id)
            evidence.append(dict(kind='refund', id=row.refund_id, hash=row.payload_hash))
        # Unknown legacy monetary reversals cannot be converted back into raw
        # buyer consumption by dividing rounded financial totals.
        for b in db.scalars(select(Baseline).where(Baseline.reference_settlement_id == source.settlement_id,
                                                   Baseline.revenue < 0)):
            if b.settlement_id not in known:
                fail('version_funding_legacy_raw_consumption_requires_evidence')
        for row in db.scalars(select(finance['RefundSource']).where(
                finance['RefundSource'].source_settlement_id == source.settlement_id)):
            if row.settlement_id not in known:
                fail('version_funding_multi_raw_consumption_requires_evidence')
        if prior + old_amount > raw_amount:
            fail('version_funding_raw_source_budget_exceeded')

        before = cumulative_target(source, snapshot, raw_amount, prior)
        after = cumulative_target(source, snapshot, raw_amount, prior + old_amount)
        values = dict(gross_amount=str(before[0] - after[0]), cost_amount=str(before[1] - after[1]),
            profit_amount=str(before[2] - after[2]), shares={k: str(before[3][k] - after[3][k]) for k in SHARES})
        returned = after[0] - before[0] - old_amount
        if (returned < 0 or returned > money(funding['amount'])
                or any(money(v) > 0 for v in values['shares'].values())):
            fail('version_funding_return_budget_mismatch')
        payload = dict(identity, prior_raw_amount=str(prior), cumulative_raw_amount=str(prior + old_amount),
            prior_evidence=sorted(evidence, key=canonical), values=values, returned_unused_supplement=str(returned))
        return dict(payload, projection_hash=digest(payload))

    def credit_entries(context):
        p = context['version_funding_projection']
        amount = money(p['returned_unused_supplement'])
        external = p['funding']['source'] == 'external_receipt'
        bearer = p['funding']['bearer'] if external else dict(
            participant_type='platform', participant_id='platform', participant_name='Platform')
        return [dict(kind='funding_return' if external else 'funding_absorption_return',
                     share_kind='', amount=str(amount), blocked_reason='',
                     **{k: bearer[k] for k in ('participant_type', 'participant_id', 'participant_name')})] if amount else []

    def post(db, settlement, context, actor):
        p = context['version_funding_projection']
        payload = {k: v for k, v in p.items() if k != 'projection_hash'}
        if digest(payload) != p['projection_hash'] or p['settlement_id'] != settlement.id:
            fail('version_funding_projection_hash_mismatch')
        old = db.get(Projection, settlement.id)
        if old:
            if old.payload_hash != p['projection_hash'] or parsed(old.payload_json, old.payload_hash) != payload:
                fail('version_funding_projection_changed')
        else:
            db.add(Projection(settlement_id=settlement.id, source_settlement_id=p['source_settlement_id'],
                change_id=p['change_id'], window_id=p['window_id'], payload_json=canonical(payload),
                payload_hash=p['projection_hash'], actor=str(actor)))
            db.flush()
        positions = db.scalars(select(Position).where(Position.settlement_id == settlement.id)).all()
        expected = credit_entries(context)
        returns = [position for position in positions if position.kind in ('funding_return', 'funding_absorption_return')]
        if len(returns) != len(expected):
            fail('version_funding_credit_position_mismatch')
        for position, entry in zip(returns, expected):
            direction = 'payable' if entry['kind'] == 'funding_return' else 'internal'
            if (position.direction != direction or money(position.amount) != money(entry['amount'])
                    or any(getattr(position, k) != v for k, v in entry.items() if k != 'amount')):
                fail('version_funding_credit_position_mismatch')
            old_credit = db.get(Credit, position.id)
            if not old_credit:
                db.add(Credit(position_id=position.id, settlement_id=settlement.id,
                    source_settlement_id=p['source_settlement_id'], projection_hash=p['projection_hash'], amount=position.amount))
            elif (old_credit.projection_hash != p['projection_hash'] or old_credit.amount != position.amount
                  or old_credit.settlement_id != settlement.id):
                fail('version_funding_credit_mapping_changed')
        Mapping = finance['RefundPositionSource']
        for position in positions:
            mapping = db.get(Mapping, position.id)
            if mapping is None:
                db.add(Mapping(position_id=position.id, settlement_id=settlement.id,
                    segment_id=p['window_id'], source_settlement_id=p['source_settlement_id']))
            elif (mapping.settlement_id != settlement.id or mapping.segment_id != p['window_id']
                  or mapping.source_settlement_id != p['source_settlement_id']):
                fail('version_funding_position_source_changed')
        db.flush()

    def describe(db, settlement_id):
        row = db.get(Projection, settlement_id)
        if row is None:
            return None
        payload = parsed(row.payload_json, row.payload_hash)
        baseline = db.get(Baseline, settlement_id)
        snapshot = parsed(baseline.snapshot_json, baseline.snapshot_hash) if baseline else {}
        context = snapshot.get('segment_context') or {}
        expected = dict(payload, projection_hash=row.payload_hash)
        source = db.get(Baseline, row.source_settlement_id)
        proof = db.get(finance['FundingProof'], row.source_settlement_id)
        if source:
            parsed(source.snapshot_json, source.snapshot_hash)
        if proof:
            parsed(proof.evidence_json, proof.evidence_hash)
        if (not baseline or not source or source.snapshot_hash != payload['source_hash']
                or source.order_id != payload['order_id'] or source.currency != payload['currency']
                or source.provider_enterprise_id != payload['provider_enterprise_id']
                or row.source_settlement_id != payload['source_settlement_id']
                or row.change_id != payload['change_id'] or row.window_id != payload['window_id']
                or baseline.reference_settlement_id != source.settlement_id
                or baseline.order_id != payload['order_id'] or baseline.currency != payload['currency']
                or baseline.provider_enterprise_id != payload['provider_enterprise_id']
                or context.get('side') != 'old' or context.get('version_funding_projection') != expected
                or context.get('returned_unused_supplement') != payload['returned_unused_supplement']
                or snapshot.get('shares') != payload['values']['shares']
                or context.get('context_hash') != snapshot.get('segment_context_hash')
                or digest({k: v for k, v in context.items() if k not in ('status', 'context_hash')}) != context.get('context_hash')
                or proof is None or proof.evidence_hash != payload['funding']['proof_hash']
                or proof.request_id != payload['funding']['request_id']
                or any(money(getattr(baseline, field)) != money(payload['values'][key]) for field, key in (
                    ('revenue', 'gross_amount'), ('cost', 'cost_amount'), ('profit', 'profit_amount')))):
            fail('version_funding_baseline_context_mismatch')
        credits = db.scalars(select(Credit).where(Credit.settlement_id == settlement_id)).all()
        positions = [db.get(Position, c.position_id) for c in credits]
        external = payload['funding']['source'] == 'external_receipt'
        bearer = payload['funding']['bearer'] if external else dict(
            participant_type='platform', participant_id='platform', participant_name='Platform')
        credit_kind = 'funding_return' if external else 'funding_absorption_return'
        direction = 'payable' if external else 'internal'
        mappings = [db.get(finance['RefundPositionSource'], c.position_id) for c in credits]
        all_returns = db.scalars(select(Position).where(Position.settlement_id == settlement_id,
            Position.kind.in_(('funding_return', 'funding_absorption_return')))).all()
        if (sum((c.amount for c in credits), Decimal(0)) != money(payload['returned_unused_supplement'])
                or len(all_returns) != len(positions)
                or any(p is None or c.projection_hash != row.payload_hash or p.amount != c.amount
                       or c.source_settlement_id != source.settlement_id or p.settlement_id != settlement_id
                       or p.order_id != payload['order_id'] or p.currency != payload['currency'] or p.share_kind != ''
                       or p.blocked_reason != ''
                       or p.direction != direction or p.kind != credit_kind
                       or any(getattr(p, k) != bearer[k] for k in ('participant_type', 'participant_id', 'participant_name'))
                       or m is None or m.source_settlement_id != source.settlement_id
                       or m.settlement_id != settlement_id or m.segment_id != payload['window_id']
                       for c, p, m in zip(credits, positions, mappings))):
            fail('version_funding_credit_evidence_mismatch')
        recorded = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at.astimezone(timezone.utc)
        return dict(payload, projection_hash=row.payload_hash, recorded_at=recorded.isoformat(),
            credit_kind='external_payer_return' if external else 'internal_absorption_restoration',
            external_return_amount=payload['returned_unused_supplement'] if external else '0.00',
            internal_absorption_return='0.00' if external else payload['returned_unused_supplement'],
            credits=[finance['position_dict'](db, p) for p in positions])

    hooks = dict(Projection=Projection, Credit=Credit, source_proof=source_proof,
                 project=project, credit_entries=credit_entries, post=post, describe=describe)
    ns['settlement_version_funding'] = hooks
    return hooks
