"""Order-scoped FILE revocation. No object deletion or subscription terms.

apply owns a normal transaction even when the worker passes an AUTOCOMMIT
read session. Download callers must hold the same Order lock until content
has been read and authorization recorded. A funds failure never restores access.
"""
import hashlib
import json
import secrets

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, event, select

from .refund_execution import canonical, digest
from .refund_funding_store import cents
from .subscription_terms import UTCDateTime


VERSION = 'ordinary-file-refund-plan-v1'


def fail():
    raise HTTPException(409, 'file_refund_evidence_invalid')


def install(ns):
    if 'refund_file_resources' in ns:
        return ns['refund_file_resources']

    class Recovery(ns['Base']):
        __tablename__ = 'refund_file_resource_recoveries'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = Column(ForeignKey('refunds.id'), nullable=False, unique=True)
        execution_id = Column(ForeignKey('ordinary_refund_executions.id'), nullable=False, unique=True)
        order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        stage = Column(String(30), nullable=False, default='resources_applied')
        plan_json = Column(Text, nullable=False)
        proof_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        created_at = Column(UTCDateTime(), nullable=False, default=ns['now'])

    def immutable(*args):
        raise ValueError('file refund recovery is immutable')

    event.listen(Recovery, 'before_update', immutable)
    event.listen(Recovery, 'before_delete', immutable)

    def prepare(db, order, refund, envelope):
        try:
            case = db.get(ns['refund_workflow']['Case'], refund.id)
            economic = envelope['economic_plan']
            assessment = db.get(ns['refund_planning']['ManualAssessment'], economic['manual_assessment_id'])
            if (order.delivery_method_snapshot != 'file' or order.snapshot_version != 1
                    or refund.order_id != order.id or envelope['order_id'] != order.id
                    or envelope['refund_id'] != refund.id or envelope['payment_id'] != refund.payment_id
                    or refund.status not in ('approved', 'executing', 'failed', 'completed')
                    or not case or case.source != 'ordinary'
                    or ns['refund_workflow']['controlled_source'](db, refund, case) != 'ordinary'
                    or hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash
                    or json.loads(case.plan_json) != envelope or envelope['basis'] != 'manual'
                    or envelope['calculation_version'] != VERSION
                    or economic['calculation_version'] != VERSION or economic['basis'] != 'manual'
                    or economic['source_order_id'] != order.id
                    or economic['segment_id'] != order.id
                    or economic['economic_snapshot'] != ns['order_economics']['economic_snapshot'](order)
                    or economic['delivery_snapshot'] != ns['order_economics']['delivery_snapshot'](order)
                    or not assessment or assessment.refund_id != refund.id
                    or assessment.id not in envelope['evidence_ids']
                    or not assessment.assessed_by or not assessment.reason
                    or not json.loads(assessment.evidence_refs_json)
                    or economic['evidence_refs'] != json.loads(assessment.evidence_refs_json)
                    or economic['assessed_by'] != assessment.assessed_by
                    or economic['reason'] != assessment.reason
                    or economic['assessed_at'] != assessment.assessed_at.isoformat()
                    or economic['manual_assessment_case_version'] != assessment.case_version
                    or economic['used_calls'] != assessment.used_calls
                    or cents(assessment.amount) != cents(refund.amount)
                    or cents(economic['refundable_limit']) != cents(assessment.refundable_limit)
                    or cents(envelope['amount']) != cents(refund.amount)
                    or cents(economic['refund_amount']) != cents(refund.amount)):
                fail()
            from .refund_settlement import segment_values
            segment_values(economic)
            completed = list(db.scalars(select(ns['Refund']).where(
                ns['Refund'].order_id == order.id, ns['Refund'].id != refund.id,
                ns['Refund'].status == 'completed')))
            prior_ids = economic['prior_completed_refund_ids']
            if (not isinstance(prior_ids, list) or prior_ids != sorted(set(prior_ids))
                    or not set(prior_ids).issubset({row.id for row in completed})
                    or refund.status != 'completed' and set(prior_ids) != {row.id for row in completed}):
                fail()
            prior = sum((cents(row.amount) for row in completed if row.id in prior_ids), 0)
            cumulative = prior + cents(refund.amount)
            recorded = sum((cents(row.amount) for row in completed), 0)
            if refund.status == 'completed':
                recorded += cents(refund.amount)
            if (cents(economic['original_amount']) != cents(economic['economic_snapshot']['amount'])
                    or cents(economic['cumulative_refund_amount']) != cumulative
                    or cents(order.refunded_amount) != recorded
                    or cumulative > cents(assessment.refundable_limit)):
                fail()
            if refund.status == 'completed':
                completion = db.get(ns['refund_execution']['Completion'], refund.id)
                execution = db.get(ns['refund_execution']['Execution'], completion.execution_id) if completion else None
                if (not completion or not execution or execution.status != 'completed'
                        or execution.refund_id != refund.id or execution.plan_hash != case.plan_hash
                        or completion.plan_hash != case.plan_hash
                        or json.loads(completion.economic_plan_json) != economic
                        or cents(completion.amount) != cents(refund.amount)
                        or completion.proof_hash != digest(json.loads(execution.proof_json))
                        or completion.funding_hash != digest(json.loads(execution.funding_json))):
                    fail()
            plan = dict(kind='file', refund_id=refund.id, order_id=order.id,
                        product_id=order.product_id, product_version_id=order.product_version_id,
                        case_plan_hash=case.plan_hash, frozen_envelope_hash=digest(envelope),
                        action='revoke_order_downloads', manual_assessment_id=assessment.id)
            plan['plan_hash'] = digest(plan)
            return plan
        except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError) as exc:
            raise HTTPException(409, 'file_refund_evidence_invalid') from exc

    def apply(_reads, execution):
        # Never inherit the worker's AUTOCOMMIT connection.
        with ns['SessionLocal']() as db, db.begin():
            try:
                plan = json.loads(execution.resource_plan_json)
                order = ns['refund_workflow']['lock_order'](db, plan['order_id'])
                persisted = db.get(ns['refund_execution']['Execution'], execution.id, populate_existing=True)
                refund = db.get(ns['Refund'], execution.refund_id, populate_existing=True)
                case = db.get(ns['refund_workflow']['Case'], execution.refund_id)
                phase = persisted.resume_status if persisted and persisted.status == 'failed' else getattr(persisted, 'status', None)
                if (not persisted or not refund or not case
                        or persisted.refund_id != execution.refund_id
                        or persisted.resource_plan_json != execution.resource_plan_json
                        or persisted.resource_plan_hash != digest(plan)
                        or persisted.plan_hash != case.plan_hash
                        or phase not in ('resources_pending', 'resources_applied', 'funds_pending', 'completed')
                        or prepare(db, order, refund, json.loads(case.plan_json)) != plan):
                    fail()
                row = db.scalar(select(Recovery).where(Recovery.refund_id == refund.id))
                if row:
                    proof = json.loads(row.proof_json)
                    if (row.execution_id != execution.id or row.order_id != order.id
                            or row.stage != 'resources_applied' or row.plan_json != canonical(plan)
                            or row.payload_hash != digest(dict(plan=plan, proof=proof))
                            or proof != make_proof(plan)
                            or persisted.proof_json is not None and json.loads(persisted.proof_json) != proof):
                        fail()
                    return proof
                if persisted.proof_json is not None:
                    fail()
                proof = make_proof(plan)
                db.add(Recovery(refund_id=refund.id, execution_id=execution.id, order_id=order.id,
                                plan_json=canonical(plan), proof_json=canonical(proof),
                                payload_hash=digest(dict(plan=plan, proof=proof))))
                db.flush()
                return proof
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(409, 'file_refund_evidence_invalid') from exc

    def make_proof(plan):
        return dict(schema='file_refund_resource_recovery_v1', refund_id=plan['refund_id'],
                    resource_plan_hash=digest(plan), resource_applied=True,
                    evidence_fingerprint=digest(dict(order_id=plan['order_id'], plan=plan)),
                    order_id=plan['order_id'], action='revoke_order_downloads')

    def blocked(db, order):
        # Caller holds Order FOR UPDATE; any receipt denies even during funds retry.
        if order.delivery_method_snapshot != 'file':
            return False
        row = db.scalar(select(Recovery.id).where(Recovery.order_id == order.id).limit(1))
        completed = db.scalar(select(ns['Refund'].id).where(ns['Refund'].order_id == order.id,
                              ns['Refund'].status == 'completed').limit(1))
        Execution, Refund = ns['refund_execution']['Execution'], ns['Refund']
        phases = ('resources_pending', 'resources_applied', 'funds_pending', 'completed')
        pending = db.scalar(select(Execution.id).join(Refund, Refund.id == Execution.refund_id).where(
            Refund.order_id == order.id,
            (Execution.status.in_(phases)) | ((Execution.status == 'failed')
                & Execution.resume_status.in_(phases))).limit(1))
        return bool(row or completed or pending or cents(order.refunded_amount) > 0)

    def require_download(db, order):
        if blocked(db, order):
            raise HTTPException(409, 'file_order_download_entitlement_revoked')

    def validate_economic(db, order, refund, envelope):
        prepare(db, order, refund, envelope)
        return envelope['economic_plan']

    hooks = dict(Recovery=Recovery, prepare=prepare, apply=apply, blocked=blocked,
                 validate_economic=validate_economic, require_download=require_download)
    ns['refund_file_resources'] = hooks
    return hooks
