<script setup>
import { money } from './settlementFinance.js';
defineProps({ segments: Array });
const shares = [['platform', 'platform_fee', '平台'], ['provider', 'provider_share', '提供方'], ['service', 'service_share', '服务方'], ['expert', 'expert_fee', '专家'], ['channel', 'channel_fee', '渠道']];
const fundingFields = { buyer_cash_amount: '本段买方实际现金退款', external_funding_return: '本段原补差付款方返还权益（非买方现金）', absorption_reversal: '本段平台吸收冲回（非买方现金）' };
</script>
<template>
  <section v-if="segments?.length" class="economic-segments" aria-label="退款逐段来源证据">
    <h4>退款逐段来源证据</h4>
    <section v-for="(s, i) in segments" :key="s.segment_id || i" class="source-segment">
      <h4>来源 {{ i + 1 }} · {{ s.source_order_no || s.source_order_id }}</h4>
      <dl><dt>经济段</dt><dd>{{ s.segment_id || '—' }}</dd><dt>原清算单</dt><dd>{{ s.reference_settlement_no || s.reference_settlement_id || '—' }}</dd><dt>原批次</dt><dd>{{ s.reference_batch_nos?.length ? s.reference_batch_nos.join('、') : s.reference_batch_no || s.source_batch_no || '未提供' }}</dd><dt>最终基线校验值</dt><dd>{{ s.baseline_hash || '未提供' }}</dd><dt v-if="s.receipt_hash">生效回执校验值</dt><dd v-if="s.receipt_hash">{{ s.receipt_hash }}</dd>
        <template v-for="(label, key) in fundingFields" :key="key"><template v-if="s[key] != null"><dt>{{ label }}</dt><dd>{{ money(s[key]) }}</dd></template></template>
      </dl>
      <div class="table-scroll"><table><thead><tr><th>财务收入冲回</th><th>成本冲回</th><th>利润冲回</th></tr></thead><tbody><tr><td>{{ money(s.gross_amount) }}</td><td>{{ money(s.cost_amount) }}</td><td>{{ money(s.profit_amount) }}</td></tr></tbody></table><table v-if="s.shares"><thead><tr><th>收益类别</th><th>冻结比例</th><th>本段冲回金额</th></tr></thead><tbody><tr v-for="[kind, key, label] in shares" :key="kind"><td>{{ label }}</td><td>{{ s.share_rates?.[kind] == null ? '未提供' : s.share_rates[kind] + '%' }}</td><td>{{ money(s.shares[key]) }}</td></tr></tbody></table></div>
    </section>
  </section>
</template>
<style scoped>.economic-segments{min-width:0;font-size:12px}.economic-segments h4{font-size:13px;margin:12px 0}.source-segment{border-top:1px solid #e5e6eb;padding:12px 0}.source-segment dl{display:grid;grid-template-columns:150px minmax(0,1fr);gap:10px}.source-segment dd{margin:0;overflow-wrap:anywhere}.source-segment dt{color:#697586}.table-scroll{max-width:100%;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px;margin:12px 0}th,td{padding:10px;text-align:left;border-bottom:1px solid #e5e6eb;white-space:nowrap}th{background:#f7f8fa;font-weight:500}@media(max-width:600px){.source-segment dl{grid-template-columns:1fr;gap:6px}}</style>
