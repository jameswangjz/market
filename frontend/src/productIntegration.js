export function canViewProductIntegration(user) {
  if (user?.platform_role) return ['super_admin', 'platform_operator'].includes(user.platform_role);
  return ['super_admin', 'enterprise_admin'].includes(user?.enterprise_role)
    || user?.enterprise_memberships?.some(member => member.status === 'active' && ['super_admin', 'enterprise_admin'].includes(member.role)) === true;
}

export function integrationStatus(item) {
  if (!item) return '未配置';
  if (item.status === 'verified' && item.healthy) return '验证通过';
  return { pending_integration: '待接入验证', failed: '验证失败', error: '验证失败' }[item.status] || '未就绪';
}

export function createIntegrationController({ state, api, product, changed }) {
  let epoch = 0;
  const path = (id, version) => `/products/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/integration`;
  const message = (error, fallback) => typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : fallback;
  async function load() {
    const generation = ++epoch;
    const current = product();
    state.loading = true;
    state.rows = [];
    const rows = await Promise.all((current.versions || []).filter(v => v.id).map(async version => {
      try {
        const { data } = await api.get(path(current.id, version.id));
        return { version, ...data, error: '' };
      } catch (error) {
        return { version, item: null, route: null, error: message(error, '接入状态加载失败') };
      }
    }));
    if (generation !== epoch || product().id !== current.id) return;
    state.rows = rows;
    state.loading = false;
  }
  async function verify(row) {
    if (state.busy || state.loading) return;
    const current = product();
    if (!['pending_integration', 'published'].includes(current.status)) return;
    state.busy = row.version.id;
    const generation = epoch;
    try {
      await api.post(path(current.id, row.version.id) + '/verify');
      if (generation === epoch && current.id === product().id) {
        await load();
        changed();
      }
    } catch (error) {
      if (generation === epoch && current.id === product().id) {
        // Failed verification persists its evidence before returning an error.
        await load();
        const refreshed = state.rows.find(v => v.version.id === row.version.id);
        if (refreshed) refreshed.error = message(error, '接入验证失败');
      }
    } finally {
      state.busy = '';
    }
  }
  function dispose() { epoch++; }
  return { load, verify, dispose };
}
