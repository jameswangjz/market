import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { canManageProviderSaas, canViewProductIntegration, createIntegrationController, integrationStatus } from '../src/productIntegration.js';

test('SaaS config download is gated and cannot fall through to the legacy provider download', () => {
  const source = readFileSync(new URL('../src/ProductIntegrationPanel.vue', import.meta.url), 'utf8');
  assert.match(source, /product\.product_type === 'saas' && canManageSaas/);
  assert.match(source, /v-else-if="product\.product_type !== 'saas' && provider &&/);
});

test('visibility follows real session roles; platform legacy membership cannot override platform scope', () => {
  assert.equal(canViewProductIntegration({ enterprise_role: 'super_admin' }), true);
  assert.equal(canViewProductIntegration({ platform_role: 'super_admin' }), true);
  assert.equal(canViewProductIntegration({ platform_role: 'quality_reviewer', enterprise_role: 'super_admin' }), false);
  assert.equal(canViewProductIntegration({ enterprise_role: 'member' }), false);
  assert.equal(canViewProductIntegration({ enterprise_memberships: [{ status: 'active', role: 'enterprise_admin' }] }), true);
  assert.equal(canViewProductIntegration({ enterprise_memberships: [{ status: 'disabled', role: 'enterprise_admin' }] }), false);
});

test('SaaS management requires an active verified enterprise admin membership for the owning enterprise', () => {
  const user = {
    is_active: true, activation_status: 'active', verified_status: 'verified',
    enterprise_memberships: [
      { enterprise_id: 'selected', status: 'active', role: 'enterprise_admin' },
      { enterprise_id: 'owned', status: 'active', role: 'enterprise_admin' },
      { enterprise_id: 'disabled', status: 'disabled', role: 'super_admin' },
    ],
  };
  const ownedProduct = { id: 'p', enterprise_id: 'owned', product_type: 'saas', status: 'pending_integration' };
  assert.equal(canManageProviderSaas(user, ownedProduct), true);
  assert.equal(canManageProviderSaas({ ...user, enterprise_memberships: [{ enterprise_id: 'owned', status: 'active', role: 'super_admin' }] }, { ...ownedProduct, status: 'published' }), true);
  assert.equal(canManageProviderSaas(user, { ...ownedProduct, enterprise_id: 'other', status: 'published' }), false);
  assert.equal(canManageProviderSaas(user, { ...ownedProduct, enterprise_id: undefined }), false);
  assert.equal(canManageProviderSaas(user, { ...ownedProduct, status: 'draft' }), false);
  assert.equal(canManageProviderSaas({ ...user, platform_role: 'super_admin' }, ownedProduct), false);
  assert.equal(canManageProviderSaas({ ...user, is_active: false }, ownedProduct), false);
  assert.equal(canManageProviderSaas({ ...user, activation_status: 'disabled' }, ownedProduct), false);
  assert.equal(canManageProviderSaas({ ...user, verified_status: 'pending' }, ownedProduct), false);
  assert.equal(canManageProviderSaas({ ...user, enterprise_memberships: [{ enterprise_id: 'owned', status: 'active', role: 'member' }] }, ownedProduct), false);
  assert.equal(canManageProviderSaas({ ...user, enterprise_memberships: user.enterprise_memberships.filter(membership => membership.enterprise_id !== 'owned') }, ownedProduct), false);
  assert.equal(canManageProviderSaas(user, { ...ownedProduct, enterprise_id: 'disabled' }), false);
});

function fixture() {
  const state = { rows: [], loading: false, busy: '' };
  const product = { id: 'p', status: 'pending_integration', versions: [{ id: 'v', version_code: 'v1' }] };
  const calls = [];
  const api = { get: async path => { calls.push(['get', path]); return { data: { item: { status: 'pending_integration' }, route: { route_key: 'p-v1' } } }; }, post: async path => { calls.push(['post', path]); } };
  let changes = 0;
  const controller = createIntegrationController({ state, api, product: () => product, changed: () => changes++ });
  return { state, product, api, calls, controller, changes: () => changes };
}
test('Chinese evidence labels fail closed', () => {
  assert.equal(integrationStatus(null), '未配置');
  assert.equal(integrationStatus({ status: 'failed' }), '验证失败');
  assert.equal(integrationStatus({ status: 'verified', healthy: false }), '未就绪');
  assert.equal(integrationStatus({ status: 'verified', healthy: true }), '验证通过');
});
test('load each saved release with per-release errors', async () => {
  const f = fixture();
  f.product.versions.push({ id: 'missing', version_code: 'v2' }, { version_code: 'unsaved' });
  const get = f.api.get;
  f.api.get = path => path.includes('/missing/') ? Promise.reject({ response: { data: { detail: '没有权限' } } }) : get(path);
  await f.controller.load();
  assert.equal(f.state.rows.length, 2);
  assert.equal(f.state.rows[1].error, '没有权限');
  assert.equal(f.state.loading, false);
});
test('successful verification reloads evidence and parent', async () => {
  const f = fixture();
  await f.controller.load();
  await f.controller.verify(f.state.rows[0]);
  assert.equal(f.calls.filter(x => x[0] === 'post').length, 1);
  assert.equal(f.changes(), 1);
  assert.equal(f.state.busy, '');
});
test('failed verification refreshes persisted report without publishing UI', async () => {
  const f = fixture();
  await f.controller.load();
  f.api.post = async () => { throw { response: { data: { detail: '后端不可达' } } }; };
  await f.controller.verify(f.state.rows[0]);
  assert.equal(f.state.rows[0].error, '后端不可达');
  assert.equal(f.changes(), 0);
});
test('review-stage and repeated clicks do not verify', async () => {
  const f = fixture();
  await f.controller.load();
  f.product.status = 'security_review';
  await f.controller.verify(f.state.rows[0]);
  f.product.status = 'pending_integration';
  f.state.busy = 'v';
  await f.controller.verify(f.state.rows[0]);
  assert.equal(f.calls.filter(x => x[0] === 'post').length, 0);
});
test('stale product read cannot replace current state', async () => {
  const f = fixture();
  let release;
  f.api.get = () => new Promise(resolve => { release = resolve; });
  const pending = f.controller.load();
  f.controller.dispose();
  release({ data: { item: { status: 'verified' } } });
  await pending;
  assert.equal(f.state.rows.length, 0);
});
