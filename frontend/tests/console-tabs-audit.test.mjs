import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { deliveryTabs, identityTabs, slaTabs } from '../src/consoleTabs.js';
import { auditActionLabel } from '../src/auditDisplay.js';

const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
test('tab definitions preserve business order and refund visibility', () => {
  assert.deepEqual(deliveryTabs(false).map(tab => tab.key), ['tasks', 'aftersales']);
  assert.deepEqual(deliveryTabs(true).map(tab => tab.key), ['tasks', 'aftersales', 'refunds']);
  assert.deepEqual(identityTabs.map(tab => tab.key), ['users', 'enterprises']);
  assert.deepEqual(slaTabs.map(tab => tab.key), ['overview', 'levels', 'assignments', 'evidence']);
});
test('audit display accepts Chinese server labels and preserves raw event codes', () => {
  const event = { action: 'order_paid' };
  assert.equal(auditActionLabel(event), '订单支付');
  assert.equal(event.action, 'order_paid');
  assert.equal(auditActionLabel({ action: 'future_action', action_label: '企业到期恢复标准' }), '企业到期恢复标准');
  assert.equal(auditActionLabel({ action: 'future_action', action_label: 'future_action' }), '其他操作（future_action）');
  assert.equal(auditActionLabel(null), '未记录动作');
  for (const code of ['create_product', 'generate_settlement_batch', 'assign_service_level', 'update_sla_matrix', 'expire_service_level_assignment', 'after_sales_request_intervention', 'verify_version_gateway', 'evaluate_order_sla', 'freeze_order_sla_contract', 'ordinary_refund_negative_settlement']) assert.doesNotMatch(auditActionLabel(code), /其他操作/);
  assert.equal(auditActionLabel({ action: 'create_product', action_label: '后端中文动作' }), '后端中文动作');
  assert.match(read('ConsoleApp.vue'), /<code>\{\{ auditSelected.action \}\}<\/code>/);
});
test('tab keyboard navigation wraps, skips disabled tabs and focuses its new selection', () => {
  const source = read('ConsoleTabs.vue');
  const start = source.indexOf('function move(');
  const calls = [], focus = [];
  const context = vm.createContext({ props: { tabs: [{ key: 'a' }, { key: 'b', disabled: true }, { key: 'c' }] }, emit: (...args) => calls.push(args) });
  vm.runInContext(source.slice(start, source.indexOf('</script>')), context);
  const event = key => ({ key, preventDefault() {}, currentTarget: { parentElement: { querySelectorAll: () => [0, 1, 2].map(index => ({ focus: () => focus.push(index) })) } } });
  context.move(event('ArrowRight'), 0);
  context.move(event('ArrowRight'), 2);
  context.move(event('End'), 0);
  assert.deepEqual(calls, [['update:modelValue', 'c'], ['update:modelValue', 'a'], ['update:modelValue', 'c']]);
  assert.deepEqual(focus, [2, 0, 2]);
});
test('finance, participant configuration and annual report actions stay Chinese in audit and lifecycle views', () => {
  const expected = {
    finance_draft: '登记资金凭证草稿', finance_confirm: '复核资金凭证', finance_baseline_capture: '冻结清算资金基线',
    finance_cost_exception_plan: '冻结成本例外冲回方案', finance_segment_funding_link: '关联版本变更经济段资金依据',
    bind_version_change_final_baseline: '绑定版本变更最终清算基线', project_version_change_final_baseline: '锁定前应用版本变更最终基线',
    approve_segment_cost_exception_refresh: '批准版本段成本例外并待刷新基线',
    refund_final_baseline_review: '复核退款最终清算基线', create_settlement_participant: '创建清算收益主体',
    configure_settlement_participants: '配置清算收益主体', 'configure_settlement_participants:product-42': '配置清算收益主体',
    export_settlement_reporting: '导出清算分析报表', create_settlement_annual_report: '创建年度清算报告草稿',
    export_settlement_annual_report: '导出年度清算报告', finalize_settlement_annual_report: '正式确认年度清算报告',
  };
  const source = read('ConsoleApp.vue');
  const context = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const settlementLifecycleLabels ='), source.indexOf('const settlementLifecycleFieldLabels =')) +
    source.slice(source.indexOf('function settlementLifecycleAction('), source.indexOf('function settlementLifecycleValue(')), context);
  for (const [action, label] of Object.entries(expected)) {
    const event = { action, action_label: action };
    assert.equal(auditActionLabel(action), label);
    assert.equal(auditActionLabel(event), label);
    assert.equal(event.action, action);
    assert.equal(context.settlementLifecycleAction(action), label);
  }
  assert.equal(auditActionLabel({ action: 'finance_draft', action_label: '后端提供的中文名称' }), '后端提供的中文名称');
  assert.equal(auditActionLabel('configure_settlement_participants_future'), '其他操作（configure_settlement_participants_future）');
  assert.equal(context.settlementLifecycleAction('future_finance_action'), 'future_finance_action');
  assert.equal(context.settlementLifecycleAction('lock_settlement'), '锁定清算单');
});
test('independent UI components compile and audit filters have responsive grid tracks', () => {
  for (const name of ['AuditFilters.vue', 'ConsoleTabs.vue', 'ConsoleApp.vue']) {
    const { descriptor, errors } = parse(read(name), { filename: name });
    assert.deepEqual(errors, []);
    compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: name, id: name }).errors, []);
  }
  const filters = read('AuditFilters.vue');
  assert.match(filters, /repeat\(4,minmax\(0,1fr\)\)/);
  assert.match(filters, /@container\(max-width:800px\)/);
  assert.match(filters, /@container\(max-width:420px\)/);
});
test('delivery and audit clear stale page rows before their next request', () => {
  const source = read('ConsoleApp.vue');
  const delivery = source.slice(source.indexOf('if (view === "delivery")'), source.indexOf('if (view === "settlements")'));
  assert.ok(delivery.indexOf('afterSalesItems.value = []') < delivery.indexOf('await fetchListPage'));
  const audit = source.slice(source.indexOf('async function loadAuditLogs()'), source.indexOf('function searchAuditLogs()'));
  assert.ok(audit.indexOf('auditItems.value = []') < audit.indexOf('await api.get'));
});
