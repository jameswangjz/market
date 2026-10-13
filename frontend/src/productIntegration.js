export function canViewProductIntegration(user) {
  if (user?.platform_role) return ['super_admin', 'platform_operator'].includes(user.platform_role);
  return ['super_admin', 'enterprise_admin'].includes(user?.enterprise_role)
    || user?.enterprise_memberships?.some(member => member.status === 'active' && ['super_admin', 'enterprise_admin'].includes(member.role)) === true;
}

export function canManageProviderSaas(user, product) {
  if (user?.platform_role || !user?.is_active || user.activation_status !== 'active' || user.verified_status !== 'verified') return false;
  if (product?.product_type !== 'saas' || !['pending_integration', 'published'].includes(product.status)) return false;
  if (!product.enterprise_id) return false;
  return user.enterprise_memberships?.some(membership => membership.enterprise_id === product.enterprise_id
      && membership.status === 'active' && ['super_admin', 'enterprise_admin'].includes(membership.role));
}

export function integrationStatus(item) {
  if (!item) return '未配置';
  if (item.status === 'verified' && item.healthy) return '验证通过';
  return { pending_integration: '待接入验证', failed: '验证失败', error: '验证失败' }[item.status] || '未就绪';
}

export function createIntegrationController({ state, api, product, changed }) {
  let epoch = 0;
  let verification = 0;
  const path = (id, version) => `/products/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/integration`;
  const message = (error, fallback) => typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : fallback;
  async function load(options = false) {
    const preserveBusy = options === true;
    if (!preserveBusy) {
      verification++;
      state.busy = '';
    }
    const generation = ++epoch;
    const current = product();
    state.loading = true;
    state.rows = [];
    const versions = (current.versions || []).filter(v => v.id);
    const rows = await Promise.all(versions.map(async version => {
      try {
        const { data } = await api.get(path(current.id, version.id));
        if ((data?.item?.version_id && data.item.version_id !== version.id)
            || (data?.route?.version && data.route.version !== version.version_code)) {
          return { version, item: null, route: null, error: '接入状态与当前版本不匹配' };
        }
        return { version, ...data, error: '' };
      } catch (error) {
        return { version, item: null, route: null, error: message(error, '接入状态加载失败') };
      }
    }));
    if (generation !== epoch || product().id !== current.id) return false;
    state.rows = rows;
    state.loading = false;
    return true;
  }
  async function verify(row) {
    if (state.busy || state.loading) return;
    const current = product();
    if (!['pending_integration', 'published'].includes(current.status)) return;
    const version = current.versions?.find(item => item.id === row?.version?.id);
    if (!version || !state.rows.includes(row) || !row.item) return;
    state.busy = row.version.id;
    const operation = ++verification;
    const generation = epoch;
    try {
      await api.post(path(current.id, row.version.id) + '/verify');
      if (generation === epoch && current.id === product().id) {
        if (await load(true) && operation === verification && current.id === product().id) changed();
      }
    } catch (error) {
      if (generation === epoch && current.id === product().id) {
        // Failed verification persists its evidence before returning an error.
        if (await load(true) && operation === verification && current.id === product().id) {
          const refreshed = state.rows.find(v => v.version.id === row.version.id);
          if (refreshed) refreshed.error = message(error, '接入验证失败');
        }
      }
    } finally {
      if (operation === verification && current.id === product().id) state.busy = '';
    }
  }
  function dispose() {
    epoch++;
    verification++;
    state.loading = false;
    state.busy = '';
  }
  return { load, verify, dispose };
}
