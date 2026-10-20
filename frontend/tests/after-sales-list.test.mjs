import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { compile, createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { CheckCircle2 } from 'lucide-vue-next';
import { ticketStatus } from '../src/afterSales.js';

const source = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
const start = source.indexOf('<table class="data-table compact after-sales-table"');
const template = source.slice(start, source.indexOf('</table>', start) + '</table>'.length);
const render = items => renderToString(createSSRApp({
  components: { CheckCircle2 },
  setup: () => ({ afterSalesItems: items, selectedAfterSalesId: null, openOrder() {},
    ticketStatus, partyName: (item, side) => item[`${side}_name`] || '-', fmtDate: value => `日期:${value}` }),
  render: compile(template),
}));

test('tickets use the shared compact table with one row per ticket and matching columns', async () => {
  const html = await render([
    { id: 'one', ticket_no: 'AS-ONE', order_id: 'order-one', order_no: 'ORD-ONE', product_name: 'SaaS应用',
      provider_name: '提供企业', buyer_name: '购买企业', type: '质量异议', description: '处理说明',
      assignee_name: '售后人员', priority: 'important', status: 'processing', created_at: '2026-10-20' },
    { id: 'two', ticket_no: 'AS-TWO', order_id: 'order-two', status: 'completed' },
  ]);
  assert.equal((html.match(/<th scope="col">/g) || []).length, 10);
  assert.equal((html.match(/<td[ >]/g) || []).length, 20);
  for (const text of ['AS-ONE', 'AS-TWO', 'ORD-ONE', 'SaaS应用', '提供企业', '购买企业',
    '质量异议', '处理说明', '售后人员', '重要', '处理中', '已完成', '日期:2026-10-20', '未分配']) assert.ok(html.includes(text), text);
  assert.ok(template.includes('@click="selectedAfterSalesId = item.id"'));
  assert.ok(template.includes('@click="openOrder({ id: item.order_id })"'));
  assert.ok(!template.includes('after-sales-item'));
});

test('empty table spans all columns and descriptions are escaped', async () => {
  assert.match(await render([]), /colspan="10"/);
  assert.match(await render([]), /暂无售后工单/);
  const html = await render([{ id: 'one', description: '<script>alert(1)</script>' }]);
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('filters and shared pagination remain around the table with overflow containment', () => {
  const section = source.slice(source.indexOf('<div v-show="deliveryTab === \'aftersales\'"'), source.indexOf('<template v-else-if="activeView === \'earnings\''));
  assert.ok(section.includes('<MainListFilters'));
  assert.ok(section.includes('<PaginationControls'));
  assert.ok(section.includes('<div class="table-wrap">'));
  assert.ok(source.includes('.after-sales-table { min-width: 1120px; }'));
});
