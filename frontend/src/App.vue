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
const productDirectories = ref([]);
const orders = ref([]);
const selectedOrder = ref(null);
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
const settlementRules = ref([]);
const settlementBatches = ref([]);
const settlementTab = ref("settlements");
const settlementRuleForm = ref({ name: "", version: "v1", platform_rate: 8, provider_rate: 67, service_rate: 20, expert_rate: 5, channel_rate: 0, change_reason: "" });
const settlementReconciliations = ref([]);
const settlementCorrections = ref([]);
const settlementReport = ref({ summary: {}, items: [] });
const settlementMeasurements = ref([]);
const auditItems = ref([]);
const auditCategories = ref([]);
const auditSelectedCategory = ref("");
const auditSelected = ref(null);
const auditFilters = ref({ q: "", actor: "", order_id: "", batch_no: "", rule_version: "", risk_level: "", start: "", end: "", page: 1, page_size: 50, total: 0, pages: 0 });
const userItems = ref([]);
const enterpriseItems = ref([]);
const personalVerificationItems = ref([]);
const userEnterpriseTab = ref("users");
const showPersonalVerification = ref(false);
const showEnterpriseVerification = ref(false);
const platformRoleItems = ref({ roles: {}, items: [] });
const notificationSettings = ref({
  sms_provider: "",
  sms_endpoint: "",
  email_host: "imap.263.net",
  email_ssl: false,
  email_port: 143,
  email_username: "",
  email_password: "",
});
const roleForm = ref({
  identifier: "",
  name: "",
  role: "platform_operator",
  channel: "email",
});
const verification = ref({ personal: null, enterprise: null });
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
});
const verificationFiles = ref({ front: null, back: null, license: null });
const developmentFilter = ref("all");
const search = ref("");
const showProductForm = ref(false);
const productDetailMode = ref(false);
const selectedProductId = ref("");
const emptyProductVersion = () => ({
  version_code: "v1.0",
  description: "",
  price: 0,
  cost: 0,
  cost_type: "per_order",
  rate_limit_per_minute: 60,
  daily_quota: 10000,
  monthly_quota: 0,
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
  pricing_strategy: "",
  versions: [emptyProductVersion()],
  quality_level: "标准",
  security_level: "一般",
  authorization_conditions: "",
  data_source_statement: "",
  compliance_statement: "",
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
  { key: "users", label: "用户与企业", icon: Users },
  { key: "development", label: "开发进度", icon: Activity },
];

const visibleNav = computed(() => nav);
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
  pending_acceptance: "待验收",
  accepted: "验收通过",
  exception: "交付异常",
  none: "无售后",
  processing: "售后处理中",
  resolved: "已解决",
  after_closed: "售后关闭",
};
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
function fmtDate(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "-";
}
function label(value) {
  return statusLabels[value] || value;
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
  try {
    const { data } = await api.post("/auth/register", registerForm.value);
    setToken(data.token);
    await loadSession();
    activeView.value = "users";
    showPersonalVerification.value = true;
    await loadViewData("users");
  } catch (error) {
    loginError.value =
      error.response?.data?.detail || "注册失败，请检查注册信息";
  }
}
async function loadSession() {
  if (!token.value) return;
  api.defaults.headers.common.Authorization = `Bearer ${token.value}`;
  try {
    const { data } = await api.get("/auth/me");
    user.value = { ...data.user, enterprise_role: data.role };
    enterprise.value = data.enterprise;
    await refreshData();
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
    const [productData, directoryData] = await Promise.all([
      api.get("/products"),
      api.get("/product-directories"),
    ]);
    products.value = productData.data.items;
    productDirectories.value = directoryData.data.items;
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
    const [settlements, rules, batches, reconciliations, corrections, report, measurements] = await Promise.all([
      api.get("/settlements"),
      api.get("/settlement-rules").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-batches").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-reconciliations").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-corrections").catch(() => ({ data: { items: [] } })),
      api.get("/settlement-reports").catch(() => ({ data: { summary: {}, items: [] } })),
      api.get("/settlement-measurements").catch(() => ({ data: { items: [] } })),
    ]);
    settlementItems.value = settlements.data.items;
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
  if (view === "users") {
    const [usersData, enterpriseData, personalData] = await Promise.all([
      api.get("/users"),
      api.get("/admin/enterprises").catch(() => ({ data: { items: [] } })),
      api
        .get("/admin/verifications/personal")
        .catch(() => ({ data: { items: [] } })),
    ]);
    userItems.value = usersData.data.items;
    enterpriseItems.value = enterpriseData.data.items;
    personalVerificationItems.value = personalData.data.items;
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
  const requests = [];
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
    apiCredentialReveal.value = data;
    await openOrder(order);
    apiCredentialReveal.value = data;
    notify("API 凭据已生成，请立即保存 API Key");
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
    await api.post("/products", {
      ...productForm.value,
      catalog_name:
        productForm.value.catalog_name ||
        productDirectories.value[0]?.value ||
        "未分类",
    });
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
  selectedProductId.value = "";
  productForm.value = {
    ...emptyProductForm(),
    catalog_name: productDirectories.value[0]?.value || "",
  };
  showProductForm.value = true;
}
function openProductDetail(product) {
  selectedProductId.value = product.id;
  productDetailMode.value = product.status !== "draft";
  productForm.value = JSON.parse(JSON.stringify({
    ...emptyProductForm(),
    ...product,
    versions: product.versions?.length ? product.versions : [emptyProductVersion()],
  }));
  showProductForm.value = true;
}
async function saveProductEdit() {
  if (!selectedProductId.value || productDetailMode.value) return;
  try {
    await api.put(`/products/${selectedProductId.value}`, {
      ...productForm.value,
      catalog_name: productForm.value.catalog_name || "未分类",
    });
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
async function productAction(product, action) {
  try {
    if (action === "unpublish") {
      const reason = window.prompt("请输入下架原因", "产品提供方主动下架");
      if (!reason) return;
      await api.post(`/products/${product.id}/unpublish`, { reason });
    } else if (action === "security_check") {
      const { data } = await api.post(`/products/${product.id}/security-check`);
      notify(data.unpublished ? "安全策略检查发现问题，产品已自动下架" : "安全策略检查通过");
      await loadViewData("products");
      return;
    } else if (action === "security_report") {
      const { data } = await api.get(`/products/${product.id}/security-report`);
      const report = data.security_report?.report || {};
      const findings = (report.findings || []).map((item) => `${item.severity || "提示"} · ${item.message || item.entity}`).join("\n");
      window.alert(`安全审核报告\n扫描引擎：${data.security_report?.engine}\n发现项：${data.security_report?.findings_count}\n高风险：${data.security_report?.high_risk_count}\n\n${findings || "未发现自动识别项"}`);
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
async function adjustSettlement(item) {
  const amount = window.prompt("请输入调整金额，可填写负数", "0");
  if (amount === null || amount === "" || Number.isNaN(Number(amount))) return;
  const reason = window.prompt("请输入调整原因", "");
  if (!reason) return notify("清算调整必须填写原因");
  try {
    await api.post(`/settlements/${item.id}/adjust`, {
      amount: Number(amount),
      reason,
    });
    notify("清算调整已保存");
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
  try {
    await api.post("/settlement-batches", { cycle: "manual", rule_id: settlementRules.value.find((x) => x.status === "active")?.id || "" });
    notify("清算批次已生成");
    await loadViewData("settlements");
  } catch (error) {
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
async function exportSettlementReport() {
  try {
    const response = await api.get("/settlement-reports/export", { responseType: "blob" });
    const url = URL.createObjectURL(response.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = "settlement-report.csv";
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
    notificationSettings.value.email_password = "";
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
async function uploadVerificationFile(file) {
  if (!file) return "";
  const form = new FormData();
  form.append("upload", file);
  const { data } = await api.post("/files/upload", form);
  return data.id;
}
async function submitPersonalVerification() {
  try {
    const front = await uploadVerificationFile(verificationFiles.value.front);
    const back = await uploadVerificationFile(verificationFiles.value.back);
    await api.post("/verification/personal", {
      ...verificationForm.value,
      id_front_file_id: front,
      id_back_file_id: back,
    });
    notify("个人实名认证申请已提交");
    showPersonalVerification.value = false;
    await loadViewData("users");
  } catch (error) {
    notify(error.response?.data?.detail || "个人实名认证提交失败");
  }
}
async function submitEnterpriseVerification() {
  try {
    const license = await uploadVerificationFile(
      verificationFiles.value.license,
    );
    await api.post("/verification/enterprise", {
      license_file_id: license,
      enterprise_name: verificationForm.value.enterprise_name,
      credit_code: verificationForm.value.credit_code,
      enterprise_type: verificationForm.value.enterprise_type,
      legal_representative: verificationForm.value.legal_representative,
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
    actions.push(["accept_delivery", "验收通过"]);
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
  if (token.value) loadSession();
  progressTimer = window.setInterval(() => {
    if (token.value) loadViewData("development");
  }, 10000);
});
onUnmounted(() => window.clearInterval(progressTimer));
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
          >邮箱或手机号码<input
            v-model="loginForm.email"
            autocomplete="username" /></label
        ><label
          >密码<input
            v-model="loginForm.password"
            type="password"
            autocomplete="current-password" /></label
      ></template>
      <p v-if="loginError" class="error-text">{{ loginError }}</p>
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
          <div class="user-chip">
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
                          `status-${product.status === 'published' ? 'done' : product.status === 'pending_review' || product.status === 'security_review' ? 'review' : product.status === 'rejected' || product.status === 'security_unpublished' ? 'blocked' : 'todo'}`,
                        ]"
                        >{{
                          product.status === "published"
                            ? "已发布"
                            : product.status === "pending_review"
                              ? "待审核"
                              : product.status === "security_review"
                                ? "安全审核中"
                              : product.status === "rejected"
                                ? "已驳回"
                                : product.status === "security_unpublished"
                                  ? "安全下架"
                                  : "草稿"
                        }}</span
                      >
                    </td>
                    <td>
                      <div class="table-actions">
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
                          v-if="product.status === 'security_review' && ['super_admin', 'platform_operator', 'security_compliance'].includes(user?.platform_role)"
                          class="text-btn"
                          @click="productAction(product, 'security_report')"
                        >
                          查看安全报告</button
                        ><button
                          v-if="product.status === 'security_review' && ['super_admin', 'platform_operator', 'security_compliance'].includes(user?.platform_role)"
                          class="text-btn"
                          @click="productAction(product, 'security_approve')"
                        >
                          安全通过</button
                        ><button
                          v-if="product.status === 'security_review' && ['super_admin', 'platform_operator', 'security_compliance'].includes(user?.platform_role)"
                          class="text-btn danger-text"
                          @click="productAction(product, 'security_reject')"
                        >
                          安全驳回</button
                        ><button
                          v-if="product.status === 'pending_review'"
                          class="text-btn"
                          @click="productAction(product, 'review')"
                        >
                          通过</button
                        ><button
                          v-if="product.status === 'pending_review'"
                          class="text-btn danger-text"
                          @click="productAction(product, 'reject')"
                        >
                          驳回</button
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
                      <th>状态</th>
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
                      <td>
                        <span class="status-pill status-in_progress">{{
                          label(item.status)
                        }}</span>
                      </td>
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
              <p>按订单版本成本计算利润，再依据利润进行分账；税费单独核算。</p>
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
            <button :class="{ active: settlementTab === 'corrections' }" @click="settlementTab = 'corrections'">冲正调整 {{ settlementCorrections.length }}</button>
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
            <div class="panel"><div class="panel-heading"><div><span class="section-kicker">RULE VERSIONS</span><h3>规则版本</h3></div></div><div class="settlement-rule-list"><div v-for="rule in settlementRules" :key="rule.id" class="settlement-rule-item"><div><strong>{{ rule.name }} · {{ rule.version }}</strong><small>平台 {{ rule.platform_rate }}% · 提供方 {{ rule.provider_rate }}% · 数据服务方 {{ rule.service_rate }}% · 专家 {{ rule.expert_rate }}%</small></div><div class="table-actions"><span class="status-pill" :class="rule.status === 'active' ? 'status-done' : 'status-review'">{{ rule.status }}</span><button v-if="rule.status === 'draft'" class="text-btn" @click="decideSettlementRule(rule, 'approve')">审批</button><button v-if="rule.status === 'approved'" class="text-btn" @click="decideSettlementRule(rule, 'activate')">启用</button><button class="text-btn" @click="simulateSettlementRule(rule)">试算</button></div></div></div><div v-if="!settlementRules.length" class="empty-state">暂无清算规则</div></div>
          </div>
          <div v-else-if="settlementTab === 'batches'" class="panel"><div class="panel-heading"><div><span class="section-kicker">SETTLEMENT BATCHES</span><h3>清算批次</h3></div><button class="primary-btn" @click="generateSettlementBatch">生成批次</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>批次号</th><th>周期</th><th>总额</th><th>异常</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="batch in settlementBatches" :key="batch.id"><td><span class="task-code">{{ batch.batch_no }}</span></td><td>{{ batch.cycle }}</td><td>{{ fmtMoney(batch.total_amount) }}</td><td>{{ batch.exception_count }}</td><td><span class="status-pill" :class="batch.status === 'paid' ? 'status-done' : batch.status === 'exception' || batch.status === 'disputed' ? 'status-blocked' : 'status-review'">{{ batch.status }}</span></td><td>{{ fmtDate(batch.created_at) }}</td><td><div class="table-actions"><button v-if="batch.status === 'generated'" class="text-btn" @click="batchAction(batch, 'confirm')">确认</button><button v-if="batch.status === 'generated' || batch.status === 'confirmed'" class="text-btn danger-text" @click="batchAction(batch, 'dispute')">提出异议</button><button v-if="batch.status === 'confirmed'" class="text-btn" @click="batchAction(batch, 'pay')">模拟付款</button></div></td></tr></tbody></table></div><div v-if="!settlementBatches.length" class="empty-state">暂无清算批次</div></div>
          <div v-else-if="settlementTab === 'reconciliation'" class="panel"><div class="panel-heading"><div><span class="section-kicker">FOUR LEDGER RECONCILIATION</span><h3>对账差异</h3></div><span class="muted">差异关闭前不得付款</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>批次</th><th>账簿</th><th>应有金额</th><th>实际金额</th><th>差额</th><th>状态</th><th>处理</th></tr></thead><tbody><tr v-for="item in settlementReconciliations" :key="item.id"><td>{{ item.batch_id.slice(0, 12) }}</td><td>{{ item.ledger_type }}</td><td>{{ fmtMoney(item.expected_amount) }}</td><td>{{ fmtMoney(item.actual_amount) }}</td><td>{{ fmtMoney(item.difference_amount) }}</td><td><span class="status-pill" :class="item.status === 'closed' || item.status === 'matched' ? 'status-done' : 'status-blocked'">{{ item.status }}</span></td><td><button v-if="item.status !== 'closed'" class="text-btn" @click="closeReconciliation(item)">关闭差异</button></td></tr></tbody></table></div><div v-if="!settlementReconciliations.length" class="empty-state">暂无对账记录</div></div>
          <div v-else-if="settlementTab === 'corrections'" class="panel"><div class="panel-heading"><div><span class="section-kicker">REVERSAL AND RECOVERY</span><h3>退款、冲正与清算调整</h3></div><span class="muted">原始清算结果保持不变</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>类型</th><th>清算单</th><th>金额</th><th>追回方式</th><th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in settlementCorrections" :key="item.id"><td>{{ item.correction_type }}</td><td>{{ item.settlement_id.slice(0, 12) }}</td><td>{{ fmtMoney(item.amount) }}</td><td>{{ item.recovery_mode }}</td><td>{{ item.reason }}</td><td><span class="status-pill" :class="item.status === 'approved' ? 'status-done' : 'status-review'">{{ item.status }}</span></td><td><button v-if="item.status === 'pending'" class="text-btn" @click="approveCorrection(item)">审批</button></td></tr><tr v-if="!settlementCorrections.length && settlementItems.length"><td colspan="7"><div class="empty-state"><button class="primary-btn" @click="createCorrection(settlementItems[0])">从最近清算单发起冲正</button></div></td></tr></tbody></table></div><div v-if="!settlementCorrections.length && !settlementItems.length" class="empty-state">暂无清算调整记录</div></div>
          <div v-else-if="settlementTab === 'reports'" class="panel"><div class="panel-heading"><div><span class="section-kicker">SETTLEMENT REPORTS</span><h3>清算报表</h3></div><button class="secondary-btn" @click="exportSettlementReport"><FileText :size="15" />导出 CSV</button></div><div class="metric-grid report-metrics"><div class="metric-card"><span>清算单数量</span><strong>{{ settlementReport.summary.count || 0 }}</strong></div><div class="metric-card"><span>净收入</span><strong>{{ fmtMoney(settlementReport.summary.net_amount) }}</strong></div><div class="metric-card"><span>确认成本</span><strong>{{ fmtMoney(settlementReport.summary.cost_amount) }}</strong></div><div class="metric-card"><span>可分配利润</span><strong>{{ fmtMoney(settlementReport.summary.profit_amount) }}</strong></div></div><div class="table-wrap"><table class="data-table"><thead><tr><th>清算单</th><th>订单</th><th>状态</th><th>净收入</th><th>成本</th><th>利润</th><th>提供方</th><th>服务方</th><th>创建时间</th></tr></thead><tbody><tr v-for="item in settlementReport.items" :key="item.id"><td>{{ item.settlement_no }}</td><td>{{ item.order_id.slice(0, 12) }}</td><td>{{ item.status }}</td><td>{{ fmtMoney(item.net_amount) }}</td><td>{{ fmtMoney(item.cost_amount) }}</td><td>{{ fmtMoney(item.profit_amount) }}</td><td>{{ fmtMoney(item.provider_share) }}</td><td>{{ fmtMoney(item.service_share) }}</td><td>{{ fmtDate(item.created_at) }}</td></tr></tbody></table></div></div>
          <div v-else-if="settlementTab === 'measurements'" class="panel"><div class="panel-heading"><div><span class="section-kicker">MEASUREMENT AND BILLING</span><h3>计量计费数据</h3></div><button class="primary-btn" @click="createMeasurement">登记计量</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>订单</th><th>计量类型</th><th>数量</th><th>单位</th><th>来源</th><th>校验状态</th><th>时间</th></tr></thead><tbody><tr v-for="item in settlementMeasurements" :key="item.id"><td>{{ item.order_id.slice(0, 12) }}</td><td>{{ item.measurement_type }}</td><td>{{ item.quantity }}</td><td>{{ item.unit }}</td><td>{{ item.source }}</td><td><span class="status-pill status-done">{{ item.validation_status }}</span></td><td>{{ fmtDate(item.created_at) }}</td></tr></tbody></table></div><div v-if="!settlementMeasurements.length" class="empty-state">暂无计量数据</div></div>
          <div v-else class="panel">
            <div class="panel-heading">
              <h3>
                清算单 <small>{{ settlementItems.length }} 张</small>
              </h3>
              <button
                class="text-btn"
                @click="
                  filteredOrders.length && generateSettlement(filteredOrders[0])
                "
              >
                为最近订单生成 <ArrowUpRight :size="15" />
              </button>
            </div>
            <div class="table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
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
                    <th>税费</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in settlementItems" :key="item.id">
                    <td>
                      <span class="task-code">{{ item.settlement_no }}</span
                      ><small>{{ fmtDate(item.created_at) }}</small>
                    </td>
                    <td>{{ item.order_id.slice(0, 12) }}</td>
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
                    <td>{{ fmtMoney(item.tax_amount) }}</td>
                    <td>
                      <span class="status-pill status-review">{{
                        item.status
                      }}</span>
                    </td>
                    <td>
                      <div class="table-actions">
                        <button
                          v-if="item.status !== 'locked'"
                          class="text-btn"
                          @click="adjustSettlement(item)"
                        >
                          调整</button
                        ><button
                          v-if="item.status !== 'locked'"
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
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ) === '待实名' ||
                            identityStatusLabel(
                              personalForUser(item.id)?.status ||
                                item.verified_status,
                            ) === '已拒绝'
                          "
                          class="status-pill status-todo status-action"
                          @click="showPersonalVerification = true"
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
                          }}</span
                        ><button
                          v-if="
                            personalForUser(item.id)?.status ===
                            'pending_review'
                          "
                          class="text-btn"
                          @click="
                            reviewPersonal(personalForUser(item.id), 'approve')
                          "
                        >
                          通过</button
                        ><button
                          v-if="
                            personalForUser(item.id)?.status ===
                            'pending_review'
                          "
                          class="text-btn danger-text"
                          @click="
                            reviewPersonal(personalForUser(item.id), 'reject')
                          "
                        >
                          拒绝
                        </button>
                      </div>
                    </td>
                    <td>{{ item.platform_role || "-" }}</td>
                    <td>{{ item.is_active ? "正常" : "已禁用" }}</td>
                    <td>{{ fmtDate(item.created_at) }}</td>
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
                          v-if="item.verification_status === 'pending_review'"
                          class="text-btn"
                          @click="reviewEnterprise(item, 'approve')"
                        >
                          通过</button
                        ><button
                          v-if="item.verification_status === 'pending_review'"
                          class="text-btn danger-text"
                          @click="reviewEnterprise(item, 'reject')"
                        >
                          拒绝</button
                        ><button
                          v-if="item.verification_status === 'rejected'"
                          class="text-btn"
                          @click="showEnterpriseVerification = true"
                        >
                          重新提交
                        </button>
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
              <p>配置短信、邮件平台，并由平台超级管理员分配运营角色。</p>
            </div>
            <button class="secondary-btn" @click="loadViewData('settings')">
              <RefreshCw :size="16" />刷新
            </button>
          </div>
          <div class="two-column-panels">
            <div class="panel">
              <div class="panel-heading">
                <h3>通知平台配置</h3>
                <span class="muted">密码仅保存不回显</span>
              </div>
              <div class="form-grid">
                <label
                  >邮件服务器<input
                    v-model="notificationSettings.email_host" /></label
                ><label
                  >邮件端口<input
                    v-model="notificationSettings.email_port"
                    type="number"
                /></label>
              </div>
              <div class="form-grid">
                <label
                  >发件用户名<input
                    v-model="notificationSettings.email_username" /></label
                ><label
                  >邮件密码<input
                    v-model="notificationSettings.email_password"
                    type="password"
                    placeholder="留空表示不修改"
                /></label>
              </div>
              <label class="checkbox-line"
                ><input
                  v-model="notificationSettings.email_ssl"
                  type="checkbox"
                />启用 SSL</label
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
                <h3>平台角色分配</h3>
                <span class="muted">临时密码按渠道有效期不同</span>
              </div>
              <label
                >手机或邮箱<input
                  v-model="roleForm.identifier"
                  placeholder="已注册用户的手机或邮箱"
              /></label>
              <div class="form-grid">
                <label>用户名称<input v-model="roleForm.name" /></label
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
              <label
                >通知渠道<select v-model="roleForm.channel">
                  <option value="email">邮件，临时密码 1 小时有效</option>
                  <option value="sms">短信，临时密码 10 分钟有效</option>
                </select></label
              ><button class="primary-btn" @click="assignPlatformRole">
                分配角色并生成临时密码 <ArrowUpRight :size="16" />
              </button>
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
                    <td>{{ item.email || item.phone }}</td>
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
          <div class="drawer-section-title">API 调用凭据</div>
          <p class="muted">
            网关路径：{{ apiOrderState.gateway_base_path }} ·
            凭据原文仅在生成或重新生成时显示。
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
        <fieldset :disabled="productDetailMode" class="product-fieldset">
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
              <option value="object_storage">对象存储交付</option>
              <option value="api">API 服务</option>
              <option value="model_api">模型 API</option>
              <option value="tenant_access">租户/权限开通</option>
              <option value="training">线下培训</option>
              <option value="consulting">咨询交付</option>
              <option value="custom">定制开发</option>
            </select></label
          >
        </div>
        <div class="version-editor">
          <div class="version-editor-head">
            <div>
              <strong>版本与价格</strong
              ><small>每个版本单独定义销售价格、成本和简要介绍，清算按版本利润计算</small>
            </div>
            <button v-if="!productDetailMode" type="button" class="text-btn" @click="addProductVersion">
              新增版本
            </button>
          </div>
          <div
              v-for="(version, index) in productForm.versions"
            :key="index"
            class="version-row"
            :class="{ 'api-version-row': productForm.product_type === 'api' }"
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
            ><label
              >成本口径<select v-model="version.cost_type"><option value="per_order">每订单</option><option value="per_subscription">每订阅</option><option value="per_period">每周期</option><option value="one_time">一次性</option></select></label
            ><label v-if="productForm.product_type === 'api'" title="该版本每分钟允许的最大调用次数"
              >每分钟限流<input v-model.number="version.rate_limit_per_minute" type="number" min="1" required /></label
            ><label v-if="productForm.product_type === 'api'" title="该版本每日允许的最大调用次数"
              >每日配额<input v-model.number="version.daily_quota" type="number" min="1" required /></label
            ><label v-if="productForm.product_type === 'api'" title="0 表示不单独限制月配额"
              >每月配额<input v-model.number="version.monthly_quota" type="number" min="0" /></label
            ><label class="version-description"
              >版本简要介绍<textarea
                v-model="version.description"
                rows="2"
                placeholder="说明该版本的功能范围、能力差异或适用对象"
              ></textarea></label
            ><button v-if="!productDetailMode"
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
        <button v-if="!productDetailMode" class="primary-btn full-btn" type="submit">
          {{ selectedProductId ? "保存产品信息" : "保存产品登记草稿" }} <ArrowUpRight :size="16" />
        </button>
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
            <h2>个人实名认证</h2>
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
        <div class="form-grid">
          <label
            >身份证正面<input
              type="file"
              accept="image/*"
              @change="verificationFiles.front = $event.target.files[0]"
              required /></label
          ><label
            >身份证反面<input
              type="file"
              accept="image/*"
              @change="verificationFiles.back = $event.target.files[0]"
              required
          /></label>
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
        ><button class="primary-btn full-btn" type="submit">
          提交企业认证 <ArrowUpRight :size="16" />
        </button>
      </form>
    </div>
    <div v-if="toast" class="toast"><CheckCircle2 :size="17" />{{ toast }}</div>
  </div>
</template>
