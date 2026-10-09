<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import axios from "axios";
import "./gateway-doc.css";
import {
  Activity,
  ArrowUpRight,
  BadgeCheck,
  BarChart3,
  Bell,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ClipboardCheck,
  Copy,
  Clock3,
  Database,
  FileCheck2,
  FileText,
  LayoutDashboard,
  LogOut,
  Menu,
  Network,
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
} from "lucide-vue-next";

const api = axios.create({ baseURL: "/api" });
const token = ref(localStorage.getItem("market_token") || "");
const user = ref(null);
const enterprise = ref(null);
const loginForm = ref({ email: "admin@market.local", password: "Admin123!" });
const loginError = ref("");
const activationHint = ref("");
const registerMode = ref(false);
const registerForm = ref({
  name: "",
  email: "",
  phone: "",
  password: "",
  verification_code: "",
});
const activeView = ref("overview");
const collapsed = ref(false);
const mobileOpen = ref(false);
const loading = ref(false);
const toast = ref("");
const dashboard = ref(null);
const products = ref([]);
const productFiles = ref([]);
const productLogoPreview = ref("");
const securityReport = ref(null);
const fileReportViewer = ref(null);
const productReviewMode = ref(false);
const productDirectories = ref([]);
const orders = ref([]);
const selectedOrder = ref(null);
const orderProductFiles = ref({ download_limit: 0, items: [] });
const saasOrderState = ref({ users: [], departments: [], operations: [] });
const apiOrderState = ref({
  available: false,
  items: [],
  route_key: "",
  gateway_base_path: "",
});
const apiCredentialReveal = ref(null);
const gatewayItems = ref([]);
const gatewayOverview = ref({ summary: {}, apisix_enabled: false, metrics_url: "" });
const gatewayAlerts = ref([]);
const gatewaySelected = ref(null);
const gatewayRevisions = ref([]);
const gatewayUsage = ref({ summary: {}, items: [] });
const gatewayTab = ref("routes");
const slaOverview = ref({ summary: {}, profiles: [], results: [] });
const selectedSlaResult = ref(null);
const serviceLevelForm = ref({ code: "", name: "", description: "", customer_scope: "all", support_days_per_week: 5, support_hours_per_day: 8, online_docs: true, knowledge_base: true, standard_api: true, online_customer_service: true, dedicated_manager: false, technical_support: false, initial_response_minutes: 2880, problem_response_hours: 48, quarterly_report: false, annual_optimization: false, status: "active" });
const serviceAssignmentForm = ref({ enterprise_id: "", service_level_id: "", user_id: "", expires_at: null });
const slaEnterprises = ref([]);
const slaForm = ref({ name: "", service_scope: "platform", product_id: "", evaluation_period: "daily", availability_target: 99.9, latency_target_ms: 1000, error_rate_target: 1, delivery_hours: 24, recovery_minutes: 60, warning_margin: 0.5, description: "", status: "active" });
const gatewayForm = ref({
  upstream_url: "",
  route_key: "",
  version: "v1",
  auth_mode: "api_key",
  rate_limit_per_minute: 60,
  daily_quota: 10000,
  monthly_quota: 0,
  timeout_ms: 30000,
  strip_prefix: true,
  health_path: "/health",
  health_method: "GET",
  upstream_auth_mode: "oauth2",
  upstream_scope: "resource.invoke",
});
const development = ref({
  total: 0,
  counts: {},
  completion_rate: 0,
  items: [],
});
const deliveryItems = ref([]);
const afterSalesItems = ref([]);
const settlementItems = ref([]);
const settlementFilterItems = ref([]);
const settlementFilters = ref({ batch_id: "", settlement_id: "", order_no: "", status: "" });
const settlementFilterOrders = computed(() => {
  const seen = new Set();
  return settlementFilterItems.value.filter((item) => item.order_no && !seen.has(item.order_no) && seen.add(item.order_no));
});
const settlementDetail = ref(null);
const settlementRules = ref([]);
const settlementBatches = ref([]);
const settlementTab = ref("settlements");
const settlementBatchMonth = ref(new Date().toISOString().slice(0, 7));
const settlementRuleForm = ref({ name: "", version: "v1", platform_rate: 8, provider_rate: 67, service_rate: 20, expert_rate: 5, channel_rate: 0, change_reason: "" });
const settlementReconciliations = ref([]);
const settlementCorrections = ref([]);
const settlementAdjusting = ref(null);
const settlementAdjustForm = ref({ gross_amount: 0, cost_amount: 0, profit_amount: 0, platform_rate: 0, provider_rate: 0, service_rate: 0, expert_rate: 0, channel_rate: 0, reason: "" });
const settlementReport = ref({ summary: {}, items: [] });
const settlementReportFilters = ref({ start: "", end: "" });
const settlementMeasurements = ref([]);
const auditItems = ref([]);
const auditCategories = ref([]);
const auditSelectedCategory = ref("");
const auditSelected = ref(null);
const auditFilters = ref({ q: "", actor: "", order_id: "", batch_no: "", rule_version: "", risk_level: "", start: "", end: "", page: 1, page_size: 50, total: 0, pages: 0 });
const userItems = ref([]);
const enterpriseItems = ref([]);
const enterpriseMembers = ref([]);
const enterpriseDepartments = ref([]);
const enterpriseInvitations = ref([]);
const myEnterpriseInvitations = ref([]);
const enterpriseManageModal = ref(null);
const enterpriseManageTab = ref("details");
const inviteTarget = ref("");
const inviteDepartmentId = ref("");
const inviteChannel = ref("sms");
const newDepartment = ref({ name: "", code: "", parent_id: "" });
const personalVerificationItems = ref([]);
const userEnterpriseTab = ref("users");
const showPersonalVerification = ref(false);
const editingPersonalVerification = ref(null);
const showProfileContact = ref(false);
const profileContactForm = ref({ channel: "phone", target: "", verification_code: "123456" });
const showEnterpriseVerification = ref(false);
const platformRoleItems = ref({ roles: {}, items: [] });
const notificationSettings = ref({
  sms_provider: "",
  sms_endpoint: "",
  smtp_host: "",
  smtp_ssl: false,
  smtp_starttls: true,
  smtp_port: 587,
  smtp_username: "",
  smtp_password: "",
  smtp_from_name: "数据集运营服务管理平台",
});
const roleForm = ref({
  identifier: "",
  name: "",
  role: "platform_operator",
  channel: "email",
});
const verification = ref({ personal: null, enterprise: null });
const identityReview = ref(null);
const identityReviewImages = ref({ front: "", back: "", license: "" });
const personalEditImages = ref({ front: "", back: "" });
const identityReviewComment = ref("");
const identityImagePreview = ref(null);
const verificationForm = ref({
  id_name: "",
  id_number: "",
  phone: "",
  phone_code: "123456",
  enterprise_role: "",
  enterprise_id: "",
  enterprise_name: "",
  credit_code: "",
  enterprise_type: "有限责任公司",
  legal_representative: "",
  registered_capital: "",
  establishment_date: "",
  business_address: "",
  business_scope: "",
});
const verificationFiles = ref({ front: null, back: null, license: null });
const developmentFilter = ref("all");
const search = ref("");
const showProductForm = ref(false);
const productDetailMode = ref(false);
const productReadOnlyMode = ref(false);
const selectedProductId = ref("");
const emptyProductVersion = () => ({
  version_code: "v1.0",
  description: "",
  price: 0,
  cost: 0,
  rate_limit_per_minute: 60,
  daily_quota: 10000,
  monthly_quota: 0,
  quota_unit: "",
  quota_amount: 0,
  status: "active",
});
const emptyProductForm = () => ({
  name: "",
  product_type: "dataset",
  catalog_name: "",
  provider_name: "",
  provider_type: "企业",
  description: "",
  usage_scenarios: "",
  delivery_method: "file",
  upstream_url: "",
  application_url: "",
  integration_api_url: "",
  download_limit: 0,
  logo_file_id: "",
  pricing_strategy: "",
  versions: [emptyProductVersion()],
  quality_level: "标准",
  security_level: "一般",
  authorization_conditions: "",
  data_source_statement: "",
  compliance_statement: "",
  settlement_rule_mode: "global",
  settlement_rule_id: "",
  settlement_rule: { platform_rate: 8, provider_rate: 67, service_rate: 20, expert_rate: 5, channel_rate: 0 },
});
const productForm = ref(emptyProductForm());
let progressTimer;

const nav = [
  { key: "overview", label: "运营总览", icon: LayoutDashboard },
  { key: "products", label: "数据与服务", icon: Database },
  { key: "orders", label: "订单中心", icon: ShoppingCart },
  { key: "delivery", label: "交付与售后", icon: PackageCheck },
  { key: "settlements", label: "清算分账", icon: BarChart3 },
  { key: "audit", label: "审计日志", icon: ShieldCheck },
  { key: "gateway", label: "API 网关", icon: Network },
  { key: "sla", label: "SLA 保障", icon: Activity },
  { key: "users", label: "用户与企业", icon: Users },
  { key: "development", label: "开发进度", icon: Activity },
];

const visibleNav = computed(() => user.value?.verified_status === "verified" ? nav : nav.filter((item) => ["products", "users"].includes(item.key)));
const filteredProducts = computed(() =>
  products.value.filter((p) => !search.value || p.name.includes(search.value)),
);
const filteredOrders = computed(() =>
  orders.value.filter(
    (o) =>
      !search.value ||
      `${o.order_no}${o.product_name}${o.buyer_name}`.includes(search.value),
  ),
);
const filteredTasks = computed(() =>
  development.value.items.filter(
    (t) =>
      developmentFilter.value === "all" || t.status === developmentFilter.value,
  ),
);
const selectedProductSettlementRule = computed(() =>
  settlementRules.value.find((rule) => rule.id === productForm.value.settlement_rule_id) || null,
);
const visibleProductFiles = computed(() => {
  const files = productFiles.value.filter((item) => item.file_role !== "product_logo_thumbnail");
  const logo = files.find((item) => item.file_role === "product_logo");
  return [...files.filter((item) => item.file_role !== "product_logo"), ...(logo ? [logo] : [])];
});

const statusLabels = {
  created: "创建",
  pending_review: "待审核",
  pending_fulfillment: "待履约",
  fulfilling: "履约中",
  pending_confirmation: "待确认",
  completed: "已完成",
  closed: "已关闭",
  cancelled: "已取消",
  unpaid: "未支付",
  paying: "支付中",
  paid: "已支付",
  refunding: "退款中",
  refunded: "已退款",
  not_started: "未开始",
  preparing: "准备中",
  in_delivery: "交付中",
  retrying: "等待重试",
  pending_acceptance: "待验收",
  accepted: "验收通过",
  exception: "交付异常",
  none: "无售后",
  processing: "售后处理中",
  resolved: "已解决",
  after_closed: "售后关闭",
};
const settlementStatusLabels = { pending: "待处理", disputed: "待处理调整提案", proposal_rejected: "调整提案被拒绝", adjusted: "已调整", locked: "已锁定", paid: "已付款", superseded: "已作废" };
const settlementProposalStatusLabels = { pending: "待确认", accepted: "已确认", rejected: "已拒绝", cancelled: "已取消" };
function settlementStatusLabel(value) { return settlementStatusLabels[value] || value; }
function settlementProposalStatusLabel(value) { return settlementProposalStatusLabels[value] || value; }
const taskStatusLabels = {
  todo: "待开发",
  in_progress: "进行中",
  review: "待评审",
  blocked: "已阻塞",
  done: "已完成",
};
const typeLabels = {
  dataset: "数据集",
  model: "模型",
  api: "API 服务",
  application: "数据应用",
  saas: "SaaS 应用",
  report: "数据报告",
  training: "培训",
  consulting: "咨询",
  custom: "定制开发",
};

function fmtMoney(value) {
  return `¥${Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`;
}
const settlementLifecycleLabels = { generate_settlement_batch: "生成清算单", generate_refund_settlement_line: "生成退款负向清算单", create_refund_negative_settlement: "创建退款负向清算单", create_settlement_batch: "生成清算批次", lock_settlement: "锁定清算单", settlement_batch_ready_for_confirmation: "清算批次待确认", confirm_settlement_batch: "确认清算批次", pay_settlement_batch: "支付清算批次", create_settlement_adjustment_proposal: "提交调整提案", decide_settlement_adjustment_proposal: "处理调整提案" };
const settlementLifecycleFieldLabels = { status: "状态", net_amount: "净金额", gross_amount: "订单金额", cost_amount: "订单成本", profit_amount: "订单利润", distributable_profit: "可分配利润", refund_amount: "退款金额", platform_fee: "平台运营方", provider_share: "数据/服务提供方", service_share: "数据服务方", expert_fee: "专家", channel_fee: "渠道", batch_status: "批次状态", settlement_statuses: "清算单状态" };
function settlementLifecycleAction(action) { return settlementLifecycleLabels[action] || action; }
function settlementLifecycleValue(value, key) {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number" && (key.includes("amount") || key.includes("profit") || key.includes("fee") || key.includes("share"))) return fmtMoney(value);
  return String(value);
}
function settlementLifecycleFields(event) {
  const keys = [...new Set([...Object.keys(event.before || {}), ...Object.keys(event.after || {})])];
  return keys.map((key) => ({ key, label: settlementLifecycleFieldLabels[key] || key, before: settlementLifecycleValue(event.before?.[key], key), after: settlementLifecycleValue(event.after?.[key], key) }));
}
function fmtDate(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "-";
}
function label(value) {
  return statusLabels[value] || value;
}
function participantLabel(value) {
  return { platform: "平台运营方", provider: "数据/服务提供方", service: "数据服务方", expert: "专家", channel: "渠道" }[value] || value;
}
function notify(message) {
  toast.value = message;
  window.setTimeout(() => {
    toast.value = "";
  }, 2600);
}
function setToken(value) {
  token.value = value;
  localStorage.setItem("market_token", value);
  api.defaults.headers.common.Authorization = `Bearer ${value}`;
}

async function login() {
  loginError.value = "";
  activationHint.value = "";
  try {
    const { data } = await api.post("/auth/login", loginForm.value);
    setToken(data.token);
    await loadSession();
  } catch (error) {
    loginError.value = error.response?.data?.detail || "登录失败，请检查账号";
  }
}
async function register() {
  loginError.value = "";
  activationHint.value = "";
  try {
    const { data } = await api.post("/auth/register", registerForm.value);
    if (!data.token) {
      activationHint.value = data.development_hint || "请先激活邮箱后再登录";
      loginForm.value.email = registerForm.value.email;
      registerMode.value = false;
      return;
    }
    setToken(data.token);
    await loadSession();
    activeView.value = "products";
    showPersonalVerification.value = true;
    await loadViewData("products");
  } catch (error) {
    loginError.value =
      error.response?.data?.detail || "注册失败，请检查注册信息";
  }
}
async function sendContactCode() {
  try {
    await api.post("/auth/send-code", { channel: profileContactForm.value.channel === "email" ? "email" : "sms", target: profileContactForm.value.target, purpose: "contact_update" });
    notify("验证码已发送，10 分钟内有效");
  } catch (error) {
    notify(error.response?.data?.detail || "验证码发送失败");
  }
}
async function updateProfileContact() {
  try {
    const { data } = await api.post("/auth/profile/contact", profileContactForm.value);
    user.value = { ...user.value, ...data };
    showProfileContact.value = false;
    notify("联系方式已更新");
  } catch (error) {
    notify(error.response?.data?.detail || "联系方式更新失败");
  }
}
async function loadSession() {
  if (!token.value) return;
  api.defaults.headers.common.Authorization = `Bearer ${token.value}`;
  try {
    const { data } = await api.get("/auth/me");
    user.value = { ...data.user, enterprise_role: data.role };
    enterprise.value = data.enterprise;
    if (user.value.verified_status !== "verified") {
      activeView.value = "products";
      showPersonalVerification.value = true;
      await loadViewData("products");
    } else {
      await refreshData();
    }
  } catch {
    logout();
  }
}
function logout() {
  token.value = "";
  user.value = null;
  enterprise.value = null;
  localStorage.removeItem("market_token");
  delete api.defaults.headers.common.Authorization;
}
async function refreshData() {
  loading.value = true;
  try {
    const [d, p, o, dev] = await Promise.all([
      api.get("/dashboard"),
      api.get("/products"),
      api.get("/orders"),
      api.get("/development/tasks"),
    ]);
    dashboard.value = d.data;
    products.value = p.data.items;
    orders.value = o.data.items;
    development.value = dev.data;
  } catch {
    notify("数据刷新失败，请检查后端服务");
  } finally {
    loading.value = false;
  }
}
async function loadViewData(view) {
  if (view === "development" || view === "overview") {
    const { data } = await api.get("/development/tasks");
    development.value = data;
  }
  if (view === "products") {
    const [productData, directoryData, ruleData] = await Promise.all([
      api.get("/products"),
      api.get("/product-directories"),
      api.get("/settlement-rules").catch(() => ({ data: { items: [] } })),
    ]);
    products.value = productData.data.items;
    productDirectories.value = directoryData.data.items;
    settlementRules.value = ruleData.data.items;
    if (!productForm.value.catalog_name && productDirectories.value.length)
      productForm.value.catalog_name = productDirectories.value[0].value;
  }
  if (view === "orders") {
    const { data } = await api.get("/orders");
    orders.value = data.items;
  }
  if (view === "delivery") {
    const [delivery, afterSales] = await Promise.all([
      api.get("/delivery-tasks"),
      api.get("/after-sales"),
    ]);
    deliveryItems.value = delivery.data.items;
    afterSalesItems.value = afterSales.data.items;
  }
  if (view === "settlements") {
    const [settlements, allSettlements, rules, batches, reconciliations, corrections, report, measurements] = await Promise.all([
      api.get("/settlements", { params: settlementFilters.value }),
      api.get("/settlements"),
      api.get("/settlement-rules").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-batches").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-reconciliations").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-corrections").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-reports", { params: settlementReportFilters.value }).catch(() => ({ data: { summary: {}, items: [] } })),
      api.get("/settlement-measurements").catch(() => ({ data: { items: [] } })),
    ]);
    settlementItems.value = settlements.data.items;
    settlementFilterItems.value = allSettlements.data.items;
    settlementRules.value = rules.data.items;
    settlementBatches.value = batches.data.items;
    settlementReconciliations.value = reconciliations.data.items;
    settlementCorrections.value = corrections.data.items;
    settlementReport.value = report.data;
    settlementMeasurements.value = measurements.data.items;
  }
  if (view === "audit") {
    await loadAuditLogs();
  }
  if (view === "gateway") {
    const [routes, overview, alerts] = await Promise.all([
      api.get("/gateway/routes"),
      api.get("/gateway/overview"),
      api.get("/gateway/alerts"),
    ]);
    gatewayItems.value = routes.data.items;
    gatewayOverview.value = overview.data;
    gatewayAlerts.value = alerts.data.items;
    if (gatewayItems.value.length) await selectGateway(gatewayItems.value[0]);
  }
  if (view === "sla") {
    const [sla, enterprises] = await Promise.all([api.get("/sla/overview"), api.get("/admin/enterprises").catch(() => ({ data: { items: [] } }))]);
    slaOverview.value = sla.data;
    slaEnterprises.value = enterprises.data.items;
  }
  if (view === "users") {
    const [usersData, enterpriseData, personalData, memberData, departmentData, invitationData, myInvitationData] = await Promise.all([
      api.get("/users"),
      api.get("/admin/enterprises").catch(() => ({ data: { items: [] } })),
      api
        .get("/admin/verifications/personal")
        .catch(() => ({ data: { items: [] } })),
      api.get("/enterprise/members").catch(() => ({ data: { items: [] } })),
      api.get("/enterprise/departments").catch(() => ({ data: { items: [] } })),
      api.get("/enterprise/invitations").catch(() => ({ data: { items: [] } })),
      api.get("/enterprise/my-invitations").catch(() => ({ data: { items: [] } })),
    ]);
    userItems.value = usersData.data.items;
    enterpriseItems.value = enterpriseData.data.items;
    personalVerificationItems.value = personalData.data.items;
    enterpriseMembers.value = memberData.data.items;
    enterpriseDepartments.value = departmentData.data.items;
    enterpriseInvitations.value = invitationData.data.items;
    myEnterpriseInvitations.value = myInvitationData.data.items;
  }
  if (view === "settings") {
    const [settings, roles] = await Promise.all([
      api.get("/admin/settings/notifications"),
      api.get("/admin/platform-roles"),
    ]);
    notificationSettings.value = {
      ...notificationSettings.value,
      ...settings.data,
    };
    platformRoleItems.value = roles.data;
  }
  if (view === "verification") {
    const [personal, enterprise] = await Promise.all([
      api.get("/verification/personal/me"),
      api.get("/verification/enterprise/me").catch(() => ({ data: null })),
    ]);
    verification.value = {
      personal: personal.data,
      enterprise: enterprise.data,
    };
    if (personal.data?.item)
      Object.assign(verificationForm.value, personal.data.item);
    if (enterprise.data)
      Object.assign(verificationForm.value, {
        enterprise_name: enterprise.data.name,
        credit_code: enterprise.data.credit_code,
        enterprise_type: enterprise.data.enterprise_type,
        legal_representative: enterprise.data.legal_representative,
      });
  }
}

async function createSlaProfile() {
  if (!slaForm.value.name.trim()) return notify("请输入 SLA 规则名称");
  try {
    await api.post("/sla/profiles", slaForm.value);
    notify("SLA 规则已创建");
    slaForm.value = { name: "", service_scope: "platform", product_id: "", evaluation_period: "daily", availability_target: 99.9, latency_target_ms: 1000, error_rate_target: 1, delivery_hours: 24, recovery_minutes: 60, warning_margin: 0.5, description: "", status: "active" };
    await loadViewData("sla");
  } catch (error) { notify(error.response?.data?.detail || "SLA 规则创建失败"); }
}
async function evaluateSla() {
  try {
    await api.post("/sla/evaluate");
    notify("SLA 已重新考核");
    await loadViewData("sla");
  } catch (error) { notify(error.response?.data?.detail || "SLA 考核失败"); }
}
async function toggleSlaProfile(profile) {
  try {
    await api.patch(`/sla/profiles/${profile.id}`, { ...profile, status: profile.status === "active" ? "disabled" : "active" });
    notify("SLA 规则状态已更新");
    await loadViewData("sla");
  } catch (error) { notify(error.response?.data?.detail || "SLA 规则更新失败"); }
}
function openSlaResult(result) {
  selectedSlaResult.value = result;
}
async function createServiceLevel() {
  if (!serviceLevelForm.value.code || !serviceLevelForm.value.name) return notify("请输入服务级别编码和名称");
  try {
    await api.post("/sla/service-levels", serviceLevelForm.value);
    notify("服务级别已创建");
    serviceLevelForm.value = { code: "", name: "", description: "", customer_scope: "all", support_days_per_week: 5, support_hours_per_day: 8, online_docs: true, knowledge_base: true, standard_api: true, online_customer_service: true, dedicated_manager: false, technical_support: false, initial_response_minutes: 2880, problem_response_hours: 48, quarterly_report: false, annual_optimization: false, status: "active" };
    await loadViewData("sla");
  } catch (error) { notify(error.response?.data?.detail || "服务级别创建失败"); }
}
async function assignServiceLevel() {
  if (!serviceAssignmentForm.value.enterprise_id || !serviceAssignmentForm.value.service_level_id) return notify("请选择企业和服务级别");
  try {
    await api.post("/sla/service-level-assignments", serviceAssignmentForm.value);
    notify("服务级别已绑定到企业");
    await loadViewData("sla");
  } catch (error) { notify(error.response?.data?.detail || "服务级别绑定失败"); }
}
async function inviteEnterpriseMember() {
  if (!inviteTarget.value.trim()) return notify("请输入已注册用户的邮箱或手机号");
  try { const { data } = await api.post("/enterprise/invitations", { target: inviteTarget.value.trim(), department_id: inviteDepartmentId.value, channel: inviteChannel.value }, { params: { enterprise_id: enterpriseManageModal.value?.id } }); notify(data.invitation_message ? `邀请已创建\n${data.invitation_message}` : "邀请已创建"); inviteTarget.value = ""; inviteDepartmentId.value = ""; await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "邀请发送失败"); }
}
async function resendEnterpriseInvitation(item) {
  try { const { data } = await api.post(`/enterprise/invitations/${item.id}/resend`); notify(data.invitation_message ? `邀请已重新发送\n${data.invitation_message}` : "邀请已重新发送"); await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "重新邀请失败"); }
}
async function acceptEnterpriseInvitation(item) {
  try { await api.post(`/enterprise/invitations/${item.token}/accept`); notify(`已加入企业：${item.enterprise_name}`); await loadViewData("users"); } catch (error) { notify(error.response?.data?.detail || "接受邀请失败"); }
}
async function createDepartment() {
  if (!newDepartment.value.name.trim()) return notify("请输入部门名称");
  try { await api.post("/enterprise/departments", newDepartment.value, { params: { enterprise_id: enterpriseManageModal.value?.id } }); notify("部门已创建"); newDepartment.value = { name: "", code: "", parent_id: "" }; await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "部门创建失败"); }
}
async function updateMemberRole(item, role) {
  try { await api.patch(`/enterprise/members/${item.membership_id}`, { role }); notify("成员角色已更新"); await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "成员角色更新失败"); }
}
async function updateMemberDepartment(item, departmentId) {
  try { await api.patch(`/enterprise/members/${item.membership_id}/department`, { department_id: departmentId }); notify("成员部门已更新"); await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "成员部门更新失败"); }
}
async function deleteDepartment(item) {
  if (!window.confirm(`确认删除部门“${item.name}”吗？`)) return;
  try { await api.delete(`/enterprise/departments/${item.id}`); notify("部门已删除"); await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "部门删除失败"); }
}
async function loadEnterpriseManagement() {
  if (!enterpriseManageModal.value?.id) return;
  const enterpriseId = enterpriseManageModal.value.id;
  const [members, departments, invitations] = await Promise.all([
    api.get("/enterprise/members", { params: { enterprise_id: enterpriseId } }),
    api.get("/enterprise/departments", { params: { enterprise_id: enterpriseId } }),
    api.get("/enterprise/invitations", { params: { enterprise_id: enterpriseId } }),
  ]);
  enterpriseMembers.value = members.data.items;
  enterpriseDepartments.value = departments.data.items;
  enterpriseInvitations.value = invitations.data.items;
}
async function openEnterpriseManagement(item, tab = "details") {
  enterpriseManageModal.value = item;
  enterpriseManageTab.value = tab;
  inviteTarget.value = "";
  inviteDepartmentId.value = "";
  inviteChannel.value = "sms";
  newDepartment.value = { name: "", code: "", parent_id: "" };
  try { await loadEnterpriseManagement(); } catch (error) { notify(error.response?.data?.detail || "企业管理数据加载失败"); }
}
function closeEnterpriseManagement() {
  enterpriseManageModal.value = null;
}
function openEnterpriseResubmit(item) {
  Object.assign(verificationForm.value, { enterprise_name: item.name, credit_code: item.credit_code, enterprise_type: item.enterprise_type || "有限责任公司", legal_representative: item.legal_representative || "", registered_capital: item.registered_capital || "", establishment_date: item.establishment_date || "", business_address: item.business_address || "", business_scope: item.business_scope || "" });
  verificationFiles.value.license = null;
  showEnterpriseVerification.value = true;
}

const auditCategoryLabels = {
  "": "全部审计",
  auth: "用户与权限",
  product: "产品与服务",
  order: "订单状态",
  payment_refund: "支付与退款",
  delivery: "交付与售后",
  data_access: "数据访问/API",
  settlement: "清算全链路",
  settlement_rule: "清算规则与试算",
  measurement: "计量计费",
  settlement_payment: "分账与付款",
  reconciliation: "对账差异",
  security: "配置与安全",
  ops: "运维与任务",
};
async function loadAuditLogs() {
  const params = { ...auditFilters.value };
  delete params.total;
  delete params.pages;
  if (auditSelectedCategory.value) params.category = auditSelectedCategory.value;
  const { data } = await api.get("/audit-logs", { params });
  auditItems.value = data.items;
  auditFilters.value = { ...auditFilters.value, total: data.total, pages: data.pages, page: data.page };
  const categoryData = await api.get("/audit-logs/categories");
  auditCategories.value = categoryData.data.items;
}
function selectAuditCategory(category) {
  auditSelectedCategory.value = category;
  auditFilters.value.page = 1;
  loadAuditLogs().catch(() => notify("审计日志加载失败"));
}
function resetAuditFilters() {
  auditFilters.value = { q: "", actor: "", order_id: "", batch_no: "", rule_version: "", risk_level: "", start: "", end: "", page: 1, page_size: 50, total: 0, pages: 0 };
  auditSelectedCategory.value = "";
  loadAuditLogs().catch(() => notify("审计日志加载失败"));
}
function auditPage(delta) {
  const next = auditFilters.value.page + delta;
  if (next < 1 || (auditFilters.value.pages && next > auditFilters.value.pages)) return;
  auditFilters.value.page = next;
  loadAuditLogs().catch(() => notify("审计日志加载失败"));
}
async function selectGateway(item) {
  gatewaySelected.value = item;
  try {
    const [config, revisions, usage] = await Promise.all([
      api.get(`/products/${item.product_id}/gateway-config`),
      api.get(`/products/${item.product_id}/gateway-revisions`),
      api.get(`/products/${item.product_id}/gateway-usage`),
    ]);
    gatewayForm.value = {
      ...gatewayForm.value,
      ...(config.data.item || {}),
    };
    gatewayRevisions.value = revisions.data.items;
    gatewayUsage.value = usage.data;
  } catch (error) {
    notify(error.response?.data?.detail || "网关数据加载失败");
  }
}
async function saveGatewayConfig() {
  if (!gatewaySelected.value) return;
  try {
    await api.put(
      `/products/${gatewaySelected.value.product_id}/gateway-config`,
      gatewayForm.value,
    );
    notify("网关配置已保存");
    await loadViewData("gateway");
  } catch (error) {
    notify(error.response?.data?.detail || "网关配置保存失败");
  }
}
async function validateGatewayConfig() {
  if (!gatewaySelected.value) return;
  try {
    const { data } = await api.post(
      `/products/${gatewaySelected.value.product_id}/gateway-config/validate`,
    );
    notify(data.valid ? "网关配置校验通过" : "网关配置校验未通过");
  } catch (error) {
    notify(error.response?.data?.detail || "网关配置校验失败");
  }
}
async function publishGatewayConfig() {
  if (!gatewaySelected.value) return;
  try {
    await api.post(
      `/products/${gatewaySelected.value.product_id}/gateway-config/publish`,
    );
    notify("网关路由已发布");
    await loadViewData("gateway");
  } catch (error) {
    notify(error.response?.data?.detail || "网关路由发布失败");
  }
}
async function rollbackGatewayConfig() {
  if (!gatewaySelected.value || !window.confirm("确认回滚到上一稳定网关配置吗？")) return;
  try {
    await api.post(
      `/products/${gatewaySelected.value.product_id}/gateway-config/rollback`,
    );
    notify("网关配置已回滚");
    await loadViewData("gateway");
  } catch (error) {
    notify(error.response?.data?.detail || "网关回滚失败");
  }
}
function selectView(view) {
  activeView.value = view;
  mobileOpen.value = false;
  loadViewData(view);
}
async function openOrder(order) {
  const { data } = await api.get(`/orders/${order.id}`);
  selectedOrder.value = data;
  saasOrderState.value = { users: [], departments: [], operations: [] };
  apiOrderState.value = {
    available: false,
    items: [],
    route_key: "",
    gateway_base_path: "",
  };
  apiCredentialReveal.value = null;
  orderProductFiles.value = { download_limit: 0, items: [] };
  const requests = [];
  if (data.order.payment_status === "paid") {
    requests.push(api.get(`/orders/${order.id}/product-files`).then(({ data: files }) => { orderProductFiles.value = files; }).catch(() => {}));
  }
  if (data.order.subscription_id) {
    const id = data.order.subscription_id;
    requests.push(
      Promise.all([
        api.get(`/saas-subscriptions/${id}/users`),
        api.get(`/saas-subscriptions/${id}/departments`),
        api.get(`/saas-subscriptions/${id}/operations`),
      ]).then(([users, departments, operations]) => {
        saasOrderState.value = {
          users: users.data.items,
          departments: departments.data.items,
          operations: operations.data.items,
        };
      }),
    );
  }
  requests.push(
    api
      .get(`/orders/${order.id}/api-credentials`)
      .then(({ data: credentials }) => {
        apiOrderState.value = { ...credentials, available: true };
      })
      .catch(() => {}),
  );
  await Promise.all(requests);
}
async function downloadOrderProductFile(file) {
  if (!selectedOrder.value) return;
  try {
    const response = await api.get(`/files/${file.id}/download`, { params: { order_id: selectedOrder.value.order.id }, responseType: "blob" });
    const url = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = file.original_name;
    link.click();
    URL.revokeObjectURL(url);
    await openOrder(selectedOrder.value.order);
  } catch (error) {
    notify(error.response?.data?.detail || "文件下载失败");
  }
}
async function transition(action) {
  if (!selectedOrder.value) return;
  try {
    await api.post(`/orders/${selectedOrder.value.order.id}/transition`, {
      action,
      reason: "工作台操作",
    });
    notify("订单状态已更新");
    await openOrder(selectedOrder.value.order);
    await refreshData();
  } catch (error) {
    notify(error.response?.data?.detail || "状态操作失败");
  }
}
async function saasOrderAction(action) {
  const id = selectedOrder.value?.order?.subscription_id;
  if (!id) return;
  try {
    if (action === "renew") {
      const cycle = window.prompt(
        "续费周期：monthly、quarterly、annual、perpetual",
        "annual",
      );
      if (!cycle) return;
      await api.post(`/saas-subscriptions/${id}/renew`, {
        billing_cycle: cycle,
      });
    } else if (action === "change") {
      const versionId = window.prompt("请输入目标 SaaS 版本 ID");
      if (!versionId) return;
      const cycle = window.prompt(
        "计费周期：monthly、quarterly、annual、perpetual",
        selectedOrder.value.order.billing_cycle || "annual",
      );
      if (!cycle) return;
      await api.post(`/saas-subscriptions/${id}/change-version`, {
        version_id: versionId,
        billing_cycle: cycle,
      });
    } else {
      await api.post(`/saas-subscriptions/${id}/${action}`);
    }
    notify("SaaS 订阅操作已提交");
    await openOrder(selectedOrder.value.order);
    await refreshData();
  } catch (error) {
    notify(error.response?.data?.detail || "SaaS 订阅操作失败");
  }
}
async function saasSyncUser() {
  const id = selectedOrder.value?.order?.subscription_id;
  if (!id) return;
  const userId = window.prompt("请输入企业成员用户 ID");
  if (!userId) return;
  const departmentId = window.prompt("请输入平台部门 ID（可留空）", "") || "";
  try {
    await api.post(`/saas-subscriptions/${id}/users`, {
      user_id: userId,
      department_id: departmentId,
    });
    notify("用户已同步到 SaaS");
    await openOrder(selectedOrder.value.order);
  } catch (error) {
    notify(error.response?.data?.detail || "用户同步失败");
  }
}
async function saasRemoveUser(item) {
  try {
    await api.delete(
      `/saas-subscriptions/${selectedOrder.value.order.subscription_id}/users/${item.user_id}`,
    );
    notify("SaaS 用户已删除");
    await openOrder(selectedOrder.value.order);
  } catch (error) {
    notify(error.response?.data?.detail || "删除 SaaS 用户失败");
  }
}
async function saasSyncDepartment() {
  const id = selectedOrder.value?.order?.subscription_id;
  if (!id) return;
  const name = window.prompt("请输入部门名称");
  if (!name) return;
  const parentId = window.prompt("请输入父部门 ID（可留空）", "") || "";
  try {
    await api.post(`/saas-subscriptions/${id}/departments`, {
      name,
      parent_id: parentId,
    });
    notify("部门已同步到 SaaS");
    await openOrder(selectedOrder.value.order);
  } catch (error) {
    notify(error.response?.data?.detail || "部门同步失败");
  }
}
async function saasRemoveDepartment(item) {
  try {
    await api.delete(
      `/saas-subscriptions/${selectedOrder.value.order.subscription_id}/departments/${item.id}`,
    );
    notify("SaaS 部门已删除");
    await openOrder(selectedOrder.value.order);
  } catch (error) {
    notify(error.response?.data?.detail || "删除 SaaS 部门失败");
  }
}
async function createOrderApiCredential() {
  const order = selectedOrder.value?.order;
  if (!order) return;
  try {
    const { data } = await api.post(`/orders/${order.id}/api-credentials`, {
      name: `${order.product_name} API 凭据`,
    });
    apiCredentialReveal.value = data.api_key ? data : null;
    await openOrder(order);
    apiCredentialReveal.value = data.api_key ? data : null;
    notify(data.api_key ? "企业共享 API 凭据已生成，请立即保存 API Key" : "已复用企业共享 API 凭据，本次订单额度已合并");
  } catch (error) {
    notify(error.response?.data?.detail || "API 凭据生成失败");
  }
}
async function revokeOrderApiCredential(item) {
  const order = selectedOrder.value?.order;
  if (
    !order ||
    !window.confirm(
      `确认停用凭据 ${item.key_prefix}？停用后将立即无法调用 API。`,
    )
  )
    return;
  try {
    await api.post(`/orders/${order.id}/api-credentials/${item.id}/revoke`);
    notify("API 凭据已停用");
    await openOrder(order);
  } catch (error) {
    notify(error.response?.data?.detail || "API 凭据停用失败");
  }
}
async function regenerateOrderApiCredential(item) {
  const order = selectedOrder.value?.order;
  if (
    !order ||
    !window.confirm(`确认重新生成凭据 ${item.key_prefix}？旧凭据将立即失效。`)
  )
    return;
  try {
    const { data } = await api.post(
      `/orders/${order.id}/api-credentials/${item.id}/regenerate`,
    );
    await openOrder(order);
    apiCredentialReveal.value = data;
    notify("API 凭据已重新生成，请立即保存新 API Key");
  } catch (error) {
    notify(error.response?.data?.detail || "API 凭据重新生成失败");
  }
}
async function copyApiCredential() {
  if (apiCredentialReveal.value?.api_key) {
    await navigator.clipboard.writeText(apiCredentialReveal.value.api_key);
    notify("API Key 已复制");
  }
}
async function createProduct() {
  try {
    const { data } = await api.post("/products", {
      ...productForm.value,
      catalog_name:
        productForm.value.catalog_name ||
        productDirectories.value[0]?.value ||
        "未分类",
    });
    if (productForm.value.logoFile) {
      const form = new FormData();
      form.append("upload", productForm.value.logoFile);
      form.append("product_id", data.id);
      form.append("file_role", "product_logo");
      const uploaded = await api.post("/files/upload", form);
      await api.put(`/products/${data.id}`, { ...productForm.value, logo_file_id: uploaded.data.id, logoFile: undefined });
    }
    if (productForm.value.fileUpload) {
      const form = new FormData();
      form.append("upload", productForm.value.fileUpload);
      form.append("product_id", data.id);
      form.append("file_role", "product_data");
      form.append("version_id", data.versions?.[0]?.id || "");
      form.append("version", data.versions?.[0]?.version_code || "v1.0");
      await api.post("/files/upload", form);
    }
    showProductForm.value = false;
    productForm.value = {
      ...emptyProductForm(),
      catalog_name: productDirectories.value[0]?.value || "",
    };
    notify("产品草稿已创建");
    await loadViewData("products");
  } catch (error) {
    notify(error.response?.data?.detail || "创建失败");
  }
}
function openNewProduct() {
  productDetailMode.value = false;
  productReadOnlyMode.value = false;
  productReviewMode.value = false;
  selectedProductId.value = "";
  productForm.value = {
    ...emptyProductForm(),
    catalog_name: productDirectories.value[0]?.value || "",
  };
  showProductForm.value = true;
}
async function openProductDetail(product, review = false) {
  selectedProductId.value = product.id;
  productDetailMode.value = true;
  productReviewMode.value = review && canReviewProduct(product);
  productReadOnlyMode.value = !["draft", "rejected"].includes(product.status);
  productForm.value = JSON.parse(JSON.stringify({
    ...emptyProductForm(),
    ...product,
    versions: product.versions?.length ? product.versions : [emptyProductVersion()],
  }));
  productForm.value.logoFile = null;
  productFiles.value = [];
  productLogoPreview.value = "";
  try {
    const { data } = await api.get(`/products/${product.id}/files`);
    productFiles.value = data.items || [];
    if (product.logo_thumbnail_file_id) {
      const response = await api.get(`/files/${product.logo_thumbnail_file_id}/download`, { responseType: "blob" });
      productLogoPreview.value = URL.createObjectURL(response.data);
    }
  } catch {
    productFiles.value = [];
  }
  showProductForm.value = true;
}
function isPlatformRole() {
  return ["super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"].includes(user.value?.platform_role);
}
async function downloadProductFile(file, reportType = "") {
  try {
    const suffix = reportType ? `?report_type=${reportType}` : "";
    const endpoint = reportType ? `/files/${file.id}/scan-report.pdf` : `/files/${file.id}/download`;
    const { data } = await api.get(`${endpoint}${suffix}`, { responseType: "blob" });
    const url = URL.createObjectURL(data);
    const link = document.createElement("a");
    link.href = url;
    link.download = reportType ? `${file.original_name}.${reportType}.scan-report.pdf` : file.original_name;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    notify(error.response?.data?.detail || "文件下载失败");
  }
}
async function viewProductReport(file, reportType) {
  try {
    const { data } = await api.get(`/files/${file.id}/scan-report.pdf?report_type=${reportType}`, { responseType: "blob" });
    const url = URL.createObjectURL(data);
    window.open(url, "_blank", "noopener");
  } catch (error) {
    notify(error.response?.data?.detail || "报告打开失败");
  }
}
function openFileReport(file, reportType) {
  fileReportViewer.value = { file, reportType };
}
async function reviewProductFromDetail(decision) {
  const product = productForm.value;
  if (product.status === "security_review") {
    const comment = window.prompt(decision === "approve" ? "请输入安全审核意见" : "请输入安全审核驳回原因", decision === "approve" ? "安全审核通过" : "");
    if (!comment) return;
    await api.post(`/products/${product.id}/security-review`, { decision, comment });
  } else {
    const comment = window.prompt(decision === "approve" ? "请输入审核意见" : "请输入驳回原因", decision === "approve" ? "审核通过" : "");
    if (!comment) return;
    await api.post(`/products/${product.id}/review`, { decision, comment });
  }
  showProductForm.value = false;
  productReviewMode.value = false;
  notify(decision === "approve" ? "审核已通过" : "产品已驳回");
  await loadViewData("products");
}
async function saveProductEdit() {
  if (!selectedProductId.value || productReadOnlyMode.value) return;
  try {
    const payload = {
      ...productForm.value,
      catalog_name: productForm.value.catalog_name || "未分类",
      logoFile: undefined,
    };
    if (productForm.value.logoFile) {
      const form = new FormData();
      form.append("upload", productForm.value.logoFile);
      form.append("product_id", selectedProductId.value);
      form.append("file_role", "product_logo");
      const uploaded = await api.post("/files/upload", form);
      payload.logo_file_id = uploaded.data.id;
    }
    if (productForm.value.fileUpload) {
      const form = new FormData();
      form.append("upload", productForm.value.fileUpload);
      form.append("product_id", selectedProductId.value);
      form.append("file_role", "product_data");
      form.append("version_id", productForm.value.versions[0]?.id || "");
      form.append("version", productForm.value.versions[0]?.version_code || "v1.0");
      await api.post("/files/upload", form);
    }
    await api.put(`/products/${selectedProductId.value}`, payload);
    showProductForm.value = false;
    notify("产品信息已保存");
    await loadViewData("products");
  } catch (error) {
    notify(error.response?.data?.detail || "产品信息保存失败");
  }
}
function addProductVersion() {
  productForm.value.versions.push({
    version_code: `v${productForm.value.versions.length + 1}.0`,
    description: "",
    price: 0,
    rate_limit_per_minute: 60,
    daily_quota: 10000,
    monthly_quota: 0,
    status: "active",
  });
}
function removeProductVersion(index) {
  if (productForm.value.versions.length <= 1)
    return notify("至少保留一个产品版本");
  productForm.value.versions.splice(index, 1);
}
function canReviewProduct(product) {
  const role = user.value?.platform_role;
  if (!["pending_review", "quality_review", "security_review", "operation_review"].includes(product.status)) return false;
  if (role === "super_admin") return true;
  if (product.status === "pending_review") return ["product_manager", "business_reviewer"].includes(role);
  if (product.status === "quality_review") return role === "quality_reviewer";
  if (product.status === "security_review") return ["security_compliance", "platform_operator"].includes(role);
  if (product.status === "operation_review") return role === "platform_operator";
  return false;
}
async function productAction(product, action) {
  try {
    if (action === "unpublish") {
      const reason = window.prompt("请输入下架原因", "产品提供方主动下架");
      if (!reason) return;
      await api.post(`/products/${product.id}/unpublish`, { reason });
    } else if (action === "withdraw") {
      const reason = window.prompt("请输入撤回原因", "企业管理员撤回审核");
      if (!reason) return;
      await api.post(`/products/${product.id}/withdraw`, { reason });
    } else if (action === "security_check") {
      const { data } = await api.post(`/products/${product.id}/security-check`);
      notify(data.unpublished ? "安全策略检查发现问题，产品已自动下架" : "安全策略检查通过");
      await loadViewData("products");
      return;
    } else if (action === "security_report") {
      const { data } = await api.get(`/products/${product.id}/security-report`);
      securityReport.value = data.security_report;
      return;
    } else if (action === "security_approve" || action === "security_reject") {
      let comment = window.prompt(action === "security_approve" ? "请输入安全审核意见" : "请输入安全审核驳回原因", action === "security_approve" ? "安全审核通过" : "");
      if (!comment) return;
      await api.post(`/products/${product.id}/security-review`, { decision: action === "security_approve" ? "approve" : "reject", comment });
    } else if (action === "review") {
      const comment = window.prompt("请输入审核意见", "审核通过");
      if (comment === null) return;
      await api.post(`/products/${product.id}/review`, {
        decision: "approve",
        comment,
      });
    } else if (action === "reject") {
      const comment = window.prompt("请输入驳回原因", "");
      if (!comment) return notify("驳回时必须填写原因");
      await api.post(`/products/${product.id}/review`, {
        decision: "reject",
        comment,
      });
    } else {
      await api.post(`/products/${product.id}/submit`);
    }
    notify(
      action === "unpublish"
        ? "产品已下架，已售应用继续按原授权提供服务"
        : action === "submit"
          ? "产品已提交审核"
        : action === "reject"
          ? "产品已驳回"
          : "产品已审核通过",
    );
    await loadViewData("products");
  } catch (error) {
    notify(error.response?.data?.detail || "产品状态操作失败");
  }
}
async function downloadSaasCredentials(product) {
  try {
    const { data } = await api.get(
      `/products/${product.id}/oauth-credentials-download`,
      { responseType: "blob" },
    );
    const url = URL.createObjectURL(data);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${product.name}-platform-oauth-credentials.txt`;
    link.click();
    URL.revokeObjectURL(url);
    notify("OAuth 凭据文件已下载");
  } catch (error) {
    notify(error.response?.data?.detail || "OAuth 凭据下载失败");
  }
}
async function updateTask(task, status) {
  try {
    await api.patch(`/development/tasks/${task.code}`, {
      status,
      progress: status === "done" ? 100 : task.progress,
      note: "工作台任务状态更新",
    });
    await loadViewData("development");
    notify(`${task.code} 已更新为${taskStatusLabels[status]}`);
  } catch (error) {
    notify(error.response?.data?.detail || "任务更新失败");
  }
}
async function generateSettlement(order) {
  try {
    await api.post(`/settlements/generate/${order.id}`);
    notify("清算单已生成");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算生成失败");
  }
}
async function searchSettlements() {
  await loadViewData("settlements");
}
function updateUiScale() {
  const width = window.innerWidth;
  const height = window.innerHeight || 900;
  if (width <= 760) {
    document.documentElement.style.setProperty("--ui-scale", "1");
    return;
  }
  const ratio = width / height;
  let scale = 1;
  if (window.devicePixelRatio >= 2) scale -= 0.08;
  else if (window.devicePixelRatio >= 1.5) scale -= 0.04;
  if (ratio < 1.2) scale -= 0.04;
  if (height < 800) scale -= 0.03;
  document.documentElement.style.setProperty("--ui-scale", String(Math.max(0.86, Math.min(1, scale))));
}
async function openSettlementDetail(item) {
  try {
    const { data } = await api.get(`/settlements/${item.id}/detail`);
    settlementDetail.value = data;
  } catch (error) {
    notify(error.response?.data?.detail || "清算单详情加载失败");
  }
}
function closeSettlementDetail() {
  settlementDetail.value = null;
}
function openSettlementAdjustment(item) {
  settlementAdjusting.value = item;
  settlementAdjustForm.value = { gross_amount: item.gross_amount, cost_amount: item.cost_amount, profit_amount: item.profit_amount, platform_rate: item.platform_rate || 0, provider_rate: item.provider_rate || 0, service_rate: item.service_rate || 0, expert_rate: item.expert_rate || 0, channel_rate: item.channel_rate || 0, reason: "" };
}
function closeSettlementAdjustment() {
  settlementAdjusting.value = null;
}
function adjustmentShare(rate) {
  return ((Number(settlementAdjustForm.value.profit_amount || 0) * Number(rate || 0)) / 100).toFixed(2);
}
async function adjustSettlement() {
  const form = settlementAdjustForm.value;
  if (!form.reason || form.reason.trim().length < 2) return notify("清算调整必须填写原因");
  const totalRate = [form.platform_rate, form.provider_rate, form.service_rate, form.expert_rate, form.channel_rate].reduce((sum, value) => sum + Number(value || 0), 0);
  if (Math.abs(totalRate - 100) > 0.01) return notify("五方分成比例合计必须为100%");
  if (Math.abs(Number(form.gross_amount) - Number(form.cost_amount) - Number(form.profit_amount)) > 0.01) return notify("订单利润必须等于订单金额减订单成本");
  try {
    await api.post(`/settlements/${settlementAdjusting.value.id}/proposals`, { ...form, gross_amount: Number(form.gross_amount), cost_amount: Number(form.cost_amount), profit_amount: Number(form.profit_amount), platform_rate: Number(form.platform_rate), provider_rate: Number(form.provider_rate), service_rate: Number(form.service_rate), expert_rate: Number(form.expert_rate), channel_rate: Number(form.channel_rate) });
    notify("调整提案已提交，等待其他清算参与方确认");
    closeSettlementAdjustment();
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算调整失败");
  }
}
async function lockSettlement(item) {
  if (!window.confirm("锁定后不能直接调整，确认锁定该清算单吗？")) return;
  try {
    await api.post(`/settlements/${item.id}/lock`);
    notify("清算单已锁定");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算锁定失败");
  }
}
async function decideSettlementProposal(proposal, decision) {
  const comment = window.prompt(decision === "approve" ? "请输入提案确认意见" : "请输入提案拒绝原因", "")
  if (comment === null) return;
  try {
    await api.post(`/settlement-proposals/${proposal.id}/decision`, { decision, comment });
    notify(decision === "approve" ? "调整提案已确认" : "调整提案已拒绝");
    if (settlementDetail.value) await openSettlementDetail({ id: settlementDetail.value.id });
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "调整提案处理失败");
  }
}
async function createSettlementRule() {
  if (!settlementRuleForm.value.name) return notify("请填写清算规则名称");
  try {
    await api.post("/settlement-rules", settlementRuleForm.value);
    notify("清算规则已保存");
    settlementRuleForm.value.name = "";
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算规则保存失败");
  }
}
async function decideSettlementRule(rule, decision) {
  try {
    await api.post(`/settlement-rules/${rule.id}/decision`, { decision, comment: "工作台操作" });
    notify("清算规则状态已更新");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算规则操作失败");
  }
}
async function simulateSettlementRule(rule) {
  try {
    const { data } = await api.post(`/settlement-rules/${rule.id}/simulate`, { cycle: "manual" });
    notify(`试算完成，共 ${data.total} 条订单`);
  } catch (error) {
    notify(error.response?.data?.detail || "清算规则试算失败");
  }
}
async function generateSettlementBatch() {
  const month = window.prompt("请输入要清算的自然月（格式 YYYY-MM）", settlementBatchMonth.value);
  if (month === null) return;
  if (!/^[0-9]{4}-[0-9]{2}$/.test(month)) return notify("月份格式应为 YYYY-MM");
  settlementBatchMonth.value = month;
  const [year, monthNumber] = month.split("-").map(Number);
  const start = `${month}-01T00:00:00Z`;
  const endDate = new Date(Date.UTC(monthNumber === 12 ? year + 1 : year, monthNumber === 12 ? 0 : monthNumber, 1));
  const end = endDate.toISOString();
  const ruleId = settlementRules.value.find((x) => x.status === "active")?.id || "";
  try {
    await api.post("/settlement-batches", { cycle: "monthly", period_start: start, period_end: end, rule_id: ruleId, idempotency_key: `monthly:${month}` });
    notify(`${month} 清算批次已生成`);
    await loadViewData("settlements");
  } catch (error) {
    if (error.response?.status === 409 && error.response?.data?.detail?.includes("未完成清算批次")) {
      if (!window.confirm(`${month} 已存在未完成清算批次，是否作废原批次并重新生成？`)) return;
      try {
        await api.post("/settlement-batches", { cycle: "monthly", period_start: start, period_end: end, rule_id: ruleId, rebuild: true, idempotency_key: `monthly:${month}:rebuild:${Date.now()}` });
        notify(`${month} 原清算批次已作废并重新生成`);
        await loadViewData("settlements");
        return;
      } catch (rebuildError) {
        notify(rebuildError.response?.data?.detail || "清算批次重算失败");
        return;
      }
    }
    notify(error.response?.data?.detail || "清算批次生成失败");
  }
}
async function batchAction(batch, action) {
  try {
    await api.post(`/settlement-batches/${batch.id}/${action}`, { comment: "工作台操作" });
    notify(action === "pay" ? "批次已完成模拟付款" : action === "dispute" ? "批次已标记异议" : "批次已确认");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算批次操作失败");
  }
}
async function closeReconciliation(item) {
  const resolution = window.prompt("请输入差异关闭依据", "已完成支付流水核对");
  if (!resolution) return;
  try {
    await api.post(`/settlement-reconciliations/${item.id}/close`, { resolution });
    notify("对账差异已关闭");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "差异关闭失败");
  }
}
async function createCorrection(item) {
  const amount = window.prompt("请输入冲正/追回金额", "0");
  if (!amount || Number.isNaN(Number(amount)) || Number(amount) <= 0) return;
  const reason = window.prompt("请输入调整原因", "退款后清算冲正");
  if (!reason) return;
  try {
    await api.post(`/settlements/${item.id}/corrections`, { correction_type: "reversal", amount: Number(amount), reason, recovery_mode: "future_offset" });
    notify("清算冲正已提交审批");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算冲正提交失败");
  }
}
async function approveCorrection(item) {
  try {
    await api.post(`/settlement-corrections/${item.id}/approve`, { comment: "工作台审批" });
    notify("清算调整已审批");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "清算调整审批失败");
  }
}
async function loadSettlementReport() {
  try { const { data } = await api.get("/settlement-reports", { params: settlementReportFilters.value }); settlementReport.value = data; } catch (error) { notify(error.response?.data?.detail || "清算报表加载失败"); }
}
async function exportSettlementReport(kind = "details") {
  try {
    const response = await api.get("/settlement-reports/export", { params: { ...settlementReportFilters.value, kind }, responseType: "blob" });
    const url = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = kind === "summary" ? "settlement-summary.csv" : "settlement-details.csv";
    link.click();
    URL.revokeObjectURL(url);
    notify("清算报表已导出");
  } catch (error) {
    notify(error.response?.data?.detail || "清算报表导出失败");
  }
}
async function createMeasurement() {
  const orderId = window.prompt("请输入订单 ID", orders.value[0]?.id || "");
  if (!orderId) return;
  const measurementType = window.prompt("计量类型，如 api_call、download、training_hours", "api_call");
  const quantity = window.prompt("计量数量", "1");
  if (!measurementType || !quantity || Number(quantity) < 0) return;
  try {
    await api.post("/settlement-measurements", { order_id: orderId, measurement_type: measurementType, quantity: Number(quantity), unit: "count", source: "运营工作台" });
    notify("计量数据已登记");
    await loadViewData("settlements");
  } catch (error) {
    notify(error.response?.data?.detail || "计量数据登记失败");
  }
}
async function saveNotificationSettings() {
  try {
    await api.patch(
      "/admin/settings/notifications",
      notificationSettings.value,
    );
    notificationSettings.value.smtp_password = "";
    notify("通知平台配置已保存");
  } catch (error) {
    notify(error.response?.data?.detail || "通知配置保存失败");
  }
}
async function assignPlatformRole() {
  try {
    const { data } = await api.post("/admin/platform-roles", roleForm.value);
    notify(`已分配${data.role_name}，临时密码已生成`);
    roleForm.value = {
      identifier: "",
      name: "",
      role: "platform_operator",
      channel: "email",
    };
    await loadViewData("settings");
  } catch (error) {
    notify(error.response?.data?.detail || "平台角色分配失败");
  }
}
async function uploadVerificationFile(file, fileRole = "product_data") {
  if (!file) return "";
  const form = new FormData();
  form.append("upload", file);
  form.append("file_role", fileRole);
  const { data } = await api.post("/files/upload", form);
  return data.id;
}
function canReviewIdentity() {
  return ["super_admin", "platform_operator"].includes(user.value?.platform_role);
}
function canManageEnterprise(item) {
  return canReviewIdentity() || (item?.id === enterprise.value?.id && ["super_admin", "enterprise_admin"].includes(user.value?.enterprise_role));
}
async function openVerificationFile(fileId) {
  try {
    const response = await api.get(`/files/${fileId}/download`, { responseType: "blob" });
    const url = URL.createObjectURL(response.data);
    window.open(url, "_blank", "noopener");
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    notify(error.response?.data?.detail || "文件查看失败");
  }
}
async function loadReviewImage(fileId, key) {
  if (!fileId) return;
  try {
    const response = await api.get(`/files/${fileId}/download`, { responseType: "blob" });
    identityReviewImages.value = { ...identityReviewImages.value, [key]: URL.createObjectURL(response.data) };
  } catch (error) {
    notify(error.response?.data?.detail || "实名材料加载失败");
  }
}
async function openPersonalReview(item) {
  const applicant = userItems.value.find((userItem) => userItem.id === item.user_id) || {};
  identityReview.value = { kind: "personal", item, applicant };
  identityReviewComment.value = "";
  identityReviewImages.value = { front: "", back: "", license: "" };
  await Promise.all([loadReviewImage(item.id_front_file_id, "front"), loadReviewImage(item.id_back_file_id, "back")]);
}
async function openPersonalEdit(item) {
  editingPersonalVerification.value = item;
  Object.assign(verificationForm.value, { id_name: item.id_name || "", id_number: item.id_number || "", phone: item.phone || user.value?.phone || "", enterprise_id: item.enterprise_id || "", enterprise_role: item.enterprise_role || "", phone_code: "123456" });
  verificationFiles.value.front = null;
  verificationFiles.value.back = null;
  personalEditImages.value = { front: "", back: "" };
  showPersonalVerification.value = true;
  await Promise.all([loadPersonalEditImage(item.id_front_file_id, "front"), loadPersonalEditImage(item.id_back_file_id, "back")]);
}
async function loadPersonalEditImage(fileId, key) {
  if (!fileId) return;
  try {
    const response = await api.get(`/files/${fileId}/download`, { responseType: "blob" });
    personalEditImages.value = { ...personalEditImages.value, [key]: URL.createObjectURL(response.data) };
  } catch (error) {
    notify(error.response?.data?.detail || "身份证图片加载失败");
  }
}
async function openEnterpriseReview(item) {
  identityReview.value = { kind: "enterprise", item };
  identityReviewComment.value = "";
  identityReviewImages.value = { front: "", back: "", license: "" };
  await loadReviewImage(item.license_file_id, "license");
}
async function submitIdentityReview(decision) {
  if (!identityReview.value) return;
  if (decision === "reject" && !identityReviewComment.value.trim()) return notify("拒绝实名必须填写原因");
  try {
    const path = identityReview.value.kind === "personal" ? `/admin/verifications/personal/${identityReview.value.item.id}/review` : `/admin/verifications/enterprise/${identityReview.value.item.id}/review`;
    await api.post(path, { decision, comment: identityReviewComment.value.trim() });
    notify(decision === "approve" ? "实名认证已通过" : "实名认证已拒绝");
    identityReview.value = null;
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "实名认证审核失败");
  }
}
async function submitPersonalVerification() {
  try {
    const current = editingPersonalVerification.value;
    const front = verificationFiles.value.front ? await uploadVerificationFile(verificationFiles.value.front, "identity_id_front") : current?.id_front_file_id || "";
    const back = verificationFiles.value.back ? await uploadVerificationFile(verificationFiles.value.back, "identity_id_back") : current?.id_back_file_id || "";
    const payload = {
      ...verificationForm.value,
      id_front_file_id: front,
      id_back_file_id: back,
    };
    if (current) await api.put(`/verification/personal/${current.id}`, payload);
    else await api.post("/verification/personal", payload);
    notify("个人实名认证申请已提交");
    showPersonalVerification.value = false;
    editingPersonalVerification.value = null;
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "个人实名认证提交失败");
  }
}
async function submitEnterpriseVerification() {
  try {
    const license = await uploadVerificationFile(verificationFiles.value.license, "enterprise_license");
    await api.post("/verification/enterprise", {
      license_file_id: license,
      enterprise_name: verificationForm.value.enterprise_name,
      credit_code: verificationForm.value.credit_code,
      enterprise_type: verificationForm.value.enterprise_type,
      legal_representative: verificationForm.value.legal_representative,
      registered_capital: verificationForm.value.registered_capital,
      establishment_date: verificationForm.value.establishment_date,
      business_address: verificationForm.value.business_address,
      business_scope: verificationForm.value.business_scope,
    });
    notify("企业实名认证申请已提交");
    showEnterpriseVerification.value = false;
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "企业实名认证提交失败");
  }
}
function identityStatusLabel(status) {
  return (
    {
      pending: "待实名",
      pending_review: "待审核",
      verified: "已实名",
      rejected: "已拒绝",
    }[status] || "待实名"
  );
}
function identityStatusClass(status) {
  return status === "verified"
    ? "status-done"
    : status === "rejected"
      ? "status-blocked"
      : status === "pending_review"
        ? "status-review"
        : "status-todo";
}
function personalForUser(userId) {
  return personalVerificationItems.value.find(
    (item) => item.user_id === userId,
  );
}
async function reviewPersonal(item, decision) {
  const comment = window.prompt(
    decision === "approve" ? "请输入审核意见" : "请输入拒绝原因",
    decision === "approve" ? "人工审核通过" : "",
  );
  if (!comment) return;
  try {
    await api.post(`/admin/verifications/personal/${item.id}/review`, {
      decision,
      comment,
    });
    notify("个人实名认证状态已更新");
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "实名认证审核失败");
  }
}
async function reviewEnterprise(item, decision) {
  const comment = window.prompt(
    decision === "approve" ? "请输入审核意见" : "请输入拒绝原因",
    decision === "approve" ? "人工审核通过" : "",
  );
  if (!comment) return;
  try {
    await api.post(`/admin/verifications/enterprise/${item.id}/review`, {
      decision,
      comment,
    });
    notify("企业实名认证状态已更新");
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "企业认证审核失败");
  }
}
function taskCount(key) {
  return development.value.items.filter((task) => task.status === key).length;
}
function nextActions(order) {
  const actions = [];
  if (order.main_status === "created")
    actions.push(["submit_review", "提交审核"]);
  if (order.main_status === "pending_review")
    actions.push(["approve", "审核通过"]);
  if (order.payment_status === "unpaid")
    actions.push(["start_payment", "发起模拟支付"]);
  if (order.payment_status === "paying")
    actions.push(["confirm_payment", "模拟确认支付"]);
  if (
    order.payment_status === "paid" &&
    order.after_sales_status === "processing"
  )
    actions.push(["approve_refund", "同意退款"]);
  if (order.payment_status === "refunding")
    actions.push(["complete_refund", "确认退款完成"]);
  if (order.delivery_status === "not_started")
    actions.push(["create_task", "生成履约任务"]);
  if (order.main_status === "pending_fulfillment")
    actions.push(["start_delivery", "开始履约"]);
  if (order.delivery_status === "preparing")
    actions.push(["submit_delivery", "提交交付物"]);
  if (order.delivery_status === "pending_acceptance")
    actions.push(["accept_delivery", "验收通过"], ["reject_delivery", "拒绝并退回整改"]);
  if (order.main_status === "pending_confirmation")
    actions.push(["confirm_order", "确认完成"]);
  if (order.delivery_status === "exception")
    actions.push(["retry_delivery", "整改后重试"]);
  if (
    order.after_sales_status === "none" &&
    ["completed", "fulfilling"].includes(order.main_status)
  )
    actions.push(["submit_after_sales", "发起售后"]);
  return actions;
}

onMounted(() => {
  updateUiScale();
  window.addEventListener("resize", updateUiScale);
  if (token.value) loadSession();
  progressTimer = window.setInterval(() => {
    if (token.value) loadViewData("development");
  }, 10000);
});
onUnmounted(() => {
  window.clearInterval(progressTimer);
  window.removeEventListener("resize", updateUiScale);
});
</script>

<template>
  <div v-if="!user" class="login-shell">
    <div class="login-art">
      <div class="eyebrow">MARKET OPERATIONS</div>
      <h1>让数据产品<br /><span>持续流通和增值</span></h1>
      <p>从产品登记到订单履约，再到清算审计，一处掌握平台运营闭环。</p>
      <div class="login-metric">
        <strong>8</strong><span>核心业务域<br />统一运营</span>
      </div>
    </div>
    <form
      class="login-card"
      @submit.prevent="registerMode ? register() : login()"
    >
      <div class="brand-mark">M</div>
      <div class="eyebrow">
        {{ registerMode ? "注册数据基础设施账户" : "工作台登录" }}
      </div>
      <h2>{{ registerMode ? "创建账户" : "欢迎回来" }}</h2>
      <p class="muted">
        {{
          registerMode
            ? "注册后将引导完成个人实名认证"
            : "登录数据集运营服务管理平台"
        }}
      </p>
      <template v-if="registerMode"
        ><label
          >姓名<input
            v-model="registerForm.name"
            required
            placeholder="请输入真实姓名" /></label
        ><label
          >手机号码<input
            v-model="registerForm.phone"
            placeholder="手机号，开发环境验证码为 123456" /></label
        ><label
          >邮箱地址<input
            v-model="registerForm.email"
            type="email"
            placeholder="可选" /></label
        ><label
          >手机验证码<input
            v-model="registerForm.verification_code"
            placeholder="手机注册填写 123456" /></label
        ><label
          >密码<input
            v-model="registerForm.password"
            type="password"
            minlength="8"
            required /></label></template
      ><template v-else
        ><label
          >用户名、邮箱或手机号码<input
            v-model="loginForm.email"
            autocomplete="username" /></label
        ><label
          >密码<input
            v-model="loginForm.password"
            type="password"
            autocomplete="current-password" /></label
      ></template>
      <p v-if="loginError" class="error-text">{{ loginError }}</p>
      <p v-if="activationHint" class="login-hint activation-hint">{{ activationHint }}</p>
      <button class="primary-btn full-btn" type="submit">
        {{ registerMode ? "注册并进行实名认证" : "进入运营工作台" }}
        <ArrowUpRight :size="16" /></button
      ><button
        class="text-btn full-btn"
        type="button"
        @click="
          registerMode = !registerMode;
          loginError = '';
        "
      >
        {{ registerMode ? "已有账户，返回登录" : "注册新账户" }}
      </button>
      <p class="login-hint">
        {{
          registerMode
            ? "当前为开发环境，短信验证码固定为 123456，未发送真实短信。"
            : "演示账号已预填，可直接登录查看完整工作台。"
        }}
      </p>
    </form>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar" :class="{ collapsed, open: mobileOpen }">
      <div class="brand">
        <div class="brand-mark">M</div>
        <div v-if="!collapsed" class="brand-copy">
          <strong>market</strong><small>运营服务管理平台</small>
        </div>
      </div>
      <div class="workspace-switch">
        <div class="workspace-avatar">天</div>
        <div v-if="!collapsed" class="workspace-copy">
          <strong>{{ enterprise?.name }}</strong
          ><small>主租户 · 已认证</small>
        </div>
        <ChevronRight v-if="!collapsed" :size="15" />
      </div>
      <nav>
        <button
          v-for="item in visibleNav"
          :key="item.key"
          :class="['nav-item', { active: activeView === item.key }]"
          :title="item.label"
          @click="selectView(item.key)"
        >
          <component :is="item.icon" :size="18" /><span v-if="!collapsed">{{
            item.label
          }}</span
          ><span
            v-if="item.key === 'development' && !collapsed"
            class="nav-badge"
            >{{ development.total }}</span
          >
        </button>
      </nav>
      <div class="sidebar-bottom">
        <button
          class="nav-item"
          :class="{ active: activeView === 'settings' }"
          @click="selectView('settings')"
        >
          <Settings2 :size="18" /><span v-if="!collapsed"
            >系统设置</span
          ></button
        ><button class="nav-item" @click="logout">
          <LogOut :size="18" /><span v-if="!collapsed">退出登录</span>
        </button>
      </div>
    </aside>
    <div
      v-if="mobileOpen"
      class="mobile-scrim"
      @click="mobileOpen = false"
    ></div>
    <main class="main-shell">
      <header class="topbar">
        <button class="icon-btn mobile-menu" @click="mobileOpen = true">
          <Menu :size="20" /></button
        ><button
          class="icon-btn desktop-collapse"
          @click="collapsed = !collapsed"
        >
          <PanelLeftOpen v-if="collapsed" :size="19" /><PanelLeftClose
            v-else
            :size="19"
          />
        </button>
        <div class="breadcrumb">
          <span>运营工作台</span><ChevronRight :size="14" /><strong>{{
            nav.find((x) => x.key === activeView)?.label
          }}</strong>
        </div>
        <div class="top-actions">
          <div class="search-box">
            <Search :size="16" /><input
              v-model="search"
              placeholder="搜索订单、产品或企业"
            />
          </div>
          <button class="icon-btn notification-btn">
            <Bell :size="18" /><i></i>
          </button>
          <div class="user-chip" role="button" tabindex="0" @click="showProfileContact = true">
            <div class="avatar">{{ user.name?.slice(0, 1) }}</div>
            <div class="user-chip-copy">
              <strong>{{ user.name }}</strong
              ><small>平台管理员</small>
            </div>
          </div>
        </div>
      </header>
      <section class="content">
        <div v-if="loading" class="loading-line">
          <span></span>正在同步运营数据...
        </div>
        <div v-if="activeView === 'settings'" class="tabs settings-tabs">
          <button class="active" type="button">通知与角色</button
          ><button type="button" disabled>安全与审计</button>
        </div>
        <template v-if="activeView === 'overview'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">
                运营概览 · {{ new Date().toLocaleDateString("zh-CN") }}
              </div>
              <h1>早上好，{{ user.name }}</h1>
              <p>平台运营状态清晰可见，今天也保持节奏。</p>
            </div>
            <button class="secondary-btn" @click="refreshData">
              <RefreshCw :size="16" />刷新数据
            </button>
          </div>
          <div class="metric-grid">
            <div class="metric-card">
              <div class="metric-icon orange"><Database :size="19" /></div>
              <span>在运营产品</span
              ><strong>{{ dashboard?.metrics.products || 0 }}</strong
              ><small><ArrowUpRight :size="13" /> 数据目录持续增长</small>
            </div>
            <div class="metric-card">
              <div class="metric-icon blue"><ShoppingCart :size="19" /></div>
              <span>订单总量</span
              ><strong>{{ dashboard?.metrics.orders || 0 }}</strong
              ><small
                ><Activity :size="13" />
                {{ dashboard?.metrics.active_orders || 0 }} 个履约中</small
              >
            </div>
            <div class="metric-card">
              <div class="metric-icon green"><CheckCircle2 :size="19" /></div>
              <span>已完成订单</span
              ><strong>{{ dashboard?.metrics.completed_orders || 0 }}</strong
              ><small><ClipboardCheck :size="13" /> 验收闭环率稳定</small>
            </div>
            <div class="metric-card">
              <div class="metric-icon violet"><BarChart3 :size="19" /></div>
              <span>累计确认收入</span
              ><strong>{{ fmtMoney(dashboard?.metrics.revenue) }}</strong
              ><small><ArrowUpRight :size="13" /> 模拟支付口径</small>
            </div>
          </div>
          <div class="overview-grid">
            <div class="panel progress-panel">
              <div class="panel-heading">
                <div>
                  <span class="section-kicker">DEVELOPMENT CONTROL</span>
                  <h3>开发进度</h3>
                </div>
                <button class="text-btn" @click="selectView('development')">
                  查看任务 <ChevronRight :size="15" />
                </button>
              </div>
              <div class="progress-ring-row">
                <div
                  class="progress-ring"
                  :style="{ '--progress': `${development.completion_rate}%` }"
                >
                  <div>
                    <strong>{{ development.completion_rate }}%</strong
                    ><small>已完成</small>
                  </div>
                </div>
                <div class="progress-summary">
                  <div>
                    <span class="dot green-dot"></span
                    ><strong>{{ taskCount("done") }}</strong
                    ><small>已完成</small>
                  </div>
                  <div>
                    <span class="dot orange-dot"></span
                    ><strong>{{ taskCount("in_progress") }}</strong
                    ><small>进行中</small>
                  </div>
                  <div>
                    <span class="dot gray-dot"></span
                    ><strong>{{ taskCount("todo") }}</strong
                    ><small>待开发</small>
                  </div>
                </div>
              </div>
              <div
                class="mini-task"
                v-for="task in development.items
                  .filter((x) => x.status === 'in_progress')
                  .slice(0, 3)"
                :key="task.code"
              >
                <div>
                  <span class="task-code">{{ task.code }}</span
                  ><strong>{{ task.title }}</strong>
                </div>
                <div class="mini-bar">
                  <i :style="{ width: `${task.progress}%` }"></i>
                </div>
                <span>{{ task.progress }}%</span>
              </div>
            </div>
            <div class="panel activity-panel">
              <div class="panel-heading">
                <div>
                  <span class="section-kicker">ORDER OPERATIONS</span>
                  <h3>订单状态分布</h3>
                </div>
                <button class="text-btn" @click="selectView('orders')">
                  进入订单 <ChevronRight :size="15" />
                </button>
              </div>
              <div class="status-list" v-if="orders.length">
                <div
                  v-for="item in [
                    { key: 'fulfilling', label: '履约中', color: 'orange' },
                    {
                      key: 'pending_confirmation',
                      label: '待确认',
                      color: 'blue',
                    },
                    { key: 'completed', label: '已完成', color: 'green' },
                  ]"
                  :key="item.key"
                  class="status-row"
                >
                  <div>
                    <span :class="['dot', `${item.color}-dot`]"></span
                    ><span>{{ item.label }}</span>
                  </div>
                  <strong>{{
                    orders.filter((o) => o.main_status === item.key).length
                  }}</strong>
                </div>
              </div>
              <div v-else class="empty-state">
                <ShoppingCart :size="28" /><span>还没有订单数据</span>
              </div>
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'gateway'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">APISIX · ROUTE CONTROL PLANE</div>
              <h1>API 网关</h1>
              <p>统一管理路由、版本策略、发布记录与调用运行状态。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('gateway')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="metric-grid">
            <div class="metric-card"><div class="metric-icon orange"><Network :size="17" /></div><span>已登记路由</span><strong>{{ gatewayOverview.summary.routes || 0 }}</strong><small>{{ gatewayOverview.apisix_enabled ? 'APISIX 已接管' : '兼容网关模式' }}</small></div>
            <div class="metric-card"><div class="metric-icon green"><CheckCircle2 :size="17" /></div><span>活跃路由</span><strong>{{ gatewayOverview.summary.active_routes || 0 }}</strong><small>自动发布与健康检查</small></div>
            <div class="metric-card"><div class="metric-icon blue"><Activity :size="17" /></div><span>近期开调用</span><strong>{{ gatewayOverview.summary.requests || 0 }}</strong><small>最近 1000 条调用记录</small></div>
            <div class="metric-card"><div class="metric-icon violet"><Clock3 :size="17" /></div><span>平均延迟</span><strong>{{ gatewayOverview.summary.avg_latency_ms || 0 }}<small> ms</small></strong><small>成功 {{ gatewayOverview.summary.success || 0 }} · 错误 {{ gatewayOverview.summary.errors || 0 }}</small></div>
          </div>
          <div class="gateway-layout">
            <div class="panel">
              <div class="panel-heading"><div><span class="section-kicker">ROUTES</span><h3>网关路由</h3></div><span class="muted">{{ gatewayItems.length }} 条</span></div>
              <div v-if="gatewayItems.length" class="gateway-route-list">
                <button v-for="item in gatewayItems" :key="item.id" :class="['gateway-route-item', { active: gatewaySelected?.id === item.id }]" @click="selectGateway(item)">
                  <strong>{{ item.product_name || item.route_key }}</strong>
                  <small>/gateway/{{ item.route_key }} · {{ item.version }} · {{ item.status }}</small>
                  <small>{{ item.rate_limit_per_minute }}/分钟 · 日配额 {{ item.daily_quota || '不限' }}</small>
                </button>
              </div>
              <div v-else class="empty-state"><Network :size="28" /><span>暂无网关路由</span></div>
            </div>
            <div class="panel">
              <div class="panel-heading"><div><span class="section-kicker">CONFIGURATION</span><h3>路由与策略配置</h3></div><span v-if="gatewaySelected" class="status-pill status-done">{{ gatewaySelected.status }}</span></div>
              <div v-if="gatewaySelected" class="gateway-form-grid">
                <label class="wide">上游地址<input v-model="gatewayForm.upstream_url" /></label>
                <label>路由标识<input v-model="gatewayForm.route_key" /></label>
                <label>版本<input v-model="gatewayForm.version" /></label>
                <label>入口认证<select v-model="gatewayForm.auth_mode"><option value="api_key">API Key</option><option value="oauth2">OAuth2</option></select></label>
                <label>上游认证<select v-model="gatewayForm.upstream_auth_mode"><option value="oauth2">OAuth2</option><option value="none">无</option></select></label>
                <label>上游 Scope<input v-model="gatewayForm.upstream_scope" /></label>
                <label>每分钟限流<input v-model.number="gatewayForm.rate_limit_per_minute" type="number" min="1" /></label>
                <label>每日配额<input v-model.number="gatewayForm.daily_quota" type="number" min="0" /></label>
                <label>每月配额<input v-model.number="gatewayForm.monthly_quota" type="number" min="0" /></label>
                <label>超时（毫秒）<input v-model.number="gatewayForm.timeout_ms" type="number" min="100" /></label>
                <label>健康检查路径<input v-model="gatewayForm.health_path" /></label>
                <label>健康检查方法<select v-model="gatewayForm.health_method"><option>GET</option><option>HEAD</option></select></label>
                <label class="wide checkbox-line"><input v-model="gatewayForm.strip_prefix" type="checkbox" /> 转发时剥离网关前缀</label>
              </div>
              <div v-if="gatewaySelected" class="gateway-actions"><button class="secondary-btn" @click="saveGatewayConfig"><CheckCircle2 :size="15" />保存</button><button class="secondary-btn" @click="validateGatewayConfig"><CheckCircle2 :size="15" />校验</button><button class="primary-btn" @click="publishGatewayConfig"><ArrowUpRight :size="15" />发布</button><button class="secondary-btn" @click="rollbackGatewayConfig"><RefreshCw :size="15" />回滚</button></div>
              <div v-else class="empty-state"><Network :size="28" /><span>请选择一条路由</span></div>
            </div>
          </div>
          <div class="panel gateway-lower-panel">
            <div class="panel-heading"><div><span class="section-kicker">OBSERVABILITY</span><h3>运行状态</h3></div><div class="settings-tabs"><button :class="{ active: gatewayTab === 'usage' }" @click="gatewayTab = 'usage'">调用统计</button><button :class="{ active: gatewayTab === 'revisions' }" @click="gatewayTab = 'revisions'">发布记录</button><button :class="{ active: gatewayTab === 'alerts' }" @click="gatewayTab = 'alerts'">告警</button></div></div>
            <div v-if="gatewayTab === 'usage'" class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>API Key</th><th>状态码</th><th>延迟</th><th>版本</th></tr></thead><tbody><tr v-for="item in gatewayUsage.items" :key="item.id"><td>{{ fmtDate(item.created_at) }}</td><td>{{ item.api_key || '-' }}</td><td><span :class="['status-pill', item.status_code < 400 ? 'status-done' : 'status-blocked']">{{ item.status_code }}</span></td><td>{{ item.latency_ms }} ms</td><td>{{ item.version || '-' }}</td></tr><tr v-if="!gatewayUsage.items.length"><td colspan="5"><div class="empty-state">暂无调用数据</div></td></tr></tbody></table></div>
            <div v-else-if="gatewayTab === 'revisions'" class="table-wrap"><table class="data-table"><thead><tr><th>版本</th><th>状态</th><th>操作人</th><th>时间</th><th>错误</th></tr></thead><tbody><tr v-for="item in gatewayRevisions" :key="item.id"><td>Revision {{ item.revision }}</td><td><span :class="['status-pill', item.status === 'published' ? 'status-done' : item.status === 'failed' ? 'status-blocked' : 'status-in_progress']">{{ item.status }}</span></td><td>{{ item.created_by }}</td><td>{{ fmtDate(item.created_at) }}</td><td>{{ item.error_message || '-' }}</td></tr><tr v-if="!gatewayRevisions.length"><td colspan="5"><div class="empty-state">暂无发布记录</div></td></tr></tbody></table></div>
            <div v-else class="table-wrap"><table class="data-table"><thead><tr><th>等级</th><th>告警</th><th>说明</th><th>时间</th></tr></thead><tbody><tr v-for="item in gatewayAlerts" :key="item.id"><td><span class="status-pill status-blocked">{{ item.severity }}</span></td><td><strong>{{ item.title }}</strong></td><td>{{ item.message }}</td><td>{{ fmtDate(item.created_at) }}</td></tr><tr v-if="!gatewayAlerts.length"><td colspan="4"><div class="empty-state"><CheckCircle2 :size="22" />暂无未处理告警</div></td></tr></tbody></table></div>
          </div></template
        >
        <template v-else-if="activeView === 'sla'"
          ><div class="page-heading">
            <div><div class="eyebrow">SERVICE ASSURANCE · SLA</div><h1>平台服务与 SLA 保障</h1><p>统一配置服务等级目标，按 API 调用质量和交付及时率进行周期考核。</p></div>
            <button class="primary-btn" @click="evaluateSla"><RefreshCw :size="15" />立即考核</button>
          </div>
          <div class="metric-grid">
            <div class="metric-card"><div class="metric-icon blue"><Activity :size="17" /></div><span>启用规则</span><strong>{{ slaOverview.summary.active_profiles || 0 }}</strong><small>共 {{ slaOverview.summary.profiles || 0 }} 条规则</small></div>
            <div class="metric-card"><div class="metric-icon green"><CheckCircle2 :size="17" /></div><span>达标结果</span><strong>{{ slaOverview.summary.met || 0 }}</strong><small>最近考核周期</small></div>
            <div class="metric-card"><div class="metric-icon orange"><CircleAlert :size="17" /></div><span>预警结果</span><strong>{{ slaOverview.summary.warning || 0 }}</strong><small>需要关注</small></div>
            <div class="metric-card"><div class="metric-icon red"><CircleAlert :size="17" /></div><span>违约结果</span><strong>{{ slaOverview.summary.breached || 0 }}</strong><small>需要处置</small></div>
          </div>
          <div class="two-column-panels">
            <div class="panel sla-policy-panel">
              <div class="panel-heading sla-panel-heading"><div><span class="section-kicker">SLA POLICY</span><h3>规则配置</h3></div><span class="muted">平台管理员</span></div>
              <div class="form-grid sla-rule-form">
                <label>规则名称<input v-model="slaForm.name" placeholder="如 API 核心服务等级" /></label>
                <label>适用范围<select v-model="slaForm.service_scope"><option value="platform">平台服务</option><option value="api">API 服务</option><option value="delivery">交付服务</option><option value="product">指定产品</option></select></label>
                <label v-if="slaForm.service_scope === 'product'">绑定产品<select v-model="slaForm.product_id"><option value="">请选择产品</option><option v-for="product in products" :key="product.id" :value="product.id">{{ product.name }}</option></select></label>
                <label>可用性目标（%）<input v-model.number="slaForm.availability_target" type="number" step="0.01" min="0" max="100" /></label>
                <label>延迟目标（ms）<input v-model.number="slaForm.latency_target_ms" type="number" min="1" /></label>
                <label>错误率上限（%）<input v-model.number="slaForm.error_rate_target" type="number" step="0.01" min="0" /></label>
                <label>交付时限（小时）<input v-model.number="slaForm.delivery_hours" type="number" min="1" /></label>
                <label>恢复目标（分钟）<input v-model.number="slaForm.recovery_minutes" type="number" min="1" /></label>
                <label>预警裕量（百分点）<input v-model.number="slaForm.warning_margin" type="number" step="0.1" min="0" /></label>
                <label class="wide">说明<textarea v-model="slaForm.description" rows="2" placeholder="描述服务范围、考核口径和处置要求"></textarea></label>
              </div>
              <div class="sla-form-actions"><button class="primary-btn" @click="createSlaProfile"><CheckCircle2 :size="15" />创建规则</button></div>
              <div class="sla-rule-table"><div class="sla-subheading"><strong>已配置规则</strong><span>{{ slaOverview.profiles.length }} 条</span></div><div class="table-wrap"><table class="data-table compact"><thead><tr><th>规则</th><th>范围</th><th>目标</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in slaOverview.profiles" :key="item.id"><td><strong>{{ item.name }}</strong><small>{{ item.description || '无说明' }}</small></td><td>{{ item.service_scope }}</td><td>可用 {{ item.availability_target }}% · 延迟 {{ item.latency_target_ms }}ms · 错误 {{ item.error_rate_target }}%</td><td><span :class="['status-pill', item.status === 'active' ? 'status-done' : 'status-todo']">{{ item.status === 'active' ? '启用' : '停用' }}</span></td><td><button class="text-btn" @click="toggleSlaProfile(item)">{{ item.status === 'active' ? '停用' : '启用' }}</button></td></tr><tr v-if="!slaOverview.profiles.length"><td colspan="5"><div class="empty-state">暂无 SLA 规则</div></td></tr></tbody></table></div></div>
            </div>
            <div class="panel sla-results-panel"><div class="panel-heading"><div><span class="section-kicker">SLA RESULTS</span><h3>周期考核结果</h3></div><span class="muted">最近 100 条</span></div><div class="table-wrap"><table class="data-table compact"><thead><tr><th>规则</th><th>可用性</th><th>延迟</th><th>错误率</th><th>交付及时率</th><th>结论</th></tr></thead><tbody><tr v-for="item in slaOverview.results" :key="item.id"><td><strong>{{ item.profile_name }}</strong><small>{{ fmtDate(item.calculated_at) }}</small></td><td>{{ item.availability }}%</td><td>{{ item.avg_latency_ms }} ms</td><td>{{ item.error_rate }}%</td><td>{{ item.delivery_compliance }}%</td><td><button class="sla-result-button" @click="openSlaResult(item)"><span :class="['status-pill', item.status === 'met' ? 'status-done' : item.status === 'warning' ? 'status-review' : 'status-blocked']">{{ item.status === 'met' ? '达标' : item.status === 'warning' ? '预警' : '违约' }}</span><small>{{ item.breach_reason || '查看考核详情' }}</small></button></td></tr><tr v-if="!slaOverview.results.length"><td colspan="6"><div class="empty-state">暂无考核结果，请先执行考核</div></td></tr></tbody></table></div></div>
          </div><div v-if="selectedSlaResult" class="drawer-scrim" @click.self="selectedSlaResult = null"><aside class="order-drawer sla-result-drawer"><div class="drawer-head"><div><div class="eyebrow">SLA RESULT DETAIL</div><h2>{{ selectedSlaResult.status === 'breached' ? 'SLA 违约详情' : selectedSlaResult.status === 'warning' ? 'SLA 预警详情' : 'SLA 达标详情' }}</h2></div><button class="icon-btn" @click="selectedSlaResult = null"><X :size="18" /></button></div><div class="drawer-summary"><strong>{{ selectedSlaResult.profile_name }}</strong><span>{{ fmtDate(selectedSlaResult.period_start) }} 至 {{ fmtDate(selectedSlaResult.period_end) }}</span><span :class="['status-pill', selectedSlaResult.status === 'met' ? 'status-done' : selectedSlaResult.status === 'warning' ? 'status-review' : 'status-blocked']">{{ selectedSlaResult.status === 'met' ? '达标' : selectedSlaResult.status === 'warning' ? '预警' : '违约' }}</span></div><div class="drawer-section"><div class="drawer-section-title">异常内容</div><p class="sla-result-reason">{{ selectedSlaResult.breach_reason || '本周期各项指标均达到目标。' }}</p></div><div class="drawer-section"><div class="drawer-section-title">考核指标</div><div class="state-grid"><div><small>可用性</small><strong>{{ selectedSlaResult.availability }}%</strong></div><div><small>平均延迟</small><strong>{{ selectedSlaResult.avg_latency_ms }} ms</strong></div><div><small>错误率</small><strong>{{ selectedSlaResult.error_rate }}%</strong></div><div><small>交付及时率</small><strong>{{ selectedSlaResult.delivery_compliance }}%</strong></div><div><small>请求样本</small><strong>{{ selectedSlaResult.sample_count }}</strong></div><div><small>成功请求</small><strong>{{ selectedSlaResult.success_count }}</strong></div></div></div><div class="drawer-section"><div class="drawer-section-title">处理建议</div><p class="sla-result-advice">{{ selectedSlaResult.status === 'breached' ? '请创建服务事件，定位故障原因并记录恢复过程。' : selectedSlaResult.status === 'warning' ? '建议持续观察下一周期指标，必要时提前处理容量、延迟或交付风险。' : '当前周期无需额外处置。' }}</p></div></aside></div><div class="panel service-level-panel"><div class="panel-heading"><div><span class="section-kicker">SERVICE LEVEL</span><h3>服务级别与支持权益</h3></div><span class="muted">时长均可配置</span></div><div class="service-level-cards"><article v-for="level in slaOverview.service_levels" :key="level.id" class="service-level-card"><div class="service-level-card-head"><div><strong>{{ level.name }}</strong><small>{{ level.code }} · {{ level.support_schedule }} 支持</small></div><span :class="['status-pill', level.status === 'active' ? 'status-done' : 'status-todo']">{{ level.status === 'active' ? '启用' : '停用' }}</span></div><p>{{ level.description }}</p><div class="service-level-meta"><span>首响 {{ level.initial_response_minutes }} 分钟</span><span>问题 {{ level.problem_response_hours }} 小时</span><span v-if="level.dedicated_manager">专属经理</span><span v-if="level.quarterly_report">季度报告</span><span v-if="level.annual_optimization">年度优化</span></div></article></div><div class="service-level-config"><div class="service-level-config-title">新增可配置级别</div><div class="form-grid service-level-form"><label>编码<input v-model="serviceLevelForm.code" placeholder="如 premium" /></label><label>名称<input v-model="serviceLevelForm.name" placeholder="如 专业级" /></label><label>每周支持天数<input v-model.number="serviceLevelForm.support_days_per_week" type="number" min="1" max="7" /></label><label>每天支持小时<input v-model.number="serviceLevelForm.support_hours_per_day" type="number" min="1" max="24" /></label><label>首次响应（分钟）<input v-model.number="serviceLevelForm.initial_response_minutes" type="number" min="1" /></label><label>问题响应（小时）<input v-model.number="serviceLevelForm.problem_response_hours" type="number" min="1" /></label><label class="wide">说明<input v-model="serviceLevelForm.description" placeholder="支持权益说明" /></label></div><div class="service-level-options"><label><input v-model="serviceLevelForm.dedicated_manager" type="checkbox" />专属技术支持经理</label><label><input v-model="serviceLevelForm.quarterly_report" type="checkbox" />季度服务报告</label><label><input v-model="serviceLevelForm.annual_optimization" type="checkbox" />年度服务优化建议</label></div><button class="primary-btn" @click="createServiceLevel"><CheckCircle2 :size="15" />创建服务级别</button></div><div class="service-level-assignment"><div class="service-level-config-title">企业服务级别绑定</div><div class="form-grid"><label>企业<select v-model="serviceAssignmentForm.enterprise_id"><option value="">请选择企业</option><option v-for="enterprise in slaEnterprises" :key="enterprise.id" :value="enterprise.id">{{ enterprise.name }}</option></select></label><label>服务级别<select v-model="serviceAssignmentForm.service_level_id"><option value="">请选择级别</option><option v-for="level in slaOverview.service_levels" :key="level.id" :value="level.id">{{ level.name }}</option></select></label></div><button class="secondary-btn" @click="assignServiceLevel"><Users :size="15" />绑定企业级别</button></div></div></template
        >
        <template v-else-if="activeView === 'development'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">主 Agent · 实时任务监控</div>
              <h1>开发进度控制台</h1>
              <p>
                功能拆解、负责人、依赖和验收状态集中管理。页面每 10 秒自动刷新。
              </p>
            </div>
            <button class="secondary-btn" @click="loadViewData('development')">
              <RefreshCw :size="16" />立即刷新
            </button>
          </div>
          <div class="dev-summary">
            <button
              v-for="item in [
                {
                  key: 'all',
                  label: '全部任务',
                  value: development.total,
                  icon: Activity,
                },
                {
                  key: 'done',
                  label: '已完成',
                  value: taskCount('done'),
                  icon: CheckCircle2,
                },
                {
                  key: 'in_progress',
                  label: '进行中',
                  value: taskCount('in_progress'),
                  icon: Clock3,
                },
                {
                  key: 'todo',
                  label: '待开发',
                  value: taskCount('todo'),
                  icon: FileText,
                },
                {
                  key: 'blocked',
                  label: '已阻塞',
                  value: taskCount('blocked'),
                  icon: CircleAlert,
                },
              ]"
              :key="item.key"
              :class="[
                'dev-stat',
                { selected: developmentFilter === item.key },
              ]"
              @click="developmentFilter = item.key"
            >
              <component :is="item.icon" :size="17" /><span>{{
                item.label
              }}</span
              ><strong>{{ item.value }}</strong>
            </button>
          </div>
          <div class="panel task-panel">
            <div class="panel-heading">
              <div>
                <span class="section-kicker">TASK REGISTER</span>
                <h3>
                  功能项清单
                  <small
                    >共 {{ development.total }} 项 · 完成率
                    {{ development.completion_rate }}%</small
                  >
                </h3>
              </div>
              <span class="sync-note"
                ><span class="live-dot"></span>实时同步
                {{ fmtDate(development.updated_at) }}</span
              >
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>功能项</th>
                    <th>负责人</th>
                    <th>优先级</th>
                    <th>依赖</th>
                    <th>进度</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="task in filteredTasks" :key="task.code">
                    <td>
                      <span class="task-code">{{ task.code }}</span
                      ><small>{{ task.area }}</small>
                    </td>
                    <td>
                      <strong>{{ task.title }}</strong
                      ><small class="acceptance"
                        >验收：{{ task.acceptance }}</small
                      >
                    </td>
                    <td>{{ task.owner }}</td>
                    <td>
                      <span
                        :class="['priority', task.priority.toLowerCase()]"
                        >{{ task.priority }}</span
                      >
                    </td>
                    <td class="dependency">{{ task.dependencies || "无" }}</td>
                    <td>
                      <div class="progress-cell">
                        <div class="mini-bar">
                          <i :style="{ width: `${task.progress}%` }"></i>
                        </div>
                        <span>{{ task.progress }}%</span>
                      </div>
                    </td>
                    <td>
                      <span :class="['status-pill', `status-${task.status}`]">{{
                        taskStatusLabels[task.status]
                      }}</span>
                    </td>
                    <td>
                      <select
                        class="status-select"
                        :value="task.status"
                        @change="updateTask(task, $event.target.value)"
                      >
                        <option value="todo">待开发</option>
                        <option value="in_progress">进行中</option>
                        <option value="review">待评审</option>
                        <option value="blocked">已阻塞</option>
                        <option value="done">已完成</option>
                      </select>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'products'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">产品目录 · {{ products.length }} 个产品</div>
              <h1>数据与服务</h1>
              <p>登记产品所属目录和完整运营元数据，再进入审核发布流程。</p>
            </div>
            <button class="primary-btn" @click="openNewProduct">
              <FileText :size="16" />新建产品
            </button>
          </div>
          <div class="panel">
            <div class="panel-heading">
              <h3>产品目录</h3>
              <span class="muted"
                >名称、目录、提供方、安全等级和授权条件均可追溯</span
              >
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>产品</th>
                    <th>所属目录</th>
                    <th>提供方</th>
                    <th>类型</th>
                    <th>交付方式</th>
                    <th>安全等级</th>
                    <th>价格</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="product in filteredProducts" :key="product.id">
                    <td>
                      <button class="product-link" @click="openProductDetail(product)">{{ product.name }}</button
                      ><small
                        >{{ product.version }} ·
                        {{ product.description }}</small
                      >
                    </td>
                    <td>
                      <span class="catalog-tag">{{
                        product.catalog_name
                      }}</span>
                    </td>
                    <td>{{ product.provider_name || "-" }}</td>
                    <td>
                      {{
                        typeLabels[product.product_type] || product.product_type
                      }}
                    </td>
                    <td>{{ product.delivery_method }}</td>
                    <td>
                      <span class="quality-tag">{{
                        product.security_level
                      }}</span>
                    </td>
                    <td>{{ fmtMoney(product.price) }}</td>
                    <td>
                      <span
                        :class="[
                          'status-pill',
                          `status-${product.status === 'published' ? 'done' : ['pending_review','quality_review','security_review','operation_review'].includes(product.status) ? 'review' : product.status === 'rejected' || product.status === 'security_unpublished' ? 'blocked' : 'todo'}`,
                        ]"
                        >{{
                          product.status === "published"
                            ? "已发布"
                            : product.status === "pending_review"
                            ? "业务审核"
                            : product.status === "quality_review"
                              ? "质量审核"
                            : product.status === "security_review"
                              ? "安全审核中"
                              : product.status === "operation_review"
                                ? "运营审核"
                              : product.status === "rejected"
                                ? "已驳回"
                                : product.status === "security_unpublished"
                                  ? "安全下架"
                                  : "草稿"
                        }}</span
                      >
                    </td>
                    <td>
                      <div v-if="isPlatformRole()" class="table-actions">
                        <button v-if="canReviewProduct(product)" class="text-btn" @click="openProductDetail(product, true)">审核</button>
                        <button v-if="product.status === 'published' && ['super_admin', 'platform_operator'].includes(user?.platform_role)" class="text-btn danger-text" @click="productAction(product, 'unpublish')">下架</button>
                        <span v-if="!canReviewProduct(product) && !(product.status === 'published' && ['super_admin', 'platform_operator'].includes(user?.platform_role))" class="muted">-</span>
                      </div>
                      <div v-else class="table-actions">
                        <button
                          v-if="
                            product.status === 'draft' ||
                            product.status === 'rejected'
                          "
                          class="text-btn"
                          @click="productAction(product, 'submit')"
                        >
                          提交审核</button
                        ><button
                          v-if="canReviewProduct(product)"
                          class="text-btn"
                          @click="productAction(product, 'review')"
                        >
                          通过</button
                        ><button
                          v-if="product.status === 'pending_review' && (['super_admin', 'enterprise_admin'].includes(user?.enterprise_role))"
                          class="text-btn"
                          @click="productAction(product, 'withdraw')"
                        >
                          撤回</button
                        ><button
                          v-if="product.status === 'published' && (['super_admin', 'platform_operator'].includes(user?.platform_role) || ['super_admin', 'enterprise_admin'].includes(user?.enterprise_role))"
                          class="text-btn danger-text"
                          @click="productAction(product, 'unpublish')"
                        >
                          下架</button
                        ><button
                          v-if="product.status === 'published' && ['super_admin', 'platform_operator', 'security_compliance'].includes(user?.platform_role)"
                          class="text-btn"
                          @click="productAction(product, 'security_check')"
                        >
                          安全检查</button
                        ><span
                          v-if="product.status === 'published'"
                          class="muted"
                          >已完成</span
                        ><a
                          v-if="
                            product.status === 'published' &&
                            ['api', 'model'].includes(product.product_type)
                          "
                          class="text-btn"
                          href="/api-gateway-integration.md"
                          download="API应用提供方接入统一网关接口与规范.md"
                          >提供方文档</a
                        ><a
                          v-if="
                            product.status === 'published' &&
                            ['api', 'model'].includes(product.product_type)
                          "
                          class="text-btn"
                          href="/api-consumer-integration.md"
                          download="API应用调用方接入与调用规范.md"
                          >调用方文档</a
                        ><button
                          v-if="
                            product.status === 'published' &&
                            ['api', 'model', 'saas'].includes(product.product_type)
                          "
                          class="text-btn"
                          @click="downloadSaasCredentials(product)"
                        >
                          下载 OAuth 凭据
                        </button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'orders'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">业务交易 · 四域状态机</div>
              <h1>订单中心</h1>
              <p>主状态、支付、交付和售后状态分别记录，跨域动作全程留痕。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('orders')">
              <RefreshCw :size="16" />刷新订单
            </button>
          </div>
          <div class="panel">
            <div class="panel-heading">
              <h3>
                订单列表 <small>{{ filteredOrders.length }} 条</small>
              </h3>
              <span class="muted">模拟支付 · 人工确认</span>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>订单号</th>
                    <th>产品和客户</th>
                    <th>主状态</th>
                    <th>支付</th>
                    <th>交付</th>
                    <th>售后</th>
                    <th>金额</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="order in filteredOrders" :key="order.id">
                    <td>
                      <span class="order-no">{{ order.order_no }}</span
                      ><small>{{ fmtDate(order.created_at) }}</small>
                    </td>
                    <td>
                      <strong>{{ order.product_name }}</strong
                      ><small>{{ order.buyer_name }}</small>
                    </td>
                    <td>
                      <span class="status-pill status-in_progress">{{
                        label(order.main_status)
                      }}</span>
                    </td>
                    <td>{{ label(order.payment_status) }}</td>
                    <td>{{ label(order.delivery_status) }}</td>
                    <td>{{ label(order.after_sales_status) }}</td>
                    <td>
                      <strong>{{ fmtMoney(order.amount) }}</strong>
                    </td>
                    <td>
                      <button
                        class="icon-btn"
                        title="查看订单"
                        @click="openOrder(order)"
                      >
                        <ChevronRight :size="17" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div></div
        ></template>
        <template v-else-if="activeView === 'delivery'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">履约管理 · 交付与售后</div>
              <h1>交付与售后</h1>
              <p>交付任务、验收材料和售后工单在同一工作台协同处理。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('delivery')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="two-column-panels">
            <div class="panel">
              <div class="panel-heading">
                <h3>
                  履约任务 <small>{{ deliveryItems.length }} 项</small>
                </h3>
              </div>
              <div class="table-wrap">
                <table class="data-table compact">
                  <thead>
                    <tr>
                      <th>任务</th>
                      <th>负责人</th>
                      <th>方式</th>
                      <th>模式</th>
                      <th>状态</th>
                      <th>重试</th>
                      <th>SLA截止</th>
                      <th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="item in deliveryItems" :key="item.id">
                      <td>
                        <span class="task-code">{{ item.id.slice(0, 10) }}</span
                        ><small>{{ item.order_id.slice(0, 10) }}</small>
                      </td>
                      <td>{{ item.assignee }}</td>
                      <td>{{ item.method }}</td>
                      <td>{{ item.delivery_mode === "automatic" ? "自动交付" : "人工交付" }}</td>
                      <td>
                        <span :class="['status-pill', item.status === 'exception' ? 'status-blocked' : item.status === 'pending_acceptance' ? 'status-review' : 'status-in_progress']">{{
                          label(item.status)
                        }}</span>
                      </td>
                      <td>{{ item.retry_count || 0 }} / {{ item.max_retries || 3 }}<small v-if="item.last_error" class="delivery-error">{{ item.last_error }}</small></td>
                      <td>{{ item.sla_due_at ? fmtDate(item.sla_due_at) : "-" }}</td>
                      <td>{{ item.note || "-" }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="!deliveryItems.length" class="empty-state">
                <PackageCheck :size="28" /><span>暂无履约任务</span>
              </div>
            </div>
            <div class="panel">
              <div class="panel-heading">
                <h3>
                  售后工单 <small>{{ afterSalesItems.length }} 项</small>
                </h3>
              </div>
              <div class="after-sales-list">
                <div
                  v-for="item in afterSalesItems"
                  :key="item.id"
                  class="after-sales-item"
                >
                  <div>
                    <span class="task-code">{{ item.ticket_no }}</span
                    ><strong>{{ item.type }}</strong
                    ><small>{{ item.description }}</small>
                  </div>
                  <span
                    :class="[
                      'status-pill',
                      item.status === 'processing'
                        ? 'status-in_progress'
                        : 'status-done',
                    ]"
                    >{{ label(item.status) }}</span
                  >
                </div>
              </div>
              <div v-if="!afterSalesItems.length" class="empty-state">
                <CheckCircle2 :size="28" /><span>暂无售后工单</span>
              </div>
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'settlements'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">财务运营 · 可核对分账</div>
              <h1>清算分账</h1>
              <p>按订单版本成本计算利润，再依据利润进行分账。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('settlements')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="tabs settlement-tabs">
            <button :class="{ active: settlementTab === 'settlements' }" @click="settlementTab = 'settlements'">清算单 {{ settlementItems.length }}</button>
            <button :class="{ active: settlementTab === 'rules' }" @click="settlementTab = 'rules'">规则与试算 {{ settlementRules.length }}</button>
            <button :class="{ active: settlementTab === 'batches' }" @click="settlementTab = 'batches'">清算批次 {{ settlementBatches.length }}</button>
            <button :class="{ active: settlementTab === 'reconciliation' }" @click="settlementTab = 'reconciliation'">对账差异 {{ settlementReconciliations.filter((x) => x.status !== 'closed').length }}</button>
            <button :class="{ active: settlementTab === 'corrections' }" @click="settlementTab = 'corrections'">退款与冲正（预留） {{ settlementCorrections.length }}</button>
            <button :class="{ active: settlementTab === 'reports' }" @click="settlementTab = 'reports'">清算报表</button>
            <button :class="{ active: settlementTab === 'measurements' }" @click="settlementTab = 'measurements'">计量计费 {{ settlementMeasurements.length }}</button>
          </div>
          <div v-if="settlementTab === 'rules'" class="settlement-management-grid">
            <div class="panel">
              <div class="panel-heading"><div><span class="section-kicker">RULE CONFIGURATION</span><h3>新增清算规则</h3></div></div>
              <div class="gateway-form-grid">
                <label class="wide">规则名称<input v-model="settlementRuleForm.name" placeholder="例如：API服务年度分账规则" /></label>
                <label>版本<input v-model="settlementRuleForm.version" /></label>
                <label>平台服务费 %<input v-model.number="settlementRuleForm.platform_rate" type="number" min="0" max="100" /></label>
                <label>提供方分成 %<input v-model.number="settlementRuleForm.provider_rate" type="number" min="0" max="100" /></label>
                <label>数据服务方 %<input v-model.number="settlementRuleForm.service_rate" type="number" min="0" max="100" /></label>
                <label>专家费用 %<input v-model.number="settlementRuleForm.expert_rate" type="number" min="0" max="100" /></label>
                <label>渠道费用 %<input v-model.number="settlementRuleForm.channel_rate" type="number" min="0" max="100" /></label>
              </div>
              <div class="gateway-actions"><button class="primary-btn" @click="createSettlementRule">保存规则</button></div>
            </div>
            <div class="panel"><div class="panel-heading"><div><span class="section-kicker">RULE VERSIONS</span><h3>规则版本</h3></div></div><div class="settlement-rule-list"><div v-for="rule in settlementRules" :key="rule.id" class="settlement-rule-item"><div><strong>{{ rule.name }} · {{ rule.version }}</strong><small>平台 {{ rule.platform_rate }}% · 提供方 {{ rule.provider_rate }}% · 数据服务方 {{ rule.service_rate }}% · 专家 {{ rule.expert_rate }}% · 渠道 {{ rule.channel_rate }}%</small></div><div class="table-actions"><span class="status-pill" :class="rule.status === 'active' ? 'status-done' : 'status-review'">{{ rule.status }}</span><button v-if="rule.status === 'draft'" class="text-btn" @click="decideSettlementRule(rule, 'approve')">审批</button><button v-if="['approved', 'disabled'].includes(rule.status)" class="text-btn" @click="decideSettlementRule(rule, 'activate')">启用</button><button class="text-btn" @click="simulateSettlementRule(rule)">试算</button></div></div></div><div v-if="!settlementRules.length" class="empty-state">暂无清算规则</div></div>
          </div>
          <div v-else-if="settlementTab === 'batches'" class="panel"><div class="panel-heading"><div><span class="section-kicker">SETTLEMENT BATCHES</span><h3>清算批次</h3></div><button class="primary-btn" @click="generateSettlementBatch">生成批次</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>批次号</th><th>周期</th><th>总额</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="batch in settlementBatches" :key="batch.id"><td><span class="task-code">{{ batch.batch_no }}</span></td><td>{{ batch.cycle }}</td><td>{{ fmtMoney(batch.total_amount) }}</td><td><span class="status-pill" :class="batch.status === 'paid' ? 'status-done' : 'status-review'">{{ batch.status }}</span></td><td>{{ fmtDate(batch.created_at) }}</td><td><div class="table-actions"><button v-if="batch.status === 'pending_confirm'" class="text-btn" @click="batchAction(batch, 'confirm')">确认</button><button v-if="batch.status === 'confirmed'" class="text-btn" @click="batchAction(batch, 'pay')">模拟付款</button></div></td></tr></tbody></table></div><div v-if="!settlementBatches.length" class="empty-state">暂无清算批次</div></div>
          <div v-else-if="settlementTab === 'reconciliation'" class="panel"><div class="panel-heading"><div><span class="section-kicker">FOUR LEDGER RECONCILIATION</span><h3>对账差异</h3></div><span class="muted">差异关闭前不得付款</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>批次</th><th>账簿</th><th>应有金额</th><th>实际金额</th><th>差额</th><th>状态</th><th>处理</th></tr></thead><tbody><tr v-for="item in settlementReconciliations" :key="item.id"><td>{{ item.batch_id.slice(0, 12) }}</td><td>{{ item.ledger_type }}</td><td>{{ fmtMoney(item.expected_amount) }}</td><td>{{ fmtMoney(item.actual_amount) }}</td><td>{{ fmtMoney(item.difference_amount) }}</td><td><span class="status-pill" :class="item.status === 'closed' || item.status === 'matched' ? 'status-done' : 'status-blocked'">{{ item.status }}</span></td><td><button v-if="item.status !== 'closed'" class="text-btn" @click="closeReconciliation(item)">关闭差异</button></td></tr></tbody></table></div><div v-if="!settlementReconciliations.length" class="empty-state">暂无对账记录</div></div>
          <div v-else-if="settlementTab === 'corrections'" class="panel"><div class="panel-heading"><div><span class="section-kicker">REFUND AND REVERSAL · RESERVED</span><h3>退款与冲正（预留）</h3></div><span class="muted">退款流程尚未上线；现有清算调整请在清算单中处理</span></div><div class="empty-state">当前版本尚未实现退款、原路退回和负向清算单。已发生的清算金额调整，请通过清算单的“调整提案”完成并保留审计记录。</div><div v-if="settlementCorrections.length" class="table-wrap"><table class="data-table"><thead><tr><th>类型</th><th>清算单</th><th>金额</th><th>追回方式</th><th>原因</th><th>状态</th></tr></thead><tbody><tr v-for="item in settlementCorrections" :key="item.id"><td>{{ item.correction_type }}</td><td>{{ item.settlement_id.slice(0, 12) }}</td><td>{{ fmtMoney(item.amount) }}</td><td>{{ item.recovery_mode }}</td><td>{{ item.reason }}</td><td><span class="status-pill" :class="item.status === 'approved' ? 'status-done' : 'status-review'">{{ item.status }}</span></td></tr></tbody></table></div></div>
          <div v-else-if="settlementTab === 'reports'" class="panel"><div class="panel-heading"><div><span class="section-kicker">SETTLEMENT REPORTS</span><h3>清算报表</h3><span class="muted">支持按清算周期在线核对和下载</span></div><div class="table-actions"><button class="secondary-btn" @click="exportSettlementReport('summary')"><FileText :size="15" />下载参与方总表</button><button class="secondary-btn" @click="exportSettlementReport('details')"><FileText :size="15" />下载订单明细</button><button class="secondary-btn" @click="exportSettlementReport('products')"><FileText :size="15" />下载产品汇总</button></div></div><div class="report-filter-bar"><label>周期开始<input v-model="settlementReportFilters.start" type="date" /></label><label>周期结束<input v-model="settlementReportFilters.end" type="date" /></label><button class="primary-btn" @click="loadSettlementReport"><RefreshCw :size="15" />查询周期</button></div><div class="metric-grid report-metrics"><div class="metric-card"><span>订单总额</span><strong>{{ fmtMoney(settlementReport.summary.gross_amount) }}</strong></div><div class="metric-card"><span>订单数量</span><strong>{{ settlementReport.summary.count || 0 }}</strong></div><div class="metric-card"><span>总成本</span><strong>{{ fmtMoney(settlementReport.summary.cost_amount) }}</strong></div><div class="metric-card"><span>总利润</span><strong>{{ fmtMoney(settlementReport.summary.profit_amount) }}</strong></div></div><div class="report-section-heading"><strong>自然月清算汇总</strong><span class="muted">按订单清算明细的创建月份统计</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>自然月</th><th>订单数</th><th>订单金额</th><th>订单成本</th><th>订单利润</th><th>平台运营方</th><th>数据/服务提供方</th><th>数据服务方</th><th>专家</th><th>渠道</th></tr></thead><tbody><tr v-for="item in settlementReport.monthly || []" :key="item.label"><td>{{ item.label }}</td><td>{{ item.order_count }}</td><td>{{ fmtMoney(item.gross_amount) }}</td><td>{{ fmtMoney(item.cost_amount) }}</td><td>{{ fmtMoney(item.profit_amount) }}</td><td>{{ fmtMoney(item.platform_fee) }}</td><td>{{ fmtMoney(item.provider_share) }}</td><td>{{ fmtMoney(item.service_share) }}</td><td>{{ fmtMoney(item.expert_fee) }}</td><td>{{ fmtMoney(item.channel_fee) }}</td></tr><tr v-if="!(settlementReport.monthly || []).length"><td colspan="10"><div class="empty-state">当前筛选周期暂无自然月汇总数据</div></td></tr></tbody></table></div><div class="report-section-heading"><strong>产品维度清算汇总</strong><span class="muted">按产品统计订单、成本、利润和参与方金额</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>产品</th><th>订单数</th><th>订单金额</th><th>订单成本</th><th>订单利润</th><th>平台运营方</th><th>数据/服务提供方</th><th>数据服务方</th><th>专家</th><th>渠道</th></tr></thead><tbody><tr v-for="item in settlementReport.products || []" :key="item.product_id || item.product_name"><td>{{ item.product_name }}</td><td>{{ item.order_count }}</td><td>{{ fmtMoney(item.gross_amount) }}</td><td>{{ fmtMoney(item.cost_amount) }}</td><td>{{ fmtMoney(item.profit_amount) }}</td><td>{{ fmtMoney(item.platform_fee) }}</td><td>{{ fmtMoney(item.provider_share) }}</td><td>{{ fmtMoney(item.service_share) }}</td><td>{{ fmtMoney(item.expert_fee) }}</td><td>{{ fmtMoney(item.channel_fee) }}</td></tr><tr v-if="!(settlementReport.products || []).length"><td colspan="10"><div class="empty-state">当前筛选周期暂无产品汇总数据</div></td></tr></tbody></table></div><div class="report-section-heading"><strong>参与清算各方汇总</strong><span class="muted">按参与方分别合计</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>参与方类型</th><th>参与方</th><th>订单数</th><th>清算小计金额</th></tr></thead><tbody><tr v-for="item in settlementReport.summary.participants || []" :key="`${item.participant_type}-${item.participant_name}`"><td>{{ participantLabel(item.participant_type) }}</td><td>{{ item.participant_name }}</td><td>{{ item.order_count }}</td><td>{{ fmtMoney(item.amount) }}</td></tr><tr v-if="!(settlementReport.summary.participants || []).length"><td colspan="4"><div class="empty-state">当前周期暂无参与方清算数据</div></td></tr></tbody></table></div><div class="report-section-heading"><strong>订单清算明细</strong><span class="muted">每个订单的金额、成本、利润和参与方分配</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>清算单</th><th>订单</th><th>订单金额</th><th>订单成本</th><th>订单利润</th><th>专家</th><th>渠道</th><th>参与方清算小计</th><th>参与方明细</th><th>状态</th></tr></thead><tbody><tr v-for="item in settlementReport.items" :key="item.id"><td>{{ item.settlement_no }}</td><td>{{ item.order_no || item.order_id.slice(0, 12) }}</td><td>{{ fmtMoney(item.gross_amount) }}</td><td>{{ fmtMoney(item.cost_amount) }}</td><td>{{ fmtMoney(item.profit_amount) }}</td><td>{{ fmtMoney(item.expert_fee) }}</td><td>{{ fmtMoney(item.channel_fee) }}</td><td>{{ fmtMoney(item.participant_total) }}</td><td><div class="report-participant-list"><span v-for="participant in item.participants" :key="`${item.id}-${participant.participant_type}-${participant.participant_name}`">{{ participant.participant_name }}：{{ fmtMoney(participant.amount) }}</span></div></td><td>{{ item.status }}</td></tr><tr v-if="!settlementReport.items.length"><td colspan="10"><div class="empty-state">当前周期暂无订单清算数据</div></td></tr></tbody></table></div></div>
          <div v-else-if="settlementTab === 'measurements'" class="panel"><div class="panel-heading"><div><span class="section-kicker">MEASUREMENT AND BILLING</span><h3>计量计费数据</h3></div><button class="primary-btn" @click="createMeasurement">登记计量</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>订单</th><th>计量类型</th><th>数量</th><th>单位</th><th>来源</th><th>校验状态</th><th>时间</th></tr></thead><tbody><tr v-for="item in settlementMeasurements" :key="item.id"><td>{{ item.order_id.slice(0, 12) }}</td><td>{{ item.measurement_type }}</td><td>{{ item.quantity }}</td><td>{{ item.unit }}</td><td>{{ item.source }}</td><td><span class="status-pill status-done">{{ item.validation_status }}</span></td><td>{{ fmtDate(item.created_at) }}</td></tr></tbody></table></div><div v-if="!settlementMeasurements.length" class="empty-state">暂无计量数据</div></div>
          <div v-else class="panel">
            <div class="panel-heading">
              <h3>
                清算单 <small>{{ settlementItems.length }} 张</small>
              </h3>
              <button class="primary-btn" @click="generateSettlementBatch">
                生成指定月份清算批次 <ArrowUpRight :size="15" />
              </button>
            </div>
            <div class="settlement-filter-bar">
              <label>清算批次<input v-model="settlementFilters.batch_id" list="settlement-batch-options" placeholder="下拉选择或输入批次号模糊匹配" /><datalist id="settlement-batch-options"><option value=""></option><option v-for="batch in settlementBatches" :key="batch.id" :value="batch.batch_no"></option></datalist></label>
              <label>清算单<input v-model="settlementFilters.settlement_id" list="settlement-options" placeholder="下拉选择或输入清算单号模糊匹配" /><datalist id="settlement-options"><option v-for="item in settlementFilterItems" :key="item.id" :value="item.settlement_no"></option></datalist></label>
              <label>订单号<input v-model="settlementFilters.order_no" list="settlement-order-options" placeholder="下拉选择或输入订单号模糊匹配" /><datalist id="settlement-order-options"><option v-for="item in settlementFilterOrders" :key="item.order_no" :value="item.order_no"></option></datalist></label>
              <label>状态<select v-model="settlementFilters.status"><option value="">全部状态</option><option value="pending">待处理</option><option value="disputed">待处理调整提案</option><option value="proposal_rejected">调整提案被拒绝</option><option value="adjusted">已调整</option><option value="locked">已锁定</option><option value="paid">已付款</option></select></label>
              <button class="primary-btn" @click="searchSettlements"><Search :size="15" />检索</button>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>清算批次</th>
                    <th>清算单</th>
                    <th>订单</th>
                    <th>原始金额</th>
                    <th>退款追回</th>
                    <th>净收入</th>
                    <th>版本成本</th>
                    <th>利润</th>
                    <th>平台服务费</th>
                    <th>提供方</th>
                    <th>服务方</th>
                    <th>专家</th>
                    <th>渠道</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in settlementItems" :key="item.id">
                    <td>
                      <span class="task-code">{{ item.batch_no || '-' }}</span><small>{{ item.batch_status || '-' }}</small>
                    </td>
                    <td>
                      <button class="link-btn" @click="openSettlementDetail(item)">{{ item.settlement_no }}</button
                      ><small>{{ fmtDate(item.created_at) }}</small>
                    </td>
                    <td><span class="task-code">{{ item.order_no || item.order_id }}</span></td>
                    <td>
                      <strong>{{ fmtMoney(item.gross_amount) }}</strong>
                    </td>
                    <td>{{ fmtMoney(item.refund_recovery) }}</td>
                    <td>
                      <strong>{{ fmtMoney(item.net_amount) }}</strong>
                    </td>
                    <td>{{ fmtMoney(item.cost_amount) }}</td>
                    <td><strong>{{ fmtMoney(item.profit_amount) }}</strong></td>
                    <td>{{ fmtMoney(item.platform_fee) }}</td>
                    <td>{{ fmtMoney(item.provider_share) }}</td>
                    <td>{{ fmtMoney(item.service_share) }}</td>
                    <td>{{ fmtMoney(item.expert_fee) }}</td>
                    <td>{{ fmtMoney(item.channel_fee) }}</td>
                    <td>
                      <span class="status-pill status-review">{{ settlementStatusLabel(item.status) }}</span>
                    </td>
                    <td>
                      <div class="table-actions">
                        <button
                          v-if="!['locked','paid','superseded'].includes(item.status)"
                          class="text-btn"
                          @click="openSettlementAdjustment(item)"
                        >
                          调整</button
                        ><button
                          v-if="!['locked','paid','superseded'].includes(item.status)"
                          class="text-btn danger-text"
                          @click="lockSettlement(item)"
                        >
                          锁定</button
                        ><span v-else class="muted">已锁定</span>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="!settlementItems.length" class="empty-state">
              <BarChart3 :size="28" /><span
                >暂无清算单，请先为已支付订单生成</span
              >
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'audit'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">可信运营 · 全链路留痕</div>
              <h1>审计日志</h1>
              <p>按业务分类、订单、清算批次和规则版本快速定位审计事件。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('audit')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="audit-category-bar">
            <button
              v-for="(labelText, category) in auditCategoryLabels"
              :key="category || 'all'"
              class="audit-category-btn"
              :class="{ active: auditSelectedCategory === category }"
              @click="selectAuditCategory(category)"
            >
              {{ labelText }}
              <small v-if="category">{{ auditCategories.find((x) => x.category === category)?.count || 0 }}</small>
            </button>
          </div>
          <div class="panel audit-panel">
            <div class="audit-filter-grid">
              <label>关键词<input v-model="auditFilters.q" placeholder="动作、对象或说明" @keyup.enter="loadAuditLogs" /></label>
              <label>操作人<input v-model="auditFilters.actor" placeholder="用户名/邮箱" @keyup.enter="loadAuditLogs" /></label>
              <label>订单号<input v-model="auditFilters.order_id" placeholder="订单 ID" @keyup.enter="loadAuditLogs" /></label>
              <label>清算批次<input v-model="auditFilters.batch_no" placeholder="批次号" @keyup.enter="loadAuditLogs" /></label>
              <label>规则版本<input v-model="auditFilters.rule_version" placeholder="如 v1" @keyup.enter="loadAuditLogs" /></label>
              <label>风险级别<select v-model="auditFilters.risk_level"><option value="">全部</option><option value="normal">正常</option><option value="high">高风险</option></select></label>
            </div>
            <div class="audit-filter-actions">
              <button class="primary-btn" @click="auditFilters.page = 1; loadAuditLogs()"><Search :size="15" />检索</button>
              <button class="secondary-btn" @click="resetAuditFilters">清空条件</button>
              <span class="muted">共 {{ auditFilters.total }} 条，当前第 {{ auditFilters.page }} / {{ auditFilters.pages || 1 }} 页</span>
            </div>
            <div class="panel-heading audit-list-heading">
              <h3>{{ auditCategoryLabels[auditSelectedCategory] || "审计事件" }}</h3>
              <span class="muted">点击行查看变更摘要和关联链</span>
            </div>
            <div class="table-wrap">
              <table class="data-table audit-table">
                <thead><tr><th>时间</th><th>分类</th><th>操作人</th><th>动作</th><th>业务关联</th><th>结果</th><th>说明</th></tr></thead>
                <tbody>
                  <tr v-for="item in auditItems" :key="item.id" @click="auditSelected = item">
                    <td>{{ fmtDate(item.created_at) }}</td>
                    <td><span class="quality-tag">{{ auditCategoryLabels[item.category] || item.category }}</span></td>
                    <td>{{ item.actor }}</td>
                    <td><span class="task-code">{{ item.action }}</span></td>
                    <td><strong v-if="item.order_id">订单 {{ item.order_id.slice(0, 10) }}</strong><small v-if="item.batch_no">批次 {{ item.batch_no }}</small><small v-if="item.rule_version">规则 {{ item.rule_version }}</small></td>
                    <td><span class="status-pill" :class="item.risk_level === 'high' ? 'status-blocked' : 'status-done'">{{ item.risk_level === 'high' ? '高风险' : item.result }}</span></td>
                    <td>{{ item.detail || "-" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="!auditItems.length" class="empty-state"><ShieldCheck :size="28" /><span>暂无符合条件的审计事件</span></div>
            <div class="audit-pagination"><button class="secondary-btn" :disabled="auditFilters.page <= 1" @click="auditPage(-1)">上一页</button><button class="secondary-btn" :disabled="auditFilters.pages && auditFilters.page >= auditFilters.pages" @click="auditPage(1)">下一页</button></div>
          </div>
          <div v-if="auditSelected" class="drawer-scrim" @click.self="auditSelected = null"><aside class="order-drawer audit-drawer"><div class="drawer-head"><div><div class="eyebrow">AUDIT EVENT</div><h2>{{ auditSelected.action }}</h2></div><button class="icon-btn" @click="auditSelected = null"><X :size="18" /></button></div><div class="drawer-summary"><strong>{{ auditCategoryLabels[auditSelected.category] || auditSelected.category }}</strong><span>{{ fmtDate(auditSelected.created_at) }} · {{ auditSelected.actor }}</span><span>{{ auditSelected.detail || "无补充说明" }}</span></div><div class="drawer-section"><div class="drawer-section-title">业务关联</div><div class="state-grid"><div><small>对象</small><strong>{{ auditSelected.target_type }} / {{ auditSelected.target_id }}</strong></div><div><small>订单</small><strong>{{ auditSelected.order_id || "-" }}</strong></div><div><small>清算批次</small><strong>{{ auditSelected.batch_no || "-" }}</strong></div><div><small>规则版本</small><strong>{{ auditSelected.rule_version || "-" }}</strong></div><div><small>请求号</small><strong>{{ auditSelected.request_id || "-" }}</strong></div><div><small>风险级别</small><strong>{{ auditSelected.risk_level }}</strong></div></div></div><div class="drawer-section"><div class="drawer-section-title">变更前后摘要</div><pre class="audit-json">{{ JSON.stringify(auditSelected.before || {}, null, 2) }}
{{ JSON.stringify(auditSelected.after || {}, null, 2) }}</pre></div></aside></div>
        </template>
        <template v-else-if="activeView === 'users'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">企业主租户 · 成员和身份</div>
              <h1>用户与企业</h1>
              <p>用户实名、企业实名和企业成员关系统一管理。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('users')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="tabs">
            <button
              :class="{ active: userEnterpriseTab === 'users' }"
              @click="userEnterpriseTab = 'users'"
            >
              平台注册用户 {{ userItems.length }} 人</button
            ><button
              :class="{ active: userEnterpriseTab === 'enterprises' }"
              @click="userEnterpriseTab = 'enterprises'"
            >
              平台注册企业 {{ enterpriseItems.length }} 个
            </button>
          </div>
          <div v-if="userEnterpriseTab === 'users' && myEnterpriseInvitations.length" class="panel received-invitations-panel"><div class="panel-heading"><div><span class="section-kicker">MY INVITATIONS</span><h3>待接受的企业邀请</h3></div><span class="muted">接受后默认加入为企业普通成员</span></div><div class="received-invitations"><div v-for="item in myEnterpriseInvitations" :key="item.id" class="received-invitation"><span>{{ item.enterprise_name }}</span><button class="text-btn" @click="acceptEnterpriseInvitation(item)">接受并加入</button></div></div></div>
          <div v-if="userEnterpriseTab === 'users'" class="panel">
            <div class="panel-heading">
              <h3>平台注册用户</h3>
              <span class="muted">实名状态可按流程操作</span>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>登录名</th>
                    <th>实名认证</th>
                    <th>平台角色</th>
                    <th>账户状态</th>
                    <th>注册时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in userItems" :key="item.id">
                    <td>
                      <strong>{{ item.name }}</strong
                      ><small>{{ item.id.slice(0, 12) }}</small>
                    </td>
                    <td>{{ item.email || item.phone || "-" }}</td>
                    <td>
                      <div class="table-actions">
                        <button
                          v-if="
                            item.id === user?.id &&
                            (identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ) === '待实名' ||
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ) === '待审核' ||
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ) === '已拒绝')
                          "
                          class="status-pill status-todo status-action"
                          @click="openPersonalEdit(personalForUser(item.id))"
                        >
                          {{
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            )
                          }}</button
                        ><span
                          v-else
                          :class="[
                            'status-pill',
                            identityStatusClass(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ),
                          ]"
                          >{{
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            )
                          }}</span>
                      </div>
                    </td>
                    <td>{{ item.platform_role || "-" }}</td>
                    <td>{{ item.is_active ? "正常" : "已禁用" }}</td>
                    <td>{{ fmtDate(item.created_at) }}</td>
                    <td>
                      <div class="table-actions" v-if="personalForUser(item.id)">
                        <template v-if="canReviewIdentity() && personalForUser(item.id)?.status === 'pending_review'">
                          <button class="text-btn" @click="openPersonalReview(personalForUser(item.id))">实名审核</button>
                        </template>
                        <button v-else-if="canReviewIdentity() && ['pending', 'pending_review'].includes(personalForUser(item.id)?.status)" class="text-btn" @click="openPersonalReview(personalForUser(item.id))">实名审核</button>
                      </div>
                      <span v-else class="muted">-</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-else class="panel">
            <div class="panel-heading">
              <h3>平台注册企业</h3>
              <button
                class="primary-btn"
                @click="showEnterpriseVerification = true"
              >
                注册企业 <ArrowUpRight :size="16" />
              </button>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>企业名称</th>
                    <th>统一社会信用代码</th>
                    <th>企业类型</th>
                    <th>法定代表人</th>
                    <th>认证状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in enterpriseItems" :key="item.id">
                    <td>
                      <strong>{{ item.name }}</strong>
                    </td>
                    <td>{{ item.credit_code }}</td>
                    <td>{{ item.enterprise_type || "-" }}</td>
                    <td>{{ item.legal_representative || "-" }}</td>
                    <td>
                      <span
                        :class="[
                          'status-pill',
                          item.verification_status === 'verified'
                            ? 'status-done'
                            : item.verification_status === 'rejected'
                              ? 'status-blocked'
                              : 'status-review',
                        ]"
                        >{{
                          item.verification_status === "verified"
                            ? "已认证"
                            : item.verification_status === "rejected"
                              ? "已拒绝"
                              : "待审核"
                        }}</span
                      >
                    </td>
                    <td>
                      <div class="table-actions">
                        <button
                          v-if="item.verification_status === 'rejected' && item.id === enterprise?.id"
                          class="text-btn"
                          @click="openEnterpriseResubmit(item)"
                        >
                          重新提交
                        </button><button v-if="canReviewIdentity() && item.verification_status === 'pending_review'" class="text-btn" @click="openEnterpriseReview(item)">实名审核</button><template v-if="item.verification_status === 'verified' && canManageEnterprise(item)"><button class="text-btn" @click="openEnterpriseManagement(item, 'details')">企业详情</button><button class="text-btn" @click="openEnterpriseManagement(item, 'invite')">邀请用户</button><button class="text-btn" @click="openEnterpriseManagement(item, 'departments')">部门管理</button><button class="text-btn" @click="openEnterpriseManagement(item, 'members')">成员管理</button></template>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div></template
        >
        <template v-else-if="activeView === 'settings'"
          ><div class="page-heading">
            <div>
              <div class="eyebrow">平台管理 · 通知与角色</div>
              <h1>系统设置</h1>
              <p>配置短信、SMTP 发件平台，并由平台超级管理员维护固化运营角色。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('settings')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="two-column-panels">
            <div class="panel">
              <div class="panel-heading">
                <h3>短信与 SMTP 发件配置</h3>
                <span class="muted">密码仅保存不回显</span>
              </div>
              <div class="form-grid">
                <label
                  >SMTP 服务器<input
                    v-model="notificationSettings.smtp_host"
                    placeholder="例如 smtp.example.com" /></label
                ><label
                  >SMTP 端口<input
                    v-model="notificationSettings.smtp_port"
                    type="number"
                /></label>
              </div>
              <div class="form-grid">
                <label
                  >SMTP 发件用户名<input
                    v-model="notificationSettings.smtp_username" /></label
                ><label
                  >SMTP 发件密码<input
                    v-model="notificationSettings.smtp_password"
                    type="password"
                    placeholder="留空表示不修改"
                /></label>
              </div>
              <label class="checkbox-line"
                ><input
                  v-model="notificationSettings.smtp_ssl"
                  type="checkbox"
                />使用 SSL</label
              ><label class="checkbox-line"
                ><input
                  v-model="notificationSettings.smtp_starttls"
                  type="checkbox"
                />使用 STARTTLS</label
              ><label
                >发件人名称<input v-model="notificationSettings.smtp_from_name"
              /></label>
              >
              <div class="form-grid">
                <label
                  >短信平台<input
                    v-model="notificationSettings.sms_provider"
                    placeholder="阿里云、腾讯云等" /></label
                ><label
                  >短信接口地址<input
                    v-model="notificationSettings.sms_endpoint"
                /></label>
              </div>
              <button class="primary-btn" @click="saveNotificationSettings">
                保存通知配置 <ArrowUpRight :size="16" />
              </button>
            </div>
            <div class="panel">
              <div class="panel-heading">
                <h3>固化平台角色账号</h3>
                <span class="muted">七类角色由平台统一维护，初始密码：Admin123!</span>
              </div>
              <p class="muted">角色账号不可通过普通注册或动态分配产生。平台管理员如需调整权限，应修改固定账号的角色配置。</p>
            </div>
          </div>
          <div class="panel">
            <div class="panel-heading">
              <h3>
                已分配平台角色
                <small>{{ platformRoleItems.items.length }} 人</small>
              </h3>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>登录名</th>
                    <th>平台角色</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in platformRoleItems.items" :key="item.id">
                    <td>{{ item.name }}</td>
                    <td><strong>{{ item.username }}</strong><small>{{ item.email }}</small></td>
                    <td>
                      <span class="status-pill status-done">{{
                        item.role_name
                      }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div></template
        >
        <template v-else
          ><div class="page-heading">
            <div>
              <div class="eyebrow">运营模块</div>
              <h1>{{ nav.find((x) => x.key === activeView)?.label }}</h1>
              <p>
                该工作台模块已接入首版接口边界，功能数据将在对应开发任务完成后持续丰富。
              </p>
            </div>
            <button class="secondary-btn" @click="refreshData">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="empty-module">
            <component
              :is="nav.find((x) => x.key === activeView)?.icon"
              :size="38"
            />
            <h3>模块骨架已就绪</h3>
            <p>
              开发任务监控台会显示该模块的完成状态，当前主闭环优先保证订单和清算逻辑。
            </p>
            <button class="text-btn" @click="selectView('development')">
              查看开发任务 <ChevronRight :size="15" />
            </button></div
        ></template>
      </section>
      <div v-if="false && activeView === 'settings'" class="settings-overlay">
        <div class="panel settings-sheet">
          <div class="panel-heading">
            <div>
              <span class="section-kicker">PLATFORM ADMINISTRATION</span>
              <h3>系统通知与平台角色</h3>
            </div>
            <button class="icon-btn" @click="selectView('overview')">
              <X :size="18" />
            </button>
          </div>
          <div class="two-column-panels">
            <div>
              <h4>邮件与短信配置</h4>
              <div class="form-grid">
                <label
                  >邮件服务器<input
                    v-model="notificationSettings.email_host" /></label
                ><label
                  >端口<input
                    v-model="notificationSettings.email_port"
                    type="number"
                /></label>
              </div>
              <label
                >发件用户名<input
                  v-model="notificationSettings.email_username" /></label
              ><label
                >邮件密码<input
                  v-model="notificationSettings.email_password"
                  type="password"
                  placeholder="留空表示不修改" /></label
              ><button class="primary-btn" @click="saveNotificationSettings">
                保存通知配置
              </button>
            </div>
            <div>
              <h4>分配平台角色</h4>
              <label>手机或邮箱<input v-model="roleForm.identifier" /></label>
              <div class="form-grid">
                <label>姓名<input v-model="roleForm.name" /></label
                ><label
                  >角色<select v-model="roleForm.role">
                    <option
                      v-for="(name, key) in platformRoleItems.roles"
                      :key="key"
                      :value="key"
                    >
                      {{ name }}
                    </option>
                  </select></label
                >
              </div>
              <button class="primary-btn" @click="assignPlatformRole">
                生成临时登录密码
              </button>
            </div>
          </div>
        </div>
      </div>
      <div v-if="activeView === 'verification'" class="settings-overlay">
        <div class="panel settings-sheet">
          <div class="panel-heading">
            <div>
              <span class="section-kicker">IDENTITY VERIFICATION</span>
              <h3>个人与企业实名认证</h3>
            </div>
            <button class="icon-btn" @click="selectView('overview')">
              <X :size="18" />
            </button>
          </div>
          <div class="two-column-panels">
            <div>
              <h4>个人实名认证 · 人工审核</h4>
              <label
                >身份证姓名<input v-model="verificationForm.id_name" /></label
              ><label
                >身份证号码<input v-model="verificationForm.id_number"
              /></label>
              <div class="form-grid">
                <label
                  >身份证正面<input
                    type="file"
                    accept="image/*"
                    @change="
                      verificationFiles.front = $event.target.files[0]
                    " /></label
                ><label
                  >身份证反面<input
                    type="file"
                    accept="image/*"
                    @change="verificationFiles.back = $event.target.files[0]"
                /></label>
              </div>
              <label
                >实名认证手机号<input v-model="verificationForm.phone" /></label
              ><label
                >短信验证码<input
                  v-model="verificationForm.phone_code"
                  placeholder="开发环境固定 123456" /></label
              ><label
                >个人在企业中的角色（可选）<input
                  v-model="verificationForm.enterprise_role" /></label
              ><button class="primary-btn" @click="submitPersonalVerification">
                提交个人实名认证
              </button>
              <p class="muted">
                当前状态：{{
                  verification.personal?.item?.status ||
                  verification.personal?.user_verified_status ||
                  "未提交"
                }}
              </p>
            </div>
            <div>
              <h4>企业实名认证 · 人工审核</h4>
              <label
                >营业执照复印件<input
                  type="file"
                  accept="image/*,.pdf"
                  @change="
                    verificationFiles.license = $event.target.files[0]
                  " /></label
              ><label
                >企业法定全称<input
                  v-model="verificationForm.enterprise_name" /></label
              ><label
                >统一社会信用代码<input
                  v-model="verificationForm.credit_code" /></label
              ><label
                >企业类型/主体类型<input
                  v-model="verificationForm.enterprise_type" /></label
              ><label
                >法定代表人/负责人<input
                  v-model="verificationForm.legal_representative" /></label
              ><label
                >注册资本/出资额<input
                  v-model="verificationForm.registered_capital" /></label
              ><label
                >成立日期<input
                  v-model="verificationForm.establishment_date"
                  type="date" /></label
              ><label
                >经营场所<input
                  v-model="verificationForm.business_address" /></label
              ><label
                >经营范围<textarea
                  v-model="verificationForm.business_scope"
                ></textarea></label
              ><button
                class="primary-btn"
                @click="submitEnterpriseVerification"
              >
                提交企业实名认证
              </button>
              <p class="muted">
                当前状态：{{
                  verification.enterprise?.verification_status ||
                  "需先完成个人认证"
                }}
              </p>
            </div>
          </div>
        </div>
      </div>
    </main>
    <div
      v-if="selectedOrder"
      class="drawer-scrim"
      @click="selectedOrder = null"
    >
      <aside class="order-drawer" @click.stop>
        <div class="drawer-head">
          <div>
            <span class="eyebrow">订单详情</span>
            <h2>{{ selectedOrder.order.order_no }}</h2>
          </div>
          <button class="icon-btn" @click="selectedOrder = null">
            <X :size="19" />
          </button>
        </div>
        <div class="drawer-summary">
          <strong>{{ selectedOrder.order.product_name }}</strong
          ><span>{{ selectedOrder.order.buyer_name }}</span
          ><b>{{ fmtMoney(selectedOrder.order.amount) }}</b>
        </div>
        <div class="state-grid">
          <div>
            <small>主状态</small
            ><strong>{{ label(selectedOrder.order.main_status) }}</strong>
          </div>
          <div>
            <small>支付状态</small
            ><strong>{{ label(selectedOrder.order.payment_status) }}</strong>
          </div>
          <div>
            <small>交付状态</small
            ><strong>{{ label(selectedOrder.order.delivery_status) }}</strong>
          </div>
          <div>
            <small>售后状态</small
            ><strong>{{
              label(selectedOrder.order.after_sales_status)
            }}</strong>
          </div>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">可执行动作</div>
          <div class="action-list">
            <button
              v-for="action in nextActions(selectedOrder.order)"
              :key="action[0]"
              class="secondary-btn"
              @click="transition(action[0])"
            >
              {{ action[1] }} <ArrowUpRight :size="14" /></button
            ><span v-if="!nextActions(selectedOrder.order).length" class="muted"
              >当前没有可执行动作</span
            >
          </div>
        </div>
        <div v-if="orderProductFiles.items.length" class="drawer-section">
          <div class="drawer-section-title">数据文件交付</div>
          <p class="muted">本订单下载次数：{{ orderProductFiles.download_limit === 0 ? '不限制' : `${orderProductFiles.items[0]?.downloaded || 0}/${orderProductFiles.download_limit}` }}</p>
          <div v-for="file in orderProductFiles.items" :key="file.id" class="file-status-row">
            <span>{{ file.original_name }}<small class="muted"> · 已下载{{ file.downloaded }}次</small></span>
            <button class="secondary-btn" @click="downloadOrderProductFile(file)">下载</button>
          </div>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">状态时间轴</div>
          <div class="timeline">
            <div
              v-for="item in selectedOrder.logs"
              :key="`${item.created_at}-${item.action}`"
              class="timeline-item"
            >
              <span class="timeline-dot"></span>
              <div>
                <strong>{{ item.action }}</strong
                ><small
                  >{{ item.operator }} · {{ fmtDate(item.created_at) }}</small
                >
                <p>{{ item.from_status || "初始" }} → {{ item.to_status }}</p>
              </div>
            </div>
          </div>
        </div>
        <div
          v-if="selectedOrder.order.subscription_id"
          class="drawer-section saas-order-panel"
        >
          <div class="drawer-section-title">SaaS 订阅管理</div>
          <p class="muted">
            订阅 ID：{{ selectedOrder.order.subscription_id }}
          </p>
          <div class="action-list">
            <button class="secondary-btn" @click="saasOrderAction('renew')">
              续费</button
            ><button class="secondary-btn" @click="saasOrderAction('change')">
              变更版本</button
            ><button class="secondary-btn" @click="saasOrderAction('close')">
              关闭租户</button
            ><button class="secondary-btn" @click="saasOrderAction('restore')">
              恢复租户
            </button>
          </div>
          <div class="saas-sync-grid">
            <div>
              <div class="drawer-section-title">
                用户同步
                <button class="text-btn" @click="saasSyncUser">增加用户</button>
              </div>
              <div
                v-for="item in saasOrderState.users"
                :key="item.id"
                class="saas-sync-item"
              >
                <span>{{ item.user_id }}</span
                ><button
                  class="icon-btn"
                  title="删除用户"
                  @click="saasRemoveUser(item)"
                >
                  <X :size="14" />
                </button>
              </div>
              <small v-if="!saasOrderState.users.length" class="muted"
                >暂无同步用户</small
              >
            </div>
            <div>
              <div class="drawer-section-title">
                部门同步
                <button class="text-btn" @click="saasSyncDepartment">
                  增加部门
                </button>
              </div>
              <div
                v-for="item in saasOrderState.departments"
                :key="item.id"
                class="saas-sync-item"
              >
                <span>{{ item.name }}</span
                ><button
                  class="icon-btn"
                  title="删除部门"
                  @click="saasRemoveDepartment(item)"
                >
                  <X :size="14" />
                </button>
              </div>
              <small v-if="!saasOrderState.departments.length" class="muted"
                >暂无同步部门</small
              >
            </div>
          </div>
          <small class="muted"
            >最近操作：{{ saasOrderState.operations[0]?.operation || "暂无" }} ·
            {{ saasOrderState.operations[0]?.status || "" }}</small
          >
        </div>
        <div
          v-if="apiOrderState.available"
          class="drawer-section api-order-panel"
        >
          <div class="drawer-section-title">API 调用凭据（企业共享）</div>
          <p class="muted">
            网关路径：{{ apiOrderState.gateway_base_path }} ·
            同一企业购买同一 API 服务的多个订单共用一套凭据，订单额度自动合并；凭据原文仅在首次生成或重新生成时显示。
          </p>
          <div class="action-list">
            <button class="secondary-btn" @click="createOrderApiCredential">
              <ArrowUpRight :size="14" />获取凭据
            </button>
          </div>
          <div
            v-for="item in apiOrderState.items"
            :key="item.id"
            class="api-credential-item"
          >
            <div>
              <strong>{{ item.name }}</strong
              ><small
                >{{ item.key_prefix }} ·
                {{ item.status === "active" ? "启用中" : "已停用" }} ·
                企业共享总额度 {{ item.total_quota || "不限" }} ·
                {{ fmtDate(item.created_at) }}</small
              >
            </div>
            <div class="action-list">
              <button
                v-if="item.status === 'active'"
                class="text-btn danger-text"
                @click="revokeOrderApiCredential(item)"
              >
                停用</button
              ><button
                v-if="item.status === 'active'"
                class="text-btn"
                @click="regenerateOrderApiCredential(item)"
              >
                重新生成
              </button>
            </div>
          </div>
          <div v-if="apiCredentialReveal" class="api-credential-reveal">
            <div class="drawer-section-title">本次生成的 API Key</div>
            <div class="credential-value">
              <code>{{ apiCredentialReveal.api_key }}</code
              ><button
                class="icon-btn"
                title="复制 API Key"
                @click="copyApiCredential"
              >
                <Copy :size="15" />
              </button>
            </div>
            <small class="muted"
              >请立即保存。平台不会再次显示此 API Key 原文。</small
            >
          </div>
        </div>
      </aside>
    </div>
    <div
      v-if="showProductForm"
      class="modal-scrim"
      @click="showProductForm = false"
    >
      <form
        class="modal-card product-modal"
        @submit.prevent="productDetailMode ? saveProductEdit() : createProduct()"
        @click.stop
      >
        <div class="drawer-head">
          <div>
            <span class="eyebrow">PRODUCT REGISTRATION</span>
            <h2>{{ productDetailMode ? "产品信息" : "登记数据或服务" }}</h2>
            <p v-if="productDetailMode" class="modal-status-line">
              当前状态：{{ productForm.status === "published" ? "已发布" : productForm.status === "pending_review" ? "待审核" : productForm.status === "security_review" ? "安全审核中" : productForm.status === "security_unpublished" ? "安全下架" : productForm.status === "rejected" ? "已驳回" : "草稿" }} · {{ productForm.review_comment || "暂无审核意见" }}
            </p>
          </div>
          <button
            type="button"
            class="icon-btn"
            @click="showProductForm = false"
          >
            <X :size="19" />
          </button>
        </div>
        <fieldset :disabled="productReadOnlyMode" class="product-fieldset">
        <div class="form-section-title">基础元数据</div>
        <label
          >产品或服务名称<input
            v-model="productForm.name"
            required
            placeholder="例如：矿山装备质量数据集"
        /></label>
        <div class="form-grid">
          <label
            >所属目录<select v-model="productForm.catalog_name" required>
              <option disabled value="">请选择产品或服务所属目录</option>
              <option
                v-for="item in productDirectories"
                :key="item.value"
                :value="item.value"
              >
                {{ item.label }}
              </option>
            </select></label
          ><label
            >产品类型<select v-model="productForm.product_type">
              <option v-for="(name, key) in typeLabels" :key="key" :value="key">
                {{ name }}
              </option>
            </select></label
          >
        </div>
        <div class="form-grid">
          <label
            >提供方名称<input
              v-model="productForm.provider_name"
              placeholder="默认使用当前企业" /></label
          ><label
            >提供方类型<select v-model="productForm.provider_type">
              <option value="企业">企业</option>
              <option value="机构">机构</option>
              <option value="个人专家">个人专家</option>
              <option value="平台运营方">平台运营方</option>
            </select></label
          >
        </div>
        <label
          >产品或服务描述<textarea
            v-model="productForm.description"
            rows="3"
            required
            placeholder="描述内容、能力边界和主要特点"
          ></textarea></label
        ><label
          >适用场景<textarea
            v-model="productForm.usage_scenarios"
            rows="2"
            placeholder="例如：质量追溯、缺陷根因分析、供应商评价"
          ></textarea>
        </label>
        <div class="form-section-title">运营与合规元数据</div>
        <div class="form-grid">
          <label
            >交付方式<select v-model="productForm.delivery_method">
              <option value="file">文件下载</option>
              <option value="api">API 服务</option>
              <option value="model_api">模型 API</option>
              <option value="tenant_access">租户/权限开通</option>
              <option value="training">线下培训</option>
              <option value="consulting">咨询交付</option>
              <option value="custom">定制开发</option>
            </select></label
          >
        </div>
        <div v-if="productForm.delivery_method === 'file'" class="form-grid">
          <label>数据文件（压缩包）<input type="file" accept=".zip,.tar,.gz,.tgz,.tar.gz,.bz2,.xz,.rar,.7z" @change="productForm.fileUpload = $event.target.files[0]" /></label>
          <label>每订单下载次数限制<input v-model.number="productForm.download_limit" type="number" min="0" step="1" /><small class="muted">0表示不限制</small></label>
        </div>
        <div v-if="['api', 'model_api'].includes(productForm.delivery_method)" class="form-grid">
          <label class="wide">提供方后端访问URL<input v-model="productForm.upstream_url" type="url" required placeholder="https://provider.example.com/api" /></label>
        </div>
        <div v-if="productForm.delivery_method === 'tenant_access'" class="form-grid">
          <label>应用访问URL<input v-model="productForm.application_url" type="url" required /></label>
          <label>租户/权限开通API URL<input v-model="productForm.integration_api_url" type="url" required /></label>
        </div>
        <div class="form-grid">
          <label>产品Logo（380×280）<input type="file" accept="image/*,.svg" @change="productForm.logoFile = $event.target.files[0]" /></label>
          <label>Logo说明<small class="muted">支持SVG及常见图片格式，提交后执行尺寸和安全校验。</small></label>
        </div>
        <div v-if="productDetailMode && productFiles.length" class="product-file-status">
          <div class="form-section-title">已上传文件与安全状态</div>
          <div v-if="productLogoPreview" class="product-logo-preview"><img :src="productLogoPreview" alt="产品Logo缩略图" /></div>
          <div v-for="file in visibleProductFiles" :key="file.id" class="file-status-row product-file-row">
            <div class="product-file-name"><strong>{{ file.original_name }}</strong><small class="muted"> · {{ file.version || "产品级" }} · {{ file.size }} bytes</small></div>
            <div class="product-file-scan">
              <button class="text-btn" @click="downloadProductFile(file)">下载文件</button>
              <template v-if="file.file_role === 'product_logo'">
                <span :class="['status-pill', file.clamav_status === 'clean' ? 'status-done' : 'status-blocked']">病毒：{{ file.clamav_status === 'clean' ? '通过' : file.clamav_status }}</span>
                <button class="text-btn" @click="openFileReport(file, 'clamav')">病毒报告</button>
              </template>
              <template v-else>
                <span :class="['status-pill', file.clamav_status === 'clean' ? 'status-done' : 'status-blocked']">病毒：{{ file.clamav_status === 'clean' ? '通过' : file.clamav_status }}</span>
                <button class="text-btn" @click="openFileReport(file, 'clamav')">病毒报告</button>
                <span :class="['status-pill', file.presidio_status === 'available' ? 'status-done' : file.presidio_status === 'not_scanned' ? 'status-review' : 'status-blocked']">Presidio：{{ file.presidio_status === 'available' ? '完成' : file.presidio_status }}</span>
                <button class="text-btn" @click="openFileReport(file, 'presidio')">Presidio报告</button>
              </template>
            </div>
          </div>
        </div>
        <div class="version-editor">
          <div class="version-editor-head">
            <div>
              <strong>版本与价格</strong
              ><small>每个版本单独定义销售价格、成本和简要介绍；SaaS版本成本按月计，订单成本按订阅周期自动计算</small>
            </div>
            <button v-if="!productReadOnlyMode" type="button" class="text-btn" @click="addProductVersion">
              新增版本
            </button>
          </div>
          <div
              v-for="(version, index) in productForm.versions"
            :key="index"
            class="version-row"
            :class="{ 'api-version-row': ['api', 'model'].includes(productForm.product_type) }"
          >
            <label
              >版本号<input v-model="version.version_code" required /></label
            ><label
              >版本价格<input
                v-model.number="version.price"
                type="number"
                min="0"
                step="0.01"
                required /></label
            ><label
              >版本成本<input
                v-model.number="version.cost"
                type="number"
                min="0"
                step="0.01"
                required /></label
            ><label v-if="['api', 'model'].includes(productForm.product_type)" title="该版本每分钟允许的最大调用次数"
              >每分钟限流<input v-model.number="version.rate_limit_per_minute" type="number" min="1" required /></label
            ><label v-if="['api', 'model'].includes(productForm.product_type)" title="该版本每日允许的最大调用次数"
              >每日配额<input v-model.number="version.daily_quota" type="number" min="1" required /></label
            ><label v-if="['api', 'model'].includes(productForm.product_type)" title="0 表示不单独限制月配额"
              >每月配额<input v-model.number="version.monthly_quota" type="number" min="0" /></label
            ><label v-if="['api', 'model_api'].includes(productForm.delivery_method)">
              额度单位<select v-model="version.quota_unit"><option value="1000">千次</option><option value="10000">万次</option></select></label
            ><label v-if="['api', 'model_api'].includes(productForm.delivery_method)">
              购买额度<input v-model.number="version.quota_amount" type="number" min="0" step="1" /></label
            ><label class="version-description"
              >版本简要介绍<textarea
                v-model="version.description"
                rows="2"
                placeholder="说明该版本的功能范围、能力差异或适用对象"
              ></textarea></label
            ><button v-if="!productReadOnlyMode"
              type="button"
              class="icon-btn"
              title="删除版本"
              @click="removeProductVersion(index)"
            >
              <X :size="16" />
            </button>
          </div>
        </div>
        <div class="form-grid">
          <label
            >价格策略<textarea
              v-model="productForm.pricing_strategy"
              rows="2"
              placeholder="按版本定价、按次、按量、按周期或阶梯价格"
            ></textarea></label
          ><label
            >安全等级<select v-model="productForm.security_level">
              <option value="一般">一般</option>
              <option value="重要">重要</option>
              <option value="敏感">敏感</option>
              <option value="核心">核心</option>
            </select></label
          >
        </div>
        <label
          >授权条件<textarea
            v-model="productForm.authorization_conditions"
            rows="2"
            required
            placeholder="使用主体、使用期限、使用范围、是否允许转授权"
          ></textarea></label
        ><label
          >数据来源声明<textarea
            v-model="productForm.data_source_statement"
            rows="2"
            placeholder="说明数据来源、权属和授权情况"
          ></textarea></label
        ><label
          >合法合规声明<textarea
            v-model="productForm.compliance_statement"
            rows="2"
            placeholder="说明分类分级、脱敏和合规审核情况"
          ></textarea>
        </label>
        <div class="form-section-title">清算规则</div>
        <div class="form-grid">
          <label>规则来源<select v-model="productForm.settlement_rule_mode">
            <option value="global">平台全局默认规则</option>
            <option value="custom">产品专属规则</option>
          </select></label>
          <label v-if="productForm.settlement_rule_mode === 'global'">全局规则<select v-model="productForm.settlement_rule_id">
            <option value="">审核时使用当前启用规则</option>
            <option v-for="rule in settlementRules.filter((item) => item.status === 'active')" :key="rule.id" :value="rule.id">{{ rule.name }} · {{ rule.version }}</option>
          </select></label>
        </div>
        <div v-if="productForm.settlement_rule_mode === 'global' && selectedProductSettlementRule" class="settlement-rule-preview">
          <div class="settlement-rule-preview-head"><strong>{{ selectedProductSettlementRule.name }} · {{ selectedProductSettlementRule.version }}</strong><span class="status-pill status-done">{{ selectedProductSettlementRule.status }}</span></div>
          <div class="settlement-rule-preview-grid">
            <span>平台服务费 <b>{{ selectedProductSettlementRule.platform_rate }}%</b></span>
            <span>提供方分成 <b>{{ selectedProductSettlementRule.provider_rate }}%</b></span>
            <span>数据服务方 <b>{{ selectedProductSettlementRule.service_rate }}%</b></span>
            <span>专家费用 <b>{{ selectedProductSettlementRule.expert_rate }}%</b></span>
            <span>渠道费用 <b>{{ selectedProductSettlementRule.channel_rate || 0 }}%</b></span>
            <span>规则生效 <b>{{ selectedProductSettlementRule.effective_at ? fmtDate(selectedProductSettlementRule.effective_at) : "立即" }}</b></span>
          </div>
          <small v-if="selectedProductSettlementRule.change_reason">变更说明：{{ selectedProductSettlementRule.change_reason }}</small>
        </div>
        <div v-if="productForm.settlement_rule_mode === 'custom'" class="form-grid settlement-product-rule">
          <label>平台服务费 (%)<input v-model.number="productForm.settlement_rule.platform_rate" type="number" min="0" max="100" step="0.01" /></label>
          <label>提供方分成 (%)<input v-model.number="productForm.settlement_rule.provider_rate" type="number" min="0" max="100" step="0.01" /></label>
          <label>数据服务方分成 (%)<input v-model.number="productForm.settlement_rule.service_rate" type="number" min="0" max="100" step="0.01" /></label>
          <label>专家费用 (%)<input v-model.number="productForm.settlement_rule.expert_rate" type="number" min="0" max="100" step="0.01" /></label>
          <label>渠道费用 (%)<input v-model.number="productForm.settlement_rule.channel_rate" type="number" min="0" max="100" step="0.01" /></label>
        </div>
        <p class="muted settlement-rule-hint">清算规则随产品提交审核，审核通过后用于该产品订单；税费不在此处作为分配项。</p>
        <div class="form-grid">
          <label>质量等级<input v-model="productForm.quality_level" /></label>
        </div>
        <div v-if="productForm.product_type === 'api'" class="api-doc-inline">
          <FileText :size="15" />
          <div>
            <strong>API 提供方接入规范</strong
            ><small>登记和审核后，提供方按此文档完成网关接入</small>
          </div>
          <a
            href="/api-gateway-integration.md"
            download="API应用提供方接入统一网关接口与规范.md"
            target="_blank"
            >下载文档</a
          >
        </div>
        <div v-if="productForm.product_type === 'api'" class="api-doc-inline">
          <FileText :size="15" />
          <div>
            <strong>API 调用方接入规范</strong
            ><small>购买前了解凭据获取、调用地址、配额和错误处理</small>
          </div>
          <a
            href="/api-consumer-integration.md"
            download="API应用调用方接入与调用规范.md"
            target="_blank"
            >下载文档</a
          >
        </div>
        <div v-if="productForm.product_type === 'saas'" class="api-doc-inline">
          <FileText :size="15" />
          <div>
            <strong>SaaS 第三方接口规范</strong
            ><small>配置 SaaS 应用前，请先阅读租户、版本和同步接口要求</small>
          </div>
          <a
            href="/saas-integration-standard.md"
            download="SaaS应用第三方接口对接规范.md"
            target="_blank"
            >下载文档</a
          >
        </div>
        </fieldset>
        <div v-if="productReviewMode" class="review-action-bar">
          <button type="button" class="primary-btn" @click="reviewProductFromDetail('approve')">通过审核</button>
          <button type="button" class="secondary-btn danger-text" @click="reviewProductFromDetail('reject')">拒绝审核</button>
        </div>
        <button v-if="!productReadOnlyMode" class="primary-btn full-btn" type="submit">
          {{ selectedProductId ? "保存产品信息" : "保存产品登记草稿" }} <ArrowUpRight :size="16" />
        </button>
      </form>
    </div>
    <div v-if="fileReportViewer" class="modal-scrim" @click="fileReportViewer = null">
      <section class="modal-card security-report-modal" @click.stop>
        <div class="drawer-head"><div><span class="eyebrow">FILE SCAN REPORT</span><h2>{{ fileReportViewer.reportType === 'clamav' ? '病毒扫描报告' : 'Presidio扫描报告' }}</h2></div><button type="button" class="icon-btn" @click="fileReportViewer = null"><X :size="19" /></button></div>
        <div class="state-grid"><div><small>文件名称</small><strong>{{ fileReportViewer.file.original_name }}</strong></div><div><small>文件大小</small><strong>{{ fileReportViewer.file.size }} bytes</strong></div><div><small>扫描工具</small><strong>{{ fileReportViewer.reportType === 'clamav' ? 'ClamAV' : 'Presidio' }}</strong></div><div><small>扫描结论</small><strong>{{ fileReportViewer.reportType === 'clamav' ? (fileReportViewer.file.clamav_status === 'clean' ? '通过' : fileReportViewer.file.clamav_status) : (fileReportViewer.file.presidio_status === 'available' ? '完成' : fileReportViewer.file.presidio_status) }}</strong></div><div><small>扫描时间</small><strong>{{ fmtDate(fileReportViewer.file.scanned_at) }}</strong></div></div>
        <div class="drawer-section"><div class="drawer-section-title">扫描详情</div><pre class="scan-report-detail">{{ fileReportViewer.reportType === 'clamav' ? fileReportViewer.file.clamav_report : JSON.stringify(fileReportViewer.file.presidio_findings || [], null, 2) }}</pre></div>
        <button class="primary-btn full-btn" @click="downloadProductFile(fileReportViewer.file, fileReportViewer.reportType)">下载{{ fileReportViewer.reportType === 'clamav' ? '病毒' : 'Presidio' }}报告</button>
      </section>
    </div>
    <div v-if="securityReport" class="modal-scrim" @click="securityReport = null">
      <section class="modal-card security-report-modal" @click.stop>
        <div class="drawer-head"><div><span class="eyebrow">SECURITY REPORT</span><h2>数据集安全审核报告</h2></div><button type="button" class="icon-btn" @click="securityReport = null"><X :size="19" /></button></div>
        <div class="state-grid"><div><small>扫描引擎</small><strong>{{ securityReport.engine }}</strong></div><div><small>扫描状态</small><strong>{{ securityReport.status }}</strong></div><div><small>发现项</small><strong>{{ securityReport.findings_count }}</strong></div><div><small>高风险</small><strong>{{ securityReport.high_risk_count }}</strong></div><div><small>Presidio</small><strong>{{ securityReport.report?.presidio_status || '未返回' }}</strong></div></div>
        <div class="drawer-section"><div class="drawer-section-title">自动识别结果</div><div v-if="securityReport.report?.findings?.length" class="security-finding-list"><div v-for="(finding, index) in securityReport.report.findings" :key="`${finding.entity}-${index}`" class="security-finding"><span>{{ finding.entity }}</span><small>{{ finding.message || finding.severity || '发现敏感信息' }}<template v-if="finding.count"> · {{ finding.count }}处</template></small></div></div><p v-else class="muted">未发现自动识别项，仍需安全审核人员结合授权和脱敏材料确认。</p></div>
        <div class="drawer-section"><div class="drawer-section-title">文件扫描明细</div><div v-for="file in securityReport.report?.files || []" :key="file.file_id" class="file-status-row"><span>{{ file.name }}</span><span><small>{{ file.clamav_status || (file.sample_scanned ? '已提取样本' : '未提取样本') }} · Presidio {{ file.presidio_status || '未执行' }}</small><a class="text-btn" :href="`/api/files/${file.file_id}/scan-report.pdf`" target="_blank" download>查看/下载 PDF 报告</a></span></div></div>
      </section>
    </div>
    <div v-if="showProfileContact" class="modal-scrim" @click="showProfileContact = false">
      <form class="modal-card" @submit.prevent="updateProfileContact" @click.stop>
        <div class="drawer-head"><div><span class="eyebrow">ACCOUNT CONTACT</span><h2>账号联系方式</h2></div><button type="button" class="icon-btn" @click="showProfileContact = false"><X :size="19" /></button></div>
        <p class="muted">手机号或邮箱变更均需验证码确认，验证码有效期 10 分钟。</p>
        <label>变更类型<select v-model="profileContactForm.channel"><option value="phone">手机号码</option><option value="email">邮箱地址</option></select></label>
        <label>{{ profileContactForm.channel === 'phone' ? '新手机号码' : '新邮箱地址' }}<input v-model="profileContactForm.target" required /></label>
        <div class="inline-form"><input v-model="profileContactForm.verification_code" placeholder="验证码" required /><button type="button" class="secondary-btn" @click="sendContactCode">发送验证码</button></div>
        <button class="primary-btn full-btn" type="submit">确认更新 <ArrowUpRight :size="16" /></button>
      </form>
    </div>
    <div
      v-if="showPersonalVerification"
      class="modal-scrim"
      @click="showPersonalVerification = false"
    >
      <form
        class="modal-card verification-modal"
        @submit.prevent="submitPersonalVerification"
        @click.stop
      >
        <div class="drawer-head">
          <div>
            <span class="eyebrow">IDENTITY VERIFICATION</span>
            <h2>{{ editingPersonalVerification ? '编辑个人实名认证' : '个人实名认证' }}</h2>
          </div>
          <button
            type="button"
            class="icon-btn"
            @click="showPersonalVerification = false"
          >
            <X :size="19" />
          </button>
        </div>
        <p class="muted">提交身份证正反面、手机号和验证码后进入人工审核。</p>
        <label
          >身份证姓名<input
            v-model="verificationForm.id_name"
            required /></label
        ><label
          >身份证号码<input v-model="verificationForm.id_number" required
        /></label>
        <div class="form-grid identity-edit-files">
          <div class="identity-edit-file">
            <span class="field-label">身份证正面</span>
            <img v-if="personalEditImages.front" :src="personalEditImages.front" alt="身份证正面" class="identity-edit-thumb" @click="identityImagePreview = { url: personalEditImages.front, title: '身份证正面' }" />
            <span v-else-if="editingPersonalVerification" class="muted">暂无已上传图片</span>
            <input type="file" accept="image/*" @change="verificationFiles.front = $event.target.files[0]" :required="!editingPersonalVerification" />
            <small v-if="editingPersonalVerification" class="muted">选择新文件可替换原图片</small>
          </div>
          <div class="identity-edit-file">
            <span class="field-label">身份证反面</span>
            <img v-if="personalEditImages.back" :src="personalEditImages.back" alt="身份证反面" class="identity-edit-thumb" @click="identityImagePreview = { url: personalEditImages.back, title: '身份证反面' }" />
            <span v-else-if="editingPersonalVerification" class="muted">暂无已上传图片</span>
            <input type="file" accept="image/*" @change="verificationFiles.back = $event.target.files[0]" :required="!editingPersonalVerification" />
            <small v-if="editingPersonalVerification" class="muted">选择新文件可替换原图片</small>
          </div>
        </div>
        <label>手机号<input v-model="verificationForm.phone" required /></label
        ><label
          >短信验证码<input
            v-model="verificationForm.phone_code"
            required
            placeholder="开发环境固定 123456" /></label
        ><label
          >个人在企业中的角色（可选）<input
            v-model="verificationForm.enterprise_role" /></label
        ><button class="primary-btn full-btn" type="submit">
          提交实名认证 <ArrowUpRight :size="16" />
        </button>
      </form>
    </div>
    <div v-if="identityReview" class="modal-scrim" @click.self="identityReview = null">
      <section class="modal-card verification-review-modal" @click.stop>
        <div class="drawer-head">
          <div><span class="eyebrow">IDENTITY REVIEW</span><h2>{{ identityReview.kind === 'personal' ? '个人实名审核' : '企业实名审核' }}</h2></div>
          <button type="button" class="icon-btn" @click="identityReview = null"><X :size="19" /></button>
        </div>
        <template v-if="identityReview.kind === 'personal'">
          <div class="state-grid">
            <div><small>用户名</small><strong>{{ identityReview.applicant.name || '-' }}</strong></div>
            <div><small>注册时间</small><strong>{{ fmtDate(identityReview.applicant.created_at) }}</strong></div>
            <div><small>手机号码</small><strong>{{ identityReview.applicant.phone || '-' }}</strong></div>
            <div><small>邮箱地址</small><strong>{{ identityReview.applicant.email || '-' }}</strong></div>
          </div>
          <div class="identity-review-images">
            <div><small>身份证正面</small><img v-if="identityReviewImages.front" :src="identityReviewImages.front" alt="身份证正面" @click="identityImagePreview = { url: identityReviewImages.front, title: '身份证正面' }" /><span v-else class="muted">图片加载失败</span></div>
            <div><small>身份证反面</small><img v-if="identityReviewImages.back" :src="identityReviewImages.back" alt="身份证反面" @click="identityImagePreview = { url: identityReviewImages.back, title: '身份证反面' }" /><span v-else class="muted">图片加载失败</span></div>
          </div>
        </template>
        <template v-else>
          <div class="state-grid">
            <div><small>企业名称</small><strong>{{ identityReview.item.name }}</strong></div>
            <div><small>统一社会信用代码</small><strong>{{ identityReview.item.credit_code }}</strong></div>
            <div><small>企业类型</small><strong>{{ identityReview.item.enterprise_type || '-' }}</strong></div>
            <div><small>法定代表人</small><strong>{{ identityReview.item.legal_representative || '-' }}</strong></div>
            <div><small>注册资本/出资额</small><strong>{{ identityReview.item.registered_capital || '-' }}</strong></div>
            <div><small>成立日期</small><strong>{{ identityReview.item.establishment_date || '-' }}</strong></div>
            <div><small>经营场所</small><strong>{{ identityReview.item.business_address || '-' }}</strong></div>
            <div><small>申请实名时间</small><strong>{{ fmtDate(identityReview.item.created_at) }}</strong></div>
          </div>
          <div class="review-long-text"><small>经营范围</small><p>{{ identityReview.item.business_scope || '-' }}</p></div>
          <div class="identity-review-images single"><div><small>营业执照</small><img v-if="identityReviewImages.license" :src="identityReviewImages.license" alt="营业执照" @click="identityImagePreview = { url: identityReviewImages.license, title: '营业执照' }" /><span v-else class="muted">图片加载失败</span></div></div>
        </template>
        <label class="review-comment">审核意见/拒绝原因<textarea v-model="identityReviewComment" placeholder="拒绝时必须填写原因"></textarea></label>
        <div class="modal-actions"><button class="secondary-btn" @click="identityReview = null">取消</button><button class="text-btn danger-text" @click="submitIdentityReview('reject')">拒绝</button><button class="primary-btn" @click="submitIdentityReview('approve')">通过</button></div>
      </section>
    </div>
    <div v-if="identityImagePreview" class="modal-scrim image-preview-scrim" @click.self="identityImagePreview = null">
      <section class="image-preview-modal" @click.stop><div class="drawer-head"><h3>{{ identityImagePreview.title }}</h3><button class="icon-btn" @click="identityImagePreview = null"><X :size="19" /></button></div><img :src="identityImagePreview.url" :alt="identityImagePreview.title" /></section>
    </div>
    <div
      v-if="enterpriseManageModal"
      class="modal-scrim"
      @click="closeEnterpriseManagement"
    >
      <section class="modal-card enterprise-manage-modal" @click.stop>
        <div class="drawer-head">
          <div><span class="eyebrow">ENTERPRISE MANAGEMENT</span><h2>{{ enterpriseManageModal.name }}</h2><p class="muted">已认证企业 · 企业成员、部门和邀请管理</p></div>
          <button type="button" class="icon-btn" @click="closeEnterpriseManagement"><X :size="19" /></button>
        </div>
        <div class="tabs modal-tabs">
          <button :class="{ active: enterpriseManageTab === 'details' }" @click="enterpriseManageTab = 'details'">企业详情</button>
          <button :class="{ active: enterpriseManageTab === 'invite' }" @click="enterpriseManageTab = 'invite'">邀请用户</button>
          <button :class="{ active: enterpriseManageTab === 'departments' }" @click="enterpriseManageTab = 'departments'">部门管理</button>
          <button :class="{ active: enterpriseManageTab === 'members' }" @click="enterpriseManageTab = 'members'">成员管理</button>
        </div>
        <div v-if="enterpriseManageTab === 'details'" class="enterprise-detail-grid">
          <div><small>企业名称</small><strong>{{ enterpriseManageModal.name }}</strong></div><div><small>统一社会信用代码</small><strong>{{ enterpriseManageModal.credit_code }}</strong></div><div><small>企业类型</small><strong>{{ enterpriseManageModal.enterprise_type || '-' }}</strong></div><div><small>法定代表人</small><strong>{{ enterpriseManageModal.legal_representative || '-' }}</strong></div><div><small>注册资本/出资额</small><strong>{{ enterpriseManageModal.registered_capital || '-' }}</strong></div><div><small>成立日期</small><strong>{{ enterpriseManageModal.establishment_date || '-' }}</strong></div><div><small>经营场所</small><strong>{{ enterpriseManageModal.business_address || '-' }}</strong></div><div><small>认证状态</small><strong>已认证</strong></div><div><small>认证时间</small><strong>{{ fmtDate(enterpriseManageModal.verified_at) }}</strong></div><div class="enterprise-detail-wide"><small>经营范围</small><strong>{{ enterpriseManageModal.business_scope || '-' }}</strong></div>
        </div>
        <div v-else-if="enterpriseManageTab === 'invite'" class="enterprise-manage-section">
          <div class="panel-heading"><div><h3>邀请用户加入企业</h3><span class="muted">输入平台注册用户的邮箱或手机号</span></div></div>
          <div class="inline-form"><input v-model="inviteTarget" placeholder="邮箱或手机号" /><select v-model="inviteChannel" title="邀请渠道"><option value="sms">手机短信</option><option value="email">邮件</option></select><select v-model="inviteDepartmentId" title="加入后的归属部门"><option value="">不指定部门</option><option v-for="department in enterpriseDepartments" :key="department.id" :value="department.id">{{ department.name }}</option></select><button class="primary-btn" @click="inviteEnterpriseMember"><Users :size="15" />发送邀请</button></div>
          <div class="invite-list"><div v-for="item in enterpriseInvitations" :key="item.id"><span>{{ item.target }} <small>{{ item.channel === 'email' ? '邮件' : '短信' }}</small></span><span><small>{{ item.status === 'accepted' ? '已接受' : item.status === 'pending' ? (item.created_user ? '待激活' : '待接受') : item.status === 'expired' ? '已过期' : item.status }}</small><button v-if="item.status === 'expired' || (item.status === 'pending' && item.created_user)" class="text-btn" @click="resendEnterpriseInvitation(item)">重新邀请</button></span></div><div v-if="!enterpriseInvitations.length" class="muted">暂无邀请记录</div></div>
        </div>
        <div v-else-if="enterpriseManageTab === 'departments'" class="enterprise-manage-section">
          <div class="panel-heading"><div><h3>企业部门管理</h3><span class="muted">部门存在成员时不可直接删除</span></div><span class="muted">{{ enterpriseDepartments.length }} 个部门</span></div>
          <div class="inline-form"><input v-model="newDepartment.name" placeholder="部门名称" /><input v-model="newDepartment.code" placeholder="部门编码（可选）" /><button class="primary-btn" @click="createDepartment"><CheckCircle2 :size="15" />创建</button></div>
          <div class="department-list"><div v-for="item in enterpriseDepartments" :key="item.id"><span>{{ item.parent_id ? '└ ' : '' }}{{ item.name }} <small>{{ item.code || '无编码' }}</small></span><button class="text-btn danger-text" @click="deleteDepartment(item)">删除</button></div><div v-if="!enterpriseDepartments.length" class="muted">暂无部门，请先创建</div></div>
        </div>
        <div v-else class="enterprise-manage-section">
          <div class="panel-heading"><div><h3>企业成员与部门归属</h3><span class="muted">企业超级管理员和企业管理员可维护成员</span></div></div>
          <div class="member-role-hint">企业注册人自动成为企业超级管理员，该角色不可降级；请先通过“邀请用户”加入普通成员，再为其指定企业管理员角色。</div>
          <div class="table-wrap"><table class="data-table compact"><thead><tr><th>成员</th><th>角色</th><th>部门</th><th>实名认证</th></tr></thead><tbody><tr v-for="item in enterpriseMembers" :key="item.membership_id"><td><strong>{{ item.name }}</strong><small>{{ item.email || item.phone || '-' }}</small></td><td><select class="status-select" :title="item.role === 'super_admin' ? '企业注册人自动成为超级管理员，不可调整' : '调整企业成员角色'" :value="item.role" @change="updateMemberRole(item, $event.target.value)" :disabled="item.role === 'super_admin'"><option value="member">普通成员</option><option value="enterprise_admin">企业管理员</option><option value="super_admin">企业超级管理员</option></select></td><td><select class="status-select" :value="item.department_id" @change="updateMemberDepartment(item, $event.target.value)"><option value="">未分配</option><option v-for="department in enterpriseDepartments" :key="department.id" :value="department.id">{{ department.name }}</option></select></td><td><span class="status-pill status-done">{{ item.verified_status === 'verified' ? '已实名' : item.verified_status }}</span></td></tr><tr v-if="!enterpriseMembers.length"><td colspan="4"><div class="empty-state">暂无企业成员</div></td></tr></tbody></table></div>
        </div>
      </section>
    </div>
    <div
      v-if="showEnterpriseVerification"
      class="modal-scrim"
      @click="showEnterpriseVerification = false"
    >
      <form
        class="modal-card verification-modal"
        @submit.prevent="submitEnterpriseVerification"
        @click.stop
      >
        <div class="drawer-head">
          <div>
            <span class="eyebrow">ENTERPRISE VERIFICATION</span>
            <h2>注册企业</h2>
          </div>
          <button
            type="button"
            class="icon-btn"
            @click="showEnterpriseVerification = false"
          >
            <X :size="19" />
          </button>
        </div>
        <p class="muted">提交营业执照后进入平台人工审核。</p>
        <label
          >营业执照复印件<input
            type="file"
            accept="image/*,.pdf"
            @change="verificationFiles.license = $event.target.files[0]"
            required /></label
        ><label
          >企业法定全称<input
            v-model="verificationForm.enterprise_name"
            required /></label
        ><label
          >统一社会信用代码<input
            v-model="verificationForm.credit_code"
            required /></label
        ><label
          >企业类型/主体类型<input
            v-model="verificationForm.enterprise_type"
            required /></label
        ><label
          >法定代表人/负责人<input
            v-model="verificationForm.legal_representative"
            required /></label
        ><label
          >注册资本/出资额<input
            v-model="verificationForm.registered_capital"
            required /></label
        ><label
          >成立日期<input
            v-model="verificationForm.establishment_date"
            type="date"
            required /></label
        ><label
          >经营场所<input
            v-model="verificationForm.business_address"
            required /></label
        ><label
          >经营范围<textarea
            v-model="verificationForm.business_scope"
            required
          ></textarea></label
        ><button class="primary-btn full-btn" type="submit">
          提交企业认证 <ArrowUpRight :size="16" />
        </button>
      </form>
    </div>
    <div v-if="settlementAdjusting" class="modal-scrim" @click.self="closeSettlementAdjustment">
      <form class="modal-card settlement-adjust-modal" @submit.prevent="adjustSettlement" @click.stop>
        <div class="drawer-head">
          <div><span class="eyebrow">SETTLEMENT ADJUSTMENT</span><h2>调整清算单</h2><p class="muted">调整前须确认清算单尚未锁定，提交后将记录完整审计信息。</p></div>
          <button type="button" class="icon-btn" @click="closeSettlementAdjustment"><X :size="19" /></button>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">关联信息</div>
          <div class="state-grid settlement-context-grid">
            <div><small>清算批次 ID</small><strong>{{ settlementAdjusting.batch_id || '-' }}</strong></div>
            <div><small>清算批次号</small><strong>{{ settlementAdjusting.batch_no || '-' }}</strong></div>
            <div><small>清算单 ID</small><strong>{{ settlementAdjusting.id }}</strong></div>
            <div><small>订单 ID</small><strong>{{ settlementAdjusting.order_id }}</strong></div>
            <div><small>购买方</small><strong>{{ settlementAdjusting.buyer_name || '-' }}</strong></div>
            <div><small>订单号</small><strong>{{ settlementAdjusting.order_no || '-' }}</strong></div>
          </div>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">订单金额与利润</div>
          <div class="form-grid settlement-adjust-form-grid">
            <label>订单金额<input v-model.number="settlementAdjustForm.gross_amount" type="number" min="0" step="0.01" /></label>
            <label>订单成本<input v-model.number="settlementAdjustForm.cost_amount" type="number" min="0" step="0.01" /></label>
            <label>订单利润<input v-model.number="settlementAdjustForm.profit_amount" type="number" min="0" step="0.01" /></label>
          </div>
          <div class="settlement-adjust-hint">校验关系：订单利润 = 订单金额 - 订单成本，当前差额 {{ fmtMoney(Number(settlementAdjustForm.gross_amount || 0) - Number(settlementAdjustForm.cost_amount || 0) - Number(settlementAdjustForm.profit_amount || 0)) }}</div>
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">五方清算比例与金额</div>
          <div class="settlement-adjust-grid">
            <div class="settlement-adjust-head"><span>参与方</span><span>比例（可调整）</span><span>金额（自动计算）</span></div>
            <label><span>平台运营方</span><input v-model.number="settlementAdjustForm.platform_rate" type="number" min="0" max="100" step="0.01" /><strong>{{ fmtMoney(adjustmentShare(settlementAdjustForm.platform_rate)) }}</strong></label>
            <label><span>数据/服务提供方</span><input v-model.number="settlementAdjustForm.provider_rate" type="number" min="0" max="100" step="0.01" /><strong>{{ fmtMoney(adjustmentShare(settlementAdjustForm.provider_rate)) }}</strong></label>
            <label><span>数据服务方</span><input v-model.number="settlementAdjustForm.service_rate" type="number" min="0" max="100" step="0.01" /><strong>{{ fmtMoney(adjustmentShare(settlementAdjustForm.service_rate)) }}</strong></label>
            <label><span>专家</span><input v-model.number="settlementAdjustForm.expert_rate" type="number" min="0" max="100" step="0.01" /><strong>{{ fmtMoney(adjustmentShare(settlementAdjustForm.expert_rate)) }}</strong></label>
            <label><span>渠道</span><input v-model.number="settlementAdjustForm.channel_rate" type="number" min="0" max="100" step="0.01" /><strong>{{ fmtMoney(adjustmentShare(settlementAdjustForm.channel_rate)) }}</strong></label>
          </div>
          <div class="settlement-adjust-total">比例合计：{{ (Number(settlementAdjustForm.platform_rate || 0) + Number(settlementAdjustForm.provider_rate || 0) + Number(settlementAdjustForm.service_rate || 0) + Number(settlementAdjustForm.expert_rate || 0) + Number(settlementAdjustForm.channel_rate || 0)).toFixed(2) }}%</div>
        </div>
        <div class="drawer-section"><label>调整原因<textarea v-model="settlementAdjustForm.reason" rows="3" required placeholder="请填写调整依据和业务原因"></textarea></label></div>
        <div class="modal-actions"><button type="button" class="secondary-btn" @click="closeSettlementAdjustment">取消</button><button type="submit" class="primary-btn">保存调整 <CheckCircle2 :size="15" /></button></div>
      </form>
    </div>
    <div v-if="settlementDetail" class="modal-scrim" @click.self="closeSettlementDetail">
      <aside class="modal-card settlement-detail-modal" @click.stop>
        <div class="drawer-head"><div><span class="eyebrow">SETTLEMENT DETAIL</span><h2>{{ settlementDetail.settlement_no }}</h2><p class="muted">清算单完整关联信息和状态生命周期</p></div><button class="icon-btn" @click="closeSettlementDetail"><X :size="19" /></button></div>
        <div class="drawer-section"><div class="drawer-section-title">清算批次与订单</div><div class="state-grid settlement-context-grid"><div><small>清算批次</small><strong>{{ settlementDetail.batch?.batch_no || '-' }}</strong></div><div><small>批次 ID</small><strong>{{ settlementDetail.batch?.id || '-' }}</strong></div><div><small>批次状态</small><strong>{{ settlementDetail.batch?.status || '-' }}</strong></div><div><small>清算单 ID</small><strong>{{ settlementDetail.id }}</strong></div><div><small>订单号</small><strong>{{ settlementDetail.order?.order_no || '-' }}</strong></div><div><small>订单 ID</small><strong>{{ settlementDetail.order?.id || '-' }}</strong></div><div><small>购买方</small><strong>{{ settlementDetail.order?.buyer_name || '-' }}</strong></div><div><small>当前状态</small><strong>{{ settlementStatusLabel(settlementDetail.status) }}</strong></div></div></div>
        <div class="drawer-section"><div class="drawer-section-title">金额信息</div><div class="state-grid"><div><small>订单金额</small><strong>{{ fmtMoney(settlementDetail.amounts.gross_amount) }}</strong></div><div><small>订单成本</small><strong>{{ fmtMoney(settlementDetail.amounts.cost_amount) }}</strong></div><div><small>订单利润</small><strong>{{ fmtMoney(settlementDetail.amounts.profit_amount) }}</strong></div><div><small>退款金额</small><strong>{{ fmtMoney(settlementDetail.amounts.refund_amount) }}</strong></div><div><small>净收入</small><strong>{{ fmtMoney(settlementDetail.amounts.net_amount) }}</strong></div></div></div>
        <div class="drawer-section"><div class="drawer-section-title">五方清算比例和金额</div><div class="settlement-detail-participants"><div v-for="item in settlementDetail.participants" :key="item.participant_type"><span>{{ participantLabel(item.participant_type) }}</span><strong>{{ item.rate }}%</strong><b>{{ fmtMoney(item.amount) }}</b></div></div></div>
        <div class="drawer-section"><div class="drawer-section-title">清算单生命周期</div><div class="settlement-lifecycle"><div v-for="(event, index) in settlementDetail.lifecycle" :key="`${event.action}-${event.created_at}-${index}`"><span class="lifecycle-dot"></span><div class="settlement-lifecycle-event"><strong>{{ settlementLifecycleAction(event.action) }}</strong><small>{{ fmtDate(event.created_at) }} · {{ event.actor }}</small><p v-if="event.detail" class="lifecycle-detail">{{ event.detail }}</p><div v-if="settlementLifecycleFields(event).length" class="lifecycle-change-table"><div class="lifecycle-change-head"><span>字段</span><span>变更前</span><span>变更后</span></div><div v-for="field in settlementLifecycleFields(event)" :key="field.key" class="lifecycle-change-row"><span>{{ field.label }}</span><span>{{ field.before }}</span><strong>{{ field.after }}</strong></div></div></div></div><div v-if="!settlementDetail.lifecycle.length" class="muted">暂无生命周期审计记录</div></div></div>
        <div v-if="settlementDetail.proposals?.length" class="drawer-section"><div class="drawer-section-title">调整提案</div><div class="settlement-proposal-list"><div v-for="proposal in settlementDetail.proposals" :key="proposal.id" class="settlement-proposal-item"><div><strong>{{ settlementProposalStatusLabel(proposal.status) }}</strong><span>{{ proposal.proposed_by }} · {{ fmtDate(proposal.created_at) }}</span></div><p>{{ proposal.reason }}</p><div v-if="proposal.status === 'pending'" class="table-actions"><button class="text-btn" @click="decideSettlementProposal(proposal, 'approve')">确认提案</button><button class="text-btn danger-text" @click="decideSettlementProposal(proposal, 'reject')">拒绝提案</button></div></div></div></div>
      </aside>
    </div>
    <div v-if="toast" class="toast"><CheckCircle2 :size="17" />{{ toast }}</div>
  </div>
</template>
