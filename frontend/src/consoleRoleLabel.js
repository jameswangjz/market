const platformRoleLabels = {
  super_admin: "平台超级管理员",
  platform_operator: "运营",
  product_manager: "数据产品经理",
  quality_reviewer: "质量审核",
  security_compliance: "安全合规",
  delivery_monitor: "交付监控",
  finance_settlement: "财务清算",
};

const enterpriseRoleLabels = {
  super_admin: "企业超级管理员",
  enterprise_admin: "企业管理员",
  member: "企业普通用户",
};

export function consoleRoleLabel(user, enterprise) {
  if (user?.platform_role) {
    return platformRoleLabels[user.platform_role] || user.platform_role;
  }
  const membership = enterprise?.id && user?.enterprise_memberships?.find(item =>
    item.enterprise_id === enterprise.id && (item.status || item.membership_status) === "active");
  return enterpriseRoleLabels[membership?.role] || "未加入企业";
}
