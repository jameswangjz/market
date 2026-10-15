"""Exact fixture-only database cleanup. No application import or remote I/O.

collect/validate accept an existing transaction. cleanup owns its transaction;
journal receives the JSON-safe exact PK map before any Core DELETE.
External FK exceptions: receipt/delivery pairs anchored to a proven Message or
fixture User (read-only opposite parent), and exact startup default
assignments referencing the read-only standard ServiceLevel catalog entry.
"""
import re

from sqlalchemy import delete, func, or_, select, text, tuple_

import version_change_saga_runtime_acceptance as base
import version_change_quote_acceptance as guarded

PROTECTED = 'ORD-261012005144E1B3'
ALLOWED = frozenset('''users enterprises memberships products product_release_versions
saas_product_versions orders order_state_logs payments refunds delivery_tasks
api_gateway_routes gateway_config_revisions gateway_publish_records api_credentials
oauth_clients subscription_combinations subscription_terms subscription_renewals
subscription_term_revisions version_change_requests version_change_funds
version_change_idempotency audit_logs platform_notifications notification_messages
notification_recipients notification_deliveries notification_preferences
notification_delivery_attempts notification_expired_urgent ordinary_refund_cases
ordinary_refund_events ordinary_refund_executions ordinary_refund_payment_legs
ordinary_refund_local_completions refund_resource_recoveries refund_funding_allocations
ordinary_refund_settlement_links refund_segment_receipts refund_segment_windows
refund_manual_assessments settlements settlement_measurements
api_financial_charges settlement_measurement_orders version_gateway_configs
order_sla_contracts api_usage application_health_observations application_health_slots
sla_online_assessments sla_assessment_evidence service_level_assignments'''.split())
TARGETS = dict(user='users', enterprise='enterprises', product='products',
    order='orders', delivery_task='delivery_tasks', refund='refunds', payment='payments',
    api_gateway_route='api_gateway_routes', gateway_config_revision='gateway_config_revisions',
    oauth_client='oauth_clients', api_credential='api_credentials',
    subscription_term='subscription_terms', version_change='version_change_requests',
    settlement='settlements', measurement='settlement_measurements',
    settlement_measurement='settlement_measurements', api_financial_charge='api_financial_charges',
    version_gateway_config='version_gateway_configs', order_sla_contract='order_sla_contracts',
    sla_online_assessment='sla_online_assessments')


def require(condition, label):
    if not condition:
        raise guarded.AcceptanceFailure(label)


def preflight_metadata(m):
    """Metadata labels only; unknown inventory never expands the allowlist."""
    present = set(m.Base.metadata.tables)
    require(all(re.fullmatch(r'[a-z][a-z0-9_]*', n) for n in present), 'invalid_table_label')
    return {'expected_present': sorted(ALLOWED & present),
            'expected_missing': sorted(ALLOWED - present),
            'unapproved_present': sorted(present - ALLOWED)}


def scope(manifest):
    require(isinstance(manifest.get('run_id'), str) and
            re.fullmatch(r'qa-vcs-[0-9a-f]{12}', manifest['run_id']), 'invalid_run_id')
    ids = base.fixture_ids(manifest['run_id'])
    require(all(manifest.get(k) == ids[k] for k in
                ('users', 'enterprises', 'products', 'versions')), 'fixture_ids_changed')
    return {'run_id': ids['run_id'], 'users': ids['users'],
        'enterprises': list(ids['enterprises'].values()),
        'products': {i: ids['products'][k] for i, k in enumerate(('api', 'model'))},
        'versions': {i: ids['versions'][k] for i, k in enumerate(('api', 'model'))},
        'request_ids': [ids['request_id']]}


def predicate(table, keys):
    return tuple_(*table.primary_key.columns).in_(list(keys))


def selected(db, table, owned):
    keys = owned.get(table.name, set())
    return db.execute(select(table).where(predicate(table, keys))).mappings().all() if keys else []


def retained_sla(m, db, manifest, routes):
    """Read-only proof of exact retained facts; routes may be a pre-delete proof."""
    fields = ('event_id', 'content_sha256', 'product_id', 'version_id', 'route_id')
    journal = manifest.get('database_ids', {}).get('native_refund', {}).get('retained_sla_facts', [])
    require(isinstance(journal, list) and len(journal) <= 2000, 'invalid_retained_sla_journal')
    require(all(isinstance(r, dict) and set(r) == set(fields) and
                all(isinstance(r[k], str) and r[k] for k in fields) for r in journal),
            'invalid_retained_sla_journal')
    expected = {tuple(r[k] for k in fields) for r in journal}
    require(len(expected) == len(journal), 'duplicate_retained_sla_fact')
    ids = base.fixture_ids(manifest['run_id'])
    versions = {v: (ids['products'][kind], ids['routes'][kind][side])
                for kind, group in ids['versions'].items() for side, v in group.items()}
    for row in journal:
        pair = versions.get(row['version_id'])
        require(pair is not None and pair[0] == row['product_id'] and
                re.fullmatch(r'[0-9a-f]{64}', row['content_sha256']), 'retained_sla_ownership_mismatch')
        require(row['route_id'] == pair[1] or any(
            r['id'] == row['route_id'] and r['product_id'] == pair[0] and r['route_key'] == pair[1]
            for r in routes), 'retained_sla_route_mismatch')
    table = m.Base.metadata.tables.get('sla_gateway_terminal_events')
    if table is None:
        require(not expected, 'retained_sla_table_missing')
        return
    require(not table.foreign_keys, 'retained_sla_has_fk')
    # A small synthetic schema can prove the original empty-facts guard too.
    require(set(fields) <= set(table.c.keys()) or not expected, 'retained_sla_schema_mismatch')
    query = select(table).where(table.c.product_id.in_(list(ids['products'].values()))).limit(2001)
    actual = db.execute(query).mappings().all()
    require(len(actual) <= 2000, 'retained_sla_row_bound')
    require(not actual or bool(expected), 'owned_sla_terminal_events_present')
    require({tuple(r[k] for k in fields) for r in actual} == expected, 'retained_sla_set_mismatch')


def system_message(row, ids, by_id, rows):
    kind, target, key = row['target_type'], row['target_id'], row['event_key']
    require(row['category'] == 'system', 'system_message_category_mismatch')
    if kind == 'product':
        match = re.fullmatch(r'application-health:' + re.escape(target) + r':([^:]+):(incident|recovered):[0-9]{14}', key)
        version_products = {v: ids['products'][i] for i, group in ids['versions'].items() for v in group.values()}
        require(target in ids['products'].values() and match is not None and
                version_products.get(match[1]) == target and row['tenant_id'] == ids['enterprises'][1],
                'health_message_mismatch')
    else:
        credential = by_id.get('api_credentials', {}).get(target)
        match = re.fullmatch(r'quota:' + re.escape(target) + r':month:([^:]+):([1-9][0-9]*):(0|[1-9][0-9]*):2', key)
        require(credential is not None and match is not None and
                credential['enterprise_id'] == row['tenant_id'] == ids['enterprises'][0], 'quota_message_mismatch')
        combo = by_id.get('subscription_combinations', {}).get(match[1])
        route = by_id.get('api_gateway_routes', {}).get(credential['route_id'])
        generation, index = int(match[2]), int(match[3])
        require(combo is not None and route is not None and
                combo['enterprise_id'] == ids['enterprises'][0] and combo['product_id'] == route['product_id'] and
                combo['generation'] == generation and index < combo['total_months'] and any(
                    t['combination_id'] == combo['id'] and t['generation'] == generation and
                    t['start_month'] <= index < t['start_month'] + t['months'] and
                    t['order_id'] in by_id.get('orders', {}) and
                    by_id['orders'][t['order_id']]['product_id'] == combo['product_id']
                    for t in rows.get('subscription_terms', [])), 'quota_period_mismatch')


def validate(m, db, manifest, owned, *, retained_routes=None):
    """Check exact parent PKs, except anchored inbox pairs/startup catalog FKs."""
    ids = scope(manifest)
    tables = m.Base.metadata.tables
    preflight_metadata(m)
    require(set(owned) <= set(tables), 'unknown_owned_table')
    unknown = set(owned) - ALLOWED
    require(not unknown, 'unknown_owned_table:' + ','.join(sorted(unknown)))
    require(sum(len(keys) for keys in owned.values()) <= 2000, 'row_bound_exceeded')
    rows = {name: selected(db, tables[name], owned) for name in owned}
    retained_sla(m, db, manifest, rows.get('api_gateway_routes', [])
                 if retained_routes is None else retained_routes)
    require(all(len(rows[n]) == len(keys) for n, keys in owned.items()), 'missing_owned_row')
    for row in rows.get('orders', []):
        require(row['id'] != PROTECTED and row['order_no'] != PROTECTED, 'protected_order_selected')
    by_id = {name: {r['id']: r for r in records} for name, records in rows.items()
             if 'id' in tables[name].c}
    for row in rows.get('service_level_assignments', []):
        require(row['enterprise_id'] in ids['enterprises'] and row['source'] == 'default' and
                row['created_by'] == 'system' and row['user_id'] == '', 'default_assignment_mismatch')
        level = tables.get('service_levels')
        require(level is not None and db.scalar(select(func.count()).select_from(level).where(
            level.c.id == row['service_level_id'], level.c.code == 'standard')) == 1,
            'default_assignment_catalog_mismatch')
    for name in ('application_health_observations', 'application_health_slots'):
        for row in rows.get(name, []):
            version = by_id.get('product_release_versions', {}).get(row['version_id'])
            require(version is not None and version['product_id'] == row['product_id'], 'health_version_mismatch')
    for row in rows.get('version_gateway_configs', []):
        version = by_id.get('product_release_versions', {}).get(row['version_id'])
        require(version is not None and version['product_id'] == row['product_id'], 'gateway_version_mismatch')
        for field, parent in (('route_id', 'api_gateway_routes'),
                              ('revision_id', 'gateway_config_revisions')):
            if row[field] is not None:
                linked = by_id.get(parent, {}).get(row[field])
                require(linked is not None and linked['product_id'] == row['product_id'], 'gateway_parent_mismatch')
                if field == 'revision_id':
                    require(linked['route_id'] == row['route_id'], 'gateway_revision_route_mismatch')
        if row['publish_record_id'] is not None:
            record = by_id.get('gateway_publish_records', {}).get(row['publish_record_id'])
            require(record is not None and record['route_id'] == row['route_id'] and
                    record['revision_id'] == row['revision_id'], 'gateway_publish_mismatch')
    for row in rows.get('order_sla_contracts', []):
        payment = by_id.get('payments', {}).get(row['source_payment_id'])
        require(payment is not None and payment['order_id'] == row['order_id'], 'sla_payment_mismatch')
        if row['source_delivery_task_id'] is not None:
            task = by_id.get('delivery_tasks', {}).get(row['source_delivery_task_id'])
            require(task is not None and task['order_id'] == row['order_id'], 'sla_delivery_mismatch')
    for row in rows.get('sla_online_assessments', []):
        contract = by_id.get('order_sla_contracts', {}).get(row['contract_id'])
        require(contract is not None and contract['order_id'] == row['order_id'],
                'sla_assessment_contract_mismatch')
    for row in rows.get('sla_assessment_evidence', []):
        assessment = by_id.get('sla_online_assessments', {}).get(row['assessment_id'])
        require(assessment is not None and assessment['order_id'] == row['order_id'],
                'sla_evidence_assessment_mismatch')
    targets = {kind: {pk[0] for pk in owned.get(name, set())} for kind, name in TARGETS.items()}
    # Native negative-settlement audits use the owned settlement's number.
    targets['settlement'].update(r['settlement_no'] for r in rows.get('settlements', []))
    emails = {uid + '@example.invalid' for uid in ids['users'].values()}
    tenants = set(ids['enterprises'])
    for row in rows.get('audit_logs', []):
        kind = row['target_type']
        export = (row['actor'] in emails and row['action'] == 'export_settlement_report' and
                  kind == 'settlement_report' and row['target_id'] == '' and
                  row.get('category') == 'settlement_report' and
                  row.get('business_domain') == 'settlement' and
                  row.get('order_id') == '' and row.get('tenant_id') == '')
        if export:
            continue  # FK validation below still covers this exact selected row.
        legacy_delivery = (row['actor'] in emails and row['action'] == 'create_delivery_task' and
                  kind == 'delivery_task' and row['target_id'] == '' and
                  row.get('category') == 'delivery' and row.get('business_domain') == 'delivery' and
                  row.get('order_id') in targets['order'])
        require(legacy_delivery or (kind in targets and row['target_id'] in targets[kind]), 'audit_target_mismatch')
        require(not row.get('order_id') or row['order_id'] in targets['order'], 'audit_order_mismatch')
        require(not row.get('tenant_id') or row['tenant_id'] in tenants, 'audit_tenant_mismatch')
        worker = (row['actor'] == 'refund-worker' and kind == 'refund' and
                  row['action'].startswith('ordinary_refund_') and row.get('category') == 'refund')
        financial = (row['actor'] in {'refund-execution-worker', 'system'} and
                     kind == 'settlement' and row['action'] == 'ordinary_refund_negative_settlement' and
                     row.get('category') == 'settlement_adjustment' and
                     row.get('order_id') in targets['order'])
        sla = (row['actor'] == 'system' and kind == 'order_sla_contract' and
               row['action'] in {'freeze_order_sla_contract', 'verify_order_sla_delivery'} and
               row.get('category') == 'sla' and
               row.get('order_id') == by_id['order_sla_contracts'][row['target_id']]['order_id'])
        assessment_audit = (row['actor'] in {'system', 'system:sla-online-monitor'} and kind == 'sla_online_assessment' and
               row['action'] == 'evaluate_order_sla' and row.get('category') == 'sla' and
               row.get('business_domain') == 'sla' and row.get('order_id') ==
               by_id['sla_online_assessments'][row['target_id']]['order_id'])
        require(row['actor'] in emails or worker or financial or sla or assessment_audit, 'audit_actor_mismatch')
    messages = set()
    for row in rows.get('notification_messages', []):
        kind, target = row['target_type'], row['target_id']
        if kind in {'product', 'api_credential'}:
            system_message(row, ids, by_id, rows)
            messages.add(row['id'])
            continue
        require(kind in {'order', 'delivery_task', 'refund'} and target in targets[kind]
                and row['tenant_id'] in tenants, 'message_target_mismatch')
        key = row['event_key']
        if kind == 'refund':
            events = {e['operation_key'] for e in rows.get('ordinary_refund_events', [])
                      if e['refund_id'] == target}
            executions = {e['id'] for e in rows.get('ordinary_refund_executions', [])
                          if e['refund_id'] == target}
            valid = key in {'ordinary-refund:' + e for e in events}
            valid |= any(key.startswith('refund-execution:' + e + ':') and
                         re.fullmatch(r'[^:]+:[0-9]+', key[len('refund-execution:' + e + ':'):])
                         for e in executions)
            require(row['category'] == 'refund' and valid, 'refund_event_key_mismatch')
        else:
            valid = key.startswith('business:' + kind + ':' + str(target) + ':')
            valid |= (kind == 'order' and key in {'subscription_term_granted:' + str(t['id'])
                      for t in rows.get('subscription_terms', []) if t['order_id'] == target})
            require(row['category'] == 'order' and valid, 'message_event_key_mismatch')
        messages.add(row['id'])
    for row in rows.get('platform_notifications', []):
        if row['target_type'] in {'product', 'api_credential'}:
            require(any(r.get('legacy_id') == row['id'] and r['user_id'] == row['recipient_user_id'] and
                any(msg['id'] == r['message_id'] and msg['id'] in messages and
                    msg['target_type'] == row['target_type'] and msg['target_id'] == row['target_id']
                    for msg in rows.get('notification_messages', []))
                for r in rows.get('notification_recipients', [])), 'legacy_system_message_unproven')
            continue
        require(row['target_type'] in {'order', 'delivery_task', 'refund'} and
                row['target_id'] in targets[row['target_type']], 'notification_target_mismatch')
    own_users = by_id.get('users', {})
    receipt_pairs = set()
    for row in rows.get('notification_recipients', []):
        require(row['message_id'] in messages or row['user_id'] in own_users, 'receipt_anchor_unproven')
        if row['message_id'] not in messages:
            require(own_users[row['user_id']]['email'] == row['user_id'] + '@example.invalid',
                    'receipt_fixture_address_mismatch')
        receipt_pairs.add((row['message_id'], row['user_id']))
    for row in rows.get('notification_deliveries', []):
        require((row['message_id'], row['user_id']) in receipt_pairs and
                (row['message_id'] in messages or row['user_id'] in own_users), 'delivery_receipt_unproven')
        if row['message_id'] not in messages:
            require(own_users[row['user_id']]['email'] == row['user_id'] + '@example.invalid',
                    'delivery_fixture_address_mismatch')
    for name, records in rows.items():
        table = tables[name]
        for constraint in table.foreign_key_constraints:
            edges = list(constraint.elements)
            parent = edges[0].column.table
            for row in records:
                values = [row[e.parent.name] for e in edges]
                if any(v is None for v in values):
                    continue
                if (name == 'service_level_assignments' and len(edges) == 1 and
                        edges[0].parent.name == 'service_level_id' and parent.name == 'service_levels' and
                        edges[0].column.name == 'id'):
                    continue  # Exact startup default and catalog existence proved above.
                if (name in {'notification_recipients', 'notification_deliveries'} and len(edges) == 1 and
                        edges[0].column.name == 'id' and
                        (edges[0].parent.name, parent.name) in {
                            ('user_id', 'users'), ('message_id', 'notification_messages')}):
                    # Both directions were anchored above; opposite parent is read-only.
                    require(db.scalar(select(func.count()).select_from(parent).where(
                        edges[0].column == values[0])) == 1, 'notification_parent_missing')
                    continue
                keys = {tuple(r) for r in db.execute(select(*parent.primary_key.columns).where(
                    *[e.column == value for e, value in zip(edges, values)]))}
                require(len(keys) == 1 and keys <= owned.get(parent.name, set()), 'external_fk_parent')
    return owned


def collect(m, db, manifest, lock=False):
    ids = scope(manifest)
    if lock:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
        for model, values in ((m.User, list(ids['users'].values())),
                              (m.Enterprise, ids['enterprises']),
                              (m.Product, list(ids['products'].values())),
                              (m.ProductReleaseVersion, [v for g in ids['versions'].values() for v in g.values()])):
            db.execute(select(model).where(model.id.in_(values)).order_by(model.id).with_for_update()).all()
        db.execute(select(m.Order).where(m.Order.product_id.in_(list(ids['products'].values())))
                   .order_by(m.Order.id).with_for_update()).all()
    try:
        owned = guarded.owned_rows(m, db, ids)
    except guarded.AcceptanceFailure:
        raise guarded.AcceptanceFailure('root_ownership_refused') from None
    return validate(m, db, manifest, owned)


def report(owned):
    return {'tablecounts': {n: len(k) for n, k in sorted(owned.items())},
            'cleanup_rows': {n: sorted(map(list, k)) for n, k in sorted(owned.items())}}


def cleanup(m, manifest, journal_callback=None, *, dry_run=False):
    """COLLECT/VALIDATE, or locked revalidation + journal + atomic DELETE.

    journal_callback(exact_pk_map) must durably persist before returning. No callback is
    required for DRYRUN. Callers supply environment-authorized base manifests.
    """
    scope(manifest)
    require(dry_run or (manifest.get('authorized') is True and callable(journal_callback)), 'delete_not_authorized')
    if not dry_run:
        require(manifest.get('environment') == base.environment_fingerprint(m), 'environment_mismatch')
    with m.SessionLocal() as db:
        with db.begin():
            owned = collect(m, db, manifest)
            if dry_run:
                return report(owned)
            protected_before = db.scalar(select(func.count()).select_from(m.Order).where(
                or_(m.Order.id == PROTECTED, m.Order.order_no == PROTECTED)))
            locked = collect(m, db, manifest, lock=True)
            require(locked == owned, 'graph_changed_under_locks')
            result = report(locked)
            journal_callback(result['cleanup_rows'])
            require(collect(m, db, manifest) == locked, 'graph_changed_after_journal')
            retained_routes = selected(db, m.ApiGatewayRoute.__table__, locked) if hasattr(m, 'ApiGatewayRoute') else []
            for table in reversed(m.Base.metadata.sorted_tables):
                if locked.get(table.name):
                    deleted = db.execute(delete(table).where(predicate(table, locked[table.name])))
                    require(deleted.rowcount == len(locked[table.name]), 'delete_count_mismatch')
            remaining = guarded.owned_rows(m, db, scope(manifest))
            require(not validate(m, db, manifest, remaining, retained_routes=retained_routes), 'owned_rows_remain')
            require(db.scalar(select(func.count()).select_from(m.Order).where(
                or_(m.Order.id == PROTECTED, m.Order.order_no == PROTECTED))) == protected_before,
                'protected_order_count_changed')
            return result['tablecounts']
