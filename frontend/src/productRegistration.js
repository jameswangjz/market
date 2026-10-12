const versionFields = ["version_code", "description", "compatibility_mode", "compatibility_notes", "price", "cost", "rate_limit_per_minute", "daily_quota", "monthly_quota", "quota_unit", "quota_amount", "status"];
const productFields = ["name", "product_type", "catalog_name", "provider_name", "provider_type", "description", "usage_scenarios", "delivery_method", "upstream_url", "application_url", "integration_api_url", "oidc_redirect_uris", "download_limit", "logo_file_id", "price", "pricing_strategy", "version", "quality_level", "security_level", "authorization_conditions", "data_source_statement", "compliance_statement", "settlement_rule_mode", "settlement_rule_id", "settlement_rule"];
const integrationFields = ["upstream_url", "route_key", "auth_mode", "upstream_auth_mode", "upstream_scope", "timeout_ms", "strip_prefix", "health_path", "health_method"];

export function splitOidcRedirectUris(text) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

export function isValidOidcRedirectUri(value, options = {}) {
  if (typeof value !== "string" || value.length > 2000 || /[\s\x00-\x1f\\#*]/.test(value)) return false;
  const authority = value.match(/^https?:\/\/([^/?#]+)\//i)?.[1];
  if (!authority || authority.includes("@")) return false;
  try {
    const url = new URL(value);
    if (!url.hostname || url.username || url.password || url.hash || url.port === "0") return false;
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && options.allowHttp === true;
  } catch {
    return false;
  }
}

export function registrationPayload(form, options = {}) {
  const payload = Object.fromEntries(productFields.map((key) => [key, form[key]]));
  const redirects = form.oidc_redirect_uris ?? [];
  if (!Array.isArray(redirects) || redirects.some((uri) => typeof uri !== "string")) {
    throw new TypeError("OIDC回调地址须为字符串数组");
  }
  const normalizedRedirects = redirects.map((uri) => uri.trim()).filter(Boolean);
  if (normalizedRedirects.some((uri) => !isValidOidcRedirectUri(uri, options))) {
    throw new TypeError("OIDC回调地址须为完整HTTPS URL（除非管理员已允许HTTP），不得包含片段或用户信息");
  }
  payload.oidc_redirect_uris = normalizedRedirects;
  return { ...payload, versions: form.versions.map((version) => ({
    ...Object.fromEntries(versionFields.map((key) => [key, version[key]])),
    compatibility_mode: version.compatibility_mode ?? "compatible",
    compatibility_notes: version.compatibility_notes ?? "",
  })) };
}

export function emptyVersionIntegration() {
  return { upstream_url: "", route_key: "", auth_mode: "api_key", upstream_auth_mode: "oauth2", upstream_scope: "resource.invoke", timeout_ms: 30000, strip_prefix: true, health_path: "/health", health_method: "GET" };
}

export async function loadRegistrationIntegrations(api, productId, versions) {
  for (const version of versions) {
    const { data } = await api.get(`/products/${productId}/versions/${version.id}/integration`);
    version.integration = { ...emptyVersionIntegration(), ...Object.fromEntries(integrationFields.filter((key) => data.route && key in data.route).map((key) => [key, data.route[key]])) };
  }
}

export function bindRegistrationVersions(versions, saved) {
  for (const version of versions) {
    const persisted = saved.find((item) => item.version_code === version.version_code);
    if (!persisted?.id) throw new Error("产品版本保存结果不完整，请重新打开产品");
    version.id = persisted.id;
  }
}

export async function saveRegistrationAttachments(api, form, productId) {
  if (["api", "model_api"].includes(form.delivery_method)) {
    for (const version of form.versions) {
      if (version.is_sold === true) continue;
      const config = version.integration;
      if (!config) throw new Error("请重新打开产品加载每版本接入配置");
      const payload = Object.fromEntries(integrationFields.map((key) => [key, config[key]]));
      Object.assign(payload, { version: version.version_code, rate_limit_per_minute: version.rate_limit_per_minute, daily_quota: version.daily_quota, monthly_quota: version.monthly_quota });
      await api.put(`/products/${productId}/versions/${version.id}/integration`, payload);
    }
  }
  if (form.delivery_method !== "file") return;
  for (const version of form.versions) {
    if (!version.fileUpload) continue;
    if (version.is_sold === true) throw new Error("已售版本的交付文件不可覆盖，请新增版本");
    const upload = new FormData();
    upload.append("upload", version.fileUpload);
    upload.append("product_id", productId);
    upload.append("file_role", "product_data");
    upload.append("version_id", version.id);
    upload.append("version", version.version_code);
    const { data } = await api.post("/files/upload", upload);
    version.uploadedFileName = data.original_name || version.fileUpload.name;
    version.fileUpload = null;
  }
}
