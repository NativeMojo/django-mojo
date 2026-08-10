const ROUTE_KEYS = new Set([
  'tab', 'search', 'start', 'size', 'sort', 'status', 'category', 'level', 'kind',
  'date_from', 'date_to', 'subject_type', 'subject_id', 'subject_model',
  'inspector', 'return', 'domain', 'vhost', 'webapp', 'deployment',
]);

function safeRoute(value) {
  const route = String(value || '').replace(/^#\/?/, '').split(/[?\/]/)[0];
  return /^[a-z][a-z0-9-]{0,63}$/.test(route) ? route : 'dashboard';
}

export function decodeRouteState(value = location.hash) {
  const raw = String(value || '').replace(/^#\/?/, '');
  const [routePart, query = ''] = raw.split('?', 2);
  const params = new URLSearchParams(query);
  const state = {}; const unknown = [];
  for (const [key, item] of params) {
    if (ROUTE_KEYS.has(key)) state[key] = item;
    else unknown.push(key);
  }
  return {route: safeRoute(routePart), state, unknown};
}

export function routeHref(route, state = {}) {
  const params = new URLSearchParams();
  Object.keys(state).sort().forEach((key) => {
    const value = state[key];
    if (ROUTE_KEYS.has(key) && value != null && value !== ''
        && !(key === 'start' && Number(value) === 0)) params.set(key, String(value));
  });
  const query = params.toString();
  return `#/${safeRoute(route)}${query ? `?${query}` : ''}`;
}

export function returnLocation(value = location.hash) {
  const decoded = decodeRouteState(value);
  delete decoded.state.return;
  const safe = routeHref(decoded.route, decoded.state);
  return safe.length <= 500 ? safe : `#/${decoded.route}`;
}

export function restoreReturnLocation(value) {
  if (typeof value !== 'string' || value.length > 500 || !value.startsWith('#/')) return null;
  const decoded = decodeRouteState(value);
  return routeHref(decoded.route, decoded.state);
}

export function activityHref(tab, subject = {}, options = {}) {
  const state = {tab, size: 25, sort: '-created', ...options};
  if (subject.type) state.subject_type = subject.type;
  if (subject.id != null) state.subject_id = subject.id;
  if (subject.model) state.subject_model = subject.model;
  return routeHref('activity', state);
}

export const routeStateKeys = ROUTE_KEYS;
