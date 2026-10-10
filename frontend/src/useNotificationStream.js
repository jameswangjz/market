import { ref, unref, watch, onMounted, onUnmounted } from 'vue';

// Preserve partial lines and events, including a CRLF split between chunks.
export function createSSEParser(onEvent) {
  let buffer = '', data = [], event = 'message', lastId;
  function line(value) {
    if (!value) {
      if (data.length) onEvent({ event, data: data.join('\n'), id: lastId });
      data = []; event = 'message'; return;
    }
    if (value.startsWith(':')) return;
    const colon = value.indexOf(':');
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? '' : value.slice(colon + 1);
    if (content.startsWith(' ')) content = content.slice(1);
    if (field === 'data') data.push(content);
    if (field === 'event') event = content;
    if (field === 'id' && !content.includes('\0')) lastId = content;
  }
  return chunk => {
    buffer += chunk;
    let match;
    while ((match = /[\r\n]/.exec(buffer))) {
      const index = match.index;
      if (buffer[index] === '\r' && index === buffer.length - 1) break;
      const width = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1;
      line(buffer.slice(0, index));
      buffer = buffer.slice(index + width);
    }
  };
}

export function useNotificationStream(onRefresh, { token: tokenSource, pollMs = 30000, onAuthExpired = () => {} } = {}) {
  const connected = ref(false);
  let stopped = true, controller, retryTimer, pollTimer, refreshTimer, failures = 0, lastEventId, expiredToken, stopWatch;
  let generation = 0;
  const getToken = () => tokenSource === undefined ? localStorage.getItem('market_token') || '' : (typeof tokenSource === 'function' ? tokenSource() : unref(tokenSource)) || '';
  const active = version => !stopped && generation === version;
  const refresh = version => {
    if (!active(version) || refreshTimer || !getToken()) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      if (active(version)) Promise.resolve().then(() => { if (active(version)) return onRefresh(); }).catch(() => {});
    }, 250);
  };
  function poll(version) { if (!pollTimer && active(version)) pollTimer = setInterval(() => refresh(version), pollMs); }
  function stop() {
    stopped = true; ++generation; controller?.abort(); connected.value = false;
    clearTimeout(retryTimer); clearTimeout(refreshTimer); clearInterval(pollTimer);
    retryTimer = null; refreshTimer = null; pollTimer = null;
  }
  function restart() {
    stop(); failures = 0; lastEventId = undefined; expiredToken = null;
    if (!getToken()) return;
    stopped = false; poll(generation); connect(generation);
  }
  function expire(token) {
    expiredToken = token; connected.value = false;
    clearInterval(pollTimer); pollTimer = null;
    clearTimeout(refreshTimer); refreshTimer = null;
    onAuthExpired();
  }
  async function connect(version) {
    if (!active(version)) return;
    const token = getToken();
    if (!token || token === expiredToken) return;
    const requestController = new AbortController();
    controller = requestController;
    let reader;
    try {
      const headers = { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' };
      if (lastEventId) headers['Last-Event-ID'] = lastEventId;
      const response = await fetch('/api/notifications/stream', { headers, signal: requestController.signal, cache: 'no-store' });
      if (!active(version)) { await response.body?.cancel(); return; }
      if (response.status === 401 || response.status === 403) {
        expire(token);
      }
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('Stream unavailable');
      connected.value = true;
      clearInterval(pollTimer); pollTimer = null; refresh(version);
      const parse = createSSEParser(message => {
        if (!active(version)) return;
        if (message.event === 'auth_expired') {
          expire(token); requestController.abort(); return;
        }
        if (expiredToken) return;
        if (message.id !== undefined) lastEventId = message.id;
        failures = 0;
        if (!['ping', 'heartbeat'].includes(message.event)) refresh(version);
      });
      const decoder = new TextDecoder();
      reader = response.body.getReader();
      while (active(version)) {
        const { done, value } = await reader.read();
        if (done) { parse(decoder.decode()); break; }
        parse(decoder.decode(value, { stream: true }));
      }
    } catch { /* Polling compensates while SSE is unavailable. */ }
    finally {
      if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      if (active(version)) {
        connected.value = false;
        if (expiredToken) return;
        poll(version); refresh(version);
        const delay = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5));
        retryTimer = setTimeout(() => connect(version), delay + Math.random() * 500);
      }
    }
  }
  const storageChanged = event => { if (tokenSource === undefined && (!event.key || event.key === 'market_token')) restart(); };
  onMounted(() => {
    window.addEventListener('storage', storageChanged);
    if (tokenSource !== undefined) stopWatch = watch(getToken, restart, { immediate: true, flush: 'sync' });
    else restart();
  });
  onUnmounted(() => {
    stop(); stopWatch?.(); window.removeEventListener('storage', storageChanged);
  });
  return { connected, restart, stop };
}
