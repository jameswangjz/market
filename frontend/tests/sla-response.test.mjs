import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { metricNames, slaTerms } from '../src/slaManagement.js';

test('matrix distinguishes normal and urgent response and support/natural clocks', () => {
  assert.equal(metricNames.first_response, '普通首次响应');
  assert.equal(metricNames.urgent_first_response, '紧急首次响应');
  assert.equal(slaTerms.support, '支持时间');
  assert.equal(slaTerms.natural, '自然时间');
  for (const name of ['SlaManagement.vue', 'SlaMatrixPanel.vue', 'SlaBindingsPanel.vue']) {
    const source = readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
    const { descriptor, errors } = parse(source, { filename: name });
    assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: name, id: name, compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
  }
});
