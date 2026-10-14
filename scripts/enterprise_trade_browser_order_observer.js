(expected => {
  if (window.__enterpriseTradeObserverInstalled) return true;
  const matches = (method, url) => {
    const target = new URL(url, location.href);
    return String(method).toUpperCase() === 'POST' && target.origin === expected.origin &&
      ['/api/orders', '/api/orders/quote'].includes(target.pathname) && !target.search && !target.hash
      ? target.pathname : '';
  };
  const captureQuote = (status, quote, body) => {
    try { if (typeof body === 'string') body = JSON.parse(body); } catch { return; }
    if (status >= 200 && status < 300 && body?.product_id === expected.product &&
        body.product_version_id === expected.version && body.buyer_enterprise_id === expected.enterprise &&
        body.subscription_months === 1 && quote?.product_id === expected.product &&
        quote.product_version_id === expected.version && quote.subscription_months === 1 &&
        /^[a-f0-9]{64}$/.test(quote.quote_id)) window.__enterpriseTradeQuoteId = quote.quote_id;
  };
  const capture = (status, order) => {
    if (status < 200 || status >= 300 || !order || !/^[a-f0-9]{32}$/.test(order.id) ||
        order.product_id !== expected.product || order.product_version_id !== expected.version ||
        order.buyer_enterprise_id !== expected.enterprise || order.buyer_user_id !== expected.buyer ||
        order.order_no === 'ORD-261012005144E1B3' || order.product_id === 'qa-vcq-a044ea9ea181-p2') return;
    sessionStorage.setItem('market_enterprise_trade_order', JSON.stringify({ id: order.id }));
  };
  const fetch = window.fetch;
  window.fetch = async function(input, init) {
    const response = await fetch.apply(this, arguments);
    const path = matches(init?.method || input?.method || 'GET', typeof input === 'string' || input instanceof URL ? input : input.url);
    if (path) {
      try {
        const data = await response.clone().json();
        if (path === '/api/orders') capture(response.status, data);
        else captureQuote(response.status, data, init?.body || (input?.clone ? await input.clone().text() : null));
      } catch {}
    }
    return response;
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  const requests = new WeakMap();
  XMLHttpRequest.prototype.open = function(method, url) {
    requests.set(this, matches(method, url));
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    if (requests.get(this)) this.addEventListener('load', () => {
      try {
        const data = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
        if (requests.get(this) === '/api/orders') capture(this.status, data);
        else captureQuote(this.status, data, body);
      } catch {}
    }, { once: true });
    return send.apply(this, arguments);
  };
  window.__enterpriseTradeObserverInstalled = true;
  return true;
})
