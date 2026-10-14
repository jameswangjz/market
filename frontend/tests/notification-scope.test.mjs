import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createRenderer, ref } from 'vue';
import { notificationScope, notificationScopeKey, notificationRequest, notificationUrl, isNotificationRequestCurrent } from '../src/notificationScope.js';
import { useNotificationStream } from '../src/useNotificationStream.js';

const enterprise = { id: 'company-a' };
const user = { verified_status: 'verified', enterprise_memberships: [{ enterprise_id: enterprise.id, status: 'active', role: 'enterprise_admin' }] };

test('notification scope uses the current enterprise and authorized mode, preserving global accounts', () => {
  assert.deepEqual(notificationScope(user, enterprise, 'provider'), { enterprise_id: 'company-a', business_view: 'provider' });
  assert.deepEqual(notificationScope(user, enterprise, 'buyer'), { enterprise_id: 'company-a', business_view: 'buyer' });
  assert.deepEqual(notificationScope({ ...user, verified_status: 'pending' }, enterprise, 'provider'), {});
  assert.deepEqual(notificationScope({ ...user, platform_role: 'super_admin' }, enterprise, 'buyer'), {});
  assert.deepEqual(notificationScope({ ...user, enterprise_memberships: [{ ...user.enterprise_memberships[0], role: 'member' }] }, enterprise, 'provider'), { enterprise_id: 'company-a' });
});

test('notification requests replace stale scope and preserve filters and sending payload', () => {
  const payload = { tenant_id: 'company-a', recipient_ids: ['same-company-user'] };
  const config = notificationRequest({ params: { folder: 'unread', enterprise_id: 'old', business_view: 'buyer' }, headers: { 'X-Enterprise-Id': 'old' }, data: payload }, notificationScope(user, enterprise, 'provider'));
  assert.deepEqual(config.params, { folder: 'unread', enterprise_id: 'company-a', business_view: 'provider' });
  assert.equal(config.headers['X-Enterprise-Id'], 'company-a');
  assert.equal(config.data, payload);
  notificationRequest(config, {});
  assert.deepEqual(config.params, { folder: 'unread' });
  assert.equal(config.headers['X-Enterprise-Id'], undefined);
  assert.equal(notificationUrl('/api/notifications/stream', {}), '/api/notifications/stream');
});

test('responses from a previous scope or an unmounted message center are rejected', () => {
  assert.equal(isNotificationRequestCurrent({ notificationScopeVersion: 1 }, 1), true);
  assert.equal(isNotificationRequestCurrent({ notificationScopeVersion: 1 }, 2), false);
  assert.equal(isNotificationRequestCurrent({ notificationScopeVersion: 1 }, 1, true), false);
});

test('a delayed console summary cannot overwrite the newly selected company or business view', async () => {
  const source = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  const pending = [];
  const state = vm.createContext({
    notificationSummaryVersion: 0, token: { value: 'token' },
    messageScope: { value: { enterprise_id: 'company-a', business_view: 'provider' } },
    notificationSummary: { value: {} }, notificationScopeKey,
    api: { get: (path, config) => new Promise(resolve => pending.push({ path, config, resolve })) },
  });
  vm.runInContext(source.slice(source.indexOf('async function loadNotificationSummary()'), source.indexOf('async function loadBuyerEnterprises()')), state);
  const old = state.loadNotificationSummary();
  state.messageScope.value = { enterprise_id: 'company-b', business_view: 'buyer' };
  const current = state.loadNotificationSummary();
  assert.equal(pending[1].config.params.enterprise_id, 'company-b');
  assert.equal(pending[1].config.params.business_view, 'buyer');
  pending[1].resolve({ data: { unread: 2 } });
  await current;
  pending[0].resolve({ data: { unread: 99 } });
  await old;
  assert.equal(state.notificationSummary.value.unread, 2);
});

test('SSE and fallback configuration restart with scope, aborting previous connections', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests = [];
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  globalThis.fetch = (url, options) => {
    requests.push({ url, ...options });
    return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
  };
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
  const scope = ref({ enterprise_id: 'company-a', business_view: 'provider' });
  const app = renderer.createApp({ setup() { useNotificationStream(() => {}, { token: ref('token'), scope }); return () => null; } });
  try {
    app.mount({});
    assert.equal(requests.length, 2);
    scope.value = { enterprise_id: 'company-b', business_view: 'buyer' };
    assert.equal(requests.length, 4);
    assert.ok(requests.slice(0, 2).every(request => request.signal.aborted));
    assert.ok(requests.slice(2).every(request => request.url.includes('enterprise_id=company-b&business_view=buyer')));
    scope.value = {};
    assert.deepEqual(requests.slice(4).map(request => request.url), ['/api/notifications/stream', '/api/notifications/sender-context']);
  } finally {
    app.unmount();
    await Promise.resolve();
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
