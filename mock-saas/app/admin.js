'use strict';
(() => {
  let key = '';
  let defaults = {};
  const el = id => document.getElementById(id);
  const form = el('client-form');
  const field = name => form.elements.namedItem(name);
  const status = message => { el('admin-status').textContent = message; };
  function lock() {
    key = '';
    form.reset();
    el('admin-key').value = '';
    el('clients-list').tBodies[0].replaceChildren();
    el('workspace').hidden = true;
    el('login-section').hidden = false;
  }
  async function api(path, method = 'GET', body) {
    const response = await fetch('/admin/api/' + path, {
      method, cache: 'no-store', credentials: 'omit', redirect: 'error',
      headers: {'X-Mock-Admin-Secret': key, 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) {
      if (response.status === 403) lock();
      const messages = {400: '配置不匹配或新建缺少密钥。', 403: '管理认证失败，请重新登录。',
        409: '客户端不可绑定其他产品。', 422: '配置格式无效。', 503: '连接或可信发现配置检查失败。'};
      throw new Error(messages[response.status] || '请求失败。');
    }
    return response.json();
  }
  function reset() {
    form.reset();
    for (const name of ['client_id', 'product_id']) field(name).readOnly = false;
    for (const name of ['issuer', 'redirect_uri']) field(name).value = defaults[name];
    el('form-title').textContent = '新建客户端';
    preview();
  }
  function preview() {
    field('application_url').value = field('product_id').value && defaults.public_base
      ? defaults.public_base + '/apps/' + encodeURIComponent(field('product_id').value) : '';
  }
  function addressCell(url) {
    const cell = document.createElement('td');
    const input = document.createElement('input');
    input.readOnly = true; input.value = url; input.setAttribute('aria-label', '应用访问地址');
    const copy = document.createElement('button');
    copy.type = 'button'; copy.textContent = '复制'; copy.title = '复制应用访问地址';
    copy.addEventListener('click', async () => {
      try {
        if (!window.isSecureContext || !navigator.clipboard) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(url); status('应用访问地址已复制。');
      } catch (_) {
        input.focus(); input.select(); status('应用访问地址已选中。');
      }
    });
    cell.append(input, copy);
    return cell;
  }
  async function load() {
    const data = await api('clients');
    defaults = {issuer: data.issuer, redirect_uri: data.redirect_uri, public_base: data.public_base};
    const tbody = el('clients-list').tBodies[0];
    tbody.replaceChildren();
    for (const client of data.clients) {
      const row = document.createElement('tr');
      for (const value of [client.client_id, client.product_id, client.product_name,
        client.secret_configured ? '已配置' : '未配置']) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      row.append(addressCell(client.application_url));
      const cell = document.createElement('td');
      const edit = document.createElement('button');
      edit.type = 'button'; edit.textContent = '编辑';
      edit.addEventListener('click', () => {
        reset();
        for (const name of ['client_id', 'product_id', 'product_name', 'issuer', 'redirect_uri']) field(name).value = client[name];
        field('client_id').readOnly = true; field('product_id').readOnly = true;
        preview();
        el('form-title').textContent = '编辑客户端';
        field('client_secret').focus();
      });
      cell.append(edit); row.append(cell); tbody.append(row);
    }
  }
  async function run(button, action) {
    button.disabled = true;
    try { await action(); } catch (error) { status(error instanceof TypeError ? '连接失败。' : error.message); }
    finally { button.disabled = false; }
  }
  el('login-form').addEventListener('submit', event => {
    event.preventDefault(); key = el('admin-key').value; el('admin-key').value = '';
    run(el('admin-login'), async () => {
      try { await load(); } catch (error) { lock(); throw error; }
      reset(); el('workspace').hidden = false; el('login-section').hidden = true; status('已登录。');
    });
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    delete body.application_url;
    field('client_secret').value = '';
    run(el('client-save'), async () => {
      try { await api('clients', 'PUT', body); } finally { body.client_secret = ''; }
      await load(); reset(); status('已保存。');
    });
  });
  el('discovery-check').addEventListener('click', () => run(el('discovery-check'), async () => {
    await api('check', 'POST'); status('平台OIDC连接检查通过；仅检查平台连通性与发现配置。');
  }));
  field('product_id').addEventListener('input', preview);
  el('new-client').addEventListener('click', reset);
  el('logout').addEventListener('click', () => { lock(); status('已退出管理。'); });
  window.addEventListener('pagehide', lock);
})();
