<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import axios from 'axios'
import {
  Activity,
  ArrowUpRight,
  BarChart3,
  Bell,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ClipboardCheck,
  Clock3,
  Database,
  FileCheck2,
  FileText,
  LayoutDashboard,
  LogOut,
  Menu,
  PackageCheck,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  ShoppingCart,
  Users,
  X,
} from 'lucide-vue-next'

const api = axios.create({ baseURL: '/api' })
const token = ref(localStorage.getItem('market_token') || '')
const user = ref(null)
const enterprise = ref(null)
const loginForm = ref({ email: 'admin@market.local', password: 'Admin123!' })
const loginError = ref('')
const activeView = ref('overview')
const collapsed = ref(false)
const mobileOpen = ref(false)
const loading = ref(false)
const toast = ref('')
const dashboard = ref(null)
const products = ref([])
const orders = ref([])
const selectedOrder = ref(null)
const development = ref({ total: 0, counts: {}, completion_rate: 0, items: [] })
const developmentFilter = ref('all')
const search = ref('')
const showProductForm = ref(false)
const productForm = ref({ name: '', product_type: 'dataset', delivery_method: 'file', price: 0, description: '', quality_level: '标准' })
let progressTimer

const nav = [
  { key: 'overview', label: '运营总览', icon: LayoutDashboard },
  { key: 'products', label: '数据与服务', icon: Database },
  { key: 'orders', label: '订单中心', icon: ShoppingCart },
  { key: 'delivery', label: '交付与售后', icon: PackageCheck },
  { key: 'settlements', label: '清算分账', icon: BarChart3 },
  { key: 'audit', label: '审计日志', icon: ShieldCheck },
  { key: 'users', label: '用户与企业', icon: Users },
  { key: 'development', label: '开发进度', icon: Activity },
]

const visibleNav = computed(() => nav)
const filteredProducts = computed(() => products.value.filter((p) => !search.value || p.name.includes(search.value)))
const filteredOrders = computed(() => orders.value.filter((o) => !search.value || `${o.order_no}${o.product_name}${o.buyer_name}`.includes(search.value)))
const filteredTasks = computed(() => development.value.items.filter((t) => developmentFilter.value === 'all' || t.status === developmentFilter.value))

const statusLabels = {
  created: '创建', pending_review: '待审核', pending_fulfillment: '待履约', fulfilling: '履约中', pending_confirmation: '待确认', completed: '已完成', closed: '已关闭', cancelled: '已取消',
  unpaid: '未支付', paying: '支付中', paid: '已支付', refunding: '退款中', refunded: '已退款',
  not_started: '未开始', preparing: '准备中', in_delivery: '交付中', pending_acceptance: '待验收', accepted: '验收通过', exception: '交付异常',
  none: '无售后', processing: '售后处理中', resolved: '已解决', after_closed: '售后关闭',
}
const taskStatusLabels = { todo: '待开发', in_progress: '进行中', review: '待评审', blocked: '已阻塞', done: '已完成' }
const typeLabels = { dataset: '数据集', model: '模型', api: 'API 服务', application: '数据应用', report: '数据报告', training: '培训', consulting: '咨询', custom: '定制开发' }

function fmtMoney(value) { return `¥${Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}` }
function fmtDate(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-' }
function label(value) { return statusLabels[value] || value }
function notify(message) { toast.value = message; window.setTimeout(() => { toast.value = '' }, 2600) }
function setToken(value) { token.value = value; localStorage.setItem('market_token', value); api.defaults.headers.common.Authorization = `Bearer ${value}` }

async function login() {
  loginError.value = ''
  try { const { data } = await api.post('/auth/login', loginForm.value); setToken(data.token); await loadSession() } catch (error) { loginError.value = error.response?.data?.detail || '登录失败，请检查账号' }
}
async function loadSession() {
  if (!token.value) return
  api.defaults.headers.common.Authorization = `Bearer ${token.value}`
  try { const { data } = await api.get('/auth/me'); user.value = data.user; enterprise.value = data.enterprise; await refreshData() } catch { logout() }
}
function logout() { token.value = ''; user.value = null; enterprise.value = null; localStorage.removeItem('market_token'); delete api.defaults.headers.common.Authorization }
async function refreshData() {
  loading.value = true
  try {
    const [d, p, o, dev] = await Promise.all([api.get('/dashboard'), api.get('/products'), api.get('/orders'), api.get('/development/tasks')])
    dashboard.value = d.data; products.value = p.data.items; orders.value = o.data.items; development.value = dev.data
  } catch { notify('数据刷新失败，请检查后端服务') } finally { loading.value = false }
}
async function loadViewData(view) {
  if (view === 'development' || view === 'overview') { const { data } = await api.get('/development/tasks'); development.value = data }
  if (view === 'products') { const { data } = await api.get('/products'); products.value = data.items }
  if (view === 'orders') { const { data } = await api.get('/orders'); orders.value = data.items }
}
function selectView(view) { activeView.value = view; mobileOpen.value = false; loadViewData(view) }
async function openOrder(order) { const { data } = await api.get(`/orders/${order.id}`); selectedOrder.value = data }
async function transition(action) {
  if (!selectedOrder.value) return
  try { await api.post(`/orders/${selectedOrder.value.order.id}/transition`, { action, reason: '工作台操作' }); notify('订单状态已更新'); await openOrder(selectedOrder.value.order); await refreshData() } catch (error) { notify(error.response?.data?.detail || '状态操作失败') }
}
async function createProduct() {
  try { await api.post('/products', productForm.value); showProductForm.value = false; productForm.value = { name: '', product_type: 'dataset', delivery_method: 'file', price: 0, description: '', quality_level: '标准' }; notify('产品草稿已创建'); await loadViewData('products') } catch (error) { notify(error.response?.data?.detail || '创建失败') }
}
async function updateTask(task, status) {
  try { await api.patch(`/development/tasks/${task.code}`, { status, progress: status === 'done' ? 100 : task.progress, note: '工作台任务状态更新' }); await loadViewData('development'); notify(`${task.code} 已更新为${taskStatusLabels[status]}`) } catch (error) { notify(error.response?.data?.detail || '任务更新失败') }
}
function taskCount(key) { return development.value.counts?.[key] || 0 }
function nextActions(order) {
  const actions = []
  if (order.main_status === 'created') actions.push(['submit_review', '提交审核'])
  if (order.main_status === 'pending_review') actions.push(['approve', '审核通过'])
  if (order.payment_status === 'unpaid') actions.push(['start_payment', '发起模拟支付'])
  if (order.payment_status === 'paying') actions.push(['confirm_payment', '模拟确认支付'])
  if (order.payment_status === 'paid' && order.after_sales_status === 'processing') actions.push(['approve_refund', '同意退款'])
  if (order.payment_status === 'refunding') actions.push(['complete_refund', '确认退款完成'])
  if (order.delivery_status === 'not_started') actions.push(['create_task', '生成履约任务'])
  if (order.main_status === 'pending_fulfillment') actions.push(['start_delivery', '开始履约'])
  if (order.delivery_status === 'preparing') actions.push(['submit_delivery', '提交交付物'])
  if (order.delivery_status === 'pending_acceptance') actions.push(['accept_delivery', '验收通过'])
  if (order.main_status === 'pending_confirmation') actions.push(['confirm_order', '确认完成'])
  if (order.delivery_status === 'exception') actions.push(['retry_delivery', '整改后重试'])
  if (order.after_sales_status === 'none' && ['completed', 'fulfilling'].includes(order.main_status)) actions.push(['submit_after_sales', '发起售后'])
  return actions
}

onMounted(() => { if (token.value) loadSession(); progressTimer = window.setInterval(() => { if (token.value) loadViewData('development') }, 10000) })
onUnmounted(() => window.clearInterval(progressTimer))
</script>

<template>
  <div v-if="!user" class="login-shell">
    <div class="login-art"><div class="eyebrow">MARKET OPERATIONS</div><h1>让数据产品<br /><span>持续流通和增值</span></h1><p>从产品登记到订单履约，再到清算审计，一处掌握平台运营闭环。</p><div class="login-metric"><strong>8</strong><span>核心业务域<br />统一运营</span></div></div>
    <form class="login-card" @submit.prevent="login"><div class="brand-mark">M</div><div class="eyebrow">工作台登录</div><h2>欢迎回来</h2><p class="muted">登录数据集运营服务管理平台</p><label>工作邮箱<input v-model="loginForm.email" type="email" autocomplete="username" /></label><label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" /></label><p v-if="loginError" class="error-text">{{ loginError }}</p><button class="primary-btn full-btn" type="submit">进入运营工作台 <ArrowUpRight :size="16" /></button><p class="login-hint">演示账号已预填，可直接登录查看完整工作台。</p></form>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar" :class="{ collapsed, open: mobileOpen }"><div class="brand"><div class="brand-mark">M</div><div v-if="!collapsed" class="brand-copy"><strong>market</strong><small>运营服务管理平台</small></div></div><div class="workspace-switch"><div class="workspace-avatar">天</div><div v-if="!collapsed" class="workspace-copy"><strong>{{ enterprise?.name }}</strong><small>主租户 · 已认证</small></div><ChevronRight v-if="!collapsed" :size="15" /></div><nav><button v-for="item in visibleNav" :key="item.key" :class="['nav-item', { active: activeView === item.key }]" :title="item.label" @click="selectView(item.key)"><component :is="item.icon" :size="18" /><span v-if="!collapsed">{{ item.label }}</span><span v-if="item.key === 'development' && !collapsed" class="nav-badge">{{ development.total }}</span></button></nav><div class="sidebar-bottom"><button class="nav-item"><Settings2 :size="18" /><span v-if="!collapsed">系统设置</span></button><button class="nav-item" @click="logout"><LogOut :size="18" /><span v-if="!collapsed">退出登录</span></button></div></aside>
    <div v-if="mobileOpen" class="mobile-scrim" @click="mobileOpen = false"></div>
    <main class="main-shell"><header class="topbar"><button class="icon-btn mobile-menu" @click="mobileOpen = true"><Menu :size="20" /></button><button class="icon-btn desktop-collapse" @click="collapsed = !collapsed"> <PanelLeftOpen v-if="collapsed" :size="19" /><PanelLeftClose v-else :size="19" /></button><div class="breadcrumb"><span>运营工作台</span><ChevronRight :size="14" /><strong>{{ nav.find((x) => x.key === activeView)?.label }}</strong></div><div class="top-actions"><div class="search-box"><Search :size="16" /><input v-model="search" placeholder="搜索订单、产品或企业" /></div><button class="icon-btn notification-btn"><Bell :size="18" /><i></i></button><div class="user-chip"><div class="avatar">{{ user.name?.slice(0, 1) }}</div><div class="user-chip-copy"><strong>{{ user.name }}</strong><small>平台管理员</small></div></div></div></header>
      <section class="content">
        <div v-if="loading" class="loading-line"><span></span>正在同步运营数据...</div>
        <template v-if="activeView === 'overview'"><div class="page-heading"><div><div class="eyebrow">运营概览 · {{ new Date().toLocaleDateString('zh-CN') }}</div><h1>早上好，{{ user.name }}</h1><p>平台运营状态清晰可见，今天也保持节奏。</p></div><button class="secondary-btn" @click="refreshData"><RefreshCw :size="16" />刷新数据</button></div><div class="metric-grid"><div class="metric-card"><div class="metric-icon orange"><Database :size="19" /></div><span>在运营产品</span><strong>{{ dashboard?.metrics.products || 0 }}</strong><small><ArrowUpRight :size="13" /> 数据目录持续增长</small></div><div class="metric-card"><div class="metric-icon blue"><ShoppingCart :size="19" /></div><span>订单总量</span><strong>{{ dashboard?.metrics.orders || 0 }}</strong><small><Activity :size="13" /> {{ dashboard?.metrics.active_orders || 0 }} 个履约中</small></div><div class="metric-card"><div class="metric-icon green"><CheckCircle2 :size="19" /></div><span>已完成订单</span><strong>{{ dashboard?.metrics.completed_orders || 0 }}</strong><small><ClipboardCheck :size="13" /> 验收闭环率稳定</small></div><div class="metric-card"><div class="metric-icon violet"><BarChart3 :size="19" /></div><span>累计确认收入</span><strong>{{ fmtMoney(dashboard?.metrics.revenue) }}</strong><small><ArrowUpRight :size="13" /> 模拟支付口径</small></div></div><div class="overview-grid"><div class="panel progress-panel"><div class="panel-heading"><div><span class="section-kicker">DEVELOPMENT CONTROL</span><h3>开发进度</h3></div><button class="text-btn" @click="selectView('development')">查看任务 <ChevronRight :size="15" /></button></div><div class="progress-ring-row"><div class="progress-ring" :style="{ '--progress': `${development.completion_rate}%` }"><div><strong>{{ development.completion_rate }}%</strong><small>已完成</small></div></div><div class="progress-summary"><div><span class="dot green-dot"></span><strong>{{ taskCount('done') }}</strong><small>已完成</small></div><div><span class="dot orange-dot"></span><strong>{{ taskCount('in_progress') }}</strong><small>进行中</small></div><div><span class="dot gray-dot"></span><strong>{{ taskCount('todo') }}</strong><small>待开发</small></div></div></div><div class="mini-task" v-for="task in development.items.filter((x) => x.status === 'in_progress').slice(0, 3)" :key="task.code"><div><span class="task-code">{{ task.code }}</span><strong>{{ task.title }}</strong></div><div class="mini-bar"><i :style="{ width: `${task.progress}%` }"></i></div><span>{{ task.progress }}%</span></div></div><div class="panel activity-panel"><div class="panel-heading"><div><span class="section-kicker">ORDER OPERATIONS</span><h3>订单状态分布</h3></div><button class="text-btn" @click="selectView('orders')">进入订单 <ChevronRight :size="15" /></button></div><div class="status-list" v-if="orders.length"><div v-for="item in [{ key: 'fulfilling', label: '履约中', color: 'orange' }, { key: 'pending_confirmation', label: '待确认', color: 'blue' }, { key: 'completed', label: '已完成', color: 'green' }]" :key="item.key" class="status-row"><div><span :class="['dot', `${item.color}-dot`]"></span><span>{{ item.label }}</span></div><strong>{{ orders.filter((o) => o.main_status === item.key).length }}</strong></div></div><div v-else class="empty-state"><ShoppingCart :size="28" /><span>还没有订单数据</span></div></div></div></template>
        <template v-else-if="activeView === 'development'"><div class="page-heading"><div><div class="eyebrow">主 Agent · 实时任务监控</div><h1>开发进度控制台</h1><p>功能拆解、负责人、依赖和验收状态集中管理。页面每 10 秒自动刷新。</p></div><button class="secondary-btn" @click="loadViewData('development')"><RefreshCw :size="16" />立即刷新</button></div><div class="dev-summary"><button v-for="item in [{ key: 'all', label: '全部任务', value: development.total, icon: Activity }, { key: 'done', label: '已完成', value: taskCount('done'), icon: CheckCircle2 }, { key: 'in_progress', label: '进行中', value: taskCount('in_progress'), icon: Clock3 }, { key: 'todo', label: '待开发', value: taskCount('todo'), icon: FileText }, { key: 'blocked', label: '已阻塞', value: taskCount('blocked'), icon: CircleAlert }]" :key="item.key" :class="['dev-stat', { selected: developmentFilter === item.key }]" @click="developmentFilter = item.key"><component :is="item.icon" :size="17" /><span>{{ item.label }}</span><strong>{{ item.value }}</strong></button></div><div class="panel task-panel"><div class="panel-heading"><div><span class="section-kicker">TASK REGISTER</span><h3>功能项清单 <small>共 {{ development.total }} 项 · 完成率 {{ development.completion_rate }}%</small></h3></div><span class="sync-note"><span class="live-dot"></span>实时同步 {{ fmtDate(development.updated_at) }}</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>任务</th><th>功能项</th><th>负责人</th><th>优先级</th><th>依赖</th><th>进度</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="task in filteredTasks" :key="task.code"><td><span class="task-code">{{ task.code }}</span><small>{{ task.area }}</small></td><td><strong>{{ task.title }}</strong><small class="acceptance">验收：{{ task.acceptance }}</small></td><td>{{ task.owner }}</td><td><span :class="['priority', task.priority.toLowerCase()]">{{ task.priority }}</span></td><td class="dependency">{{ task.dependencies || '无' }}</td><td><div class="progress-cell"><div class="mini-bar"><i :style="{ width: `${task.progress}%` }"></i></div><span>{{ task.progress }}%</span></div></td><td><span :class="['status-pill', `status-${task.status}`]">{{ taskStatusLabels[task.status] }}</span></td><td><select class="status-select" :value="task.status" @change="updateTask(task, $event.target.value)"><option value="todo">待开发</option><option value="in_progress">进行中</option><option value="review">待评审</option><option value="blocked">已阻塞</option><option value="done">已完成</option></select></td></tr></tbody></table></div></div></template>
        <template v-else-if="activeView === 'products'"><div class="page-heading"><div><div class="eyebrow">产品目录 · {{ products.length }} 个产品</div><h1>数据与服务</h1><p>管理数据集、模型、API 和服务类产品的生命周期。</p></div><button class="primary-btn" @click="showProductForm = true"><FileText :size="16" />新建产品</button></div><div class="panel"><div class="panel-heading"><h3>产品目录</h3><span class="muted">支持在线预览和授权下载</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>产品</th><th>类型</th><th>交付方式</th><th>质量等级</th><th>价格</th><th>状态</th></tr></thead><tbody><tr v-for="product in filteredProducts" :key="product.id"><td><strong>{{ product.name }}</strong><small>{{ product.version }} · {{ product.description }}</small></td><td>{{ typeLabels[product.product_type] || product.product_type }}</td><td>{{ product.delivery_method }}</td><td><span class="quality-tag">{{ product.quality_level }}</span></td><td>{{ fmtMoney(product.price) }}</td><td><span :class="['status-pill', `status-${product.status === 'published' ? 'done' : 'todo'}`]">{{ product.status === 'published' ? '已发布' : '草稿' }}</span></td></tr></tbody></table></div></div></template>
        <template v-else-if="activeView === 'orders'"><div class="page-heading"><div><div class="eyebrow">业务交易 · 四域状态机</div><h1>订单中心</h1><p>主状态、支付、交付和售后状态分别记录，跨域动作全程留痕。</p></div><button class="secondary-btn" @click="loadViewData('orders')"><RefreshCw :size="16" />刷新订单</button></div><div class="panel"><div class="panel-heading"><h3>订单列表 <small>{{ filteredOrders.length }} 条</small></h3><span class="muted">模拟支付 · 人工确认</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>订单号</th><th>产品和客户</th><th>主状态</th><th>支付</th><th>交付</th><th>售后</th><th>金额</th><th></th></tr></thead><tbody><tr v-for="order in filteredOrders" :key="order.id"><td><span class="order-no">{{ order.order_no }}</span><small>{{ fmtDate(order.created_at) }}</small></td><td><strong>{{ order.product_name }}</strong><small>{{ order.buyer_name }}</small></td><td><span class="status-pill status-in_progress">{{ label(order.main_status) }}</span></td><td>{{ label(order.payment_status) }}</td><td>{{ label(order.delivery_status) }}</td><td>{{ label(order.after_sales_status) }}</td><td><strong>{{ fmtMoney(order.amount) }}</strong></td><td><button class="icon-btn" title="查看订单" @click="openOrder(order)"><ChevronRight :size="17" /></button></td></tr></tbody></table></div></div></template>
        <template v-else><div class="page-heading"><div><div class="eyebrow">运营模块</div><h1>{{ nav.find((x) => x.key === activeView)?.label }}</h1><p>该工作台模块已接入首版接口边界，功能数据将在对应开发任务完成后持续丰富。</p></div><button class="secondary-btn" @click="refreshData"><RefreshCw :size="16" />刷新</button></div><div class="empty-module"><component :is="nav.find((x) => x.key === activeView)?.icon" :size="38" /><h3>模块骨架已就绪</h3><p>开发任务监控台会显示该模块的完成状态，当前主闭环优先保证订单和清算逻辑。</p><button class="text-btn" @click="selectView('development')">查看开发任务 <ChevronRight :size="15" /></button></div></template>
      </section>
    </main>
    <div v-if="selectedOrder" class="drawer-scrim" @click="selectedOrder = null"><aside class="order-drawer" @click.stop><div class="drawer-head"><div><span class="eyebrow">订单详情</span><h2>{{ selectedOrder.order.order_no }}</h2></div><button class="icon-btn" @click="selectedOrder = null"><X :size="19" /></button></div><div class="drawer-summary"><strong>{{ selectedOrder.order.product_name }}</strong><span>{{ selectedOrder.order.buyer_name }}</span><b>{{ fmtMoney(selectedOrder.order.amount) }}</b></div><div class="state-grid"><div><small>主状态</small><strong>{{ label(selectedOrder.order.main_status) }}</strong></div><div><small>支付状态</small><strong>{{ label(selectedOrder.order.payment_status) }}</strong></div><div><small>交付状态</small><strong>{{ label(selectedOrder.order.delivery_status) }}</strong></div><div><small>售后状态</small><strong>{{ label(selectedOrder.order.after_sales_status) }}</strong></div></div><div class="drawer-section"><div class="drawer-section-title">可执行动作</div><div class="action-list"><button v-for="action in nextActions(selectedOrder.order)" :key="action[0]" class="secondary-btn" @click="transition(action[0])">{{ action[1] }} <ArrowUpRight :size="14" /></button><span v-if="!nextActions(selectedOrder.order).length" class="muted">当前没有可执行动作</span></div></div><div class="drawer-section"><div class="drawer-section-title">状态时间轴</div><div class="timeline"><div v-for="item in selectedOrder.logs" :key="`${item.created_at}-${item.action}`" class="timeline-item"><span class="timeline-dot"></span><div><strong>{{ item.action }}</strong><small>{{ item.operator }} · {{ fmtDate(item.created_at) }}</small><p>{{ item.from_status || '初始' }} → {{ item.to_status }}</p></div></div></div></div></aside></div>
    <div v-if="showProductForm" class="modal-scrim" @click="showProductForm = false"><form class="modal-card" @submit.prevent="createProduct" @click.stop><div class="drawer-head"><div><span class="eyebrow">产品目录</span><h2>新建产品</h2></div><button type="button" class="icon-btn" @click="showProductForm = false"><X :size="19" /></button></div><label>产品名称<input v-model="productForm.name" required placeholder="例如：矿山装备质量数据集" /></label><div class="form-grid"><label>产品类型<select v-model="productForm.product_type"><option v-for="(name, key) in typeLabels" :key="key" :value="key">{{ name }}</option></select></label><label>交付方式<select v-model="productForm.delivery_method"><option value="file">文件下载</option><option value="object_storage">对象存储</option><option value="api">API 服务</option><option value="model_api">模型 API</option><option value="training">线下培训</option><option value="consulting">咨询交付</option><option value="custom">定制开发</option></select></label></div><div class="form-grid"><label>参考价格<input v-model="productForm.price" type="number" min="0" /></label><label>质量等级<input v-model="productForm.quality_level" /></label></div><label>产品简介<textarea v-model="productForm.description" rows="4" placeholder="描述产品内容、适用场景和交付边界"></textarea></label><button class="primary-btn full-btn" type="submit">创建产品草稿 <ArrowUpRight :size="16" /></button></form></div>
    <div v-if="toast" class="toast"><CheckCircle2 :size="17" />{{ toast }}</div>
  </div>
</template>
