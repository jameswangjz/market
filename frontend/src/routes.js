export function resolveRoute(pathname) {
  if (pathname === "/") return { name: "storefront" };
  if (["/console", "/console/"].includes(pathname)) return { name: "console" };
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
    if (consumeReturnTo) url.searchParams.delete("returnTo");
    return `${url.pathname}${url.search}${url.hash}`;
  } catch { return "/console"; }
}

export function navigate(target) {
  const path = safeReturnTarget(target, window.location.origin, false);
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function consoleLoginUrl(returnTo) {
  return `/console?${new URLSearchParams({ returnTo: safeReturnTarget(returnTo) })}`;
}
