"""Dry-run by default: add telemetry labels without rewriting live route policies."""
import argparse
from copy import deepcopy
import json


def run(m, *, apply=False):
    from sqlalchemy import select
    from app.api_auto_delivery import install as install_api_delivery
    changes, skipped = [], []
    matcher = install_api_delivery(vars(m))['route_matches']
    with m.SessionLocal() as db:
        for route in db.scalars(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.status == 'active')):
            product = db.get(m.Product, route.product_id)
            if not product or product.status not in {'published', 'draft'}:
                continue
            expected = m.apisix_route_payload(route, product, db)
            try:
                actual = m.apisix_admin_request('GET', '/routes/' + route.route_key)
                actual = actual.get('node', actual)
                actual = actual.get('value', actual)
                baseline = deepcopy(expected)
                baseline['labels'] = {key: value for key, value in expected.get('labels', {}).items()
                    if key in {'market_managed', 'market_product_id', 'market_version'}}
                if not matcher(actual, baseline):
                    skipped.append({'route_id': route.id, 'reason': 'business_configuration_mismatch'})
                    continue
                labels = {**actual.get('labels', {}), **expected['labels']}
                if labels == actual.get('labels', {}):
                    continue
                changes.append({'route_id': route.id, 'route_key': route.route_key,
                                'before': actual.get('labels', {}), 'after': labels})
                if apply:
                    m.apisix_admin_request('PATCH', '/routes/' + route.route_key, {'labels': labels})
                    m.audit(db, 'system:sla-deployment', 'update_gateway_sla_labels', 'api_gateway_route', route.id,
                        'Add telemetry labels; retain live routing/authentication/quota configuration',
                        category='sla', business_domain='sla', before=changes[-1]['before'], after=labels)
            except Exception as exc:
                response = getattr(exc.__cause__, 'response', None)
                if response is not None and response.status_code == 404:
                    skipped.append({'route_id': route.id, 'reason': 'live_route_missing'})
                    continue
                # No raw Admin API response or credentials enter the report.
                raise RuntimeError('SLA label refresh failed for route ' + route.id
                    + ' (' + type(exc).__name__ + ')') from None
        if apply:
            db.commit()
    return {'applied': apply, 'changes': changes, 'skipped': skipped, 'business_configuration_rewritten': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from app import main as m
    print(json.dumps(run(m, apply=args.apply)))


if __name__ == '__main__':
    main()
