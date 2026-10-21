import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const canonical = readFileSync(new URL('../../docs/SaaS应用第三方接口对接规范.md', import.meta.url), 'utf8');

test('SaaS public download and internal standard match the provider standard', () => {
  for (const path of ['../public/saas-integration-standard.md', '../../docs/saas-integration-standard.md']) {
    assert.equal(readFileSync(new URL(path, import.meta.url), 'utf8'), canonical);
  }
});

test('provider sample preserves OIDC credentials but omits platform machine credentials', () => {
  const block = canonical.match(/```json\n([\s\S]*?)\n```/);
  assert.ok(block);
  const sample = JSON.parse(block[1]);
  assert.equal(sample.schema_version, '2.0');
  assert.equal(sample.oauth2_api, undefined);
  assert.equal(sample.resource_server.validation_method, 'introspection');
  for (const key of ['client_id', 'client_secret', 'token_url']) {
    assert.equal(sample.resource_server[key], undefined);
  }
  assert.ok(sample.oidc_sso.client_id);
  assert.ok(sample.oidc_sso.client_secret);
  assert.equal(sample.oidc_sso.code_challenge_method, 'S256');
  assert.deepEqual(sample.resource_server.scopes, ['saas.department', 'saas.tenant', 'saas.user']);
});
