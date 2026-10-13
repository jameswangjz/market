import test from 'node:test';
import assert from 'node:assert/strict';
import { createReadinessController, readinessLabel } from '../src/productIntegration.js';

test('readiness ignores stale responses and clears on modal close', async () => {
  const state = {};
  let release;
  const api = { get: path => path.includes('old')
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve({ data: { product_id: 'next', readiness: [] } }) };
  const controller = createReadinessController({ state, api });
  const previous = controller.load('old');
  await controller.load('next');
  release({ data: { product_id: 'old' } });
  await previous;
  assert.equal(state.data.product_id, 'next');
  await controller.load(null);
  assert.equal(state.data, null);
  assert.equal(state.loading, false);
});

test('readiness never renders raw error detail or unknown reason values', async () => {
  const state = {};
  const controller = createReadinessController({ state, api: { get: async () => {
    throw { response: { data: { detail: 'Bearer private-token' } } };
  } } });
  await controller.load('p');
  assert.equal(state.error, '接入健康状态加载失败');
  assert.equal(readinessLabel('Bearer private-token'), '状态未知');
  assert.equal(readinessLabel('probe_timeout'), '健康检查超时');
  assert.equal(readinessLabel('integration_failed'), '接入验证失败');
});
