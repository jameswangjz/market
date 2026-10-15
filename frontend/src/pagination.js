export const pageSizes = Object.freeze([10, 20, 50, 100]);
export function pageCount(total, size) { return Math.max(1, Math.ceil(Math.max(0, Number(total) || 0) / size)); }
export function validPage(value, pages) {
  if (!/^\d+$/.test(String(value))) return null;
  const page = Number(value);
  return Number.isSafeInteger(page) && page >= 1 && page <= pages ? page : null;
}
export function pageSizeValue(value, fallback = 20, legacy = false) {
  const size = Number(value);
  return pageSizes.includes(size) || (legacy && size === 12) ? size : fallback;
}
