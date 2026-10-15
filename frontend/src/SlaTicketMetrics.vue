<script setup>
import { slaClockState, slaMetricNames, slaParty, slaMinutes, slaEvidenceReason } from "./slaEvidence.js";
defineProps({ items: { type: Array, default: () => [] } });
const metricName = value => Object.hasOwn(slaMetricNames, value) ? slaMetricNames[value] : value || "未记录指标";
const date = value => value ? new Date(value).toLocaleString() : "—";
</script>
<template>
  <section class="sla-ticket-metrics">
    <h3>三方责任指标</h3>
    <p class="muted">买方配合时间单列；平台介入不默认暂停提供方解决时钟。冻结秒值按秒 ÷ 60 换算为分钟。</p>
    <div class="table-wrap"><table class="data-table compact"><thead><tr><th>责任方 / 指标</th><th>状态</th><th>累计 / 冻结目标</th><th>计时口径</th><th>测量时间</th></tr></thead><tbody>
      <tr v-for="item in items" :key="item.id || `${item.metric}:${item.round}:${item.responsibility}`">
        <td><strong>{{ slaParty(item.responsibility) }}</strong><small>{{ metricName(item.metric) }} · 第 {{ item.round || 1 }} 轮</small></td>
        <td><span :class="['status-pill', slaClockState(item).className]">{{ slaClockState(item).label }}</span><small v-if="item.reason" :title="item.reason">{{ slaEvidenceReason(item.reason) }}</small></td>
        <td>{{ slaMinutes(item.elapsed_seconds) }} / {{ slaMinutes(item.target_seconds) }}<small>预警：{{ slaMinutes(item.warning_seconds) }}</small></td>
        <td>{{ item.clock === 'support' ? '支持时间' : item.clock === 'natural' ? '自然时间' : '口径未记录' }}</td>
        <td>{{ date(item.as_of) }}<small>{{ date(item.started_at) }} 至 {{ date(item.ended_at) }}</small></td>
      </tr><tr v-if="!items.length"><td colspan="5">暂无已记录时钟，不能推断达标或违约。</td></tr>
    </tbody></table></div>
    <details v-for="item in items" :key="`evidence:${item.id || `${item.metric}:${item.round}`}`"><summary>{{ slaParty(item.responsibility) }} · {{ metricName(item.metric) }} · 冻结与事件依据</summary>
      <dl><dt>冻结规则版本</dt><dd>{{ item.rule_version || '—' }}</dd><dt>来源工单版本</dt><dd>{{ item.source_ticket_version ?? '—' }}</dd><dt>事件 ID</dt><dd>{{ item.source_event_ids?.join('、') || '未提供事件依据' }}</dd><dt>支持日历版本</dt><dd>{{ item.calendar_version || '—' }}</dd></dl>
      <ul v-if="item.calendar_bindings?.length"><li v-for="binding in item.calendar_bindings" :key="binding.year">{{ binding.year }}：{{ binding.calendar_version_id || binding.version || '—' }}<small>{{ binding.content_hash || '—' }}</small></li></ul>
    </details>
  </section>
</template>
<style scoped>
.sla-ticket-metrics{min-width:0}.sla-ticket-metrics details{padding:12px 0;border-bottom:1px solid #dce2e8}.sla-ticket-metrics summary{cursor:pointer;overflow-wrap:anywhere}.sla-ticket-metrics dd{margin:4px 0 12px;overflow-wrap:anywhere}.sla-ticket-metrics dt{font-size:12px;color:#647078}
</style>
