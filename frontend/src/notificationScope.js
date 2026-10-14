import { businessViewParams } from './consoleBusinessView.js';

export function notificationScope(user, enterprise, mode) {
  if (!user || user.verified_status !== 'verified' || user.platform_role) return {};
  return businessViewParams(user, enterprise, mode);
}

export function notificationScopeKey(scope = {}) {
  return JSON.stringify([scope.enterprise_id || '', scope.business_view || '']);
}

export function notificationRequest(config, scope = {}) {
  const params = { ...config.params };
  delete params.enterprise_id;
  delete params.business_view;
  config.params = { ...params, ...scope };
  config.headers = config.headers || {};
  delete config.headers['X-Enterprise-Id'];
  if (scope.enterprise_id) config.headers['X-Enterprise-Id'] = scope.enterprise_id;
  return config;
}

export function notificationUrl(path, scope = {}) {
  const query = new URLSearchParams(scope).toString();
  return query ? `${path}?${query}` : path;
}

export function isNotificationRequestCurrent(config, version, disposed = false) {
  return !disposed && config?.notificationScopeVersion === version;
}
