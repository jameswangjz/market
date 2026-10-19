import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');

test('users page does not preload current-enterprise management endpoints', () => {
  const block = source.slice(source.indexOf('if (view === "users") {'), source.indexOf('if (view === "settings") {'));
  for (const endpoint of ['members', 'departments', 'invitations']) {
    assert.ok(!block.includes(`"/enterprise/${endpoint}"`));
  }
  assert.ok(block.includes('loadIdentityList("users")'));
  assert.ok(block.includes('loadIdentityList("enterprises")'));
});

test('enterprise management loads explicit enterprise scope behind the permission guard', () => {
  const load = source.slice(source.indexOf('async function loadEnterpriseManagement()'), source.indexOf('function closeEnterpriseManagement()'));
  for (const endpoint of ['members', 'departments', 'invitations']) {
    assert.ok(load.includes(`api.get("/enterprise/${endpoint}", { params: { enterprise_id: enterpriseId } })`));
  }
  assert.match(load, /enterpriseManageReadOnly\.value = !canManageEnterprise\(item\)/);
  assert.match(load, /if \(!enterpriseManageReadOnly\.value\)\s*\{\s*try \{ await loadEnterpriseManagement\(\)/);
});
