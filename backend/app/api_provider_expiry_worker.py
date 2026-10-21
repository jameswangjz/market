"""Bounded provider expiry sweep; lifecycle owns durable retry accounting."""
import importlib
import logging
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .refund_execution import RUNNABLE


logger = logging.getLogger(__name__)


def blocked(db, body, *, ns):
    enterprise, product = body['enterprise_id'], body['product_id']
    combo = ns['subscription_terms']['lock_combination'](db, enterprise, product)
    if (combo is None or combo.id != body['combination_id']
            or combo.generation != body['generation'] or combo.status != 'active'):
        return True
    try:
        ns['api_auto_delivery']['assert_no_pending'](db, enterprise, product)
        ns['version_change_saga']['assert_no_pending'](db, enterprise, product)
    except HTTPException:
        return True
    Execution = ns['refund_execution']['Execution']
    Refund, Order = ns['Refund'], ns['Order']
    return db.scalar(select(Execution.id).join(Refund, Refund.id == Execution.refund_id)
        .join(Order, Order.id == Refund.order_id).where(
            Order.buyer_enterprise_id == enterprise, Order.product_id == product,
            Execution.status.in_((*RUNNABLE, 'failed'))).limit(1)) is not None


def run(*, ns=None, monotonic=time.monotonic, sleep=time.sleep,
        now=lambda: datetime.now(timezone.utc)):
    """Commit each stage, reservation and dispatch, including failed dispatches.

    The process budget is forty seconds between ticks. Kubernetes also bounds
    in-flight network work with the job's active deadline.
    """
    ns = ns if ns is not None else vars(importlib.import_module('app.main'))
    deadline = monotonic() + 40
    immediate = 0
    while monotonic() < deadline:
        try:
            with ns['SessionLocal']() as db:
                results = ns['api_provider_lifecycle']['expire_once'](
                    db, blocked=lambda db, body: blocked(db, body, ns=ns), at=now())
                db.commit()
        except Exception:
            logger.error('Provider expiry sweep failed')
            return 1
        if not results or all(row['status'] in {'needs_manual', 'verified', 'blocked'}
                              for row in results):
            return 0
        # Stage and reserve without delay; a dispatch failure carries attempts.
        if (immediate < 2 and any(row['status'] in {'prepared', 'reserved'}
                                 and 'attempts' not in row for row in results)):
            immediate += 1
            continue
        immediate = 0
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(10, remaining))
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run()
    except Exception:
        logger.error('Provider expiry worker initialization failed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
