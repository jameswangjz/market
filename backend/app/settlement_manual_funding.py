"""Immutable manual supplemental receipts; never bank reconciliation or buyer Payment.

Wiring: install(ns, finance_hooks) at the end of settlement_finance.install,
before create_all. Existing funding verify consumes receipt_reference=manual:<id>.
Core currently covers one complete gap, so registration requires that exact amount.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import secrets
import unicodedata

from fastapi import Depends, HTTPException
from sqlalchemy import Column, Date, DateTime, ForeignKey, Numeric, String, Text, CheckConstraint, event, select
from sqlalchemy.exc import IntegrityError

from .settlement_finance import OPERATORS, canonical, money


def install(ns, core=None):
    if 'settlement_manual_funding' in ns:
        return ns['settlement_manual_funding']
    core = core or ns['settlement_finance']
    Base = ns['Base']
    Request, Decision, Proof, Command, Baseline = (core[key] for key in (
        'FundingRequest', 'FundingDecision', 'FundingProof', 'FundingCommand', 'Baseline'))

    class Evidence(Base):
        __tablename__ = 'settlement_manual_funding_evidence'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        funding_request_id = Column(ForeignKey(Request.id), nullable=False, unique=True)
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, index=True)
        creator_id = Column(String(120), nullable=False)
        amount = Column(Numeric(14, 2), nullable=False)
        currency = Column(String(12), nullable=False)
        reference = Column(String(240), nullable=False)
        paid_on = Column(Date, nullable=False)
        source_hash = Column(String(64), nullable=False, unique=True)
        evidence_json = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)
        created_at = Column(DateTime(timezone=True), nullable=False)
        __table_args__ = (CheckConstraint('amount > 0'),)

    class Review(Base):
        __tablename__ = 'settlement_manual_funding_reviews'
        evidence_id = Column(ForeignKey(Evidence.id), primary_key=True)
        reviewer_id = Column(String(120), nullable=False)
        decision = Column(String(12), nullable=False)
        note = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)
        created_at = Column(DateTime(timezone=True), nullable=False)

    def immutable(*_):
        raise ValueError('manual funding evidence is immutable')

    for model in (Evidence, Review):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def operator(user):
        if (not getattr(user, 'is_active', False) or getattr(user, 'activation_status', 'active') != 'active'
                or getattr(user, 'platform_role', '') not in OPERATORS):
            raise HTTPException(403, 'settlement_finance_operator_required')

    def text(value, size, code):
        if not isinstance(value, str) or not value.strip() or len(value) > size:
            raise HTTPException(422, code)
        return value.strip()

    def digest(value):
        return hashlib.sha256(canonical(value).encode()).hexdigest()

    def basis(db, identity):
        request = db.scalar(select(Request).where(Request.id == identity).with_for_update())
        if request is None:
            raise HTTPException(404, 'funding_request_not_found')
        payload = json.loads(request.payload_json)
        baseline = db.get(Baseline, request.settlement_id)
        settlement = db.get(ns['Settlement'], request.settlement_id)
        decision = db.get(Decision, request.id)
        if (payload.get('kind') != 'external_receipt' or not baseline or not settlement
                or settlement.status != 'locked' or db.get(Proof, request.settlement_id)
                or decision and decision.decision != 'approved'):
            raise HTTPException(409, 'manual_funding_request_unavailable')
        snapshot = json.loads(baseline.snapshot_json)
        if (request.payload_hash != hashlib.sha256(request.payload_json.encode()).hexdigest()
                or payload.get('settlement_id') != baseline.settlement_id
                or payload.get('baseline_hash') != baseline.snapshot_hash
                or payload.get('currency') != baseline.currency
                or snapshot.get('actual_receipts') is None or snapshot.get('segment_context')
                or baseline.revenue <= 0):
            raise HTTPException(409, 'manual_funding_basis_mismatch')
        gap = baseline.revenue - money(snapshot['actual_receipts'])
        bearer = payload.get('bearer', {})
        enterprise = db.get(ns['Enterprise'], bearer.get('participant_id')) if bearer.get('participant_type') == 'enterprise' else None
        if (gap <= 0 or money(payload.get('amount'), True) != gap or not enterprise
                or enterprise.verification_status != 'verified'):
            raise HTTPException(409, 'manual_funding_basis_mismatch')
        return request, payload, gap

    def serialize(db, row):
        review = db.get(Review, row.id)
        return dict(id=row.id, funding_request_id=row.funding_request_id, settlement_id=row.settlement_id,
            creator_id=row.creator_id, amount=str(row.amount), currency=row.currency,
            reference=row.reference, paid_on=row.paid_on.isoformat(), evidence_hash=row.evidence_hash,
            evidence_refs=json.loads(row.evidence_json)['evidence_refs'],
            note=json.loads(row.evidence_json)['note'], reviewer_id=review.reviewer_id if review else None,
            review_note=review.note if review else None, status=review.decision if review else 'registered',
            version=2 if review else 1, receipt_reference='manual:' + row.id,
            method='manual_independent_review', bank_receipt_verified=False)

    def command(db, user, body, action, operation):
        operator(user)
        if not isinstance(body, dict):
            raise HTTPException(422, 'manual_funding_body_required')
        key = text(body.get('request_id'), 180, 'request_id_required')
        # Match the finance command hash format; request IDs share its actor namespace.
        hashed = digest(dict(action=action, body=body))
        core['lock_finance'](db)
        previous = db.get(Command, (user.id, key))
        if previous:
            if previous.payload_hash != hashed:
                raise HTTPException(409, 'idempotency_payload_mismatch')
            return json.loads(previous.response_json)
        try:
            with db.begin_nested():
                result = operation()
                db.add(Command(actor_id=user.id, request_id=key, payload_hash=hashed, response_json=canonical(result)))
                ns['audit'](db, user.id, action, 'settlement_finance', result['id'],
                    category='settlement', business_domain='settlement',
                    after=dict(result=result, request_id=key, method='manual_independent_review', bank_receipt_verified=False))
                db.flush()
            return result
        except IntegrityError:
            raise HTTPException(409, 'manual_funding_conflict_refresh_required') from None

    def register(db, user, body):
        def operation():
            if set(body) != {'request_id', 'funding_request_id', 'amount', 'currency', 'reference', 'paid_on', 'evidence_refs', 'note'}:
                raise HTTPException(422, 'manual_funding_fields_invalid')
            identity = text(body.get('funding_request_id'), 36, 'funding_request_id_required')
            request, payload, gap = basis(db, identity)
            amount = money(body.get('amount'), True)
            if amount != gap:
                raise HTTPException(409, 'manual_funding_exact_gap_required')
            if body.get('currency') != payload['currency']:
                raise HTTPException(422, 'funding_currency_mismatch')
            reference = text(body.get('reference'), 240, 'manual_funding_reference_required')
            note = text(body.get('note'), 4000, 'manual_funding_note_required')
            refs = body.get('evidence_refs')
            if (not isinstance(refs, list) or not 1 <= len(refs) <= 100):
                raise HTTPException(422, 'manual_funding_evidence_required')
            refs = [text(value, 240, 'manual_funding_evidence_required') for value in refs]
            try:
                paid_on = date.fromisoformat(body.get('paid_on'))
                if paid_on.isoformat() != body['paid_on'] or paid_on > datetime.now(timezone.utc).date():
                    raise ValueError()
            except (TypeError, ValueError):
                raise HTTPException(422, 'manual_funding_date_invalid') from None
            source_hash = digest([payload['bearer']['participant_type'], payload['bearer']['participant_id'],
                                  payload['currency'], unicodedata.normalize('NFKC', reference).casefold()])
            encoded = canonical(dict(funding_request_id=request.id, settlement_id=request.settlement_id,
                funding_payload_hash=request.payload_hash, baseline_hash=payload['baseline_hash'],
                amount=str(amount), currency=payload['currency'], bearer=payload['bearer'], reference=reference,
                paid_on=paid_on.isoformat(), evidence_refs=refs, note=note, creator_id=user.id,
                source_hash=source_hash, method='manual_independent_review', bank_receipt_verified=False))
            row = Evidence(funding_request_id=request.id, settlement_id=request.settlement_id, creator_id=user.id,
                amount=amount, currency=payload['currency'], reference=reference, paid_on=paid_on,
                source_hash=source_hash, evidence_json=encoded, evidence_hash=hashlib.sha256(encoded.encode()).hexdigest(),
                created_at=datetime.now(timezone.utc))
            db.add(row)
            db.flush()
            return serialize(db, row)
        return command(db, user, body, 'finance_manual_funding_register', operation)

    def review(db, user, identity, body):
        def operation():
            if set(body) != {'request_id', 'expected_version', 'decision', 'note'}:
                raise HTTPException(422, 'manual_funding_review_fields_invalid')
            row = db.scalar(select(Evidence).where(Evidence.id == identity).with_for_update())
            if row is None:
                raise HTTPException(404, 'manual_funding_evidence_not_found')
            if user.id == row.creator_id:
                raise HTTPException(403, 'different_manual_funding_reviewer_required')
            if type(body.get('expected_version')) is not int or body['expected_version'] != 1 or db.get(Review, row.id):
                raise HTTPException(409, 'manual_funding_review_version_conflict')
            decision = body.get('decision')
            if decision not in ('verified', 'rejected'):
                raise HTTPException(422, 'manual_funding_review_decision_invalid')
            note = text(body.get('note'), 4000, 'manual_funding_note_required')
            request, payload, gap = basis(db, row.funding_request_id)
            validate(row, request, payload, gap)
            db.add(Review(evidence_id=row.id, reviewer_id=user.id, decision=decision, note=note,
                          evidence_hash=row.evidence_hash, created_at=datetime.now(timezone.utc)))
            db.flush()
            return serialize(db, row)
        return command(db, user, body, 'finance_manual_funding_review:' + identity, operation)

    def validate(row, request, payload, gap):
        fact = json.loads(row.evidence_json)
        expected = dict(funding_request_id=request.id, settlement_id=request.settlement_id,
            funding_payload_hash=request.payload_hash, baseline_hash=payload['baseline_hash'],
            amount=str(money(row.amount)), currency=row.currency, bearer=payload['bearer'],
            reference=row.reference, paid_on=row.paid_on.isoformat(), creator_id=row.creator_id,
            source_hash=row.source_hash, method='manual_independent_review', bank_receipt_verified=False)
        if (hashlib.sha256(row.evidence_json.encode()).hexdigest() != row.evidence_hash
                or row.amount != gap or row.currency != payload['currency']
                or row.funding_request_id != request.id or row.settlement_id != request.settlement_id
                or any(fact.get(key) != value for key, value in expected.items())):
            raise HTTPException(409, 'manual_funding_evidence_mismatch')

    previous_verifier = ns.get('settlement_funding_proofs', {}).get('verify')

    def verify(db, payload, reference):
        if not isinstance(reference, str) or not reference.startswith('manual:'):
            if callable(previous_verifier):
                return previous_verifier(db, payload, reference)
            raise HTTPException(409, 'reviewed_manual_funding_evidence_required')
        row = db.get(Evidence, reference[7:])
        if row is None:
            raise HTTPException(404, 'manual_funding_evidence_not_found')
        request, actual, gap = basis(db, row.funding_request_id)
        if actual != payload:
            raise HTTPException(409, 'manual_funding_evidence_mismatch')
        validate(row, request, payload, gap)
        checked = db.get(Review, row.id)
        if (not checked or checked.decision != 'verified' or checked.reviewer_id == row.creator_id
                or checked.evidence_hash != row.evidence_hash):
            raise HTTPException(409, 'reviewed_manual_funding_evidence_required')
        return dict(status='verified', amount=str(row.amount), currency=row.currency,
            payer_type=payload['bearer']['participant_type'], payer_id=payload['bearer']['participant_id'],
            source_key='manual:' + row.source_hash, evidence_hash=row.evidence_hash,
            evidence_id=row.id, registrar_id=row.creator_id, reviewer_id=checked.reviewer_id,
            method='manual_independent_review', bank_receipt_verified=False)

    def tenant_scope(db, user, enterprise_id, business_view):
        if not getattr(user, 'is_active', False) or getattr(user, 'activation_status', 'active') != 'active':
            raise HTTPException(403, 'active_user_required')
        if not enterprise_id and business_view != 'provider' and getattr(user, 'platform_role', '') in OPERATORS | {'security_compliance'}:
            return None
        if not enterprise_id or business_view not in ('', 'provider') or getattr(user, 'verified_status', '') != 'verified':
            raise HTTPException(403, 'provider_scope_required')
        M = ns['Membership']
        membership = db.scalar(select(M).where(M.user_id == user.id, M.enterprise_id == enterprise_id,
            M.status == 'active', M.role.in_(['super_admin', 'enterprise_admin'])))
        enterprise = db.get(ns['Enterprise'], enterprise_id)
        if (not membership or not enterprise or enterprise.verification_status != 'verified'
                or 'provider' not in (membership.business_roles or '').split(',')):
            raise HTTPException(403, 'verified_provider_admin_required')
        return enterprise_id

    def list_evidence(db, user, enterprise_id='', business_view='', funding_request_id=''):
        tenant = tenant_scope(db, user, enterprise_id, business_view)
        query = select(Evidence).join(Baseline, Baseline.settlement_id == Evidence.settlement_id)
        if tenant:
            query = query.where(Baseline.provider_enterprise_id == tenant)
        if funding_request_id:
            query = query.where(Evidence.funding_request_id == funding_request_id)
        rows = db.scalars(query.order_by(Evidence.created_at, Evidence.id)).all()
        return dict(items=[serialize(db, row) for row in rows], total=len(rows))

    app = ns.get('app')
    if app is not None:
        @app.get('/api/settlement-finance/manual-funding-evidence')
        def list_endpoint(enterprise_id: str = '', business_view: str = '', funding_request_id: str = '',
                          user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            return list_evidence(db, user, enterprise_id, business_view, funding_request_id)

        @app.post('/api/settlement-finance/manual-funding-evidence')
        def register_endpoint(body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = register(db, user, body)
            db.commit()
            return result

        @app.post('/api/settlement-finance/manual-funding-evidence/{identity}/review')
        def review_endpoint(identity: str, body: dict, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = review(db, user, identity, body)
            db.commit()
            return result

    ns['settlement_funding_proofs'] = dict(ns.get('settlement_funding_proofs', {}), verify=verify)
    hooks = dict(Evidence=Evidence, Review=Review, register=register, review=review, verify=verify, list=list_evidence)
    ns['settlement_manual_funding'] = hooks
    return hooks
