"""Repair only SaaS-APP1's missing subscription versions, retaining source data."""
import json
import os
from decimal import Decimal

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
from app import main as m
from sqlalchemy import select

PRODUCT_ID = 'e101107c3f6414ab48b9ebc43de368e8'


def main():
    with m.SessionLocal() as db:
        product = db.scalar(select(m.Product).where(m.Product.id == PRODUCT_ID).with_for_update())
        if not product or product.name != 'SaaS-APP1' or product.product_type != 'saas' or product.status != 'published':
            raise RuntimeError('Product repair scope changed')
        generic = db.scalars(select(m.ProductReleaseVersion).where(
            m.ProductReleaseVersion.product_id == PRODUCT_ID).order_by(m.ProductReleaseVersion.id).with_for_update()).all()
        if len(generic) != 2:
            raise RuntimeError('Source version scope changed')
        snapshot = [(v.id, v.version_code, Decimal(v.price), Decimal(v.cost), v.status) for v in generic]
        before = db.scalars(select(m.SaaSProductVersion).where(m.SaaSProductVersion.product_id == PRODUCT_ID)).all()
        if before:
            raise RuntimeError('Subscription versions already exist; inspect instead of overwriting')
        m.trading_policy['sync_saas_versions'](db, product)
        db.flush()
        after = db.scalars(select(m.SaaSProductVersion).where(
            m.SaaSProductVersion.product_id == PRODUCT_ID).order_by(m.SaaSProductVersion.id)).all()
        result = [(v.id, v.version_code, Decimal(v.monthly_price), Decimal(v.cost), v.status) for v in after]
        if result != snapshot:
            raise RuntimeError('Subscription versions do not match registration values')
        if not m.storefront['versions_for'](db, product):
            raise RuntimeError('Product still unavailable; transaction rolled back')
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not admin or admin.platform_role != 'super_admin':
            raise RuntimeError('Platform audit actor unavailable')
        m.audit(db, admin.email, 'repair_saas_registration_versions', 'product', product.id,
                'Synchronize missing subscription versions from unchanged registration values',
                before={'saas_version_count': 0}, after={'saas_version_count': len(after),
                'version_ids': [v.id for v in after]})
        db.commit()
    print(json.dumps({'product': 'SaaS-APP1', 'product_id': PRODUCT_ID,
                      'versions_added': len(result), 'source_values_unchanged': True}))


if __name__ == '__main__':
    main()
