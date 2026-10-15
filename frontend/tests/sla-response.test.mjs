import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileTemplate } from '@vue/compiler-sfc';

const source = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');

test('service level presentation distinguishes normal support time and urgent natural time', () => {
  assert.match(source, /普通首响 \{\{ level.initial_response_minutes \}\} 分钟（支持时间）/);
  assert.match(source, /紧急首响 \{\{ level.urgent_initial_response_minutes \}\} 分钟（自然时间）/);
  const start = source.indexOf('class="service-level-cards"');
  const end = source.indexOf('class="service-level-config"', start);
  assert.ok(start >= 0 && end > start, 'service level cards must exist');
  const cards = source.slice(start, end);
  assert.doesNotMatch(cards, /level.problem_response_hours/);
  assert.match(source, /v-model.number="serviceLevelForm.urgent_initial_response_minutes"/);
  const { descriptor } = parse(source);
  const result = compileTemplate({ source: descriptor.template.content, filename: 'ConsoleApp.vue', id: 'sla-response' });
  assert.deepEqual(result.errors, []);
});
