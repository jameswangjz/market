"""Reviewed native refund runtime composer and exact ownership guards."""
from datetime import timedelta
import hmac
import importlib
import json
import os
from pathlib import Path
import re
import time

import version_change_saga_runtime_acceptance as base

check = base.check
STREAM = 'market:apisix:charges:v1'
COVERAGE = 'market:apisix:charges:coverage:'
INTENT = 'market:apisix:refund:execution:v1:'
SLA_FIELDS = ('event_id', 'content_sha256', 'product_id', 'version_id', 'route_id')
SLA_NOT_TESTED = ['SLA metrics, completeness, attribution and window sealing']
FINANCIAL_TABLES = frozenset({
    'refunds', 'ordinary_refund_cases', 'ordinary_refund_events',
    'ordinary_refund_executions', 'ordinary_refund_payment_legs',
    'ordinary_refund_local_completions', 'refund_resource_recoveries',
    'refund_funding_allocations',
})


def exact_id(value):
    check(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value),
          'Expected an exact generated fixture ID')
    return value


def stream_position(value):
    check(isinstance(value, str) and re.fullmatch(r'(0|[1-9]\d{0,19})-(0|[1-9]\d{0,19})', value),
          'Invalid stream position')
    parts = tuple(map(int, value.split('-')))
    check(all(part <= 18446744073709551615 for part in parts), 'Stream position overflow')
    return parts


def quota_token(value):
    return value.replace('%', '%25').replace(':', '%3A')


def extra_keys(*, combination_id, generation, period_index, refund_id):
    """Derive a whitelist, never accept arbitrary caller-provided Redis keys."""
    exact_id(combination_id)
    exact_id(refund_id)
    check(type(generation) is int and generation == 1 and
          type(period_index) is int and period_index == 0,
          'Baseline requires exactly the original one-month period')
    period = f'{combination_id}:{generation}:{period_index}'
    return frozenset({COVERAGE + quota_token(combination_id) + ':' + quota_token(period),
                      INTENT + refund_id})


def validate_charge(event_id, values, *, start, end, order_id, term_id,
                    combination_id, route_key, source_run_id):
    """Prove one journaled event belongs to an exact bounded fixture period."""
    for value in (order_id, term_id, combination_id):
        exact_id(value)
    check(isinstance(route_key, str) and re.fullmatch(r'qa-vcs-[0-9a-f]{12}-api-source', route_key),
          'Unexpected fixture route')
    check(isinstance(source_run_id, str) and re.fullmatch(r'[0-9a-f]{40}', source_run_id),
          'Invalid charge source epoch')
    check(stream_position(start) < stream_position(event_id) <= stream_position(end),
          'Charge is outside the journaled call window')
    expected = dict(order_id=order_id, segment_id=term_id, scope=combination_id,
                    period_key=combination_id + ':1:0', route_key=route_key,
                    source_run_id=source_run_id, quantity='1', policy='gateway_actual_charge_v1')
    check(isinstance(values, dict) and set(values) == set(expected) | {'charged_at_us'}
          and all(values[key] == value for key, value in expected.items())
          and isinstance(values['charged_at_us'], str)
          and re.fullmatch(r'[1-9]\d{0,15}', values['charged_at_us']),
          'Charge is not an exact owned native event')
    return event_id


def prove_fk_edges(owned, edges):
    """Check extracted FK edges against exact PK tuples, without DB access.

    edges entries: (child_table, child_pk, parent_table, parent_pk). The DB
    collector must enumerate every FK on every selected row, resolving full
    parent PKs. A null parent_pk denotes a nullable FK. This function does not
    establish root ownership or completeness; retain guarded.owned_rows and
    base.expand_fk_ids before collecting edges.
    """
    for child, child_pk, parent, parent_pk in edges:
        check(child_pk in owned.get(child, set()), 'FK edge has an unowned child')
        if parent_pk is not None:
            check(parent_pk in owned.get(parent, set()), 'FK edge reaches an unowned parent')


def validate_refund_audit(row, *, refund_id, order_id, buyer_id, operator_id):
    """Validate a refund audit exception before unchanged base guard delegation."""
    for value in (refund_id, order_id):
        exact_id(value)
    check(isinstance(row, dict) and row.get('target_type') == 'refund'
          and row.get('target_id') == refund_id and row.get('order_id') == order_id
          and row.get('category') == 'refund' and row.get('business_domain') == 'refund'
          and isinstance(row.get('action'), str) and row['action'].startswith('ordinary_refund_')
          and row.get('actor') in {buyer_id + '@example.invalid',
                                   operator_id + '@example.invalid', 'refund-worker'},
          'Refund audit does not have exact local ownership')


def validate_financial_tables(names):
    check(set(names) <= FINANCIAL_TABLES, 'Unknown financial child requires explicit review')


def safe_failure(exc):
    import version_change_quote_acceptance as guarded
    result = base.safe_exception_metadata(exc)
    if isinstance(exc, (base.AcceptanceFailure, guarded.AcceptanceFailure)):
        detail = str(exc)
        if (re.fullmatch(r'[a-z][a-z0-9_]{0,95}', detail) and
                not re.search(r'(?i)(mk_|eyj|password|secret|bearer|authorization)', detail)):
            result['safe_detail'] = detail
        else:
            label, _, payload = detail.partition(':')
            if label in {'runtime_cleanup_failed', 'runtime_failed_cleanup_refused', 'runtime_failed'}:
                try:
                    failure = json.loads(payload)
                except ValueError:
                    failure = None
                if (isinstance(failure, dict) and set(failure) <= {
                        'stage', 'error_type', 'safe_detail', 'sqlstate', 'constraint_name', 'table_name'}
                        and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', v)
                                for v in failure.values()) and not any(re.search(
                                    r'(?i)(mk_|eyj|password|secret|bearer|authorization)', v)
                                    for v in failure.values())):
                    result.update(safe_detail=label, failure=failure)
    return result


def cleanup_with_predelete_retry(m, manifest, path):
    """At most three strict cleanup attempts, only for late pre-delete facts."""
    import version_change_quote_acceptance as guarded
    labels = {'retained_sla_set_mismatch', 'retained_sla_facts_changed'}
    for attempt in range(3):
        try:
            return cleanup(m, manifest, path)
        except (base.AcceptanceFailure, guarded.AcceptanceFailure) as exc:
            state = manifest['database_ids'].get('native_refund', {})
            if str(exc) not in labels or state.get('external_delete_phase') is True or attempt == 2:
                raise
            time.sleep(5)
            if state.get('external_delete_phase') is True:
                raise


def run(manifest_path=None, *, reviewed=False):
    check(reviewed is True, 'runtime_review_required')
    base.STAGE = 'preflight'
    m = base.load_main()
    ids = base.fixture_ids()
    manifest = ids
    path = Path(manifest_path or ('/tmp/' + ids['run_id'] + '.json'))
    base.preflight(m, manifest)
    manifest['environment'] = base.environment_fingerprint(m)
    manifest['authorized'] = True
    base.write_manifest(path, manifest, create=True)
    client = None
    cleanup_started = False
    try:
        import r3_native_refund_flow as flow
        from fastapi.testclient import TestClient
        base.STAGE = 'fixture'
        tokens = base.prepare(m, ids)
        client = TestClient(m.app)
        try:
            base.STAGE = 'flow'
            proof = flow.exercise(m, ids, path, manifest, client, tokens)
        finally:
            state = manifest['database_ids'].get('native_refund')
            if state is not None:
                state['local_flow_done'] = True
                base.write_manifest(path, manifest)
        cleanup_started = True
        base.STAGE = 'cleanup'
        counts = cleanup_with_predelete_retry(m, manifest, path)
        proof = dict(proof, retained_sla_count=counts['retained_sla_count'],
                     not_tested=SLA_NOT_TESTED)
        return dict(status='passed', proof=proof, cleanup=counts,
                    protected_preserved=True, manifest=str(path), manifest_mode='0600')
    except Exception as exc:
        failure = safe_failure(exc)
        if cleanup_started:
            raise base.AcceptanceFailure('runtime_cleanup_failed:' +
                json.dumps(failure, sort_keys=True)) from None
        # Unwound local calls may be cleaned only through the full same proof.
        try:
            base.STAGE = 'cleanup'
            cleanup_with_predelete_retry(m, manifest, path)
        except Exception as cleanup_exc:
            raise base.AcceptanceFailure('runtime_failed_cleanup_refused:' +
                json.dumps(safe_failure(cleanup_exc), sort_keys=True)) from None
        raise base.AcceptanceFailure('runtime_failed:' +
            json.dumps(failure, sort_keys=True)) from None
    finally:
        if client is not None:
            client.close()


def _roots(m, db, manifest, owned):
    from sqlalchemy import select
    ids = base.fixture_ids(manifest['run_id'])
    state = manifest['database_ids'].get('native_refund', {})
    check(not state or state.get('local_flow_done') is True, 'local_flow_active')
    def rows(model):
        return db.scalars(select(model).where(model.id.in_(
            [pk[0] for pk in owned.get(model.__tablename__, set())]))).all()
    orders, refunds = rows(m.Order), rows(m.Refund)
    check({r.id for r in orders} == set(state.get('order_ids', [])) and
          {r.id for r in refunds} == set(state.get('refund_ids', [])), 'native_roots_incomplete')
    for order in orders:
        exact_id(order.id)
        check(order.product_id == ids['products']['api'] and
              order.product_version_id == ids['versions']['api']['source'] and
              order.buyer_enterprise_id == ids['enterprises']['buyer'], 'native_order_mismatch')
    for refund in refunds:
        exact_id(refund.id)
        check(refund.order_id in state['order_ids'], 'native_refund_mismatch')
    terms = rows(m.subscription_terms['Term'])
    combos = rows(m.subscription_terms['Combination'])
    check(len(terms) <= 1 and len(combos) <= 1, 'unexpected_native_periods')
    for term in terms:
        exact_id(term.id)
        check(term.id == state.get('term_id') and term.order_id == state.get('order_id') and
              term.combination_id == state.get('combination_id') and term.generation == 1 and
              term.start_month == 0 and term.months == 1 and
              term.version_id == ids['versions']['api']['source'], 'native_term_mismatch')
    for combo in combos:
        check(combo.id == state.get('combination_id') and
              combo.product_id == ids['products']['api'] and
              combo.enterprise_id == ids['enterprises']['buyer'], 'native_combo_mismatch')
    check(not state.get('term_id') or len(terms) == len(combos) == 1, 'native_period_missing')
    Execution = m.refund_execution['Execution']
    executions = rows(Execution)
    for row in executions:
        check(row.refund_id in state.get('refund_ids', []), 'foreign_execution')
        if row.status not in {'completed', 'failed'}:
            check(row.lease_token is None and row.lease_expires_at is not None and
                  row.lease_expires_at >= m.now() + timedelta(minutes=5), 'execution_not_excluded')
    routes = {v for group in ids['routes'].values() for v in group.values()}
    for route in rows(m.ApiGatewayRoute):
        check(route.route_key in routes and route.product_id in ids['products'].values(),
              'unknown_database_route')
    credentials = rows(m.ApiCredential)
    check({r.id for r in credentials} == {c['id'] for c in manifest['consumers']},
          'credential_journal_incomplete')
    for credential in credentials:
        route = db.get(m.ApiGatewayRoute, credential.route_id)
        item = next(c for c in manifest['consumers'] if c['id'] == credential.id)
        check(credential.enterprise_id == ids['enterprises']['buyer'] and
              credential.order_id == '' and credential.apisix_consumer_name == item['name'] and
              route is not None and route.route_key in item['allowed_routes'],
              'native_credential_mismatch')
    for window in manifest['quota_windows']:
        check(window['kind'] == 'api' and window['scope'] == window['combination_id'] ==
              state.get('combination_id') and window['period'] == state['combination_id'] + ':1:0'
              and re.fullmatch(r'\d{4}-\d{2}-\d{2}', window['day']) and
              type(window['minute_start']) is int and window['minute_start'] >= 0,
              'foreign_quota_window')
    _protected(m, db, manifest)
    return executions


def _protected(m, db, manifest):
    from sqlalchemy import func, select
    check(db.scalar(select(func.count()).select_from(m.Order).where(
        (m.Order.id == 'ORD-261012005144E1B3') | (m.Order.order_no == 'ORD-261012005144E1B3')))
          == 1, 'protected_order_count_changed')
    protected = db.scalar(select(m.Order).where(
        (m.Order.id == 'ORD-261012005144E1B3') | (m.Order.order_no == 'ORD-261012005144E1B3')))
    check(protected is not None and base.protected_order_fingerprint(db, m, protected) ==
          manifest['protected_order_fingerprint'], 'protected_snapshot_changed')


def _redis_preflight(m, db, redis, manifest, executions, *, wait_for_cursor=True):
    state = manifest['database_ids'].get('native_refund', {})
    expected = set()
    combo = state.get('combination_id')
    if combo:
        exact_id(combo)
        expected.add(COVERAGE + quota_token(combo) + ':' + quota_token(combo + ':1:0'))
    for rid in state.get('refund_ids', []):
        exact_id(rid)
        expected.add(INTENT + rid)
    check(set(state.get('extra_keys', [])) == expected, 'foreign_extra_keys')
    check(set(manifest['external']['redis_keys']) == base.redis_key_journal(manifest),
          'original_redis_journal_changed')
    for key in expected:
        if key.startswith(COVERAGE):
            kind = redis.type(key)
            check(kind in {'none', 'hash'}, 'coverage_wrong_type')
            check(kind == 'none' or redis.hget(key, 'baseline') == '0',
                  'coverage_not_baseline_zero')
        else:
            value = redis.get(key)
            if value is None:
                continue
            rid = key[len(INTENT):]
            execution = next((r for r in executions if r.refund_id == rid), None)
            check(execution is not None, 'intent_execution_missing')
            plan, receipt = json.loads(execution.resource_plan_json), json.loads(value)
            check(plan['refund_id'] == rid and receipt.get('refund_id') == rid and
                  receipt.get('plan_hash') == plan['plan_hash'], 'foreign_refund_intent')
    events = []
    if 'stream_start' in state or 'stream_end' in state:
        start, end = state['stream_start'], state['stream_end']
        check(stream_position(start) <= stream_position(end), 'invalid_stream_bounds')
        entries = redis.xrange(STREAM, min='(' + start, max=end, count=201)
        check(len(entries) <= 200, 'stream_scan_bound_exceeded')
        for event_id, values in entries:
            if values.get('order_id') in state.get('order_ids', []):
                events.append(validate_charge(event_id, values, start=start, end=end,
                    order_id=state['order_id'], term_id=state['term_id'], combination_id=combo,
                    route_key=manifest['routes']['api']['source'], source_run_id=state['source_run_id']))
        missing = set(state.get('event_ids', [])) - set(events)
        for event_id in missing:
            record = state.get('validated_events', {}).get(event_id)
            check(state.get('external_delete_phase') is True and record is not None,
                  'foreign_journaled_events')
            validate_charge(event_id, record, start=start, end=end, order_id=state['order_id'],
                term_id=state['term_id'], combination_id=combo,
                route_key=manifest['routes']['api']['source'], source_run_id=state['source_run_id'])
            charge = db.get(m.api_charge_ledger['Charge'], event_id)
            check(charge is not None and charge.id == event_id and all(
                str(getattr(charge, key)) == value for key, value in record.items()),
                'missing_event_charge_unproven')
    else:
        check(not state.get('event_ids'), 'events_without_bounds')
    if events:
        check(redis.info('server')['run_id'] == state['source_run_id'] and
              redis.hget('market:apisix:charges:source', 'run_id') == state['source_run_id'],
              'charge_source_epoch_changed')
        deadline = time.monotonic() + 30
        while True:
            db.expire_all()
            cursor = db.get(m.api_charge_ledger['Cursor'], STREAM)
            if (cursor is not None and cursor.redis_run_id == state['source_run_id'] and
                    stream_position(cursor.last_id) >= max(map(stream_position, events))):
                break
            check(wait_for_cursor and time.monotonic() < deadline, 'ledger_cursor_not_drained')
            time.sleep(.25)
    return expected, events


def _sla_facts(m, db, manifest):
    from sqlalchemy import select
    ids = base.fixture_ids(manifest['run_id'])
    check(all(manifest[k] == ids[k] for k in ('products', 'versions', 'routes')),
          'sla_fixture_roots_changed')
    table = m.Base.metadata.tables.get('sla_gateway_terminal_events')
    if table is None:
        return []
    check(not table.foreign_keys and not any(
        fk.column.table is table for t in m.Base.metadata.tables.values() for fk in t.foreign_keys),
        'sla_fact_has_fk')
    records = [dict(row) for row in db.execute(select(*(table.c[k] for k in SLA_FIELDS)).where(
        table.c.product_id.in_(list(ids['products'].values()))).order_by(table.c.event_id)
        .limit(201)).mappings()]
    check(len(records) <= 200, 'retained_sla_bound_exceeded')
    for row in records:
        kind = next(k for k, product in ids['products'].items() if product == row['product_id'])
        side = next((s for s, version in ids['versions'][kind].items()
                     if version == row['version_id']), None)
        check(re.fullmatch(r'gw_[0-9a-f]{32}', row['event_id']) is not None and
              re.fullmatch(r'[0-9a-f]{64}', row['content_sha256']) is not None and
              side is not None and row['route_id'] == ids['routes'][kind][side],
              'retained_sla_ownership_mismatch')
    return records


def _journal_sla(m, db, manifest, path):
    state = manifest['database_ids'].get('native_refund')
    if state is None:
        check(not _sla_facts(m, db, manifest), 'sla_without_native_journal')
        return False
    current = _sla_facts(m, db, manifest)
    previous = state.get('retained_sla_facts')
    if previous == current:
        return False
    check(not state.get('external_delete_phase'), 'retained_sla_changed_after_delete_phase')
    check(previous is None or all(row in current for row in previous), 'retained_sla_fact_changed')
    # Prove live generated roots before granting a retained-fact exception.
    if current:
        import r3_native_refund_cleanup as database
        import version_change_quote_acceptance as guarded
        owned = guarded.owned_rows(m, db, database.scope(manifest))
        check(all((row['product_id'],) in owned.get(m.Product.__tablename__, set()) and
                  (row['version_id'],) in owned.get(m.ProductReleaseVersion.__tablename__, set())
                  for row in current), 'retained_sla_roots_unproven')
    state['retained_sla_facts'] = current
    base.write_manifest(path, manifest)
    return True


def _verify_sla(m, db, manifest):
    expected = manifest['database_ids'].get('native_refund', {}).get('retained_sla_facts', [])
    check(_sla_facts(m, db, manifest) == expected, 'retained_sla_facts_changed')


def _cleanup_result(manifest, counts):
    return dict(deleted_tablecounts=counts, retained_sla_count=len(
        manifest['database_ids'].get('native_refund', {}).get('retained_sla_facts', [])),
        not_tested=SLA_NOT_TESTED)


def _probe_identity(m, manifest, item):
    ids = base.fixture_ids(manifest['run_id'])
    check(isinstance(item, dict) and set(item) == {
        'name', 'route_id', 'product_id', 'version_id', 'route_key'}, 'invalid_probe_journal')
    exact_id(item['route_id'])
    check(any(item['product_id'] == ids['products'][kind] and
              item['version_id'] == ids['versions'][kind][side] and
              item['route_key'] == key for kind, group in ids['routes'].items()
              for side, key in group.items()), 'foreign_probe_identity')
    runtime = importlib.import_module(m.__package__ + '.sla_probe_runtime')
    check(item['name'] == runtime.probe_consumer_name(
        item['route_id'], item['product_id'], item['version_id']), 'foreign_probe_name')
    return runtime


def _probe_live(m, manifest, item):
    runtime = _probe_identity(m, manifest, item)
    live = base.apisix_admin(m, 'GET', '/consumers/' + item['name'], absent=True)
    if live is not None:
        key = runtime.probe_consumer_key(os.environ.get('SLA_PROBE_SECRET', ''),
            item['route_id'], item['product_id'], item['version_id'])
        plugins = live.get('plugins', {})
        auth = plugins.get('key-auth', {})
        check(live.get('username') == item['name'] and set(plugins) == {'key-auth'} and
              set(auth) == {'key'} and isinstance(auth['key'], str) and
              hmac.compare_digest(auth['key'], key), 'foreign_probe_consumer')
    return live


def _probe_preflight(m, db, manifest, owned):
    ids = base.fixture_ids(manifest['run_id'])
    items = []
    for pk in sorted(owned.get(m.ApiGatewayRoute.__tablename__, set())):
        route = db.get(m.ApiGatewayRoute, pk[0])
        pair = next(((kind, side) for kind, group in ids['routes'].items()
                     for side, key in group.items() if key == route.route_key), None)
        check(pair is not None, 'unknown_probe_route')
        kind, side = pair
        version = db.get(m.ProductReleaseVersion, ids['versions'][kind][side])
        check(route.product_id == ids['products'][kind] and version is not None and
              (version.id,) in owned.get(m.ProductReleaseVersion.__tablename__, set()) and
              version.product_id == route.product_id and version.version_code == route.version,
              'probe_version_ownership_mismatch')
        runtime = importlib.import_module(m.__package__ + '.sla_probe_runtime')
        item = dict(route_id=route.id, product_id=route.product_id, version_id=version.id,
                    route_key=route.route_key, name=runtime.probe_consumer_name(
                        route.id, route.product_id, version.id))
        _probe_live(m, manifest, item)
        items.append(item)
    state = manifest['database_ids'].get('native_refund', {})
    check(all(item in items for item in state.get('probe_consumers', [])), 'probe_journal_changed')
    return items


def cleanup(m, manifest, path, *, dry_run=False):
    import r3_native_refund_cleanup as database
    base.STAGE = 'cleanup'
    # Read through the original strict schema before write_manifest can normalize keys.
    check(base.read_manifest(path) == manifest, 'manifest_disk_mismatch')
    check(manifest['authorized'] is True and manifest['environment'] ==
          base.environment_fingerprint(m), 'cleanup_environment_unauthorized')
    ids = base.fixture_ids(manifest['run_id'])
    with m.SessionLocal() as db:
        _journal_sla(m, db, manifest, path)
        owned = database.collect(m, db, manifest)
        state = manifest['database_ids'].get('native_refund', {})
        if not owned and manifest['database_ids'].get('cleanup_rows'):
            _verify_finished(m, db, manifest)
            return _cleanup_result(manifest, {
                name: len(rows) for name, rows in manifest['database_ids']['cleanup_rows'].items()})
        executions = _roots(m, db, manifest, owned)
        redis = m.apisix_policy_redis()
        try:
            extra, events = _redis_preflight(m, db, redis, manifest, executions)
            if _journal_sla(m, db, manifest, path):
                owned = database.collect(m, db, manifest)
                executions = _roots(m, db, manifest, owned)
            # Drain before parent locks: ledger inserts need FK KEY SHARE locks.
            db.expire_all()
            locked = database.collect(m, db, manifest, lock=True)
            check(locked == owned, 'cleanup_graph_changed_under_locks')
            _verify_sla(m, db, manifest)
            executions = _roots(m, db, manifest, locked)
            rechecked = _redis_preflight(m, db, redis, manifest, executions, wait_for_cursor=False)
            check(rechecked == (extra, events), 'redis_ownership_changed_under_locks')
            probes = _probe_preflight(m, db, manifest, locked)
            route_owner = {key: ids['products'][kind] for kind, group in ids['routes'].items()
                           for key in group.values()}
            for key in manifest['external']['route_keys']:
                check(key in route_owner, 'unknown_external_route')
                live = base.apisix_admin(m, 'GET', '/routes/' + key, absent=True)
                check(live is None or live.get('labels', {}).get('market_product_id') ==
                      route_owner[key], 'foreign_external_route')
            for name in manifest['external']['consumer_names']:
                item = next((c for c in manifest['consumers'] if c['name'] == name), None)
                check(item is not None and set(item['allowed_routes']) <= set(route_owner),
                      'unknown_consumer_routes')
                live = base.apisix_admin(m, 'GET', '/consumers/' + name, absent=True)
                check(live is None or (live.get('username') == name and live.get('plugins', {}).get(
                    'market-gateway-quota', {}).get('route_key') in item['allowed_routes']),
                    'foreign_external_consumer')
            if dry_run:
                return _cleanup_result(manifest, {name: len(rows) for name, rows in owned.items()})
            _verify_sla(m, db, manifest)
            state = manifest['database_ids'].get('native_refund')
            if state is not None:
                state['probe_consumers'] = probes
                records = state.setdefault('validated_events', {})
                for event_id in events:
                    entry = redis.xrange(STREAM, min=event_id, max=event_id, count=1)
                    check(len(entry) == 1 and entry[0][0] == event_id, 'event_changed_before_journal')
                    validate_charge(event_id, entry[0][1], start=state['stream_start'],
                        end=state['stream_end'], order_id=state['order_id'], term_id=state['term_id'],
                        combination_id=state['combination_id'], route_key=ids['routes']['api']['source'],
                        source_run_id=state['source_run_id'])
                    records[event_id] = entry[0][1]
                state['event_ids'] = sorted(set(state['event_ids']) | set(events), key=stream_position)
                state['external_delete_phase'] = True
            manifest['database_ids']['cleanup_rows'] = {
                name: sorted(map(list, rows)) for name, rows in owned.items()}
            base.write_manifest(path, manifest)
            for kind, keys in (('routes', manifest['external']['route_keys']),
                               ('consumers', manifest['external']['consumer_names'])):
                for key in keys:
                    base.apisix_admin(m, 'DELETE', '/' + kind + '/' + key, absent=True)
                for key in keys:
                    check(base.apisix_admin(m, 'GET', '/' + kind + '/' + key, absent=True)
                          is None, 'external_identity_remains')
            for key in set(manifest['external']['redis_keys']) | extra:
                redis.delete(key)
            for item in probes:
                if _probe_live(m, manifest, item) is not None:
                    base.apisix_admin(m, 'DELETE', '/consumers/' + item['name'], absent=True)
                check(base.apisix_admin(m, 'GET', '/consumers/' + item['name'], absent=True)
                      is None, 'probe_consumer_remains')
            if events:
                redis.xdel(STREAM, *events)
            check(not any(redis.exists(key) for key in set(manifest['external']['redis_keys']) | extra),
                  'owned_redis_key_remains')
            check(all(not redis.xrange(STREAM, min=e, max=e, count=1) for e in events),
                  'owned_stream_event_remains')
        finally:
            redis.close()
    def journal(rows):
        check(rows == manifest['database_ids']['cleanup_rows'], 'cleanup_graph_changed')
        manifest['database_ids']['cleanup_rows'] = rows
        base.write_manifest(path, manifest)
    counts = database.cleanup(m, manifest, journal_callback=journal)
    with m.SessionLocal() as db:
        check(not database.collect(m, db, manifest), 'owned_graph_remains')
        _verify_finished(m, db, manifest)
    return _cleanup_result(manifest, counts)


def _verify_finished(m, db, manifest):
    from sqlalchemy import select, tuple_
    _verify_sla(m, db, manifest)
    check(manifest['database_ids'].get('cleanup_rows') is not None, 'missing_pk_journal')
    for name, rows in manifest['database_ids']['cleanup_rows'].items():
        table = m.Base.metadata.tables[name]
        check(not rows or db.execute(select(*table.primary_key.columns).where(
            tuple_(*table.primary_key.columns).in_([tuple(pk) for pk in rows]))).first() is None,
            'journaled_pk_remains')
    _protected(m, db, manifest)
    for kind, keys in (('routes', manifest['external']['route_keys']),
                       ('consumers', manifest['external']['consumer_names'])):
        for key in keys:
            check(base.apisix_admin(m, 'GET', '/' + kind + '/' + key, absent=True) is None,
                  'external_identity_remains')
    state = manifest['database_ids'].get('native_refund', {})
    for item in state.get('probe_consumers', []):
        _probe_identity(m, manifest, item)
        check(base.apisix_admin(m, 'GET', '/consumers/' + item['name'], absent=True) is None,
              'probe_consumer_remains')
    expected = set()
    if state.get('combination_id'):
        combo = exact_id(state['combination_id'])
        expected.add(COVERAGE + quota_token(combo) + ':' + quota_token(combo + ':1:0'))
    for rid in state.get('refund_ids', []):
        expected.add(INTENT + exact_id(rid))
    check(set(state.get('extra_keys', [])) == expected, 'foreign_extra_keys')
    redis = m.apisix_policy_redis()
    try:
        check(not any(redis.exists(k) for k in base.redis_key_journal(manifest) | expected),
              'owned_redis_key_remains')
        for event_id in state.get('event_ids', []):
            stream_position(event_id)
            check(not redis.xrange(STREAM, min=event_id, max=event_id, count=1),
                  'owned_stream_event_remains')
    finally:
        redis.close()
