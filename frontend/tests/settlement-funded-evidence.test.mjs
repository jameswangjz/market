import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as Vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import { money, safeError } from '../src/settlementFinance.js';
import { partyKinds } from '../src/settlementManagement.js';

const components = {};
function component(name) {
  if (components[name]) return components[name];
  const { descriptor } = parse(readFileSync(new URL(`../src/${name}.vue`, import.meta.url), 'utf8'));
  let code = compileScript(descriptor, { id: name, inlineTemplate: true, genDefaultAs: '__component' }).content;
  code = code.replace(/import\s*\{([^}]+)\}\s*from\s*['"]vue['"];?/g, (_, names) => `const {${names.replace(/\bas\b/g, ':')}} = Vue;`)
    .replace(/import\s*\{[^}]+\}\s*from\s*['"][^'"]+['"];?/g, '')
    .replace(/import\s+(\w+)\s+from\s*['"]\.\/(\w+)\.vue['"];?/g, (_, local, name) => `const ${local} = child('${name}');`);
  return components[name] = new Function('Vue', 'money', 'safeError', 'partyKinds', 'child', `${code};return __component;`)(Vue, money, safeError, partyKinds, component);
}
const render = (name, props) => renderToString(Vue.createSSRApp(component(name), props));

// Evaluate the actual DTO dictionary expressions, not a frontend-only serializer.
const dto = JSON.parse(execFileSync('python3', ['-c', `
import ast,json,sys
from decimal import Decimal
from types import SimpleNamespace as R
def expression(path,predicate):
    tree=ast.parse(open(path).read())
    return next(n for n in ast.walk(tree) if predicate(n))
call=expression(sys.argv[1],lambda n:isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='append' and ast.unparse(n.func.value)=="row['funding_returns']")
ns=dict(position=R(id='return-position',participant_type='enterprise',participant_id='payer',participant_name='原补差企业',currency='CNY',amount=Decimal('2.00')),paid=Decimal('0'),offset=Decimal('0'),balance=Decimal('2.00'),source_id='source',source_settlement=R(settlement_no='SET-SOURCE'),source_order=R(order_no='ORD-SOURCE'),source_batches=['BATCH-A','BATCH-B'],decimal=Decimal)
report=eval(compile(ast.Expression(call.args[0]),sys.argv[1],'eval'),ns)
kw=expression(sys.argv[2],lambda n:isinstance(n,ast.keyword) and n.arg=='funding_returns' and isinstance(n.value,ast.List) and n.value.elts)
funding=dict(bearer=dict(participant_type='enterprise',participant_id='payer',participant_name='原补差企业'),request_id='funding-request',proof_hash='proof-hash',manual_evidence_id='manual-id',manual_evidence_hash='manual-hash')
frozen=eval(compile(ast.Expression(kw.value),sys.argv[2],'eval'),dict(funding=funding,b=R(currency='CNY',settlement_id='source'),returned=Decimal('2.00')))
print(json.dumps(dict(report=report,frozen=frozen)))
`, fileURLToPath(new URL('../../backend/app/settlement_reporting.py', import.meta.url)),
fileURLToPath(new URL('../../backend/app/settlement_funded_refund.py', import.meta.url))], { encoding: 'utf8' }));

test('actual report return DTO renders separate payable and truthful unpaid/paid/offset states', async () => {
  for (const [patch, label] of [[{}, '待付款 / 抵销'], [{ paid_amount: '2.00', remaining_amount: '0.00' }, '已登记付款'],
    [{ offset_amount: '2.00', remaining_amount: '0.00' }, '已抵销'],
    [{ paid_amount: '1.00', remaining_amount: '1.00' }, '部分处理，仍有待付']]) {
    const html = await render('SettlementReportEvidence', { row: { funding_returns: [{ ...dto.report, ...patch }] } });
    for (const text of [label, '原补差企业', 'ORD-SOURCE', 'SET-SOURCE', 'BATCH-A、BATCH-B', '银行到账验证', '>否<']) assert.ok(html.includes(text), text);
    assert.ok(!html.includes('<button'));
  }
});
test('frozen refund return rights show proof but never imply payment completion', async () => {
  const html = await render('SettlementReportEvidence', { row: { source_order_no: 'ORD-RAW', reference_settlement_no: 'SET-RAW', reference_batch_no: 'BATCH-RAW', baseline_hash: 'baseline-raw-proof', buyer_cash_amount: '20.00', gross_amount: '-22.00', external_funding_return: '2.00', absorption_reversal: '0.00', funding_returns: dto.frozen } });
  for (const text of ['ORD-RAW', 'SET-RAW', 'BATCH-RAW', 'baseline-raw-proof', '买方实际现金退款', '20.00', '-22.00', '返还权益，结算证据未提供', 'proof-hash', 'manual-id', 'manual-hash', '未提供，不推断到账']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('<dd>已登记付款</dd>'));
});
test('funding_return ledger never labels separate entitlement as ordinary profit', async () => {
  const html = await render('SettlementReportEvidence', { row: { funds: { known: false, positions: [{ id: 'return', kind: 'funding_return', direction: 'payable', amount: '2.00', remaining_amount: '2.00', currency: 'CNY' }] } } });
  assert.ok(html.includes('原补差付款方返还应付'));
  assert.ok(!html.includes('利润 / 损失'));
  assert.ok(html.includes('不推断为已结清'));
});
test('refund details distinguish buyer cash, financial reversal and platform absorption with nested real return rights', async () => {
  const html = await render('SettlementRefundEvidence', { evidence: { financial_basis: { status: 'ready', buyer_cash_amount: '20.00', gross_amount: '-22.00', cost_amount: '-8.00', profit_amount: '-14.00', absorption_reversal: '0.00', external_funding_return: '2.00', references: [{ segment_id: 'segment', source_order_no: 'ORD-SOURCE', buyer_cash_amount: '20.00', external_funding_return: '2.00', funding_returns: dto.frozen }] } } });
  for (const text of ['买方实际现金退款', '财务收入冲回（含补差对应部分）', '平台吸收冲回（非买方现金）', '原补差付款方返还权益（非买方现金）', 'manual-hash']) assert.ok(html.includes(text), text);
});
test('economic segments preserve source numbers, multiple batches, proof and distinct cash/credits', async () => {
  const html = await render('SettlementEconomicSegments', { segments: [{ segment_id: 'segment', source_order_no: 'ORD-SOURCE', reference_settlement_no: 'SET-SOURCE', reference_batch_nos: ['BATCH-A', 'BATCH-B'], baseline_hash: 'baseline-proof', buyer_cash_amount: '20.00', external_funding_return: '2.00', absorption_reversal: '0.00', gross_amount: '-22.00', cost_amount: '-8.00', profit_amount: '-14.00' }] });
  for (const text of ['ORD-SOURCE', 'SET-SOURCE', 'BATCH-A、BATCH-B', 'baseline-proof', '本段买方实际现金退款', '本段原补差付款方返还权益（非买方现金）', '本段平台吸收冲回（非买方现金）']) assert.ok(html.includes(text), text);
});
test('legacy missing evidence stays compatible and unknown balances cannot imply paid', async () => {
  assert.ok(!(await render('SettlementReportEvidence', { row: {} })).includes('现金退款与财务冲回'));
  const html = await render('SettlementReportEvidence', { row: { funding_returns: [{ ...dto.report, remaining_amount: null }] } });
  assert.ok(html.includes('返还权益，结算证据未提供'));
  assert.ok(!html.includes('<dd>已登记付款</dd>'));
});
test('report credit exposes frozen original manual proof without claiming it paid the return', async () => {
  const html = await render('SettlementReportEvidence', { row: { funding_returns: [dto.report], funded_refund: { funding_returns: dto.frozen } } });
  for (const text of ['冻结补差返还来源凭据（不代表已支付）', 'proof-hash', 'manual-hash', '待付款 / 抵销']) assert.ok(html.includes(text), text);
});
test('VC internal restoration is not an external payable, payment or buyer refund', async () => {
  const html = await render('SettlementReportEvidence', { row: { version_funding: { credit_kind: 'internal_absorption_restoration', internal_absorption_return: '2.00', external_return_amount: '0.00', currency: 'CNY', projection_hash: 'projection-hash', recorded_at: '2026-10-18T00:00:00Z', policy: 'restore_unused_platform_absorption_not_target', source_hash: 'source-proof', funding: { source: 'platform_absorption', proof_hash: 'funding-proof' }, credits: [{ id: 'internal-credit', kind: 'funding_absorption_return', direction: 'internal', amount: '2.00', currency: 'CNY' }] } } });
  for (const text of ['平台内部吸收恢复', '内部权益', 'projection-hash', 'source-proof', 'funding-proof', '不是外部应付款、已付款或买方现金退款']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('<td>应付</td>'));
  assert.ok(!html.includes('原补差付款方返还应付'));
  assert.ok(!html.includes('现金退款与财务冲回'));
});
test('VC external original payer projection exposes proof, never inferred paid', async () => {
  const html = await render('SettlementReportEvidence', { row: { version_funding: { credit_kind: 'external_payer_return', external_return_amount: '2.00', internal_absorption_return: '0.00', currency: 'CNY', projection_hash: 'external-projection', policy: 'return_unused_to_original_payer_not_target', funding: { source: 'external_receipt', manual_evidence_hash: 'original-manual-proof' }, credits: [{ id: 'external-credit', kind: 'funding_return', direction: 'payable', amount: '2.00', currency: 'CNY' }] } } });
  for (const text of ['原外部补款付款方返还权益', 'external-projection', 'original-manual-proof', '不代表已经付款', '原补差付款方返还应付']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('已登记付款'));
});
