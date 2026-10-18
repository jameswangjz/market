"""Additive settlement subledger. Install before create_all; caller owns commits.

Subject mappings: economic_snapshot_json.settlement_rule.participants maps each
share field (or platform/provider/service/expert/channel) to a list of explicit
{participant_type, participant_id, participant_name, amount} entries. Single
entries may omit amount. Negative capture requires reference_settlement_id.
No bank execution, legacy payment inference, or scheduled generation occurs here.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import (Column, String, Text, Numeric, DateTime, ForeignKey,
                        UniqueConstraint, CheckConstraint, event, select, func, or_)
from sqlalchemy.exc import IntegrityError

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
ROLES = ('platform', 'provider', 'service', 'expert', 'channel')
OPERATORS = {'super_admin', 'platform_operator', 'finance_settlement'}
CENT = Decimal('.01')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def money(value, positive=False):
    try:
        n = Decimal(str(value))
        if (not n.is_finite() or abs(n) > Decimal('999999999999.99')
                or n != n.quantize(CENT) or (positive and n <= 0)):
            raise ValueError()
        return n.quantize(CENT)
    except (ValueError, InvalidOperation, TypeError):
        raise HTTPException(422, 'invalid_money_cents') from None


def install(ns):
    if 'settlement_finance' in ns:
        return ns['settlement_finance']
    Base = ns['Base']

    class Baseline(Base):
        __tablename__ = 'settlement_finance_baselines'
        settlement_id = Column(ForeignKey('settlements.id'), primary_key=True)
        order_id = Column(String(36), nullable=False, index=True)
        provider_enterprise_id = Column(String(36), nullable=False, index=True)
        reference_settlement_id = Column(String(36), nullable=False, default='')
        currency = Column(String(12), nullable=False)
        revenue = Column(Numeric(14, 2), nullable=False)
        cost = Column(Numeric(14, 2), nullable=False)
        profit = Column(Numeric(14, 2), nullable=False)
        snapshot_json = Column(Text, nullable=False)
        snapshot_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Position(Base):
        __tablename__ = 'settlement_finance_positions'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        order_id = Column(String(36), nullable=False, index=True)
        kind = Column(String(12), nullable=False)
        share_kind = Column(String(30), nullable=False, default='')
        participant_type = Column(String(60), nullable=False)
        participant_id = Column(String(120), nullable=False)
        participant_name = Column(String(180), nullable=False)
        currency = Column(String(12), nullable=False)
        direction = Column(String(12), nullable=False)
        amount = Column(Numeric(14, 2), nullable=False)
        blocked_reason = Column(String(120), nullable=False, default='')
        __table_args__ = (CheckConstraint('amount > 0'),)

    class Transaction(Base):
        __tablename__ = 'settlement_finance_transactions'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        kind = Column(String(12), nullable=False)
        mode = Column(String(12), nullable=False)
        reference = Column(String(240), nullable=False)
        note = Column(Text, nullable=False)
        creator_id = Column(String(120), nullable=False)
        payload_json = Column(Text, nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Confirmation(Base):
        __tablename__ = 'settlement_finance_confirmations'
        transaction_id = Column(ForeignKey(Transaction.id), primary_key=True)
        reviewer_id = Column(String(120), nullable=False)
        note = Column(Text, nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Allocation(Base):
        __tablename__ = 'settlement_finance_allocations'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        transaction_id = Column(ForeignKey(Confirmation.transaction_id), nullable=False, index=True)
        position_id = Column(ForeignKey(Position.id), nullable=False, index=True)
        amount = Column(Numeric(14, 2), nullable=False)
        __table_args__ = (UniqueConstraint('transaction_id', 'position_id'), CheckConstraint('amount > 0'))

    class Idempotency(Base):
        __tablename__ = 'settlement_finance_requests'
        actor_id = Column(String(120), primary_key=True)
        request_id = Column(String(180), primary_key=True)
        payload_hash = Column(String(64), nullable=False)
        transaction_id = Column(ForeignKey(Transaction.id), nullable=False)

    def immutable(*args):
        raise ValueError('settlement finance evidence is immutable')

    for model in (Baseline, Position, Transaction, Confirmation, Allocation, Idempotency):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def audit(db, actor, action, target, **values):
        ns['audit'](db, actor, action, 'settlement_finance', target,
                    category='settlement', business_domain='settlement', after=values)

    def operator(user):
        if (not getattr(user, 'is_active', False) or getattr(user, 'activation_status', 'active') != 'active'
                or user.platform_role not in OPERATORS):
            raise HTTPException(403, 'settlement_finance_operator_required')

    def applied(db, position_id):
        return money(db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(
            Allocation.position_id == position_id)) or 0)

    def balance(db, position):
        return money(position.amount) - applied(db, position.id)

    def receipt_basis(db, order_id):
        Payment = ns.get('Payment')
        if Payment is None:
            return None, []
        payments = db.scalars(select(Payment).where(Payment.order_id == order_id,
            Payment.status == 'paid').order_by(Payment.id).with_for_update()
            .execution_options(populate_existing=True)).all()
        records = [dict(payment_id=p.id, payment_no=getattr(p, 'payment_no', ''),
                        status=p.status, amount=str(money(p.amount))) for p in payments]
        if any(money(p.amount) < 0 for p in payments):
            return None, records
        return sum((money(p.amount) for p in payments), Decimal(0)), records

    def locked_positions(db, ids):
        rows = db.scalars(select(Position).where(Position.id.in_(sorted(ids))).order_by(
            Position.id).with_for_update().execution_options(populate_existing=True)).all()
        if len(rows) != len(set(ids)):
            raise HTTPException(404, 'position_not_found')
        return {r.id: r for r in rows}

    def payable_batches(db, ids):
        Line, Batch = ns['SettlementLine'], ns['SettlementBatch']
        positive_ids = set(db.scalars(select(Position.settlement_id).where(
            Position.id.in_(ids), Position.direction == 'payable')))
        if not positive_ids:
            return
        def links():
            return db.execute(select(Line.settlement_id, Line.batch_id).where(
                Line.settlement_id.in_(positive_ids), Line.status != 'superseded')).all()
        original_links = set(links())
        batch_ids = {b for _, b in original_links}
        batches = db.scalars(select(Batch).where(Batch.id.in_(batch_ids)).order_by(Batch.id)
            .with_for_update().execution_options(populate_existing=True)).all()
        if set(links()) != original_links:
            raise HTTPException(409, 'position_batch_changed')
        states = {b.id: b.status for b in batches}
        for settlement_id in positive_ids:
            owners = {b for s, b in original_links if s == settlement_id}
            if len(owners) != 1 or any(states.get(b) not in (
                    'confirmed', 'payment_processing', 'partial_paid', 'paid') for b in owners):
                raise HTTPException(409, 'confirmed_payment_batch_required')

    def validate_allocations(db, payload):
        allocations = payload['allocations']
        ids = [a['position_id'] for a in allocations]
        counter = payload.get('counter_position_id')
        if not ids or len(set(ids)) != len(ids) or counter in ids:
            raise HTTPException(422, 'invalid_allocations')
        payable_batches(db, ids + ([counter] if counter else []))
        settlement_ids = select(Position.settlement_id).where(Position.id.in_(ids + ([counter] if counter else [])))
        Settlement = ns['Settlement']
        settlements = db.scalars(select(Settlement).where(Settlement.id.in_(settlement_ids))
            .order_by(Settlement.id).with_for_update().execution_options(populate_existing=True)).all()
        if any(s.status == 'superseded' for s in settlements):
            raise HTTPException(409, 'superseded_settlement_position')
        rows = locked_positions(db, ids + ([counter] if counter else []))
        identity = None
        for a in allocations:
            p = rows[a['position_id']]
            key = (p.participant_type, p.participant_id, p.currency)
            if p.blocked_reason or not p.participant_id or p.direction == 'internal':
                raise HTTPException(409, 'position_cash_blocked')
            if identity is not None and key != identity:
                raise HTTPException(409, 'subject_currency_mismatch')
            identity = key
            required = 'payable' if payload['kind'] == 'payout' else 'receivable'
            if p.direction != required or money(a['amount'], True) > balance(db, p):
                raise HTTPException(409, 'position_direction_or_balance')
        if payload['kind'] == 'offset':
            if not counter:
                raise HTTPException(422, 'offset_counter_required')
            p = rows[counter]
            if (p.direction != 'payable' or p.blocked_reason or
                    (p.participant_type, p.participant_id, p.currency) != identity or
                    sum(money(a['amount']) for a in allocations) > balance(db, p)):
                raise HTTPException(409, 'offset_counter_mismatch_or_balance')
        elif counter:
            raise HTTPException(422, 'unexpected_counter_position')
        return rows

    def replay(db, actor, key, payload):
        if not isinstance(key, str) or not key.strip() or len(key) > 180:
            raise HTTPException(422, 'request_id_required')
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        old = db.get(Idempotency, (actor, key))
        if old and old.payload_hash != digest:
            raise HTTPException(409, 'idempotency_payload_mismatch')
        return old, digest

    def serialize_transaction(db, tx):
        confirmation = db.get(Confirmation, tx.id)
        return dict(id=tx.id, kind=tx.kind, mode=tx.mode, reference=tx.reference,
                    note=tx.note, creator_id=tx.creator_id, version=2 if confirmation else 1,
                    status='confirmed' if confirmation else 'draft',
                    reviewer_id=confirmation.reviewer_id if confirmation else None,
                    bank_receipt=False, allocations=json.loads(tx.payload_json)['allocations'],
                    counter_position_id=json.loads(tx.payload_json).get('counter_position_id'))

    def draft(db, user, body):
        operator(user)
        if not isinstance(body, dict):
            raise HTTPException(422, 'invalid_transaction_body')
        if not isinstance(body.get('note', ''), str) or len(body.get('note', '')) > 4000:
            raise HTTPException(422, 'invalid_note')
        raw = body.get('allocations')
        if not isinstance(raw, list) or not raw or len(raw) > 1000:
            raise HTTPException(422, 'invalid_allocations')
        for a in raw:
            if (not isinstance(a, dict) or not isinstance(a.get('position_id'), str)
                    or not a['position_id'].strip() or len(a['position_id']) > 36 or 'amount' not in a
                    or isinstance(a['amount'], (bool, dict, list, type(None)))):
                raise HTTPException(422, 'invalid_allocation_item')
        counter = body.get('counter_position_id')
        if counter is not None and (not isinstance(counter, str) or not counter.strip() or len(counter) > 36):
            raise HTTPException(422, 'invalid_counter_position_id')
        if body.get('kind') not in ('payout', 'recovery', 'offset') or body.get('mode') not in ('manual', 'simulated'):
            raise HTTPException(422, 'invalid_transaction_kind_or_mode')
        reference = body.get('reference')
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 240:
            raise HTTPException(422, 'reference_required')
        try:
            allocations = sorted([dict(position_id=a['position_id'], amount=str(money(a['amount'], True)))
                                  for a in body['allocations']], key=lambda a: a['position_id'])
        except (KeyError, TypeError):
            raise HTTPException(422, 'invalid_allocations') from None
        payload = dict(kind=body['kind'], mode=body['mode'], reference=reference.strip(),
                       note=body.get('note', ''), allocations=allocations,
                       counter_position_id=body.get('counter_position_id'))
        old, digest = replay(db, user.id, body.get('request_id'), dict(action='draft', **payload))
        if old:
            return serialize_transaction(db, db.get(Transaction, old.transaction_id))
        try:
            with db.begin_nested():
                validate_allocations(db, payload)
                tx = Transaction(kind=payload['kind'], mode=payload['mode'], reference=payload['reference'],
                    note=payload['note'], creator_id=user.id, payload_json=canonical(payload))
                db.add(tx)
                db.flush()
                db.add(Idempotency(actor_id=user.id, request_id=body['request_id'], payload_hash=digest, transaction_id=tx.id))
                audit(db, user.id, 'finance_draft', tx.id, mode=tx.mode, reference=tx.reference)
                db.flush()
        except IntegrityError:
            old, _ = replay(db, user.id, body['request_id'], dict(action='draft', **payload))
            if old:
                return serialize_transaction(db, db.get(Transaction, old.transaction_id))
            raise HTTPException(409, 'finance_request_conflict') from None
        return serialize_transaction(db, tx)

    def confirm(db, user, transaction_id, body):
        operator(user)
        if not isinstance(body, dict) or not isinstance(body.get('note', ''), str) or len(body.get('note', '')) > 4000:
            raise HTTPException(422, 'invalid_confirmation_body_or_note')
        payload = dict(action='confirm', transaction_id=transaction_id,
                       expected_version=body.get('expected_version'), note=body.get('note', ''))
        old, digest = replay(db, user.id, body.get('request_id'), payload)
        if old:
            return serialize_transaction(db, db.get(Transaction, old.transaction_id))
        try:
            with db.begin_nested():
                tx = db.scalar(select(Transaction).where(Transaction.id == transaction_id).with_for_update())
                if not tx:
                    raise HTTPException(404, 'transaction_not_found')
                # A concurrent identical confirmation may have committed while
                # this request waited for the transaction row lock.
                old, _ = replay(db, user.id, body['request_id'], payload)
                if old:
                    return serialize_transaction(db, db.get(Transaction, old.transaction_id))
                if tx.creator_id == user.id:
                    raise HTTPException(403, 'different_reviewer_required')
                if type(body.get('expected_version')) is not int or body['expected_version'] != 1 or db.get(Confirmation, tx.id):
                    raise HTTPException(409, 'transaction_version_conflict')
                evidence = json.loads(tx.payload_json)
                validate_allocations(db, evidence)
                db.add(Confirmation(transaction_id=tx.id, reviewer_id=user.id, note=body.get('note', '')))
                db.flush()
                for a in evidence['allocations']:
                    db.add(Allocation(transaction_id=tx.id, position_id=a['position_id'], amount=money(a['amount'])))
                if tx.kind == 'offset':
                    db.add(Allocation(transaction_id=tx.id, position_id=evidence['counter_position_id'],
                        amount=sum(money(a['amount']) for a in evidence['allocations'])))
                db.add(Idempotency(actor_id=user.id, request_id=body['request_id'], payload_hash=digest, transaction_id=tx.id))
                audit(db, user.id, 'finance_confirm', tx.id, kind=tx.kind, mode=tx.mode, reference=tx.reference)
                db.flush()
        except IntegrityError:
            old, _ = replay(db, user.id, body['request_id'], payload)
            if old:
                return serialize_transaction(db, db.get(Transaction, old.transaction_id))
            raise HTTPException(409, 'finance_confirmation_conflict') from None
        return serialize_transaction(db, tx)

    def capture(db, settlement, actor='system'):
        Settlement = ns['Settlement']
        settlement = db.scalar(select(Settlement).where(Settlement.id == settlement.id).with_for_update()
                               .execution_options(populate_existing=True))
        if not settlement or settlement.status not in ('locked', 'paid'):
            raise HTTPException(409, 'locked_settlement_required')
        old = db.get(Baseline, settlement.id)
        if old:
            return old
        order = db.get(ns['Order'], settlement.order_id)
        if not order or not order.provider_enterprise_id:
            raise HTTPException(409, 'provider_required')
        try:
            economic = json.loads(order.economic_snapshot_json or '{}')
        except (ValueError, TypeError):
            raise HTTPException(409, 'invalid_economic_snapshot') from None
        currency = economic.get('currency')
        if not isinstance(currency, str) or not currency.strip():
            raise HTTPException(409, 'explicit_currency_required')
        revenue, cost, profit = [money(getattr(settlement, k)) for k in ('gross_amount', 'cost_amount', 'profit_amount')]
        shares = {k: money(getattr(settlement, k)) for k in SHARES}
        if revenue - cost != profit or sum(shares.values()) != profit:
            raise HTTPException(409, 'settlement_conservation_failed')
        negative = revenue < 0
        if (negative and (cost > 0 or profit > 0 or any(n > 0 for n in shares.values()))) or (
                not negative and (cost < 0 or profit < 0 or any(n < 0 for n in shares.values()))):
            raise HTTPException(409, 'unsupported_mixed_sign_settlement')
        reference = getattr(settlement, 'reference_settlement_id', '') or ''
        original = db.scalar(select(Baseline).where(Baseline.settlement_id == reference).with_for_update()) if reference else None
        if negative and (not original or original.revenue <= 0 or original.order_id != order.id
                         or original.currency != currency or original.provider_enterprise_id != order.provider_enterprise_id
                         or db.get(Settlement, reference).status == 'superseded'):
            return dict(status='waiting', reason='exact_original_baseline_required', settlement_id=settlement.id)
        if negative:
            prior = db.scalars(select(Baseline).where(Baseline.reference_settlement_id == reference,
                Baseline.revenue < 0)).all()
            original_shares = json.loads(original.snapshot_json)['shares']
            if (abs(revenue) + sum(abs(b.revenue) for b in prior) > original.revenue
                    or abs(cost) + sum(abs(b.cost) for b in prior) > original.cost
                    or abs(profit) + sum(abs(b.profit) for b in prior) > original.profit
                    or any(abs(shares[k]) + sum(abs(money(json.loads(b.snapshot_json)['shares'][k]))
                        for b in prior) > money(original_shares[k]) for k in SHARES)):
                raise HTTPException(409, 'reversal_exceeds_original_baseline')
        provider = db.get(ns['Enterprise'], order.provider_enterprise_id)
        if not provider:
            raise HTTPException(409, 'provider_required')
        rule = economic.get('settlement_rule', {})
        mappings = rule.get('participants', {})
        entries = []
        if negative:
            original_snapshot = json.loads(original.snapshot_json)
            source_entries = original_snapshot['entries']
            prior_entries = [e for b in prior for e in json.loads(b.snapshot_json)['entries']]
            def entry_key(e):
                return (e['kind'], e['share_kind'], e['participant_type'], e['participant_id'], e['participant_name'])
            if len({entry_key(e) for e in source_entries}) != len(source_entries):
                return dict(status='unsupported', reason='duplicate_original_subject_entries', settlement_id=settlement.id)
            groups = sorted({(e['kind'], e['share_kind']) for e in source_entries})
            for kind, field in groups:
                group = [e for e in source_entries if (e['kind'], e['share_kind']) == (kind, field)]
                original_total = sum(money(e['amount']) for e in group)
                previous = {entry_key(e): sum((money(p['amount']) for p in prior_entries
                    if entry_key(p) == entry_key(e)), Decimal(0)) for e in group}
                delta = abs(cost if kind == 'cost' else shares[field])
                target = sum(previous.values()) + delta
                if target > original_total:
                    raise HTTPException(409, 'subject_reversal_exceeds_original_entry')
                raw = [money(e['amount']) * target / original_total for e in group]
                cumulative = [v.quantize(CENT, rounding=ROUND_DOWN) for v in raw]
                remainder = int((target - sum(cumulative)) / CENT)
                for i in sorted(range(len(group)), key=lambda i: (-(raw[i] - cumulative[i]), entry_key(group[i])))[:remainder]:
                    cumulative[i] += CENT
                for e, total in zip(group, cumulative):
                    value = total - previous[entry_key(e)]
                    if value < 0 or total > money(e['amount']):
                        return dict(status='unsupported', reason='subject_rounding_requires_explicit_reversal_plan',
                                    settlement_id=settlement.id)
                    if value:
                        entries.append(dict(e, amount=str(value)))
            if sum(money(e['amount']) for e in entries) != abs(revenue):
                raise HTTPException(409, 'original_subject_mapping_incomplete')
        else:
            if cost:
                entries.append(dict(kind='cost', share_kind='', participant_type='enterprise',
                    participant_id=provider.id, participant_name=provider.name, amount=str(cost), blocked_reason=''))
            for role, field in zip(ROLES, SHARES):
                amount = shares[field]
                if not amount:
                    continue
                configured = mappings.get(field, mappings.get(role))
                parties = ns.get('settlement_parties', {}).get('allocations')
                if callable(parties):
                    configured = parties(db, order, role, amount)
                    configured = [dict(s, participant_type=s.get('participant_type') or (
                        'enterprise' if role == 'provider' else role)) for s in configured]
                    if any(s.get('currency') and s['currency'] != currency for s in configured):
                        raise HTTPException(409, 'subject_currency_mismatch')
                if role in ('platform', 'provider'):
                    configured = [dict(participant_type='platform' if role == 'platform' else 'enterprise',
                        participant_id='platform' if role == 'platform' else provider.id,
                        participant_name='Platform' if role == 'platform' else provider.name, amount=str(amount))]
                if isinstance(configured, dict):
                    configured = [configured]
                if not configured:
                    configured = [dict(participant_type=role, participant_id='', participant_name=role,
                                       amount=str(amount))]
                mapped_total = Decimal(0)
                for subject in configured:
                    value = money(subject.get('amount', amount if len(configured) == 1 else None))
                    if value < 0:
                        raise HTTPException(409, 'negative_subject_amount')
                    mapped_total += value
                    if not value:
                        continue
                    identity = (all(subject.get(k) for k in ('participant_type', 'participant_id', 'participant_name'))
                                and not subject.get('unassigned') and subject.get('status') != 'unassigned'
                                and subject.get('participant_id') != 'unassigned')
                    entries.append(dict(kind='profit', share_kind=field,
                        participant_type=subject.get('participant_type') or role,
                        participant_id=subject.get('participant_id') or '',
                        participant_name=subject.get('participant_name') or role, amount=str(value),
                        blocked_reason='' if identity else 'missing_subject_mapping'))
                if mapped_total != amount:
                    raise HTTPException(409, 'subject_amount_conservation_failed')
        actual_receipts, receipt_records = receipt_basis(db, order.id) if not negative else (None, [])
        funding_block = not negative and (actual_receipts is None or revenue != actual_receipts)
        if funding_block:
            for entry in entries:
                if entry['share_kind'] != 'platform_fee':
                    entry['blocked_reason'] = 'unfunded_settlement_adjustment'
        # Legacy paid flags cannot establish either available cash or recovery debt.
        legacy_block = settlement.status == 'paid' or (negative and any(
            p.blocked_reason == 'legacy_paid_without_allocations' for p in db.scalars(
                select(Position).where(Position.settlement_id == reference))))
        snapshot = dict(settlement_no=settlement.settlement_no, order_no=order.order_no,
            provider_id=provider.id, provider_name=provider.name, currency=currency,
            revenue=str(revenue), cost=str(cost), profit=str(profit),
            shares={k: str(v) for k, v in shares.items()}, entries=entries,
            economic_snapshot=economic, reference_settlement_id=reference,
            actual_receipts=str(actual_receipts) if actual_receipts is not None else None,
            receipt_basis='payment_status_paid' if not negative else 'original_final_baseline',
            receipt_records=receipt_records, bank_receipt_verified=False,
            blocked_reason='unfunded_settlement_adjustment' if funding_block else '',
            product_version_id=getattr(order, 'product_version_id', ''))
        encoded = canonical(snapshot)
        with db.begin_nested():
            baseline = Baseline(settlement_id=settlement.id, order_id=order.id,
                provider_enterprise_id=provider.id, currency=currency, revenue=revenue, cost=cost,
                profit=profit, reference_settlement_id=reference, snapshot_json=encoded,
                snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(), actor=str(actor))
            db.add(baseline)
            db.flush()
            positions = []
            for e in entries:
                internal = e['share_kind'] == 'platform_fee'
                p = Position(settlement_id=settlement.id, order_id=order.id, currency=currency,
                    direction='internal' if internal else ('receivable' if negative else 'payable'),
                    **dict(e, amount=money(e['amount']), blocked_reason=(
                        'unfunded_settlement_adjustment' if funding_block and not internal else
                        'legacy_paid_without_allocations' if legacy_block and not internal else e['blocked_reason'])))
                db.add(p)
                positions.append(p)
            db.flush()
            if negative and not legacy_block:
                original_ids = list(db.scalars(select(Position.id).where(Position.settlement_id == reference)))
                offset_allowed = True
                try:
                    payable_batches(db, original_ids)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    offset_allowed = False
                originals = db.scalars(select(Position).where(Position.settlement_id == reference).order_by(
                    Position.id).with_for_update()).all()
                for p in positions:
                    if not offset_allowed or p.direction != 'receivable' or p.blocked_reason:
                        continue
                    for source in originals:
                        if (source.direction != 'payable' or source.blocked_reason or source.kind != p.kind
                            or source.share_kind != p.share_kind or source.participant_type != p.participant_type
                            or source.participant_id != p.participant_id or source.currency != p.currency):
                            continue
                        value = min(balance(db, p), balance(db, source))
                        if value <= 0:
                            continue
                        evidence = dict(allocations=[dict(position_id=p.id, amount=str(value))], counter_position_id=source.id)
                        tx = Transaction(kind='offset', mode='manual', reference='baseline:' + settlement.id,
                            note='Original unpaid entitlement reversal; no cash recovery', creator_id=str(actor),
                            payload_json=canonical(evidence))
                        db.add(tx)
                        db.flush()
                        db.add(Confirmation(transaction_id=tx.id, reviewer_id='baseline_policy', note='Exact original entitlement'))
                        db.flush()
                        db.add_all([Allocation(transaction_id=tx.id, position_id=p.id, amount=value),
                                    Allocation(transaction_id=tx.id, position_id=source.id, amount=value)])
                        db.flush()
            audit(db, str(actor), 'finance_baseline_capture', settlement.id, snapshot_hash=baseline.snapshot_hash)
            db.flush()
        return baseline

    def describe(db, settlement_id):
        b = db.get(Baseline, settlement_id)
        if b is None:
            change = ns.get('version_change_settlement', {}).get('describe')
            if callable(change) and change(db, settlement_id) is not None:
                return dict(settlement_id=settlement_id, status='waiting',
                    reason='exact_segment_final_baseline_required', positions=[], funds_settled=False)
            return None
        rows = db.scalars(select(Position).where(Position.settlement_id == settlement_id)).all()
        return dict(settlement_id=settlement_id, snapshot_hash=b.snapshot_hash,
            currency=b.currency, revenue=str(b.revenue), cost=str(b.cost), profit=str(b.profit),
            actual_receipts=json.loads(b.snapshot_json).get('actual_receipts'),
            blocked_reason=json.loads(b.snapshot_json).get('blocked_reason', ''),
            shares=json.loads(b.snapshot_json)['shares'], reference_settlement_id=b.reference_settlement_id,
            positions=[position_dict(db, p, b) for p in rows],
            funds_settled=all(p.direction == 'internal' or balance(db, p) == 0 for p in rows))

    def batch_status(db, batch_id):
        Line, Settlement = ns['SettlementLine'], ns['Settlement']
        ids = set(db.scalars(select(Line.settlement_id).join(Settlement, Settlement.id == Line.settlement_id).where(
            Line.batch_id == batch_id, Line.status != 'superseded', Settlement.status != 'superseded')))
        baselines = db.scalars(select(Baseline).where(Baseline.settlement_id.in_(ids))).all()
        rows = db.scalars(select(Position).where(Position.settlement_id.in_(ids))).all()
        currencies = {b.currency for b in baselines}
        payable = sum((balance(db, p) for p in rows if p.direction == 'payable'), Decimal(0))
        receivable = sum((balance(db, p) for p in rows if p.direction == 'receivable'), Decimal(0))
        coverage = bool(ids) and {b.settlement_id for b in baselines} == ids
        blocked = any(p.blocked_reason for p in rows)
        # Scalar balances cannot represent several currencies without conversion.
        if len(currencies) > 1:
            raise HTTPException(409, 'batch_multiple_currencies_require_separate_status')
        complete = coverage and not blocked
        return dict(positive_payment_complete=coverage and payable == 0 and not any(
                p.blocked_reason for p in rows if p.direction == 'payable'),
            funds_settled=complete and payable == 0 and receivable == 0,
            payable_balance=str(payable.quantize(CENT)), receivable_balance=str(receivable.quantize(CENT)),
            has_positions=bool(rows))

    def position_dict(db, p, baseline=None):
        b = baseline or db.get(Baseline, p.settlement_id)
        snapshot = json.loads(b.snapshot_json)
        used = applied(db, p.id)
        remaining = money(p.amount) - used
        return dict(id=p.id, settlement_id=p.settlement_id, order_id=p.order_id,
            settlement_no=snapshot['settlement_no'], order_no=snapshot['order_no'],
            participant_type=p.participant_type, participant_id=p.participant_id,
            participant_name=p.participant_name, kind=p.kind, share_kind=p.share_kind, direction=p.direction,
            amount=str(p.amount), applied_amount=str(used), balance=str(remaining), currency=p.currency,
            blocked_reason=p.blocked_reason, status=('blocked' if p.blocked_reason else
                'internal' if p.direction == 'internal' else 'settled' if not remaining else 'partial' if used else 'open'))

    def scope(db, user, enterprise_id='', business_view=''):
        if not getattr(user, 'is_active', False) or getattr(user, 'activation_status', 'active') != 'active':
            raise HTTPException(403, 'active_user_required')
        if not enterprise_id and business_view != 'provider' and user.platform_role in OPERATORS | {'security_compliance'}:
            return None
        if not enterprise_id or business_view not in ('', 'provider') or user.verified_status != 'verified':
            raise HTTPException(403, 'provider_scope_required')
        M, E = ns['Membership'], ns['Enterprise']
        membership = db.scalar(select(M).where(M.user_id == user.id, M.enterprise_id == enterprise_id,
            M.status == 'active', M.role.in_(['super_admin', 'enterprise_admin'])))
        enterprise = db.get(E, enterprise_id)
        if (not membership or not enterprise or enterprise.verification_status != 'verified'
                or 'provider' not in (membership.business_roles or '').split(',')):
            raise HTTPException(403, 'verified_provider_admin_required')
        return enterprise_id

    def positions_query(db, user, enterprise_id='', business_view='', q='', direction=''):
        tenant = scope(db, user, enterprise_id, business_view)
        Settlement = ns['Settlement']
        query = select(Position).join(Baseline, Baseline.settlement_id == Position.settlement_id).join(
            Settlement, Settlement.id == Position.settlement_id).where(Settlement.status != 'superseded')
        if tenant:
            query = query.where(Baseline.provider_enterprise_id == tenant)
        if direction:
            if direction not in ('payable', 'receivable', 'internal'):
                raise HTTPException(422, 'invalid_direction')
            query = query.where(Position.direction == direction)
        if q:
            query = query.where(or_(Position.participant_name.contains(q, autoescape=True),
                Baseline.snapshot_json.contains(q, autoescape=True)))
        return query

    def list_positions(db, user, page=1, page_size=50, **filters):
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(422, 'invalid_pagination')
        query = positions_query(db, user, **filters)
        rows = db.scalars(query.order_by(Position.id)).all()
        summary, totals = {}, {}
        for p in rows:
            group = summary.setdefault(p.currency, {k: Decimal(0) for k in ('payable', 'receivable', 'internal')})
            group[p.direction] += balance(db, p)
            total = totals.setdefault(p.currency, {k: Decimal(0) for k in ('amount', 'applied_amount', 'balance')})
            total['amount'] += p.amount
            total['applied_amount'] += applied(db, p.id)
            total['balance'] += balance(db, p)
        return dict(items=[position_dict(db, p) for p in rows[(page-1)*page_size:page*page_size]],
            total=len(rows), page=page, page_size=page_size,
            summary={c: {k: str(v) for k, v in s.items()} for c, s in summary.items()},
            totals={c: {k: str(v) for k, v in s.items()} for c, s in totals.items()})

    def refund_basis(db, refund, defaultvalues):
        """Never blocks buyer refunds. Ready values are signed reversal deltas.

        defaultvalues must provide cumulative_refund_amount (including this refund)
        and original_amount (actual refundable payment basis), both exact cents.
        """
        try:
            change_hook = ns.get('version_change_settlement', {}).get('is_change_refund')
            if callable(change_hook) and change_hook(db, refund):
                return dict(status='unsupported', reason='version_change_requires_segment_baselines')
            exception_hook = ns.get('settlement_parties', {}).get('cost_exception')
            if callable(exception_hook) and exception_hook(db, refund.id):
                return dict(status='unsupported', reason='approved_cost_exception_requires_explicit_plan')
            Settlement = ns['Settlement']
            rows = db.scalars(select(Baseline).join(Settlement, Settlement.id == Baseline.settlement_id).where(
                Baseline.order_id == refund.order_id, Baseline.revenue > 0, Settlement.status != 'superseded')
                .order_by(Baseline.settlement_id).with_for_update(of=Baseline)).all()
            if not rows:
                return dict(status='waiting', reason='final_baseline_missing')
            order = db.get(ns['Order'], refund.order_id)
            if len(rows) != 1 or getattr(order, 'business_type', '') in ('upgrade', 'downgrade', 'version_change'):
                return dict(status='unsupported', reason='exact_single_ordinary_baseline_required')
            b = rows[0]
            snapshot = json.loads(b.snapshot_json)
            total = money(defaultvalues['original_amount'], True)
            cumulative = money(defaultvalues['cumulative_refund_amount'], True)
            amount = money(refund.amount, True)
            if total != b.revenue or not amount <= cumulative <= total:
                return dict(status='unsupported', reason='refund_basis_or_supplement_mismatch')
            def values(v):
                cost = (b.cost * v / total).quantize(CENT, rounding=ROUND_HALF_UP)
                profit = v - cost
                raw = {k: money(snapshot['shares'][k]) * profit / b.profit if b.profit else Decimal(0)
                       for k in SHARES}
                parts = {k: raw[k].quantize(CENT, rounding=ROUND_DOWN) for k in SHARES}
                remainder = int((profit - sum(parts.values())) / CENT)
                for k in sorted(SHARES, key=lambda k: (-(raw[k] - parts[k]), SHARES.index(k)))[:remainder]:
                    parts[k] += CENT
                return cost, profit, parts
            current, prior = values(cumulative), values(cumulative - amount)
            if any(current[2][k] < prior[2][k] for k in SHARES):
                return dict(status='unsupported', reason='subject_rounding_requires_explicit_reversal_plan')
            return dict(status='ready', reference_settlement_id=b.settlement_id,
                baseline_hash=b.snapshot_hash, currency=b.currency, gross_amount=-amount,
                cost_amount=prior[0]-current[0], profit_amount=prior[1]-current[1],
                shares={k: prior[2][k]-current[2][k] for k in SHARES})
        except Exception:
            # This advisory hook must never interrupt an approved buyer refund.
            return dict(status='waiting', reason='final_baseline_evidence_unavailable')

    app = ns.get('app')
    if app is not None:
        @app.get('/api/settlement-finance/positions')
        def positions_endpoint(request: Request, page: int = Query(1, ge=1),
                page_size: int = Query(50, ge=1, le=100), enterprise_id: str = '', business_view: str = '',
                q: str = '', direction: str = '', user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            return list_positions(db, user, page, page_size, enterprise_id=enterprise_id,
                                  business_view=business_view, q=q, direction=direction)

        @app.get('/api/settlement-finance/transactions')
        def transactions_endpoint(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                enterprise_id: str = '', business_view: str = '',
                user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            tenant = scope(db, user, enterprise_id, business_view)
            query = select(Transaction)
            if tenant:
                # Do not expose a combined voucher involving another provider.
                allowed = select(Position.id).join(Baseline, Baseline.settlement_id == Position.settlement_id).where(
                    Baseline.provider_enterprise_id == tenant)
                query = query.where(Transaction.id.in_(select(Allocation.transaction_id).where(
                    Allocation.position_id.in_(allowed))), ~Transaction.id.in_(select(Allocation.transaction_id).where(
                    ~Allocation.position_id.in_(allowed))))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = db.scalars(query.order_by(Transaction.id).offset((page-1)*page_size).limit(page_size)).all()
            return dict(items=[serialize_transaction(db, r) for r in rows], total=total, page=page, page_size=page_size)

        @app.post('/api/settlement-finance/transactions')
        def draft_endpoint(body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = draft(db, user, body)
            db.commit()
            return result

        @app.post('/api/settlement-finance/transactions/{transaction_id}/confirm')
        def confirm_endpoint(transaction_id: str, body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = confirm(db, user, transaction_id, body)
            db.commit()
            return result

    hooks = dict(Baseline=Baseline, Position=Position, Transaction=Transaction, Confirmation=Confirmation,
        Allocation=Allocation, Idempotency=Idempotency, capture=capture, describe=describe,
        positions_query=positions_query, list_positions=list_positions, position_dict=position_dict,
        applied=applied, balance=balance, draft=draft, confirm=confirm, refund_basis=refund_basis,
        batch_status=batch_status)
    ns['settlement_finance'] = hooks
    return hooks
