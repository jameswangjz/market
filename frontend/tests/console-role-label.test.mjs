import test from "node:test";
import assert from "node:assert/strict";
import { consoleRoleLabel } from "../src/consoleRoleLabel.js";

test("console role label uses platform role when present", () => {
  const labels = {
    super_admin: "平台超级管理员",
    platform_operator: "运营",
    product_manager: "数据产品经理",
    quality_reviewer: "质量审核",
    security_compliance: "安全合规",
    delivery_monitor: "交付监控",
    finance_settlement: "财务清算",
  };
  for (const [role, label] of Object.entries(labels)) {
    assert.equal(consoleRoleLabel({ platform_role: role }), label);
  }
});

test("retired role has no selectable business-review label", () => {
  assert.equal(consoleRoleLabel({ platform_role: "business_reviewer" }), "business_reviewer");
});

test("console role label reflects the selected active enterprise membership", () => {
  const enterprise = { id: "selected" };
  const user = {
    enterprise_role: "super_admin",
    enterprise_memberships: [
      { enterprise_id: "first", role: "super_admin", status: "active" },
      { enterprise_id: "selected", role: "enterprise_admin", status: "active" },
      { enterprise_id: "disabled", role: "member", status: "disabled" },
    ],
  };
  assert.equal(consoleRoleLabel(user, enterprise), "企业管理员");
  assert.equal(consoleRoleLabel({
    enterprise_role: "member",
    enterprise_memberships: [{ enterprise_id: "selected", role: "super_admin", status: "active" }],
  }, enterprise), "企业超级管理员");
  assert.equal(consoleRoleLabel({ enterprise_memberships: [{ enterprise_id: "selected", role: "member", status: "active" }] }, enterprise), "企业普通用户");
  assert.equal(consoleRoleLabel({ enterprise_memberships: [{ enterprise_id: "selected", role: "member", status: "disabled" }] }, enterprise), "未加入企业");
  assert.equal(consoleRoleLabel({}, enterprise), "未加入企业");
});
