"""Atomic multi-source refund capture and exact-source noncash offsets.

install(ns, core) uses finance's existing immutable models and transaction tools.
Capture never takes Batch/advisory locks or posts offsets. Explicit reconcile
does, with finance guard before Batch locks. All describe helpers are read-only.
"""
import hashlib
import json
from decimal import Decimal, ROUND_DOWN

from fastapi import HTTPException
from sqlalchemy import select

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
CENT = Decimal('.01')


def install(ns, core):
    Baseline, Position, Source, PositionSource = (core[k] for k in (
        'Baseline', 'Position', 'RefundSource', 'RefundPositionSource'))
    money, canonical, audit = (core[k] for k in ('money', 'canonical', 'audit'))

    def parsed(raw, expected):
        if hashlib.sha256(raw.encode()).hexdigest() != expected:
            raise HTTPException(409, 'multi_source_financial_evidence_hash_mismatch')
        return json.loads(raw)

    def source_record(db, row):
        record = parsed(row.evidence_json, row.evidence_hash)
        parent, source = db.get(Baseline, row.settlement_id), db.get(Baseline, row.source_settlement_id)
        snapshot = parsed(parent.snapshot_json, parent.snapshot_hash) if parent else {}
        matches = [r for r in snapshot.get('references', []) if r.get('segment_id') == row.segment_id]
        if (not source or source.snapshot_hash != row.source_hash or len(matches) != 1 or matches[0] != record
                or record.get('reference_settlement_id') != row.source_settlement_id
                or record.get('baseline_hash') != row.source_hash):
            raise HTTPException(409, 'multi_source_financial_mapping_mismatch')
        return record

    def source_reversals(db, source_id):
        records = []
        for b in db.scalars(select(Baseline).where(Baseline.reference_settlement_id == source_id, Baseline.revenue < 0)):
            snapshot = parsed(b.snapshot_json, b.snapshot_hash)
            exception = snapshot.get('cost_exception_application')
            normal = exception['default_basis'] if exception else dict(profit_amount=b.profit, shares=snapshot['shares'])
            records.append(dict(gross_amount=str(b.revenue), cost_amount=str(b.cost),
                profit_amount=str(normal['profit_amount']), shares=normal['shares'],
                entries=[e for e in snapshot['entries'] if e['kind'] != 'loss']))
        for row in db.scalars(select(Source).where(Source.source_settlement_id == source_id)):
            records.append(source_record(db, row))
        return records

    def check_source_budget(source, records, values):
        snapshot = parsed(source.snapshot_json, source.snapshot_hash)
        for field, limit in (('gross_amount', source.revenue), ('cost_amount', source.cost), ('profit_amount', source.profit)):
            value = money(values[field])
            if value > 0 or abs(value) + sum(abs(money(r[field])) for r in records) > limit:
                raise HTTPException(409, 'reversal_exceeds_original_baseline')
        for field in SHARES:
            value = money(values['shares'][field])
            if value > 0 or abs(value) + sum(abs(money(r['shares'][field])) for r in records) > money(snapshot['shares'][field]):
                raise HTTPException(409, 'reversal_exceeds_original_baseline')

    def reversal_entries(source, records, values):
        original = parsed(source.snapshot_json, source.snapshot_hash)['entries']
        previous_entries = [e for r in records for e in r['entries']]

        def key(e):
            return tuple(e[k] for k in ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name'))

        if len({key(e) for e in original}) != len(original):
            raise HTTPException(409, 'duplicate_original_subject_entries')
        result = []
        for kind, field in sorted({(e['kind'], e['share_kind']) for e in original}):
            if kind not in ('cost', 'profit') or (kind == 'profit' and field not in SHARES):
                raise HTTPException(409, 'unsupported_original_subject_entry')
            group = [e for e in original if (e['kind'], e['share_kind']) == (kind, field)]
            original_total = sum(money(e['amount'], True) for e in group)
            previous = {key(e): sum((money(p['amount']) for p in previous_entries if key(p) == key(e)), Decimal(0)) for e in group}
            delta = abs(money(values['cost_amount'] if kind == 'cost' else values['shares'][field]))
            target = sum(previous.values()) + delta
            if target > original_total:
                raise HTTPException(409, 'subject_reversal_exceeds_original_entry')
            raw = [money(e['amount']) * target / original_total for e in group]
            cumulative = [v.quantize(CENT, rounding=ROUND_DOWN) for v in raw]
            remainder = int((target - sum(cumulative)) / CENT)
            for i in sorted(range(len(group)), key=lambda i: (-(raw[i] - cumulative[i]), key(group[i])))[:remainder]:
                cumulative[i] += CENT
            for e, total in zip(group, cumulative):
                amount = total - previous[key(e)]
                if amount < 0 or total > money(e['amount']):
                    raise HTTPException(409, 'subject_rounding_requires_explicit_reversal_plan')
                if amount:
                    result.append(dict(e, amount=str(amount)))
        if sum((money(e['amount']) for e in result), Decimal(0)) != -money(values['gross_amount']):
            raise HTTPException(409, 'original_subject_mapping_incomplete')
        return result

    def capture(db, settlement, actor='system'):
        Settlement = ns['Settlement']
        settlement = db.scalar(select(Settlement).where(Settlement.id == settlement.id)
            .with_for_update().execution_options(populate_existing=True))
        if not settlement or settlement.status not in ('locked', 'paid'):
            raise HTTPException(409, 'locked_settlement_required')
        hook = ns.get('refund_settlement', {}).get('capture_manifest')
        if not callable(hook):
            return dict(status='waiting', reason='multi_source_manifest_contract_required')
        manifest = hook(db, settlement)
        if not manifest or manifest.get('status') != 'ready':
            return manifest or dict(status='waiting', reason='multi_source_manifest_required')
        payload = {k: v for k, v in manifest.items() if k not in ('status', 'manifest_hash')}
        manifest_hash = hashlib.sha256(canonical(payload).encode()).hexdigest()
        if (manifest.get('kind') != 'multi_source_refund' or manifest.get('manifest_hash') != manifest_hash
                or manifest.get('settlement_id') != settlement.id or manifest.get('order_id') != settlement.order_id
                or manifest.get('refund_id') != settlement.refund_id or settlement.reference_settlement_id):
            raise HTTPException(409, 'multi_source_manifest_identity_mismatch')
        refs = manifest['references']
        if (len(refs) < 2 or len({r['segment_id'] for r in refs}) != len(refs)
                or len({r['reference_settlement_id'] for r in refs}) != len(refs)):
            raise HTTPException(409, 'unique_multi_source_references_required')
        order = db.get(ns['Order'], settlement.order_id)
        if not order or not order.provider_enterprise_id:
            raise HTTPException(409, 'provider_required')
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            value = money(manifest[field])
            if value != money(getattr(settlement, field)) or value != sum(money(r[field]) for r in refs):
                raise HTTPException(409, 'multi_source_amount_conservation_failed')
        totals = {k: sum((money(r['shares'][k]) for r in refs), Decimal(0)) for k in SHARES}
        if (money(manifest['gross_amount']) >= 0
                or money(manifest['gross_amount']) - money(manifest['cost_amount']) != money(manifest['profit_amount'])
                or sum(totals.values()) != money(manifest['profit_amount'])
                or any(totals[k] != money(getattr(settlement, k)) or totals[k] != money(manifest['shares'][k]) for k in SHARES)):
            raise HTTPException(409, 'multi_source_amount_conservation_failed')
        old = db.get(Baseline, settlement.id)
        if old:
            snapshot = parsed(old.snapshot_json, old.snapshot_hash)
            rows = db.scalars(select(Source).where(Source.settlement_id == settlement.id)).all()
            if (snapshot.get('multi_source_manifest_hash') != manifest_hash or len(rows) != len(refs)
                    or {(r.segment_id, r.source_settlement_id, r.source_hash) for r in rows} !=
                       {(r['segment_id'], r['reference_settlement_id'], r['baseline_hash']) for r in refs}):
                raise HTTPException(409, 'frozen_multi_source_capture_mismatch')
            for row in rows:
                source_record(db, row)
            return old
        ids = sorted(r['reference_settlement_id'] for r in refs)
        sources = {b.settlement_id: b for b in db.scalars(select(Baseline).where(Baseline.settlement_id.in_(ids))
            .order_by(Baseline.settlement_id).with_for_update().execution_options(populate_existing=True))}
        if set(sources) != set(ids):
            raise HTTPException(409, 'exact_original_baseline_required')
        slices = []
        for ref in sorted(refs, key=lambda r: r['reference_settlement_id']):
            source = sources[ref['reference_settlement_id']]
            source_set = db.get(Settlement, source.settlement_id)
            if (source.snapshot_hash != ref['baseline_hash'] or source.revenue <= 0 or source.order_id != order.id
                    or source.provider_enterprise_id != order.provider_enterprise_id or source.currency != manifest['currency']
                    or ref['currency'] != source.currency or not source_set or source_set.status not in ('locked', 'paid')
                    or money(ref['gross_amount']) >= 0 or money(ref['gross_amount']) - money(ref['cost_amount']) != money(ref['profit_amount'])
                    or sum(money(ref['shares'][k]) for k in SHARES) != money(ref['profit_amount'])):
                raise HTTPException(409, 'multi_source_baseline_scope_mismatch')
            records = source_reversals(db, source.settlement_id)
            check_source_budget(source, records, ref)
            entries = reversal_entries(source, records, ref)
            legacy = settlement.status == 'paid' or any(p.blocked_reason == 'legacy_paid_without_allocations'
                for p in db.scalars(select(Position).where(Position.settlement_id == source.settlement_id)))
            if legacy:
                entries = [dict(e, blocked_reason='legacy_paid_without_allocations')
                           if e['share_kind'] != 'platform_fee' else e for e in entries]
            slices.append(dict(ref, entries=entries))
        snapshot = dict(settlement_no=settlement.settlement_no, order_no=order.order_no,
            provider_id=order.provider_enterprise_id, currency=manifest['currency'],
            revenue=manifest['gross_amount'], cost=manifest['cost_amount'], profit=manifest['profit_amount'],
            shares=manifest['shares'], reference_settlement_id='', references=slices,
            entries=[e for r in slices for e in r['entries']], actual_receipts=None, receipt_records=[],
            receipt_basis='verified_multi_source_final_baselines', bank_receipt_verified=False,
            blocked_reason='', multi_source_manifest_hash=manifest_hash, multi_source_manifest=payload)
        encoded = canonical(snapshot)
        with db.begin_nested():
            baseline = Baseline(settlement_id=settlement.id, order_id=order.id,
                provider_enterprise_id=order.provider_enterprise_id, reference_settlement_id='', currency=manifest['currency'],
                revenue=money(manifest['gross_amount']), cost=money(manifest['cost_amount']), profit=money(manifest['profit_amount']),
                snapshot_json=encoded, snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(), actor=str(actor))
            db.add(baseline)
            db.flush()
            for ref in slices:
                raw = canonical(ref)
                db.add(Source(settlement_id=settlement.id, segment_id=ref['segment_id'],
                    source_settlement_id=ref['reference_settlement_id'], source_hash=ref['baseline_hash'],
                    evidence_json=raw, evidence_hash=hashlib.sha256(raw.encode()).hexdigest()))
                for entry in ref['entries']:
                    position = Position(settlement_id=settlement.id, order_id=order.id, currency=manifest['currency'],
                        direction='internal' if entry['share_kind'] == 'platform_fee' else 'receivable',
                        **dict(entry, amount=money(entry['amount'], True)))
                    db.add(position)
                    db.flush()
                    db.add(PositionSource(position_id=position.id, settlement_id=settlement.id,
                        segment_id=ref['segment_id'], source_settlement_id=ref['reference_settlement_id']))
            audit(db, str(actor), 'finance_multi_source_refund_capture', settlement.id,
                  manifest_hash=manifest_hash, source_settlement_ids=ids, cash_recovered=False)
            db.flush()
        return baseline

    def describe(db, settlement_id):
        return [dict(segment_id=r.segment_id, reference_settlement_id=r.source_settlement_id,
                     baseline_hash=r.source_hash, evidence=source_record(db, r))
                for r in db.scalars(select(Source).where(Source.settlement_id == settlement_id).order_by(Source.segment_id))]

    def position_reference(db, position_id):
        row = db.get(PositionSource, position_id)
        return dict(segment_id=row.segment_id, reference_settlement_id=row.source_settlement_id) if row else None

    def reconcile_offsets(db, settlement_ids, actor='system'):
        core['lock_finance'](db)
        Settlement, Line, Batch = (ns[k] for k in ('Settlement', 'SettlementLine', 'SettlementBatch'))
        links = db.scalars(select(Source).where(Source.settlement_id.in_(settlement_ids) |
                                               Source.source_settlement_id.in_(settlement_ids))).all()
        ids = {r.settlement_id for r in links} | {r.source_settlement_id for r in links}
        memberships = db.execute(select(Line.settlement_id, Line.batch_id).where(
            Line.settlement_id.in_(ids), Line.status != 'superseded')).all()
        batch_ids = {bid for _, bid in memberships}
        states = {b.id: b.status for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids)).order_by(Batch.id)
            .with_for_update().execution_options(populate_existing=True))}
        active = {s.id for s in db.scalars(select(Settlement).where(Settlement.id.in_(ids)).order_by(Settlement.id)
            .with_for_update().execution_options(populate_existing=True)) if s.status in ('locked', 'paid')}

        def eligible(sid):
            owners = {bid for s, bid in memberships if s == sid}
            return sid in active and len(owners) == 1 and all(states.get(bid) in (
                'confirmed', 'payment_processing', 'partial_paid', 'paid') for bid in owners)

        positions = db.scalars(select(Position).where(Position.settlement_id.in_(ids)).order_by(Position.id)
            .with_for_update().execution_options(populate_existing=True)).all()
        mapped = {r.position_id: r for r in db.scalars(select(PositionSource).where(PositionSource.settlement_id.in_(ids)))}
        txids, totals = [], {}
        with db.begin_nested():
            for link in sorted(links, key=lambda r: (r.settlement_id, r.segment_id)):
                source_record(db, link)
                if not eligible(link.settlement_id) or not eligible(link.source_settlement_id):
                    continue
                for debt in positions:
                    mapping = mapped.get(debt.id)
                    if (debt.settlement_id != link.settlement_id or debt.direction != 'receivable' or not mapping
                            or mapping.segment_id != link.segment_id or mapping.source_settlement_id != link.source_settlement_id
                            or not debt.participant_id or core['cash_block'](db, debt)):
                        continue
                    for source in positions:
                        if (source.settlement_id != link.source_settlement_id or source.direction != 'payable'
                                or core['cash_block'](db, source) or any(getattr(source, k) != getattr(debt, k)
                                    for k in ('kind', 'share_kind', 'participant_type', 'participant_id', 'currency'))):
                            continue
                        value = min(core['balance'](db, debt), core['balance'](db, source))
                        if value <= 0:
                            continue
                        evidence = dict(allocations=[dict(position_id=debt.id, amount=str(value))],
                            counter_position_id=source.id, policy='exact_multi_source_unpaid_v1',
                            original_settlement_id=source.settlement_id, negative_settlement_id=debt.settlement_id,
                            segment_id=link.segment_id, source_evidence_hash=link.evidence_hash)
                        tx = core['Transaction'](kind='offset', mode='manual', reference='multi-source:' + debt.id,
                            note='Exact segment unpaid entitlement reversal; no cash recovery', creator_id=str(actor),
                            payload_json=canonical(evidence))
                        db.add(tx)
                        db.flush()
                        db.add(core['Confirmation'](transaction_id=tx.id, reviewer_id='baseline_policy', note='Exact source entitlement'))
                        db.flush()
                        db.add_all([core['Allocation'](transaction_id=tx.id, position_id=p.id, amount=value) for p in (source, debt)])
                        audit(db, str(actor), 'finance_multi_source_offset', tx.id, segment_id=link.segment_id,
                            original_settlement_id=source.settlement_id, negative_settlement_id=debt.settlement_id,
                            amount=str(value), currency=debt.currency, cash_recovered=False)
                        db.flush()
                        txids.append(tx.id)
                        totals[debt.currency] = totals.get(debt.currency, Decimal(0)) + value
        return dict(status='ready', offset_count=len(txids), transaction_ids=txids, amounts={k: str(v) for k, v in totals.items()})

    return dict(capture=capture, source_reversals=source_reversals, check_source_budget=check_source_budget,
                reversal_entries=reversal_entries, reconcile_offsets=reconcile_offsets,
                describe=describe, position_reference=position_reference)
