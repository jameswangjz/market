"""Verified multi-source refund manifests; never posts or changes buyer cash."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, event, select

from .refund_execution import canonical, digest
from .refund_execution_resources import strict_json
from .settlement_multi_cost_exception import install as install_multi_cost


def install(ns, completed_evidence, money, segment_values, shares):
    cost = install_multi_cost(ns, money)
    class Manifest(ns['Base']):
        __tablename__ = 'ordinary_refund_multi_source_manifests'
        settlement_id = Column(ForeignKey('settlements.id'), primary_key=True)
        refund_id = Column(ForeignKey('refunds.id'), nullable=False, unique=True)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)

    def immutable(*_):
        raise ValueError('multi-source refund manifest is immutable')

    event.listen(Manifest, 'before_update', immutable)
    event.listen(Manifest, 'before_delete', immutable)

    def fail(reason):
        raise HTTPException(409, reason)

    def source_budgets(db, refund, refs):
        """Reserve against shared single/VC baselines and every frozen multi plan.

        Multi capture must require this immutable manifest, so its amounts are
        counted once here whether or not core has already posted its reversals.
        Core still repeats the same shared budget checks atomically at capture.
        """
        Baseline = ns['settlement_finance']['Baseline']
        Settlement = ns['Settlement']
        own = db.scalars(select(Baseline).join(Settlement, Settlement.id == Baseline.settlement_id).where(
            Settlement.refund_id == refund.id, Baseline.revenue < 0)).all()
        if len(own) > 1:
            fail('multi_source_duplicate_refund_capture')
        reserved = []
        for row in db.scalars(select(Manifest).where(Manifest.refund_id != refund.id)):
            payload = strict_json(row.payload_json)
            if row.payload_hash != digest(payload) or row.refund_id != payload['refund_id']:
                fail('multi_source_reserved_manifest_invalid')
            if db.get(Baseline, row.settlement_id) is None:
                reserved.extend(payload['references'])
        for ref in refs:
            source = db.get(Baseline, ref['reference_settlement_id'])
            if not source or source.snapshot_hash != ref['baseline_hash']:
                fail('multi_source_budget_baseline_mismatch')
            used = dict(gross_amount=Decimal(0), cost_amount=Decimal(0), profit_amount=Decimal(0))
            used_shares = {k: Decimal(0) for k in shares}
            hook = ns['settlement_finance'].get('source_reversals')
            if not callable(hook):
                fail('multi_source_shared_budget_contract_required')
            for record in hook(db, source.settlement_id):
                for field in used:
                    used[field] += abs(Decimal(record[field]))
                for k in shares:
                    used_shares[k] += abs(Decimal(record['shares'][k]))
            if own:
                captured = strict_json(own[0].snapshot_json)
                if digest(captured) != own[0].snapshot_hash:
                    fail('multi_source_current_capture_hash_mismatch')
                current = [r for r in captured.get('references', [])
                           if r.get('reference_settlement_id') == source.settlement_id]
                if len(current) != 1:
                    fail('multi_source_current_capture_source_mismatch')
                actual, normal = current[0], current[0].get('default_basis', current[0])
                for field in used:
                    used[field] -= abs(Decimal(actual[field] if field != 'profit_amount' else normal[field]))
                for k in shares:
                    used_shares[k] -= abs(Decimal(normal['shares'][k]))
                if any(v < 0 for v in [*used.values(), *used_shares.values()]):
                    fail('multi_source_current_capture_budget_mismatch')
            for r in reserved + [ref]:
                if r['reference_settlement_id'] != source.settlement_id:
                    continue
                if r['baseline_hash'] != source.snapshot_hash:
                    fail('multi_source_reserved_baseline_mismatch')
                normal = r.get('default_basis', r)
                used['gross_amount'] += money(-Decimal(r['gross_amount']))
                used['cost_amount'] += money(-Decimal(r['cost_amount']))
                used['profit_amount'] += money(-Decimal(normal['profit_amount']))
                for k in shares:
                    used_shares[k] += money(-Decimal(normal['shares'][k]))
            source_shares = strict_json(source.snapshot_json)['shares']
            if (used['gross_amount'] > source.revenue or used['cost_amount'] > source.cost
                    or used['profit_amount'] > source.profit
                    or any(used_shares[k] > Decimal(source_shares[k]) for k in shares)):
                fail(dict(segment_id=ref['segment_id'], reason='multi_source_reversal_exceeds_final_baseline'))

    def raw_multi_basis(db, refund, plan=None, *, apply_exception=False):
        try:
            refund, verified, evidence, _ = completed_evidence(db, refund)
            if plan is None:
                plan = verified
            if verified != plan:
                fail('multi_source_completion_plan_mismatch')
            segments = plan['segments']
            if len(segments) < 2 or len({p['segment_id'] for p in segments}) != len(segments):
                fail('unique_multi_source_segments_required')
            if any(p['source_order_id'] != refund.order_id or
                   p['calculation_version'] != 'economic-segment-refund-v1' for p in segments):
                fail('multi_source_verified_revised_segments_required')
            # Completion time orders historical economic consumption, not the
            # timing/order of later financial posting. Equal times are ambiguous.
            Completion = ns['refund_execution']['Completion']
            current = db.get(Completion, refund.id)
            prior = {p['segment_id']: Decimal(0) for p in segments}
            prior_proofs = {p['segment_id']: [] for p in segments}
            rows = db.scalars(select(ns['Refund']).join(Completion, Completion.refund_id == ns['Refund'].id).where(
                ns['Refund'].order_id == refund.order_id, ns['Refund'].id != refund.id,
                Completion.completed_at <= current.completed_at).order_by(Completion.completed_at, ns['Refund'].id)).all()
            for older in rows:
                _, older_plan, older_evidence, _ = completed_evidence(db, older)
                old_segments = older_plan.get('segments', [older_plan])
                for p in old_segments:
                    if p['segment_id'] not in prior:
                        continue
                    if db.get(Completion, older.id).completed_at == current.completed_at:
                        fail('multi_source_completion_order_ambiguous')
                    expected = next(s for s in segments if s['segment_id'] == p['segment_id'])
                    if any(p.get(k) != expected.get(k) for k in ('source_order_id', 'window_hash', 'receipt_hash', 'change_id')):
                        fail('multi_source_prior_segment_identity_mismatch')
                    segment_values(p)
                    if money(p['cumulative_refund_amount']) - money(p['refund_amount']) != prior[p['segment_id']]:
                        fail('multi_source_prior_refund_history_gap')
                    prior[p['segment_id']] += money(p['refund_amount'])
                    prior_proofs[p['segment_id']].append(dict(refund_id=older.id,
                        segment_plan_hash=digest(p), completion_evidence_hash=digest(strict_json(older_evidence)),
                        refund_amount=str(money(p['refund_amount'])),
                        cumulative_refund_amount=str(money(p['cumulative_refund_amount']))))
            refs = []
            for p in sorted(segments, key=lambda p: p['segment_id']):
                segment_values(p)
                amount, cumulative = money(p['refund_amount']), money(p['cumulative_refund_amount'])
                if cumulative - amount != prior[p['segment_id']]:
                    fail('multi_source_prior_refund_amount_mismatch')
                basis = ns['version_change_settlement']['refund_segment_basis'](db, refund, p)
                if basis.get('status') != 'ready':
                    fail(dict(segment_id=p['segment_id'], reason=basis.get('reason', 'segment_not_ready')))
                if (Decimal(basis['gross_amount']) != -amount or basis['currency'] != p['economic_snapshot']['currency']
                        or Decimal(basis['gross_amount']) - Decimal(basis['cost_amount']) != Decimal(basis['profit_amount'])
                        or sum(Decimal(basis['shares'][k]) for k in shares) != Decimal(basis['profit_amount'])):
                    fail('multi_source_segment_conservation_failed')
                refs.append(dict(basis, source_order_id=p['source_order_id'], segment_plan_hash=digest(p),
                    receipt_hash=p['receipt_hash'], prior_refund_amount=str(cumulative - amount),
                    prior_refunds=prior_proofs[p['segment_id']]))
            if len({r['reference_settlement_id'] for r in refs}) != len(refs):
                fail('multi_source_duplicate_baseline_binding')
            currencies = {r['currency'] for r in refs}
            if len(currencies) != 1:
                fail('multi_source_currency_mismatch')
            source_budgets(db, refund, refs)
            totals = {k: sum((Decimal(r[k]) for r in refs), Decimal(0))
                      for k in ('gross_amount', 'cost_amount', 'profit_amount')}
            if totals['gross_amount'] != -money(refund.amount):
                fail('multi_source_refund_total_mismatch')
            ordinary = dict(status='ready', schema_version=1, kind='multi_source_refund', refund_id=refund.id,
                order_id=refund.order_id, currency=refs[0]['currency'], reference_settlement_id='',
                references=refs, completion_evidence_hash=digest(strict_json(evidence)),
                completion_plan_hash=digest(plan), **{k: str(v) for k, v in totals.items()},
                shares={k: str(sum((Decimal(r['shares'][k]) for r in refs), Decimal(0))) for k in shares})
            funded = ns.get('settlement_funded_refund_capture', {}).get('segment_basis')
            if callable(funded):
                projected = funded(db, refund, apply_exception=apply_exception)
                if projected is not None:
                    return projected
            if not apply_exception:
                return ordinary
            parties = ns.get('settlement_parties', {})
            hook = parties.get('cost_exception_multi_basis')
            connection = db.connection()
            if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
                # Releasing the first SAVEPOINT must not commit the application
                # before the caller's later financial capture can roll it back.
                connection.exec_driver_sql('BEGIN')
            with db.begin_nested():
                proof = hook(db, refund, refs) if callable(hook) else None
                approved = parties.get('cost_exception')
                if proof is None and (not callable(approved) or approved(db, refund.id)):
                    fail('multi_source_cost_exception_plan_required')
                result = cost['apply'](db, refund, ordinary, proof)
                source_budgets(db, refund, result['references'])
                return result
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='multi_source_final_evidence_invalid')

    def financial_basis(db, refund, plan):
        return raw_multi_basis(db, refund, plan, apply_exception=True)

    def freeze(db, settlement, refund, basis, actor):
        payload = dict(basis, settlement_id=settlement.id)
        payload.pop('status')
        raw = canonical(payload)
        old = db.get(Manifest, settlement.id)
        if old:
            if old.refund_id != refund.id or old.payload_json != raw or old.payload_hash != digest(payload):
                fail('frozen_multi_source_manifest_mismatch')
        else:
            db.add(Manifest(settlement_id=settlement.id, refund_id=refund.id,
                            payload_json=raw, payload_hash=digest(payload), actor=str(actor)))
            db.flush()
        return dict(status='ready', **payload, manifest_hash=digest(payload))

    def capture_manifest(db, settlement):
        """Core capture contract: fresh validation plus byte-exact frozen evidence."""
        if not settlement.refund_id:
            return None
        try:
            refund = db.get(ns['Refund'], settlement.refund_id)
            _, plan, _, _ = completed_evidence(db, refund)
            if len(plan.get('segments', [plan])) < 2:
                return None
            result = financial_basis(db, refund, plan)
            if result['status'] != 'ready':
                return result
            manifest = db.get(Manifest, settlement.id)
            if manifest is None:
                fail('frozen_multi_source_manifest_required')
            expected = dict(result, settlement_id=settlement.id)
            expected.pop('status')
            if (manifest.refund_id != refund.id or manifest.payload_json != canonical(expected)
                    or manifest.payload_hash != digest(expected) or settlement.reference_settlement_id
                    or settlement.order_id != refund.order_id or not settlement.is_refund
                    or any(Decimal(getattr(settlement, k)) != Decimal(result[k])
                           for k in ('gross_amount', 'cost_amount', 'profit_amount'))
                    or any(Decimal(getattr(settlement, k)) != Decimal(result['shares'][k]) for k in shares)):
                fail('frozen_multi_source_manifest_mismatch')
            return dict(status='ready', **expected, manifest_hash=manifest.payload_hash)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='multi_source_final_evidence_invalid')

    hooks = dict(Model=Manifest, financial_basis=financial_basis, raw_multi_basis=raw_multi_basis,
                 freeze=freeze, capture_manifest=capture_manifest)
    ns['refund_multi_source'] = hooks
    return hooks
