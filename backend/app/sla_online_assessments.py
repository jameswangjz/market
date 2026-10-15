"""Order-scoped monthly assessments. Trusted sources are explicit, never inferred.

measure_order(db, order, contract, period_start, period_end, as_of) returns
measurements: dict[str, MetricResult], source_available: dict[str, bool],
source_version: str and evidence_digest: str. Caller owns transaction/commit.
"""
from dataclasses import asdict
from datetime import timezone
import hashlib
import json
import re
import secrets

from fastapi import HTTPException
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, or_, select
from sqlalchemy.orm import mapped_column

from .sla_metrics import BEIJING, DeliveryFact, Threshold, month_bounds
from .sla_online_facts import aggregate_deliveries
from .sla_order_assessment import _number, _snapshot, assess_order_metric


METRICS = ('availability', 'request_error_rate', 'latency_p95', 'delivery_timeliness')
ONLINE = ('file', 'api', 'model_api', 'tenant_access')
FUNDING = ('upgrade', 'downgrade', 'version_change_payment')


def delivery_duration(snapshot, contract, enterprise_id, at):
    result = dict(metric='delivery_duration', unit='seconds', value=None, sample_count=0,
                  status='insufficient_data', reason='missing_frozen_delivery')
    if contract is None:
        return result
    try:
        frozen, _ = _snapshot(snapshot, enterprise_id, 'delivery_timeliness')
        rule = frozen['metrics']['delivery']
        target, warning = rule['target_seconds'], rule['warning_seconds']
        if (rule['clock'] != 'natural' or rule['direction'] != 'upper'
                or not _number(target) or not _number(warning) or target <= 0 or warning <= 0):
            raise ValueError('Invalid delivery duration rule')
        bound = Threshold(target, warning, 'upper')
        paid, verified = utc(contract.paid_at), utc(contract.verified_at)
        deadline = utc(contract.deadline_at)
        if (not contract.delivery_eligible or paid is None or deadline is None
                or abs((deadline - paid).total_seconds() - target) > 0.000001):
            raise ValueError('Invalid frozen delivery deadline')
        stop = verified if verified is not None and verified <= at else at
        elapsed = (stop - paid).total_seconds()
        if elapsed < 0:
            raise ValueError('Invalid delivery chronology')
        result.update(value=elapsed, sample_count=1, target=target, warning=warning,
                      status=bound.classify(elapsed, sufficient=True), reason='frozen_order_deadline',
                      frozen_tier=frozen['service_level_code'], rule_version=frozen['rule_version'],
                      paid_at=paid.isoformat(), deadline_at=deadline.isoformat(),
                      verified_at=verified.isoformat() if verified is not None and verified <= at else None)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        result['reason'] = 'invalid_frozen_delivery'
    return result


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def install(ns):
    Base, Order = ns['Base'], ns['Order']
    Contract = ns['order_sla_contracts']['Contract']

    class OnlineAssessment(Base):
        __tablename__ = 'sla_online_assessments'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        order_id = mapped_column(ForeignKey('orders.id'), nullable=False, index=True)
        contract_id = mapped_column(ForeignKey('order_sla_contracts.id'), nullable=True)
        snapshot_json = mapped_column(Text, nullable=True)
        period_start = mapped_column(DateTime(timezone=True), nullable=False)
        period_end = mapped_column(DateTime(timezone=True), nullable=False)
        calculated_at = mapped_column(DateTime(timezone=True), nullable=False)
        version = mapped_column(Integer, nullable=False, default=1)
        input_hash = mapped_column(String(64), nullable=False)
        metrics_json = mapped_column(Text, nullable=False)
        source_json = mapped_column(Text, nullable=False)
        status = mapped_column(String(30), nullable=False)
        __table_args__ = (UniqueConstraint('order_id', 'period_start', name='uq_sla_order_month'),)

    Assessment = OnlineAssessment

    def output(db, row):
        order = db.get(Order, row.order_id)
        metrics = json.loads(row.metrics_json)
        def value(name):
            return metrics.get(name, {}).get('value')
        requests = metrics.get('request_error_rate', {})
        delivery = metrics.get('delivery_timeliness', {})
        source = json.loads(row.source_json)
        source_version = source.get('source_version', '')
        digest = source.get('evidence_digest', '')
        return {'id': row.id, 'order_id': row.order_id, 'contract_id': row.contract_id,
            'order_no': order.order_no if order else '',
            'product_id': order.product_id if order else '', 'buyer_enterprise_id': order.buyer_enterprise_id if order else '',
            'profile_id': '', 'profile_name': order.order_no if order else row.order_id,
            'period_start': row.period_start, 'period_end': row.period_end, 'calculated_at': row.calculated_at,
            'as_of': min(utc(row.calculated_at), utc(row.period_end)),
            'status': row.status, 'version': row.version, 'legacy': False,
            'frozen_tier': metrics.get('availability', {}).get('frozen_tier'),
            'rule_version': metrics.get('availability', {}).get('rule_version'),
            'source_summary': {
                'source_version': source_version if isinstance(source_version, str) and re.fullmatch(r'[A-Za-z0-9_.:+-]{1,128}', source_version) else 'unidentified',
                'evidence_digest': digest if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) else '',
                'source_available': {metric: source.get('source_available', {}).get(metric) is True for metric in METRICS}},
            'sample_count': requests.get('sample_count', 0), 'success_count': None,
            'availability': value('availability'), 'error_rate': value('request_error_rate'),
            'avg_latency_ms': None, 'p95_latency_ms': value('latency_p95'),
            'delivery_count': delivery.get('sample_count', 0), 'delivery_on_time': None,
            'delivery_compliance': value('delivery_timeliness'), 'metrics': metrics,
            'breach_reason': '; '.join(name + ':' + result['reason'] for name, result in metrics.items()
                                     if result['status'] != 'met')}

    def deliveries(db, order, start, end, at):
        contracts = list(db.scalars(ns['order_sla_contracts']['evaluation_query'](
            period_start=start, period_end=end, product_id=order.product_id,
            buyer_enterprise_id=order.buyer_enterprise_id)))
        facts = [DeliveryFact(item.order_id, item.delivery_method, utc(item.paid_at), utc(item.deadline_at),
                              utc(item.verified_at)) for item in contracts]
        unknown = list(db.scalars(select(Order.id).outerjoin(Contract, Contract.order_id == Order.id).where(
            Order.product_id == order.product_id, Order.buyer_enterprise_id == order.buyer_enterprise_id,
            Order.created_at < end,
            Order.paid_amount > 0, Order.payment_status.in_(('paid', 'refunding', 'refunded')),
            ~Order.business_type.in_(FUNDING), Contract.id.is_(None),
            or_((Order.snapshot_version == 1) & Order.delivery_method_snapshot.in_(ONLINE),
                Order.business_type.in_(('purchase', 'renew')) & (Order.subscription_id != '')))))
        month = start.astimezone(BEIJING)
        # Without a frozen deadline, historical payments cannot safely be assigned
        # to or excluded from this deadline-month cohort. Never drop them as met.
        return aggregate_deliveries(facts, missing_frozen_facts=len(unknown), source_available=True,
            year=month.year, month=month.month, as_of=at)

    def evaluate(db, actor='system', *, year=None, month=None, at=None):
        at = utc(at or ns['now']())
        local = at.astimezone(BEIJING)
        if (year is None) != (month is None):
            raise HTTPException(422, 'Both year and month are required')
        if year is not None and (type(year) is not int or not 2000 <= year <= 9998
                                 or type(month) is not int or not 1 <= month <= 12):
            raise HTTPException(422, 'Invalid calendar year/month')
        start, end = month_bounds(local.year if year is None else year, local.month if month is None else month)
        if start > at:
            raise HTTPException(422, 'Cannot assess a future month')
        candidates = list(db.scalars(select(Order.id).outerjoin(Contract, Contract.order_id == Order.id).where(
            Order.paid_amount > 0, Order.payment_status.in_(('paid', 'refunding', 'refunded')),
            ~Order.business_type.in_(FUNDING),
            or_(Contract.id.is_not(None), (Order.snapshot_version == 1) & Order.delivery_method_snapshot.in_(ONLINE),
                Order.business_type.in_(('purchase', 'renew')) & (Order.subscription_id != '')),
            or_(Contract.paid_at < end, Contract.id.is_(None) & (Order.created_at < end))).order_by(Order.id)))
        result = []
        cohorts = {}
        for order_id in candidates:
            # Payments/delivery use this same first lock. Never lock Enterprise here.
            order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                              .execution_options(populate_existing=True))
            contract = ns['order_sla_contracts']['get_contract'](db, order_id)
            row = db.scalar(select(Assessment).where(Assessment.order_id == order_id, Assessment.period_start == start)
                            .execution_options(populate_existing=True))
            snapshot = row.snapshot_json if row else contract.snapshot_json if contract else None
            source = ns.get('sla_online_sources', {}).get('measure_order')
            collected = source(db, order, contract, start, end, min(at, end)) if source and contract else {}
            measurements = dict(collected.get('measurements', {}))
            available = dict(collected.get('source_available', {}))
            cohort = order.product_id, order.buyer_enterprise_id
            if cohort not in cohorts:
                cohorts[cohort] = deliveries(db, order, start, end, min(at, end))
            measurements['delivery_timeliness'] = cohorts[cohort]
            available['delivery_timeliness'] = contract is not None
            values = {metric: asdict(assess_order_metric(snapshot, metric, measurements.get(metric),
                buyer_enterprise_id=order.buyer_enterprise_id, source_available=available.get(metric) is True))
                for metric in METRICS}
            values['delivery_duration'] = delivery_duration(snapshot, contract, order.buyer_enterprise_id, min(at, end))
            for metric in METRICS[:3]:
                if contract is not None and contract.delivery_method == 'file':
                    values[metric].update(value=None, status='not_applicable', reason='file_delivery_only', sample_count=0)
                elif collected.get('not_started') is True:
                    values[metric].update(value=None, status='not_started', reason='service_not_started', sample_count=0)
            statuses = {value['status'] for value in values.values()} - {'not_applicable', 'not_started'}
            status = next((value for value in ('breached', 'insufficient_data', 'warning', 'met') if value in statuses),
                          'insufficient_data')
            evidence = {'source_version': collected.get('source_version', 'missing'),
                        'evidence_digest': collected.get('evidence_digest', ''), 'source_available': available}
            signature = hashlib.sha256(canonical([snapshot, values, evidence]).encode()).hexdigest()
            if row is None:
                row = Assessment(order_id=order_id, contract_id=contract.id if contract else None,
                    snapshot_json=snapshot, period_start=start, period_end=end, version=0)
                db.add(row)
            if row.input_hash != signature:
                previous = {'version': row.version, 'status': row.status}
                row.version += 1
                row.input_hash, row.metrics_json, row.source_json = signature, canonical(values), canonical(evidence)
                row.status, row.calculated_at = status, at
                db.flush()
                ns['audit'](db, actor, 'evaluate_order_sla', 'sla_online_assessment', row.id,
                    'Evaluate recorded sources against frozen order thresholds', category='sla', business_domain='sla',
                    order_id=order.id, before=previous, after={'version': row.version, 'status': status,
                                                            'input_hash': signature})
                duration = values['delivery_duration']
                if duration['status'] in {'warning', 'breached'}:
                    ns['notify_business_event'](db, 'Order delivery SLA ' + duration['status'],
                        order.order_no + ': frozen delivery deadline ' + duration['status'],
                        'sla_online_assessment', row.id,
                        event_key='order-delivery-duration:' + order.id + ':' + duration['status'],
                        category='system', platform_roles=('super_admin', 'platform_operator', 'delivery_monitor'))
                if status in {'warning', 'breached'} and previous['status'] != status:
                    ns['notify_business_event'](db, 'Order SLA ' + status,
                        order.order_no + ': frozen order SLA ' + status, 'sla_online_assessment', row.id,
                        event_key='order-sla:' + row.id + ':' + status, category='system',
                        platform_roles=('super_admin', 'platform_operator', 'delivery_monitor'))
            result.append(row)
        db.flush()
        return [output(db, row) for row in result]

    def results(db, limit=100):
        return [output(db, row) for row in db.scalars(select(Assessment).order_by(
            Assessment.calculated_at.desc(), Assessment.order_id, Assessment.period_start.desc()).limit(limit))]

    hooks = dict(Assessment=Assessment, evaluate=evaluate, results=results, output=output)
    ns['sla_online_assessments'] = hooks
    return hooks
