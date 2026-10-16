import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRefundSession } from '../src/refunds.js';

test('refund filters preserve enterprise/order scope and receive clamped page', async () => {
  const state = {}, calls = [];
  const context = { user: { id: 'admin', verified_status: 'verified', platform_role: 'super_admin' },
    params: { business_view: 'buyer', enterprise_id: 'owned', product: 'mine', provider: 'provider', buyer: 'buyer', order_no: 'ORD', status: 'requested' },
    order: { id: 'owned-order' } };
  const api = { get: async (...args) => { calls.push(args); return { data: { items: [], total: 0, page: 1 } }; } };
  const session = createRefundSession(api, state, () => context);
  await session.list(8, 50);
  assert.deepEqual(calls[0], ['/refunds', { params: { ...context.params, page: 8, page_size: 50, order_id: 'owned-order' } }]);
  assert.equal(state.page, 1);
  session.dispose();
});

test('refund list/filter components compile and expose required parties without cost', () => {
  for (const file of ['RefundCenter', 'RefundList']) {
    const source = readFileSync(new URL(`../src/${file}.vue`, import.meta.url), 'utf8');
    const { descriptor, errors } = parse(source);
    assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: file });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: file + '.vue', id: file,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
    for (const name of ['产品', '提供方', '购买方']) assert.ok(descriptor.template.content.includes(name));
    assert.doesNotMatch(descriptor.template.content, /cost|client_secret|economic_snapshot/);
  }
  const center = readFileSync(new URL('../src/RefundCenter.vue', import.meta.url), 'utf8');
  assert.match(center, /@submit\.prevent="search"/);
  assert.match(center, /session\.closeDetail\(\); go\(1\)/);
  assert.match(center, /PaginationControls/);
});
