"""Transactional, deduplicated alerts over recorded ticket clock projections."""
import secrets

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import mapped_column


def install(ns):
    Base = ns['Base']

    class TicketSlaAlert(Base):
        __tablename__ = 'ticket_sla_alerts'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        ticket_id = mapped_column(ForeignKey('after_sales_tickets.id'), nullable=False, index=True)
        metric = mapped_column(String(50), nullable=False)
        round = mapped_column(Integer, nullable=False)
        party = mapped_column(String(30), nullable=False)
        status = mapped_column(String(40), nullable=False)
        clock_id = mapped_column(ForeignKey('ticket_sla_clocks.id'), nullable=False)
        clock_version = mapped_column(Integer, nullable=False)
        created_at = mapped_column(DateTime(timezone=True), nullable=False)
        __table_args__ = (UniqueConstraint('ticket_id', 'metric', 'round', 'party', 'status',
                                          name='uq_ticket_sla_alert_threshold'),)

    def evaluate(db, ticket_id, *, at):
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError('SLA monitoring requires an aware evaluation timestamp')
        Ticket, Order = ns['AfterSalesTicket'], ns['Order']
        order_id = db.scalar(select(Ticket.order_id).where(Ticket.id == ticket_id))
        if order_id is None:
            return {'evaluated': 0, 'alerts': 0, 'reason': 'ticket_missing'}
        # Same lock ordering as HTTP actions and calendar materialization.
        order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                          .execution_options(populate_existing=True))
        ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                           .execution_options(populate_existing=True))
        if order is None or ticket is None:
            return {'evaluated': 0, 'alerts': 0, 'reason': 'ticket_missing'}
        if ticket.evidence_status != 'recorded':
            return {'evaluated': 0, 'alerts': 0, 'reason': 'historical_evidence_unknown'}
        result = ns['ticket_sla_clocks']['refresh'](db, ticket, at)
        added = 0
        for item in result['items']:
            if item['status'] not in {'warning', 'breached'}:
                continue
            existing = db.scalar(select(TicketSlaAlert.id).where(
                TicketSlaAlert.ticket_id == ticket.id, TicketSlaAlert.metric == item['metric'],
                TicketSlaAlert.round == item['round'], TicketSlaAlert.party == item['responsibility'],
                TicketSlaAlert.status == item['status']))
            if existing:
                continue
            alert = TicketSlaAlert(ticket_id=ticket.id, metric=item['metric'], round=item['round'],
                party=item['responsibility'], status=item['status'], clock_id=item['id'],
                clock_version=item['version'], created_at=at)
            db.add(alert)
            db.flush()
            ns['notify_business_event'](db,
                '售后SLA违约' if item['status'] == 'breached' else '售后SLA预警',
                f"售后工单的{item['metric']}第{item['round']}轮已触发SLA阈值，请查看工单时钟及源事件。",
                'after_sales_ticket', ticket.id, event_key='ticket-sla:' + alert.id,
                category='system', tenant_id=order.buyer_enterprise_id,
                enterprise_ids=list(dict.fromkeys(filter(None, [order.buyer_enterprise_id,
                                                               order.provider_enterprise_id]))),
                platform_roles=['super_admin', 'platform_operator', 'delivery_monitor'])
            ns['audit'](db, 'system', 'notify_ticket_sla_threshold', 'after_sales_ticket', ticket.id,
                'Recorded clock threshold alert', category='sla', business_domain='sla', order_id=order.id,
                after={'alert_id': alert.id, 'clock_id': item['id'], 'clock_version': item['version'],
                       'metric': item['metric'], 'round': item['round'], 'status': item['status']})
            added += 1
        db.flush()
        return {'evaluated': 1, 'alerts': added, 'reason': result.get('reason')}

    def page(db, *, after_id='', limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('SLA monitor batch size must be between 1 and 1000')
        Ticket = ns['AfterSalesTicket']
        return list(db.scalars(select(Ticket.id).where(Ticket.evidence_status == 'recorded',
            Ticket.id > after_id).order_by(Ticket.id).limit(limit)))

    return {'Alert': TicketSlaAlert, 'evaluate': evaluate, 'page': page}
