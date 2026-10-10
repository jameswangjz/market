const operators = ["super_admin", "platform_operator"];
const auditors = [...operators, "finance_settlement", "security_compliance"];

export function canAccessConsoleView(user, enterprise, view) {
  if (!user) return false;
  // Keep account, verification and enterprise onboarding available to all users.
  if (["users", "messages"].includes(view)) return true;
  if (view === "products") return !user.platform_role || [...operators, "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"].includes(user.platform_role);
  if (user.verified_status !== "verified") return false;
  const role = user.platform_role;
  if (["gateway", "sla", "settings", "development"].includes(view)) return operators.includes(role);
  if (["settlements", "audit"].includes(view)) return auditors.includes(role);
  if (view === "delivery") return [...operators, "delivery_monitor"].includes(role) || Boolean(enterprise?.id && ["super_admin", "enterprise_admin"].includes(user.enterprise_role));
  if (view === "orders") return [...auditors, "delivery_monitor"].includes(role) || Boolean(!role && enterprise?.id);
  if (view === "overview") return Boolean(enterprise?.id || [...operators, "security_compliance"].includes(role));
  return false;
}
