// Isolated UI fixture: no funds, resources or backend requests are executed.
import { createApp, h, reactive } from "vue";
import RefundCenter from "../../src/RefundCenter.vue";
import "../../src/style.css";
const admin = { id: "admin", verified_status: "verified", platform_role: "super_admin" };
const view = reactive({ user: admin, epoch: 0 });
let item = { id: "refund", refund_no: "REF-EXECUTION-FIXTURE", amount: "20.00", source: "ordinary", status: "approved", version: 4,
  allowed_actions: ["execute"], execution_available: true, execution_mode: "simulated", planning: { status: "frozen" } };
const calls = [];
const clone = value => JSON.parse(JSON.stringify(value));
const api = { async get(path) { return { data: path === "/refunds" ? { items: [clone(item)], total: 1 } : clone(item) }; },
  async post(path, body) {
    calls.push({ path, body });
    item = { ...item, status: "failed", version: 5, allowed_actions: ["retry"], execution: { status: "failed", funds_mode: "simulated",
      resource_status: "completed", ledger_status: "pending", error_code: "payment_failed",
      payment_legs: [{ amount: "12.00", status: "completed", secret: "PRIVATE_SECRET" }, { amount: "8.00", status: "failed" }] } };
    return { data: clone(item) };
  } };
window.refundExecutionFixture = { calls, enterprise() { view.user = { id: "buyer", verified_status: "verified",
  enterprise_memberships: [{ enterprise_id: "enterprise", role: "enterprise_admin", status: "active" }] }; view.epoch++; } };
createApp({ render() { return h("main", { style: "padding:16px;max-width:1000px;margin:auto" }, [h(RefundCenter,
  { key: view.epoch, api, user: view.user, enterpriseId: "enterprise", params: { enterprise_id: "enterprise" } })]); } }).mount("#fixture");
