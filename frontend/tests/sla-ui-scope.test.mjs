import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { computed, reactive, compile, createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import vm from 'node:vm';
import postcss from 'postcss';

const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
const management = read('SlaManagement.vue');

test('overview cards use server totals beyond the truncated result list and ignore obsolete rule counts', async () => {
  const overviewData = computed(() => overview);
  const overview = reactive({ summary: { active_profiles: 99, met: 120, warning: 1, breached: 1 }, results: [
    { status: 'met' }, { status: 'met' }, { status: 'warning' }, { status: 'breached' },
    ...['unknown', 'insufficient_data', 'not_applicable', 'not_started', 'historical_missing', 'constructor', null].map(status => ({ status })),
  ] });
  const script = management.slice(management.indexOf('const summary = computed'), management.indexOf('async function evaluate'));
  const context = vm.createContext({ computed, overviewData });
  vm.runInContext(`${script}; globalThis.counts = summary;`, context);
  assert.deepEqual({ ...context.counts.value }, { met: 120, warning: 1, breached: 1 });
  const start = management.indexOf('<div class="metric-grid">');
  const end = management.indexOf('\n      <section>', start);
  const icon = { render: () => null };
  const html = await renderToString(createSSRApp({ setup: () => ({ summary: context.counts, CheckCircle2: icon, CircleAlert: icon }), render: compile(management.slice(start, end)) }));
  assert.equal((html.match(/class="metric-card"/g) || []).length, 3);
  assert.equal((html.match(/<small>当前周期考核结果<\/small>/g) || []).length, 3);
  assert.match(html, /120/);
  assert.doesNotMatch(html, /历史启用规则/);
  overview.results = [];
  overview.summary = { met: -1, warning: NaN, breached: 'invalid' };
  assert.deepEqual({ ...context.counts.value }, { met: 0, warning: 0, breached: 0 });
});

test('SLA field styles stay local with native option fonts and compact height exceptions', () => {
  const css = postcss.parse(read('slaManagement.css'));
  const rules = css.nodes.filter(node => node.type === 'rule');
  const font = rules.find(rule => rule.selector.includes('select option'));
  assert.ok(font);
  assert.ok(font.selector.split(',').every(selector => selector.startsWith('.sla-management')));
  assert.equal(font.nodes.find(node => node.prop === 'font-size').value, '12px');
  for (const field of ['label', 'input', 'select', 'textarea']) assert.ok(font.selector.includes(` ${field}`));
  const height = selector => rules.filter(rule => rule.selector === selector).flatMap(rule => rule.nodes).filter(node => node.prop === 'height').at(-1)?.value;
  assert.equal(height('.sla-management .pagination-controls input,.sla-management .pagination-controls select'), '30px');
  assert.equal(height('.sla-management .sla-matrix-table input'), '32px');
  assert.equal(height('.sla-management textarea'), 'auto');
  assert.ok(rules.some(rule => rule.selector === '.sla-management textarea' && rule.nodes.some(node => node.prop === 'min-height' && node.value === '68px')));
  assert.ok(rules.some(rule => rule.selector === '.sla-management .metric-grid' && rule.nodes.some(node => node.value === 'repeat(3,minmax(0,1fr))')));
  assert.doesNotMatch(read('SlaEvidencePanel.vue'), /assignments:/);
  assert.doesNotMatch(management, /overviewData\.(profiles|service_level_assignments)|active_profiles/);
});
