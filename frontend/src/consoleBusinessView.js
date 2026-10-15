const ACTIVE = item => (item?.status || item?.membership_status) === "active";

export function activeEnterpriseMembership(user, enterpriseId) {
  return user?.enterprise_memberships?.find(item => item.enterprise_id === enterpriseId && ACTIVE(item)) || null;
}

export function consoleEnterpriseSelection(user, enterpriseId) {
  if (user?.platform_role || !enterpriseId) return null;
  const membership = activeEnterpriseMembership(user, enterpriseId);
  if (!membership) return null;
  return { enterprise: { id: membership.enterprise_id, name: membership.enterprise_name || "" }, membership };
}

export function businessModes(user, enterprise) {
  if (user?.platform_role || !enterprise?.id) return [];
  const membership = activeEnterpriseMembership(user, enterprise.id);
  if (!membership || !["super_admin", "enterprise_admin"].includes(membership.role)) return [];
  return ["provider", "buyer"];
}

export function allowedBusinessViews(user, enterprise, mode) {
  if (user?.platform_role) return null;
  const membership = activeEnterpriseMembership(user, enterprise?.id);
  if (!membership) return ["applications", "users", "messages"];
  if (!["super_admin", "enterprise_admin"].includes(membership.role)) return ["applications", "users", "messages"];
  const common = ["orders", "delivery", "audit", "users", "messages"];
  if (mode === "provider") return membership.role === "super_admin" ? [...common, "products", "settlements", "overview"] : [...common, "products", "settlements"];
  return [...common, "applications"];
}

export function businessViewParams(user, enterprise, mode) {
  if (user?.platform_role || !enterprise?.id) return {};
  return { enterprise_id: enterprise.id, ...(businessModes(user, enterprise).includes(mode) ? { business_view: mode } : {}) };
}
