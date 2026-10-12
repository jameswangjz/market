export function resolveRoute(pathname) {
  if (pathname === "/") return { name: "storefront" };
  if (["/console", "/console/"].includes(pathname)) return { name: "console" };
  if (pathname === "/console/sso") return { name: "sso" };
  const match = /^\/products\/([^/]+)\/?$/.exec(pathname);
  if (match) {
    try {
      const id = decodeURIComponent(match[1]);
      if (id && !/[\\/\u0000-\u001f\u007f]/.test(id)) return { name: "product", id };
    } catch { /* Malformed URLs render the not-found view. */ }
  }
  return { name: "not-found" };
}

export function safeReturnTarget(value, origin = window.location.origin, consumeReturnTo = true) {
  if (typeof value !== "string" || !value || /[\\\u0000-\u0020\u007f]/.test(value) || value.startsWith("//")) return "/console";
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || resolveRoute(url.pathname).name === "not-found") return "/console";
    if (url.pathname === "/console/sso") {
      const id = oidcRequestId(url.search);
      return id && !url.hash ? `/console/sso?request=${id}` : "/console";
    }
    if (consumeReturnTo) url.searchParams.delete("returnTo");
    return `${url.pathname}${url.search}${url.hash}`;
  } catch { return "/console"; }
}

export function oidcRequestId(search) {
  const params = new URLSearchParams(search);
  const entries = [...params.entries()];
  return entries.length === 1 && entries[0][0] === "request" && /^[a-f0-9]{64}$/.test(entries[0][1]) ? entries[0][1] : "";
}

// Only backend launch/consent responses may supply external navigation targets.
export function safeApplicationUrl(value) {
  if (typeof value !== "string" || /[\\\u0000-\u0020\u007f]/.test(value)) throw new Error("访问地址无效");
  const url = new URL(value);
  if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error("访问地址无效");
  return url.href;
}

export function navigate(target) {
  const path = safeReturnTarget(target, window.location.origin, false);
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function consoleLoginUrl(returnTo) {
  return `/console?${new URLSearchParams({ returnTo: safeReturnTarget(returnTo) })}`;
}
