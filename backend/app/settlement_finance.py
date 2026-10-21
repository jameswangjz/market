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
                        UniqueConstraint, CheckConstraint, event, select, func, or_, text, inspect)
from sqlalchemy.exc import IntegrityError

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
ROLES = ('platform', 'provider', 'service', 'expert', 'channel')
OPERATORS = {'super_admin', 'platform_operator', 'finance_settlement'}
CENT = Decimal('.01')
FINANCE_LOCK = int.from_bytes(hashlib.sha256(b'market:settlement-finance:funds-v1').digest()[:8], 'big', signed=True)


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


def ensure_schema(engine):
    """Widen existing PostgreSQL ledgers without rewriting immutable evidence."""
    if engine.dialect.name != 'postgresql':
        return
    with engine.begin() as connection:
        schema = inspect(connection)
        for table in ('settlement_finance_positions', 'settlement_finance_transactions'):
            if not schema.has_table(table):
                continue
            kind = next((c for c in schema.get_columns(table) if c['name'] == 'kind'), None)
            if kind and isinstance(kind['type'], String) and kind['type'].length is not None and kind['type'].length < 30:
                connection.execute(text(f'ALTER TABLE {table} ALTER COLUMN kind TYPE VARCHAR(30)'))


def install(ns):
    if 'settlement_finance' in ns:
        return ns['settlement_finance']
    Base = ns['Base']
    multi_finance = {}

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
        kind = Column(String(30), nullable=False)
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
        kind = Column(String(30), nullable=False)
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

    class CostExceptionApplication(Base):
        __tablename__ = 'settlement_cost_exception_applications'
        refund_id = Column(String(36), primary_key=True)
        exception_id = Column(String(36), nullable=False, unique=True)
        baseline_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        plan_json = Column(Text, nullable=False)
        plan_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class CostExceptionPosting(Base):
        __tablename__ = 'settlement_cost_exception_postings'
        refund_id = Column(ForeignKey(CostExceptionApplication.refund_id), primary_key=True)
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, unique=True)
        plan_hash = Column(String(64), nullable=False)

    class SegmentFundingLink(Base):
        __tablename__ = 'settlement_segment_funding_links'
        source_negative_id = Column(ForeignKey(Baseline.settlement_id), primary_key=True)
        target_settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, unique=True)
        context_hash = Column(String(64), nullable=False)

    class SegmentEvidence(Base):
        __tablename__ = 'settlement_segment_capture_evidence'
        settlement_id = Column(ForeignKey(Baseline.settlement_id), primary_key=True)
        change_id = Column(String(36), nullable=False, index=True)
        window_id = Column(String(36), nullable=False)
        side = Column(String(12), nullable=False)
        context_hash = Column(String(64), nullable=False)
        __table_args__ = (UniqueConstraint('change_id', 'window_id', 'side'),)

    class FundingRequest(Base):
        __tablename__ = 'settlement_funding_requests'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        creator_id = Column(String(120), nullable=False)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)

    class FundingDecision(Base):
        __tablename__ = 'settlement_funding_decisions'
        request_id = Column(ForeignKey(FundingRequest.id), primary_key=True)
        reviewer_id = Column(String(120), nullable=False)
        decision = Column(String(12), nullable=False)
        note = Column(Text, nullable=False)

    class FundingProof(Base):
        __tablename__ = 'settlement_funding_proofs'
        settlement_id = Column(ForeignKey(Baseline.settlement_id), primary_key=True)
        request_id = Column(ForeignKey(FundingRequest.id), nullable=False, unique=True)
        source_key = Column(String(240), nullable=False, unique=True)
        verifier_id = Column(String(120), nullable=False)
        evidence_json = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)

    class FundingCommand(Base):
        __tablename__ = 'settlement_funding_commands'
        actor_id = Column(String(120), primary_key=True)
        request_id = Column(String(180), primary_key=True)
        payload_hash = Column(String(64), nullable=False)
        response_json = Column(Text, nullable=False)

    class FundingAbsorption(Base):
        __tablename__ = 'settlement_funding_absorptions'
        request_id = Column(ForeignKey(FundingRequest.id), primary_key=True)
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, unique=True)
        amount = Column(Numeric(14, 2), nullable=False)
        currency = Column(String(12), nullable=False)
        evidence_json = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)
        __table_args__ = (CheckConstraint('amount > 0'),)

    class RefundSource(Base):
        __tablename__ = 'settlement_refund_sources'
        settlement_id = Column(ForeignKey(Baseline.settlement_id), primary_key=True)
        segment_id = Column(String(120), primary_key=True)
        source_settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        source_hash = Column(String(64), nullable=False)
        evidence_json = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)

    class RefundPositionSource(Base):
        __tablename__ = 'settlement_refund_position_sources'
        position_id = Column(ForeignKey(Position.id), primary_key=True)
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        segment_id = Column(String(120), nullable=False)
        source_settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)

    def immutable(*args):
        raise ValueError('settlement finance evidence is immutable')

    for model in (Baseline, Position, Transaction, Confirmation, Allocation, Idempotency,
                  CostExceptionApplication, CostExceptionPosting):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)
    event.listen(SegmentFundingLink, 'before_update', immutable)
    event.listen(SegmentFundingLink, 'before_delete', immutable)
    event.listen(SegmentEvidence, 'before_update', immutable)
    event.listen(SegmentEvidence, 'before_delete', immutable)
    for model in (FundingRequest, FundingDecision, FundingProof, FundingCommand, FundingAbsorption,
                  RefundSource, RefundPositionSource):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def audit(db, actor, action, target, **values):
        ns['audit'](db, actor, action, 'settlement_finance', target,
                    category='settlement', business_domain='settlement', after=values)

    def operator(user):
        if (not getattr(user, 'is_active', False) or getattr(user, 'activation_status', 'active') != 'active'
                or user.platform_role not in OPERATORS):
            raise HTTPException(403, 'settlement_finance_operator_required')

    def lock_finance(db):
        """Funds/main-confirm guard, before Batch locks; never called by capture.

        A transaction-scoped advisory lock covers cross-linked batch graphs.
        Re-check pg_locks rather than caching across savepoint rollbacks.
        """
        if db.get_bind().dialect.name != 'postgresql':
            return
        unsigned = FINANCE_LOCK % (1 << 64)
        held = db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND locktype='advisory' AND granted AND objsubid=1 "
            "AND classid::bigint=:hi AND objid::bigint=:lo)"), dict(hi=unsigned >> 32, lo=unsigned & 0xffffffff))
        if held:
            return
        late = db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND relation=to_regclass(:table) AND mode='RowShareLock' AND granted)"),
            dict(table=ns['SettlementBatch'].__tablename__))
        if late:
            raise HTTPException(409, 'finance_lock_must_precede_batch_lock')
        db.execute(select(func.pg_advisory_xact_lock(FINANCE_LOCK)))

    def applied(db, position_id):
        return money(db.scalar(select(func.coalesce(func.sum(Allocation.amount), 0)).where(
            Allocation.position_id == position_id)) or 0)

    def balance(db, position):
        return money(position.amount) - applied(db, position.id)

    def funding_status(db, settlement_id):
        proof = db.get(FundingProof, settlement_id)
        if proof is None:
            return dict(status='uncovered', bank_receipt_verified=False)
        evidence = json.loads(proof.evidence_json)
        return dict(status='covered', kind=evidence['kind'], amount=evidence['amount'],
                    currency=evidence['currency'], request_id=proof.request_id,
                    evidence_hash=proof.evidence_hash, bank_receipt_verified=False)

    def cash_block(db, position):
        if position.blocked_reason != 'unfunded_settlement_adjustment':
            return position.blocked_reason
        if not position.participant_id:
            return 'missing_subject_mapping'
        return '' if db.get(FundingProof, position.settlement_id) else position.blocked_reason

    def funding_write(db, user, body, action, operation):
        operator(user)
        if not isinstance(body, dict):
            raise HTTPException(422, 'funding_body_required')
        key = body.get('request_id')
        if not isinstance(key, str) or not key.strip() or len(key) > 180:
            raise HTTPException(422, 'request_id_required')
        digest = hashlib.sha256(canonical(dict(action=action, body=body)).encode()).hexdigest()
        lock_finance(db)
        old = db.get(FundingCommand, (user.id, key))
        if old:
            if old.payload_hash != digest:
                raise HTTPException(409, 'idempotency_payload_mismatch')
            return json.loads(old.response_json)
        with db.begin_nested():
            result = operation()
            db.add(FundingCommand(actor_id=user.id, request_id=key, payload_hash=digest, response_json=canonical(result)))
            audit(db, user.id, action, result['id'], request_id=key, result=result)
            db.flush()
        return result

    def funding_request_dict(db, request):
        decision = db.get(FundingDecision, request.id)
        proof = db.get(FundingProof, request.settlement_id)
        payload = json.loads(request.payload_json)
        receipt_available = callable(ns.get('settlement_funding_proofs', {}).get('verify'))
        unavailable = 'trusted_funding_receipt_verifier_unavailable' if payload['kind'] == 'external_receipt' and not receipt_available else ''
        return dict(payload, id=request.id, creator_id=request.creator_id,
                    version=3 if proof and proof.request_id == request.id else 2 if decision else 1,
                    status='covered' if proof and proof.request_id == request.id else
                           decision.decision if decision else 'proposed',
                    reviewer_id=decision.reviewer_id if decision else None,
                    trusted_receipt_verifier_available=receipt_available,
                    can_verify=bool(decision and decision.decision == 'approved' and proof is None and not unavailable),
                    verification_unavailable_reason=unavailable,
                    bank_receipt_verified=False)

    def funding_baseline(db, settlement_id):
        b = db.get(Baseline, settlement_id)
        s = db.get(ns['Settlement'], settlement_id)
        if not b or not s or s.status != 'locked':
            raise HTTPException(409, 'locked_funding_baseline_required')
        snapshot = json.loads(b.snapshot_json)
        if b.revenue <= 0 or snapshot.get('segment_context') or snapshot.get('actual_receipts') is None:
            raise HTTPException(409, 'exact_ordinary_receipt_basis_required')
        gap = b.revenue - money(snapshot['actual_receipts'])
        if gap <= 0:
            raise HTTPException(409, 'positive_funding_gap_required')
        return b, snapshot, gap

    def propose_funding(db, user, body):
        def operation():
            sid = body.get('settlement_id')
            if not isinstance(sid, str) or not sid or len(sid) > 36:
                raise HTTPException(422, 'settlement_id_required')
            kind = body.get('kind')
            if kind not in ('external_receipt', 'platform_absorption'):
                raise HTTPException(422, 'funding_kind_invalid')
            reason, refs = body.get('reason'), body.get('evidence_refs')
            if (not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                    or not isinstance(refs, list) or not refs or len(refs) > 100
                    or any(not isinstance(r, str) or not r.strip() or len(r) > 240 for r in refs)):
                raise HTTPException(422, 'funding_reason_and_evidence_required')
            b, snapshot, gap = funding_baseline(db, sid)
            if body.get('currency', b.currency) != b.currency:
                raise HTTPException(422, 'funding_currency_mismatch')
            if money(body.get('amount'), True) != gap or db.get(FundingProof, sid):
                raise HTTPException(409, 'funding_gap_mismatch_or_already_covered')
            if kind == 'platform_absorption':
                bearer = dict(participant_type='platform', participant_id='platform', participant_name='Platform')
            else:
                identity = body.get('bearer_enterprise_id')
                if not isinstance(identity, str) or not identity or len(identity) > 36:
                    raise HTTPException(422, 'funding_bearer_required')
                enterprise = db.get(ns['Enterprise'], identity)
                if not enterprise or enterprise.verification_status != 'verified':
                    raise HTTPException(422, 'verified_funding_bearer_required')
                bearer = dict(participant_type='enterprise', participant_id=enterprise.id, participant_name=enterprise.name)
            payload = dict(settlement_id=sid, baseline_hash=b.snapshot_hash, currency=b.currency,
                amount=str(gap), actual_receipts=snapshot['actual_receipts'], kind=kind, bearer=bearer,
                reason=reason.strip(), evidence_refs=refs)
            encoded = canonical(payload)
            request = FundingRequest(settlement_id=sid, creator_id=user.id, payload_json=encoded,
                                     payload_hash=hashlib.sha256(encoded.encode()).hexdigest())
            db.add(request)
            db.flush()
            return funding_request_dict(db, request)
        return funding_write(db, user, body, 'finance_funding_propose', operation)

    def decide_funding(db, user, identity, body):
        def operation():
            request = db.scalar(select(FundingRequest).where(FundingRequest.id == identity).with_for_update())
            if request is None:
                raise HTTPException(404, 'funding_request_not_found')
            if request.creator_id == user.id:
                raise HTTPException(403, 'different_reviewer_required')
            note, decision = body.get('note'), body.get('decision')
            if not isinstance(note, str) or not note.strip() or len(note) > 4000 or decision not in ('approved', 'rejected'):
                raise HTTPException(422, 'funding_decision_and_note_required')
            if type(body.get('expected_version')) is not int or body['expected_version'] != 1 or db.get(FundingDecision, identity):
                raise HTTPException(409, 'funding_version_conflict')
            funding_baseline(db, request.settlement_id)
            db.add(FundingDecision(request_id=identity, reviewer_id=user.id, decision=decision, note=note))
            db.flush()
            return funding_request_dict(db, request)
        return funding_write(db, user, body, 'finance_funding_decide:' + identity, operation)

    def verify_funding(db, user, identity, body):
        def operation():
            request = db.scalar(select(FundingRequest).where(FundingRequest.id == identity).with_for_update())
            if request is None:
                raise HTTPException(404, 'funding_request_not_found')
            decision = db.get(FundingDecision, identity)
            if request.creator_id == user.id:
                raise HTTPException(403, 'different_verifier_required')
            if (not decision or decision.decision != 'approved' or type(body.get('expected_version')) is not int
                    or body['expected_version'] != 2 or db.get(FundingProof, request.settlement_id)):
                raise HTTPException(409, 'approved_unfunded_request_required')
            b, snapshot, gap = funding_baseline(db, request.settlement_id)
            payload = json.loads(request.payload_json)
            if (request.payload_hash != hashlib.sha256(request.payload_json.encode()).hexdigest()
                    or payload['baseline_hash'] != b.snapshot_hash or money(payload['amount']) != gap):
                raise HTTPException(409, 'funding_snapshot_mismatch')
            payables = list(db.scalars(select(Position).where(Position.settlement_id == b.settlement_id,
                Position.direction == 'payable')))
            if not payables:
                raise HTTPException(409, 'external_payable_positions_required')
            payable_batches(db, [p.id for p in payables])
            evidence = dict(payload, request_id=identity, reviewer_id=decision.reviewer_id, verifier_id=user.id,
                            bank_receipt_verified=False)
            if payload['kind'] == 'external_receipt':
                reference = body.get('receipt_reference')
                if not isinstance(reference, str) or not reference.strip() or len(reference) > 240:
                    raise HTTPException(422, 'funding_receipt_reference_required')
                verifier = ns.get('settlement_funding_proofs', {}).get('verify')
                if not callable(verifier):
                    raise HTTPException(409, 'trusted_funding_receipt_verifier_unavailable')
                proof = verifier(db, dict(payload), reference)
                if (not isinstance(proof, dict) or proof.get('status') != 'verified'
                        or money(proof.get('amount'), True) != gap or proof.get('currency') != b.currency
                        or proof.get('payer_type') != payload['bearer']['participant_type']
                        or proof.get('payer_id') != payload['bearer']['participant_id']
                        or not isinstance(proof.get('source_key'), str) or not proof['source_key'].strip()
                        or len(proof['source_key']) > 235 or not isinstance(proof.get('evidence_hash'), str)
                        or len(proof['evidence_hash']) != 64):
                    raise HTTPException(409, 'verified_funding_receipt_mismatch')
                source_key = 'cash:' + proof['source_key']
                evidence['receipt'] = proof
            else:
                sources = db.scalars(select(Position).where(Position.settlement_id == b.settlement_id,
                    Position.direction == 'internal', Position.kind == 'profit', Position.participant_type == 'platform',
                    Position.participant_id == 'platform').order_by(Position.id).with_for_update()).all()
                if (sum((balance(db, p) for p in sources), Decimal(0)) < gap
                        or sum((p.amount for p in payables), Decimal(0)) > money(snapshot['actual_receipts'])):
                    raise HTTPException(409, 'platform_absorption_exceeds_proven_available_funds')
                allocations, remaining = [], gap
                for source in sources:
                    value = min(balance(db, source), remaining)
                    if value > 0:
                        allocations.append(dict(position_id=source.id, amount=str(value)))
                        remaining -= value
                tx = Transaction(kind='internal_absorption', mode='manual', reference='funding:' + identity,
                    note='Approved platform share absorption; no external cash receipt', creator_id=request.creator_id,
                    payload_json=canonical(dict(allocations=allocations)))
                db.add(tx)
                db.flush()
                db.add(Confirmation(transaction_id=tx.id, reviewer_id=user.id, note=decision.note))
                db.flush()
                db.add_all([Allocation(transaction_id=tx.id, position_id=a['position_id'], amount=money(a['amount']))
                            for a in allocations])
                source_key = 'platform:' + b.settlement_id
                absorption = dict(request_id=identity, settlement_id=b.settlement_id, baseline_hash=b.snapshot_hash,
                    amount=str(gap), currency=b.currency, participant_type='platform', participant_id='platform',
                    transaction_id=tx.id, allocations=allocations, cash_amount='0.00',
                    creator_id=request.creator_id, reviewer_id=decision.reviewer_id, verifier_id=user.id,
                    evidence_refs=payload['evidence_refs'])
                absorbed = canonical(absorption)
                db.add(FundingAbsorption(request_id=identity, settlement_id=b.settlement_id, amount=gap,
                    currency=b.currency, evidence_json=absorbed, evidence_hash=hashlib.sha256(absorbed.encode()).hexdigest()))
                evidence.update(absorption_request_id=identity, transaction_id=tx.id)
            encoded = canonical(evidence)
            if db.scalar(select(FundingProof.settlement_id).where(FundingProof.source_key == source_key)):
                raise HTTPException(409, 'funding_source_already_used')
            db.add(FundingProof(settlement_id=b.settlement_id, request_id=identity, source_key=source_key,
                verifier_id=user.id, evidence_json=encoded, evidence_hash=hashlib.sha256(encoded.encode()).hexdigest()))
            db.flush()
            return funding_request_dict(db, request)
        return funding_write(db, user, body, 'finance_funding_verify:' + identity, operation)

    def apply_cost_exception(db, refund, baseline, default_basis, actor='system'):
        """Freeze an approved retained-cost/loss plan; never block buyer refunds.

        default_basis is the ready signed ordinary/segment reversal for exactly
        baseline, before any retained cost. No rates or cash receipts are changed.
        One approved exception targets one exact baseline and one refund amount.
        """
        try:
            hook = ns.get('settlement_parties', {}).get('cost_exception_basis')
            if not callable(hook):
                legacy = ns.get('settlement_parties', {}).get('cost_exception')
                if callable(legacy) and legacy(db, refund.id):
                    return dict(status='unsupported', reason='approved_cost_exception_requires_complete_evidence')
                return default_basis
            proof = hook(db, refund, baseline)
            if proof is None:
                return default_basis
            if proof.get('status') != 'ready':
                return proof
            baseline = db.scalar(select(Baseline).where(Baseline.settlement_id == baseline.settlement_id)
                .with_for_update().execution_options(populate_existing=True))
            amount = -money(default_basis['gross_amount'])
            cost = -money(default_basis['cost_amount'])
            profit = -money(default_basis['profit_amount'])
            shares = {k: money(default_basis['shares'][k]) for k in SHARES}
            retained = money(proof['retained_cost'], True)
            if (default_basis.get('status') != 'ready' or default_basis['reference_settlement_id'] != baseline.settlement_id
                    or default_basis['baseline_hash'] != baseline.snapshot_hash or default_basis['currency'] != baseline.currency
                    or amount != money(refund.amount, True) or amount > baseline.revenue
                    or cost < 0 or profit < 0 or amount-cost != profit or sum(shares.values()) != -profit
                    or any(v > 0 for v in shares.values()) or retained > cost):
                return dict(status='waiting', reason='cost_exception_default_basis_or_retention_invalid')
            bearer = proof['bearer']
            role = 'provider' if bearer['participant_type'] == 'enterprise' else bearer['participant_type']
            field = dict(zip(ROLES, SHARES)).get(role)
            if field is None:
                return dict(status='waiting', reason='cost_exception_bearer_category_unsupported')
            loss = dict(kind='loss', share_kind=field, participant_type=bearer['participant_type'],
                participant_id=bearer['participant_id'], participant_name=bearer['participant_name'],
                amount=str(retained), currency=baseline.currency,
                direction='internal' if role == 'platform' else 'receivable', blocked_reason='')
            result = dict(default_basis, cost_amount=-(cost-retained), profit_amount=-(profit+retained),
                shares=dict(shares), cost_exception=dict(exception_id=proof['id'], retained_cost=str(retained), loss=loss))
            result['shares'][field] -= retained
            plan = dict(version='approved-retained-cost-v1', refund_id=refund.id, order_id=refund.order_id,
                baseline_id=baseline.settlement_id, baseline_hash=baseline.snapshot_hash, currency=baseline.currency,
                default_basis=default_basis, result=result, evidence=proof, loss=loss)
            encoded = canonical(plan)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            with db.begin_nested():
                old = db.get(CostExceptionApplication, refund.id)
                if old:
                    if old.plan_json != encoded or old.plan_hash != digest:
                        return dict(status='waiting', reason='frozen_cost_exception_plan_mismatch')
                else:
                    db.add(CostExceptionApplication(refund_id=refund.id, exception_id=proof['id'],
                        baseline_id=baseline.settlement_id, plan_json=encoded, plan_hash=digest, actor=str(actor)))
                    audit(db, str(actor), 'finance_cost_exception_plan', refund.id,
                          exception_id=proof['id'], plan_hash=digest, retained_cost=str(retained),
                          reviewer_id=proof['reviewer_id'], bearer=loss, evidence_refs=proof['evidence_refs'])
                    db.flush()
            return dict(result, cost_exception_plan_hash=digest)
        except Exception:
            return dict(status='waiting', reason='cost_exception_application_evidence_unavailable')

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

    def legacy_source_reversals(db, source_id):
        """Ordinary and VC-old contributions, excluding independent losses."""
        records = []
        for b in db.scalars(select(Baseline).where(Baseline.reference_settlement_id == source_id, Baseline.revenue < 0)):
            snapshot = json.loads(b.snapshot_json)
            if hashlib.sha256(b.snapshot_json.encode()).hexdigest() != b.snapshot_hash:
                raise HTTPException(409, 'financial_source_evidence_hash_mismatch')
            exception = snapshot.get('cost_exception_application')
            ordinary = exception['default_basis'] if exception else dict(profit_amount=b.profit, shares=snapshot['shares'])
            if snapshot.get('funded_cost_exception_application_hash'):
                refs = snapshot.get('references', [])
                if len(refs) != 1 or refs[0]['reference_settlement_id'] != source_id:
                    raise HTTPException(409, 'funded_cost_source_mapping_mismatch')
                ordinary = refs[0].get('default_basis', refs[0])
            records.append(dict(gross_amount=str(b.revenue), cost_amount=str(b.cost),
                profit_amount=str(ordinary['profit_amount']), shares=ordinary['shares'],
                entries=[e for e in snapshot['entries'] if e['kind'] != 'loss']))
        return records

    def source_reversals(db, source_id):
        children = multi_finance.get('source_reversals')
        records = legacy_source_reversals(db, source_id)
        if callable(children):
            records += [r for r in children(db, source_id) if r.get('reference_settlement_id')]
        # Funding returns are new payer entitlements, not consumed beneficiary
        # budgets. Independent retained-cost losses likewise are not old profit.
        return [dict(r, entries=[e for e in r['entries'] if e['kind'] not in ('loss', 'funding_return', 'funding_absorption_return')])
                for r in records]

    def check_source_budget(source, records, values):
        snapshot = json.loads(source.snapshot_json)
        for field, limit in (('gross_amount', source.revenue), ('cost_amount', source.cost), ('profit_amount', source.profit)):
            if abs(money(values[field])) + sum(abs(money(r[field])) for r in records) > limit:
                raise HTTPException(409, 'reversal_exceeds_original_baseline')
        for field in SHARES:
            if abs(money(values['shares'][field])) + sum(abs(money(r['shares'][field])) for r in records) > money(snapshot['shares'][field]):
                raise HTTPException(409, 'reversal_exceeds_original_baseline')

    def reversal_entries(source, records, values):
        original = json.loads(source.snapshot_json)['entries']
        previous_entries = [e for r in records for e in r['entries']]
        def key(e):
            return (e['kind'], e['share_kind'], e['participant_type'], e['participant_id'], e['participant_name'])
        if len({key(e) for e in original}) != len(original):
            raise HTTPException(409, 'duplicate_original_subject_entries')
        result = []
        for kind, field in sorted({(e['kind'], e['share_kind']) for e in original}):
            group = [e for e in original if (e['kind'], e['share_kind']) == (kind, field)]
            original_total = sum(money(e['amount']) for e in group)
            previous = {key(e): sum((money(p['amount']) for p in previous_entries if key(p) == key(e)), Decimal(0)) for e in group}
            delta = abs(money(values['cost_amount'] if kind == 'cost' else values['shares'][field]))
            target = sum(previous.values()) + delta
            if target > original_total:
                raise HTTPException(409, 'subject_reversal_exceeds_original_entry')
            raw = [money(e['amount']) * target / original_total for e in group]
            cumulative = [v.quantize(CENT, rounding=ROUND_DOWN) for v in raw]
            remainder = int((target-sum(cumulative))/CENT)
            for i in sorted(range(len(group)), key=lambda i: (-(raw[i]-cumulative[i]), key(group[i])))[:remainder]:
                cumulative[i] += CENT
            for e, total in zip(group, cumulative):
                amount = total-previous[key(e)]
                if amount < 0 or total > money(e['amount']):
                    raise HTTPException(409, 'subject_rounding_requires_explicit_reversal_plan')
                if amount:
                    result.append(dict(e, amount=str(amount)))
        return result

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

    def eligible_recoveries(db, payable):
        """Read-only guard; a payout cannot bypass an already eligible debt."""
        Settlement, Line, Batch = ns['Settlement'], ns['SettlementLine'], ns['SettlementBatch']
        query = select(Position).join(Settlement, Settlement.id == Position.settlement_id).where(
            Position.direction == 'receivable',
            Position.participant_type == payable.participant_type, Position.participant_id == payable.participant_id,
            Position.currency == payable.currency, Settlement.status.in_(['locked', 'paid']),
            Position.settlement_id.in_(select(Line.settlement_id).join(Batch, Batch.id == Line.batch_id).where(
                Line.status != 'superseded', Batch.status.in_(['confirmed', 'payment_processing', 'partial_paid', 'paid']))))
        return [p for p in db.scalars(query.order_by(Position.id)) if not cash_block(db, p) and balance(db, p) > 0
                and (payable.kind != 'funding_return' or exact_position_source(db, p) == exact_position_source(db, payable))]

    def exact_position_source(db, position):
        mapping = db.get(RefundPositionSource, position.id)
        return mapping.source_settlement_id if mapping else position.settlement_id

    def segment_allocations(context):
        funding = context['funding']
        if 'segments' in funding:
            rows = funding['segments']
            allocations = {('order', a['order_id'], a['term_id']): money(a['segment_net_amount']) for a in rows}
            local = ('order', context['order_id'], context['term_id'])
        else:
            rows = funding['segment_allocations']
            allocations = {('window', a['window_id']): money(a['net_amount']) for a in rows}
            local = ('window', context['window_id'])
        if not rows or len(allocations) != len(rows) or sum(allocations.values()) != money(funding['net_amount']):
            raise ValueError('segment funding allocation mismatch')
        return allocations, local

    def segment_funding_pending(db, baseline, seen=None):
        seen = set() if seen is None else seen
        if baseline.settlement_id in seen:
            return True
        seen.add(baseline.settlement_id)
        context = json.loads(baseline.snapshot_json).get('segment_context')
        if not context or context.get('side') != 'target':
            return False
        try:
            required, _ = segment_allocations(context)
        except (ValueError, KeyError, TypeError, HTTPException):
            return True
        old = db.scalars(select(SegmentEvidence).where(SegmentEvidence.change_id == context['change_id'],
            SegmentEvidence.side == 'old')).all()
        old_baselines = [db.get(Baseline, e.settlement_id) for e in old]
        old_contexts = [json.loads(b.snapshot_json)['segment_context'] for b in old_baselines]
        if 'segments' in context['funding']:
            covered = {('order', c['order_id'], c['term_id']) for c in old_contexts}
        else:
            covered = {('window', c['window_id']) for c in old_contexts}
        if covered != set(required):
            return True
        for entry, old_baseline in zip(old, old_baselines):
            settlement = db.get(ns['Settlement'], entry.settlement_id)
            if not settlement or settlement.status not in ('locked', 'paid'):
                return True
            if any(cash_block(db, p) or balance(db, p) > 0 for p in db.scalars(select(Position).where(
                    Position.settlement_id == entry.settlement_id, Position.direction != 'internal'))):
                return True
            source = db.get(Baseline, old_baseline.reference_settlement_id)
            if source is None or segment_funding_pending(db, source, seen.copy()):
                return True
        return False

    def original_offsets(db, negative_ids, actor):
        """Only exact original, same-subject/currency entitlements; no cash."""
        Settlement, Line, Batch = ns['Settlement'], ns['SettlementLine'], ns['SettlementBatch']
        negatives = db.scalars(select(Baseline).where(Baseline.settlement_id.in_(negative_ids),
            Baseline.revenue < 0, Baseline.reference_settlement_id != '')).all()
        all_ids = {b.settlement_id for b in negatives} | {b.reference_settlement_id for b in negatives}
        links = db.execute(select(Line.settlement_id, Line.batch_id).where(
            Line.settlement_id.in_(all_ids), Line.status != 'superseded')).all()
        batch_ids = {bid for _, bid in links}
        states = {b.id: b.status for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids))
            .order_by(Batch.id).with_for_update().execution_options(populate_existing=True))}
        active = {s.id for s in db.scalars(select(Settlement).where(Settlement.id.in_(all_ids))
            .order_by(Settlement.id).with_for_update().execution_options(populate_existing=True))
            if s.status in ('locked', 'paid')}
        def confirmed(sid):
            owners = {bid for s, bid in links if s == sid}
            return sid in active and len(owners) == 1 and all(states.get(bid) in (
                'confirmed', 'payment_processing', 'partial_paid', 'paid') for bid in owners)
        pairs = [b for b in negatives if confirmed(b.settlement_id) and confirmed(b.reference_settlement_id)]
        eligible_ids = {b.settlement_id for b in pairs} | {b.reference_settlement_id for b in pairs}
        positions = db.scalars(select(Position).where(Position.settlement_id.in_(eligible_ids))
            .order_by(Position.id).with_for_update().execution_options(populate_existing=True)).all()
        results, totals = [], {}
        for b in sorted(pairs, key=lambda b: b.settlement_id):
            for debt in positions:
                if debt.settlement_id != b.settlement_id or debt.direction != 'receivable' or cash_block(db, debt) or not debt.participant_id:
                    continue
                for source in positions:
                    if (source.settlement_id != b.reference_settlement_id or source.direction != 'payable'
                            or cash_block(db, source) or source.participant_type != debt.participant_type
                            or source.participant_id != debt.participant_id or source.currency != debt.currency
                            or (debt.kind != 'loss' and (source.kind, source.share_kind) != (debt.kind, debt.share_kind))):
                        continue
                    value = min(balance(db, debt), balance(db, source))
                    if value <= 0:
                        continue
                    evidence = dict(allocations=[dict(position_id=debt.id, amount=str(value))],
                        counter_position_id=source.id, policy='exact_original_unpaid_v1',
                        original_settlement_id=source.settlement_id, negative_settlement_id=debt.settlement_id)
                    tx = Transaction(kind='offset', mode='manual', reference='baseline:' + b.settlement_id,
                        note='Original unpaid entitlement reversal; no cash recovery', creator_id=str(actor),
                        payload_json=canonical(evidence))
                    db.add(tx)
                    db.flush()
                    db.add(Confirmation(transaction_id=tx.id, reviewer_id='baseline_policy', note='Exact original entitlement'))
                    db.flush()
                    db.add_all([Allocation(transaction_id=tx.id, position_id=debt.id, amount=value),
                                Allocation(transaction_id=tx.id, position_id=source.id, amount=value)])
                    audit(db, str(actor), 'finance_original_offset', tx.id, amount=str(value),
                          currency=debt.currency, original_settlement_id=source.settlement_id,
                          negative_settlement_id=debt.settlement_id, cash_recovered=False)
                    db.flush()
                    results.append(tx.id)
                    totals[debt.currency] = totals.get(debt.currency, Decimal(0)) + value
        return dict(status='ready', offset_count=len(results), transaction_ids=results,
                    amounts={k: str(v) for k, v in totals.items()})

    def reconcile_offsets(db, batch_id, actor='system'):
        """Explicit post-confirm write hook. Caller commits; GET never calls it."""
        lock_finance(db)
        Line, Batch = ns['SettlementLine'], ns['SettlementBatch']
        batch = db.get(Batch, batch_id)
        if not batch or batch.status not in ('confirmed', 'payment_processing', 'partial_paid', 'paid'):
            return dict(status='waiting', reason='confirmed_payment_batch_required')
        ids = list(db.scalars(select(Line.settlement_id).where(Line.batch_id == batch_id, Line.status != 'superseded')))
        # Confirmation of an original batch can also unlock a reversal in a
        # previously confirmed linked batch. Revisit both sides of that graph.
        ids += list(db.scalars(select(Baseline.settlement_id).where(Baseline.reference_settlement_id.in_(ids))))
        with db.begin_nested():
            result = original_offsets(db, ids, actor)
            reconcile = multi_finance.get('reconcile_offsets')
            if callable(reconcile):
                extra = reconcile(db, ids, actor)
                result['offset_count'] += extra['offset_count']
                result['transaction_ids'] += extra['transaction_ids']
                for currency, amount in extra['amounts'].items():
                    result['amounts'][currency] = str(money(result['amounts'].get(currency, '0'))+money(amount))
            returns = ns.get('settlement_funded_refund_capture', {}).get('reconcile_returns')
            if callable(returns):
                extra = returns(db, ids, actor)
                result['offset_count'] += extra['offset_count']
                result['transaction_ids'] += extra['transaction_ids']
                for currency, amount in extra['amounts'].items():
                    result['amounts'][currency] = str(money(result['amounts'].get(currency, '0')) + money(amount))
            return result

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
            if cash_block(db, p) or not p.participant_id or p.direction == 'internal':
                raise HTTPException(409, 'position_cash_blocked')
            if identity is not None and key != identity:
                raise HTTPException(409, 'subject_currency_mismatch')
            identity = key
            required = 'payable' if payload['kind'] == 'payout' else 'receivable'
            if p.direction != required or money(a['amount'], True) > balance(db, p):
                raise HTTPException(409, 'position_direction_or_balance')
            if p.kind == 'funding_return' and payload['mode'] != 'manual':
                raise HTTPException(409, 'funding_return_requires_manual_review')
            if payload['kind'] == 'payout' and eligible_recoveries(db, p):
                raise HTTPException(409, 'matching_recovery_requires_offset')
            if payload['kind'] == 'payout':
                if segment_funding_pending(db, db.get(Baseline, p.settlement_id)):
                    raise HTTPException(409, 'segment_source_recovery_pending')
        if payload['kind'] == 'offset':
            if not counter:
                raise HTTPException(422, 'offset_counter_required')
            p = rows[counter]
            if (p.direction != 'payable' or cash_block(db, p) or
                    (p.participant_type, p.participant_id, p.currency) != identity or
                    sum(money(a['amount']) for a in allocations) > balance(db, p)):
                raise HTTPException(409, 'offset_counter_mismatch_or_balance')
            if p.kind == 'funding_return' and (payload['mode'] != 'manual' or any(
                    exact_position_source(db, rows[a['position_id']]) != exact_position_source(db, p) for a in allocations)):
                raise HTTPException(409, 'funding_return_exact_source_required')
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
        lock_finance(db)
        old, _ = replay(db, user.id, body['request_id'], dict(action='draft', **payload))
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
        lock_finance(db)
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

    def capture(db, settlement, actor='system', *, _segment_context=None, _source_negative=None):
        funded = ns.get('settlement_funded_refund', {})
        funded_model = funded.get('Plan')
        if funded_model is not None and getattr(settlement, 'refund_id', '') and db.get(funded_model, settlement.refund_id):
            hook = ns.get('settlement_funded_refund_capture', {}).get('capture')
            if not callable(hook):
                return dict(status='waiting', reason='funded_refund_capture_contract_required')
            return hook(db, settlement, actor)
        multi = ns.get('refund_settlement', {})
        model = multi.get('MultiSourceManifest')
        frozen = db.get(model, settlement.id) if model is not None else None
        if frozen is not None:
            hook = multi_finance.get('capture_multi_refund')
            if not callable(hook):
                return dict(status='waiting', reason='multi_source_refund_capture_contract_required')
            return hook(db, settlement, actor)
        Settlement = ns['Settlement']
        settlement = db.scalar(select(Settlement).where(Settlement.id == settlement.id).with_for_update()
                               .execution_options(populate_existing=True))
        if not settlement or settlement.status not in ('locked', 'paid'):
            raise HTTPException(409, 'locked_settlement_required')
        old = db.get(Baseline, settlement.id)
        if old:
            return old
        change = ns.get('version_change_settlement', {}).get('describe')
        if _segment_context is None and callable(change) and change(db, settlement.id) is not None:
            return dict(status='waiting', reason='verified_segment_capture_required', settlement_id=settlement.id)
        order = db.get(ns['Order'], settlement.order_id)
        if not order or not order.provider_enterprise_id:
            raise HTTPException(409, 'provider_required')
        try:
            economic = (_segment_context['economic_snapshot'] if _segment_context is not None
                        else json.loads(order.economic_snapshot_json or '{}'))
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
        application = None
        loss = None
        refund_id = getattr(settlement, 'refund_id', '') or ''
        if negative and refund_id:
            application = db.get(CostExceptionApplication, refund_id)
            approved = ns.get('settlement_parties', {}).get('cost_exception')
            if application is None and callable(approved) and approved(db, refund_id):
                return dict(status='waiting', reason='approved_cost_exception_plan_required', settlement_id=settlement.id)
        if application:
            plan = json.loads(application.plan_json)
            expected = plan['result']
            if (application.baseline_id != reference or plan['baseline_hash'] != original.snapshot_hash
                    or application.plan_hash != hashlib.sha256(application.plan_json.encode()).hexdigest()
                    or money(expected['gross_amount']) != revenue or money(expected['cost_amount']) != cost
                    or money(expected['profit_amount']) != profit
                    or any(money(expected['shares'][k]) != shares[k] for k in SHARES)):
                return dict(status='waiting', reason='approved_cost_exception_plan_mismatch', settlement_id=settlement.id)
            loss = plan['loss']
            posting = db.get(CostExceptionPosting, refund_id)
            if posting is not None:
                return dict(status='waiting', reason='cost_exception_already_posted', settlement_id=settlement.id)
        retained = money(loss['amount']) if loss else Decimal(0)
        ordinary_shares = dict(shares)
        if loss:
            ordinary_shares[loss['share_kind']] += retained
        if negative:
            prior = source_reversals(db, reference)
            ordinary_values = dict(gross_amount=revenue, cost_amount=cost,
                                   profit_amount=profit+retained, shares=ordinary_shares)
            check_source_budget(original, prior, ordinary_values)
        provider = db.get(ns['Enterprise'], order.provider_enterprise_id)
        if not provider:
            raise HTTPException(409, 'provider_required')
        rule = economic.get('settlement_rule', {})
        mappings = rule.get('participants', {})
        entries = []
        if negative:
            try:
                entries = reversal_entries(original, prior, ordinary_values)
            except HTTPException as exc:
                return dict(status='unsupported', reason=exc.detail, settlement_id=settlement.id)
            if loss:
                entries.append({k: v for k, v in loss.items() if k not in ('currency', 'direction')})
            if sum(money(e['amount']) for e in entries) != abs(revenue):
                raise HTTPException(409, 'original_subject_mapping_incomplete')
            if _segment_context and _segment_context.get('version_funding_projection'):
                entries = [dict(e, blocked_reason='') if e.get('blocked_reason') == 'unfunded_settlement_adjustment'
                           else e for e in entries]
                entries += ns['settlement_version_funding']['credit_entries'](_segment_context)
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
        if (_segment_context and _segment_context.get('version_funding_projection')
                and _segment_context['side'] == 'target'):
            actual_receipts = (-money(_segment_context['source_reversal']['gross_amount'])
                + money(_segment_context['segment_net_amount']) - money(_segment_context['returned_unused_supplement']))
            receipt_records = []
        funding_block = not negative and _segment_context is None and (actual_receipts is None or revenue != actual_receipts)
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
            receipt_basis=('verified_segment_transfer_and_net_cash' if _segment_context and not negative else
                           'payment_status_paid' if not negative else 'original_final_baseline'),
            receipt_records=receipt_records, bank_receipt_verified=False,
            blocked_reason='unfunded_settlement_adjustment' if funding_block else '',
            cost_exception_application=(dict(plan, plan_hash=application.plan_hash) if application else None),
            segment_context_hash=_segment_context['context_hash'] if _segment_context else None,
            segment_context=_segment_context, segment_source_negative_id=_source_negative,
            product_version_id=getattr(order, 'product_version_id', ''))
        encoded = canonical(snapshot)
        with db.begin_nested():
            baseline = Baseline(settlement_id=settlement.id, order_id=order.id,
                provider_enterprise_id=provider.id, currency=currency, revenue=revenue, cost=cost,
                profit=profit, reference_settlement_id=reference, snapshot_json=encoded,
                snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(), actor=str(actor))
            db.add(baseline)
            db.flush()
            if application:
                db.add(CostExceptionPosting(refund_id=refund_id, settlement_id=settlement.id,
                                           plan_hash=application.plan_hash))
            positions = []
            for e in entries:
                internal = e['share_kind'] == 'platform_fee'
                p = Position(settlement_id=settlement.id, order_id=order.id, currency=currency,
                    direction=('payable' if e['kind'] == 'funding_return' else 'internal'
                               if internal or e['kind'] == 'funding_absorption_return' else 'receivable' if negative else 'payable'),
                    **dict(e, amount=money(e['amount']), blocked_reason=(
                        'unfunded_settlement_adjustment' if funding_block and not internal else
                        'legacy_paid_without_allocations' if legacy_block and not internal else e['blocked_reason'])))
                db.add(p)
                positions.append(p)
            db.flush()
            audit(db, str(actor), 'finance_baseline_capture', settlement.id, snapshot_hash=baseline.snapshot_hash)
            db.flush()
        return baseline

    def _capture_segment(db, settlement, context, actor='system'):
        """Carver contract: exact verified old side first, then funded target.

        This hook does not reconcile offsets or mutate buyer payment facts.
        Missing old-side capture returns waiting rather than choosing a baseline.
        """
        try:
            verifier = ns.get('version_change_settlement', {}).get('financial_context')
            if not callable(verifier) or not isinstance(context, dict) or context.get('status') != 'ready':
                return dict(status='waiting', reason='verified_segment_context_required')
            verified = verifier(db, settlement)
            if verified.get('status') != 'ready' or canonical(verified) != canonical(context):
                return dict(status='waiting', reason='segment_context_changed')
            content = {k: v for k, v in context.items() if k not in ('status', 'context_hash')}
            if context['context_hash'] != hashlib.sha256(canonical(content).encode()).hexdigest():
                return dict(status='waiting', reason='segment_context_hash_mismatch')
            if (context['kind'] != 'version_change' or context['schema_version'] not in (1, 2)
                    or context['settlement_id'] != settlement.id or context['order_id'] != settlement.order_id):
                return dict(status='waiting', reason='segment_context_identity_mismatch')
            values = context['values']
            if (any(money(getattr(settlement, k)) != money(values[k]) for k in
                    ('gross_amount', 'cost_amount', 'profit_amount'))
                    or any(money(getattr(settlement, k)) != money(values['shares'][k]) for k in SHARES)):
                return dict(status='waiting', reason='final_segment_projection_not_applied')
            source = db.scalar(select(Baseline).where(Baseline.settlement_id == context['source_settlement_id'])
                               .with_for_update().execution_options(populate_existing=True))
            if (not source or source.snapshot_hash != context['source_baseline_hash']
                    or source.order_id != settlement.order_id or source.currency != context['currency']
                    or source.provider_enterprise_id != context['provider_enterprise_id']):
                return dict(status='waiting', reason='exact_segment_source_baseline_required')
            rechecked = verifier(db, settlement)
            if rechecked.get('status') != 'ready' or canonical(rechecked) != canonical(context):
                return dict(status='waiting', reason='segment_context_changed_under_source_lock')
            existing = db.get(Baseline, settlement.id)
            if existing:
                if json.loads(existing.snapshot_json).get('segment_context_hash') != context['context_hash']:
                    return dict(status='waiting', reason='immutable_segment_context_mismatch')
                if context.get('version_funding_projection') and context['side'] == 'old':
                    if ns['settlement_version_funding']['describe'](db, settlement.id) is None:
                        return dict(status='waiting', reason='immutable_version_funding_projection_required')
                return existing
            if context['side'] == 'old':
                if settlement.reference_settlement_id != source.settlement_id or money(values['gross_amount']) >= 0:
                    return dict(status='waiting', reason='exact_segment_negative_reference_required')
                with db.begin_nested():
                    result = capture(db, settlement, actor, _segment_context=context)
                    if not isinstance(result, dict):
                        if context.get('version_funding_projection'):
                            ns['settlement_version_funding']['post'](db, settlement, context, actor)
                        db.add(SegmentEvidence(settlement_id=settlement.id, change_id=context['change_id'],
                            window_id=context['window_id'], side='old', context_hash=context['context_hash']))
                        db.flush()
                    return result
            if context['side'] != 'target' or money(values['gross_amount']) <= 0 or settlement.reference_settlement_id:
                return dict(status='waiting', reason='exact_segment_target_required')
            funding, reversal = context['funding'], context['source_reversal']
            net = money(funding['net_amount'])
            segment_net = money(context['segment_net_amount'])
            allocations, local = segment_allocations(context)
            returned = money(context.get('returned_unused_supplement', '0'))
            if (returned < 0 or context['schema_version'] == 2 and (
                    not context.get('version_funding_projection')
                    or returned != money(context['version_funding_projection']['returned_unused_supplement']))):
                return dict(status='waiting', reason='verified_segment_return_projection_required')
            if (funding.get('basis') != 'verified_net_change_cash_not_target_gross'
                    or funding['change_id'] != context['change_id']
                    or allocations.get(local) != segment_net
                    or money(values['gross_amount']) != -money(reversal['gross_amount']) + segment_net - returned):
                return dict(status='waiting', reason='segment_transfer_cash_conservation_failed')
            candidates = db.scalars(select(Baseline).where(Baseline.reference_settlement_id == source.settlement_id,
                Baseline.revenue < 0)).all()
            matching = []
            for candidate in candidates:
                snapshot = json.loads(candidate.snapshot_json)
                old_context = snapshot.get('segment_context') or {}
                if context.get('version_funding_projection') and (
                        old_context.get('version_funding_projection') != context['version_funding_projection']
                        or old_context.get('returned_unused_supplement') != context.get('returned_unused_supplement')
                        or ns['settlement_version_funding']['describe'](db, candidate.settlement_id) is None):
                    continue
                if (old_context.get('side') == 'old' and all(old_context.get(k) == context[k] for k in
                        ('change_id', 'window_id', 'receipt_hash', 'source_baseline_hash'))
                        and candidate.revenue == money(reversal['gross_amount'])
                        and candidate.cost == money(reversal['cost_amount'])
                        and candidate.profit == money(reversal['profit_amount'])
                        and all(money(snapshot['shares'][k]) == money(reversal['shares'][k]) for k in SHARES)
                        and db.get(ns['Settlement'], candidate.settlement_id).status in ('locked', 'paid')):
                    matching.append(candidate)
            if len(matching) != 1:
                return dict(status='waiting', reason='exact_old_segment_capture_required')
            negative = matching[0]
            if db.get(SegmentFundingLink, negative.settlement_id):
                return dict(status='waiting', reason='segment_transfer_already_assigned')
            if any(cash_block(db, p) for p in db.scalars(select(Position).where(Position.settlement_id == source.settlement_id))):
                return dict(status='waiting', reason='segment_source_funding_blocked')
            with db.begin_nested():
                result = capture(db, settlement, actor, _segment_context=context, _source_negative=negative.settlement_id)
                if isinstance(result, dict):
                    return result
                db.add(SegmentFundingLink(source_negative_id=negative.settlement_id,
                    target_settlement_id=settlement.id, context_hash=context['context_hash']))
                db.add(SegmentEvidence(settlement_id=settlement.id, change_id=context['change_id'],
                    window_id=context['window_id'], side='target', context_hash=context['context_hash']))
                audit(db, str(actor), 'finance_segment_funding_link', settlement.id,
                      source_negative_id=negative.settlement_id, context_hash=context['context_hash'],
                      transfer_amount=str(-negative.revenue), segment_net_cash=str(segment_net),
                      verified_global_net_cash=str(net), bank_receipt_verified=False)
                db.flush()
            return result
        except (HTTPException, KeyError, TypeError, ValueError, ArithmeticError):
            return dict(status='waiting', reason='segment_capture_evidence_unavailable')

    def capture_segment(db, settlement, context, actor='system'):
        result = _capture_segment(db, settlement, context, actor)
        if (isinstance(result, dict) and isinstance(context, dict)
                and context.get('schema_version') == 2 and context.get('version_funding_projection')):
            # Only the new funded-source workflow must roll business locking
            # back on missing capture; v1 retains its historical waiting API.
            raise HTTPException(409, dict(code='source_funding_capture_not_ready', **result))
        return result

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
            funding=funding_status(db, settlement_id),
            funded_refund=(ns['settlement_funded_refund']['describe'](db, settlement_id)
                           if 'settlement_funded_refund' in ns else None),
            version_funding=(ns['settlement_version_funding']['describe'](db, settlement_id)
                             if 'settlement_version_funding' in ns else None),
            references=multi_finance['describe'](db, settlement_id) if multi_finance else [],
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
        blocked = any(cash_block(db, p) for p in rows)
        # Scalar balances cannot represent several currencies without conversion.
        if len(currencies) > 1:
            raise HTTPException(409, 'batch_multiple_currencies_require_separate_status')
        complete = coverage and not blocked
        return dict(positive_payment_complete=coverage and payable == 0 and not any(
                cash_block(db, p) for p in rows if p.direction == 'payable'),
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
            blocked_reason=cash_block(db, p), status=('blocked' if cash_block(db, p) else
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
            if b.revenue > total and amount <= cumulative <= total:
                hook = ns.get('settlement_funded_refund_capture', {}).get('basis')
                if callable(hook):
                    return hook(db, refund, b)
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
            result = dict(status='ready', reference_settlement_id=b.settlement_id,
                baseline_hash=b.snapshot_hash, currency=b.currency, gross_amount=-amount,
                cost_amount=prior[0]-current[0], profit_amount=prior[1]-current[1],
                shares={k: prior[2][k]-current[2][k] for k in SHARES},
                original_amount=str(total), cumulative_refund_amount=str(cumulative))
            return apply_cost_exception(db, refund, b, result)
        except Exception:
            # This advisory hook must never interrupt an approved buyer refund.
            return dict(status='waiting', reason='final_baseline_evidence_unavailable')

    app = ns.get('app')
    if app is not None:
        @app.get('/api/settlement-finance/funding-adjustments')
        def funding_list_endpoint(enterprise_id: str = '', business_view: str = '',
                user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            tenant = scope(db, user, enterprise_id, business_view)
            query = select(FundingRequest).join(Baseline, Baseline.settlement_id == FundingRequest.settlement_id)
            if tenant:
                query = query.where(Baseline.provider_enterprise_id == tenant)
            rows = db.scalars(query.order_by(FundingRequest.id)).all()
            return dict(items=[funding_request_dict(db, r) for r in rows], total=len(rows))

        @app.post('/api/settlement-finance/funding-adjustments')
        def funding_propose_endpoint(body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = propose_funding(db, user, body)
            db.commit()
            return result

        @app.post('/api/settlement-finance/funding-adjustments/{identity}/decision')
        def funding_decide_endpoint(identity: str, body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = decide_funding(db, user, identity, body)
            db.commit()
            return result

        @app.post('/api/settlement-finance/funding-adjustments/{identity}/verify')
        def funding_verify_endpoint(identity: str, body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = verify_funding(db, user, identity, body)
            db.commit()
            return result

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
        batch_status=batch_status, CostExceptionApplication=CostExceptionApplication,
        apply_cost_exception=apply_cost_exception, CostExceptionPosting=CostExceptionPosting,
        reconcile_offsets=reconcile_offsets, capture_segment=capture_segment, SegmentFundingLink=SegmentFundingLink,
        SegmentEvidence=SegmentEvidence, lock_finance=lock_finance,
        FundingRequest=FundingRequest, FundingDecision=FundingDecision, FundingProof=FundingProof,
        FundingCommand=FundingCommand, FundingAbsorption=FundingAbsorption,
        propose_funding=propose_funding, decide_funding=decide_funding,
        verify_funding=verify_funding, funding_status=funding_status, ensure_schema=ensure_schema)
    from .settlement_multi_refund_finance import install as install_multi_refund
    core = dict(hooks, RefundSource=RefundSource, RefundPositionSource=RefundPositionSource,
                money=money, canonical=canonical, audit=audit, cash_block=cash_block)
    multi_finance.update(install_multi_refund(ns, core))
    multi_finance['capture_multi_refund'] = multi_finance['capture']
    hooks.update(capture_multi_refund=multi_finance['capture'], RefundSource=RefundSource,
                 RefundPositionSource=RefundPositionSource, source_reversals=source_reversals,
                 check_source_budget=check_source_budget, reversal_entries=reversal_entries)
    ns['settlement_finance'] = hooks
    if ns.get('app') is not None and ns.get('engine') is not None:
        ns['app'].add_event_handler('startup', lambda: ensure_schema(ns['engine']))
    from .settlement_manual_funding import install as install_manual_funding
    install_manual_funding(ns, hooks)
    if ns.get('Refund') is not None:
        from .settlement_funded_refund import install as install_funded_refund
        install_funded_refund(ns)
        from .settlement_funded_refund_capture import install as install_funded_capture
        install_funded_capture(ns, dict(core, **hooks))
        from .settlement_version_funding import install as install_version_funding
        install_version_funding(ns, hooks)
    return hooks
