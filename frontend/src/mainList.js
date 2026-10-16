import { pageCount, validPage, pageSizeValue } from "./pagination.js";

export const methodLabels = { file: "文件下载", object_storage: "对象存储", api: "API 接口", model_api: "模型接口", tenant_access: "租户应用访问", saas: "SaaS 应用", training: "培训", consulting: "咨询", custom: "定制交付", manual: "人工交付", offline: "线下交付", online: "在线交付" };
export function methodLabel(value) { return value === "custom_development" ? "定制开发" : methodLabels[value] || (value ? `未知方式（${value}）` : "未记录"); }
export function partyName(row, side) {
  return row[`${side}_display_name`] || row[`${side}_enterprise_name`] || row[`${side}_name`] || "未记录";
}
export function listState(filters = {}) {
  return { filters: { ...filters }, applied: { ...filters }, page: 1, page_size: 20, total: 0, busy: false, error: "", generation: 0 };
}
export function listParams(state) {
  const params = { page: state.page, page_size: state.page_size };
  for (const [key, value] of Object.entries(state.applied)) {
    const target = state.aliases?.[key] || key;
    if (value !== "" && value != null) params[target] = typeof value === "string" ? value.trim().slice(0, 200) : value;
  }
  return params;
}
export function applyListFilters(state, reset = false) {
  if (reset) for (const key of Object.keys(state.filters)) state.filters[key] = "";
  state.applied = { ...state.filters };
  state.page = 1;
}
export function changeListPage(state, page, size) {
  if (size !== undefined) { state.page_size = pageSizeValue(size); state.page = 1; return true; }
  const next = validPage(page, pageCount(state.total, state.page_size));
  if (next === null) return false;
  state.page = next;
  return true;
}
export async function fetchListPage(api, path, state, scope, current, assign) {
  const generation = ++state.generation;
  state.busy = true; state.error = "";
  const valid = () => generation === state.generation && current();
  try {
    let { data } = await api.get(path, { params: { ...scope, ...listParams(state) } });
    if (!valid()) return;
    if (!Number.isSafeInteger(data.total) || !Array.isArray(data.items)) throw new Error("列表接口尚未返回分页总数，请完成接口联调");
    const last = pageCount(data.total, state.page_size);
    if (state.page > last) {
      state.page = last;
      ({ data } = await api.get(path, { params: { ...scope, ...listParams(state) } }));
      if (!valid()) return;
      if (!Number.isSafeInteger(data.total) || !Array.isArray(data.items)) throw new Error("列表分页响应无效");
    }
    state.total = data.total;
    assign(data.items, data);
  } catch (error) {
    if (valid()) {
      assign([]); state.total = 0;
      state.error = typeof error.response?.data?.detail === "string" ? error.response.data.detail : error.message || "列表加载失败";
    }
  } finally { if (generation === state.generation) state.busy = false; }
}
