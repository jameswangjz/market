export function createDialogController(state) {
  const queue = [];
  let active = null;
  let disposed = false;
  function next() {
    if (active || !queue.length) return;
    active = queue.shift();
    state.value = active.options;
  }
  function request(options) {
    if (disposed) return Promise.resolve(options.kind === "confirm" ? false : null);
    return new Promise((resolve) => {
      queue.push({ options, resolve });
      next();
    });
  }
  function finish(value) {
    if (!active) return;
    const current = active;
    active = null;
    state.value = null;
    current.resolve(value);
    next();
  }
  return {
    prompt: (message, value = "", options = {}) => request({
      kind: "prompt", title: "填写信息", message, value: String(value ?? ""),
      confirmText: "确认", required: false, ...options,
    }),
    confirm: (message, options = {}) => request({
      kind: "confirm", title: "操作确认", message, confirmText: "确认", ...options,
    }),
    submit(value) {
      if (!active) return false;
      const options = active.options;
      if (options.kind === "prompt" && options.required && !String(value ?? "").trim()) return false;
      finish(options.kind === "confirm" ? true : String(value ?? "").trim());
      return true;
    },
    cancel() { finish(active?.options.kind === "confirm" ? false : null); },
    dispose() {
      disposed = true;
      while (active || queue.length) {
        if (!active) next();
        finish(active.options.kind === "confirm" ? false : null);
      }
    },
  };
}
