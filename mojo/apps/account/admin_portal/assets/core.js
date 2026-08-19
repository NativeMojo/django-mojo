import {clearBusy, openModal} from './components/overlays.js';
import {RelationshipSelect} from './components/relationship.js';

export {openModal};

const SVG = {
  home: '<path d="M3 10.7 12 3l9 7.7v9.1a1.2 1.2 0 0 1-1.2 1.2H4.2A1.2 1.2 0 0 1 3 19.8Z"/><path d="M9 21v-7h6v7"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  deploy: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m8 9 3 3-3 3m5 0h3"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8-8m-3 3 3 3m-6 0 3 3"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  alert: '<path d="M10.3 3.7 2.2 18a2 2 0 0 0 1.7 3h16.2a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 4h.01"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  dns: '<circle cx="12" cy="12" r="2"/><circle cx="12" cy="12" r="7"/><path d="M12 3V1m0 22v-2M3 12H1m22 0h-2"/>',
  certificate: '<path d="M7 3h10v7a5 5 0 0 1-10 0Z"/><path d="M9 15v6l3-2 3 2v-6"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
  route: '<path d="M6 3v6a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v6"/><path d="m14 17 4 4 4-4M2 7l4-4 4 4"/>',
  refresh: '<path d="M20 11a8 8 0 1 0 1 5"/><path d="M20 4v7h-7"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6"/>',
  activity: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
  chart: '<path d="M4 3v16a2 2 0 0 0 2 2h15"/><path d="m8 15 3.5-4.5 3 2.2L20 6"/>',
  phone: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
};

export function icon(name, label = '') {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  node.setAttribute('viewBox', '0 0 24 24');
  node.setAttribute('class', 'icon');
  node.setAttribute('fill', 'none');
  node.setAttribute('stroke', 'currentColor');
  node.setAttribute('stroke-width', '1.8');
  if (label) node.setAttribute('aria-label', label); else node.setAttribute('aria-hidden', 'true');
  node.innerHTML = SVG[name] || SVG.settings;
  return node;
}

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value ?? '';
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'checked') node.checked = Boolean(value);
    else if (key === 'value') node.value = value ?? '';
    else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : String(value));
  });
  children.flat().filter((item) => item != null).forEach((item) => {
    node.append(item instanceof Node ? item : document.createTextNode(String(item)));
  });
  return node;
}

function authHeader() { return window.MojoAuth?.getAuthHeader?.(); }

let freshAuthAttempt = null;

export class FreshAuthRequired extends Error {
  constructor(path) {
    super('Recent authentication is required to continue.');
    this.name = 'FreshAuthRequired'; this.code = 'fresh_auth_required'; this.path = path;
  }
}

function requestFreshAuth(error) {
  if (freshAuthAttempt) return freshAuthAttempt;
  freshAuthAttempt = new Promise((resolve, reject) => {
    const detail = {error, resolve, reject, handled: false};
    window.dispatchEvent(new CustomEvent('mojo-admin:fresh-auth', {detail}));
    if (!detail.handled) reject(error);
  }).finally(() => { freshAuthAttempt = null; });
  return freshAuthAttempt;
}

async function renewSourceSession() {
  const header = authHeader();
  if (!header?.startsWith('Bearer ')) return false;
  const response = await fetch('/api/account/admin/session', {
    method: 'POST', headers: {Authorization: header, 'Content-Type': 'application/json'}, body: '{}',
  });
  return response.ok;
}

async function requestPayload(path, options = {}, retry = true, freshRetry = true) {
  const headers = new Headers(options.headers || {});
  const authorization = authHeader();
  if (authorization) headers.set('Authorization', authorization);
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  let response = await fetch(path, {...options, headers});
  if (response.status === 401 && retry && window.MojoAuth?.getRefreshToken?.()) {
    await window.MojoAuth.refreshToken();
    await renewSourceSession();
    return requestPayload(path, options, false, freshRetry);
  }
  if (response.status === 440) {
    const error = new FreshAuthRequired(path);
    clearBusy();
    if (!freshRetry) throw error;
    await requestFreshAuth(error);
    return requestPayload(path, options, retry, false);
  }
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok || payload.status === false) throw new Error(payload.error || payload.reason || `Request failed (${response.status})`);
  return payload;
}

export async function api(path, options = {}, retry = true) {
  const payload = await requestPayload(path, options, retry);
  return payload.data ?? payload;
}

export function responseEnvelope(payload) {
  const data = payload && Object.prototype.hasOwnProperty.call(payload, 'data') ? payload.data : payload;
  const object = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
  const items = Array.isArray(data) ? data
    : Array.isArray(object.results) ? object.results
      : Array.isArray(object.items) ? object.items
        : Array.isArray(object.data) ? object.data : [];
  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  return {
    data,
    results: Array.isArray(object.results) ? object.results : items,
    items,
    count: number(object.count, items.length),
    start: number(object.start, 0),
    size: number(object.size, items.length),
    raw: payload,
  };
}

export async function apiEnvelope(path, options = {}, retry = true) {
  return responseEnvelope(await requestPayload(path, options, retry));
}

// Mutations whose provider outcome may be ambiguous deliberately receive no
// transport retry. Their caller must reconcile authoritative state instead.
export function apiOnce(path, options = {}) { return api(path, options, false); }

export function listData(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.results || payload?.items || payload?.data || [];
}

export function formatDate(value) {
  if (value == null || value === '' || value === false) return 'Never';
  let input = value;
  const text = typeof value === 'string' ? value.trim() : '';
  const numeric = typeof value === 'number' ? value : /^-?\d+(?:\.\d+)?$/.test(text) ? Number(text) : null;
  if (numeric != null && Number.isFinite(numeric)) input = Math.abs(numeric) < 100000000000 ? numeric * 1000 : numeric;
  const date = new Date(input);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
}

export function statusTone(status) {
  const value = String(status || '').toLowerCase();
  if (['pass', 'passed', 'active', 'succeeded', 'completed', 'verified', 'ready', 'applied', 'live'].includes(value)) return 'success';
  if (['fail', 'failed', 'inactive', 'error', 'revoked', 'expired'].includes(value)) return 'danger';
  if (['warn', 'warning', 'waiting_for_choice', 'registering', 'issuing', 'partial'].includes(value)) return 'warning';
  return 'neutral';
}

export class TableView {
  constructor({columns, rows = [], empty = 'No records found.', onSelect}) {
    this.columns = columns; this.rows = rows; this.empty = empty; this.onSelect = onSelect;
  }
  render() {
    if (!this.rows.length) return h('div', {class: 'empty'}, h('p', {text: this.empty}));
    const head = h('tr', {}, ...this.columns.map((c) => h('th', {scope: 'col', text: c.label})));
    const body = this.rows.map((row) => h('tr', {tabindex: this.onSelect ? '0' : null,
      onclick: () => this.onSelect?.(row), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.onSelect?.(row); } }},
      ...this.columns.map((c) => h('td', {}, c.render ? c.render(row) : String(row[c.key] ?? '—')))));
    const labels = this.columns.map((column) => column.label).filter(Boolean).join(', ');
    return h('div', {class: 'table-wrap', tabindex: '0', role: 'region',
      'aria-label': `${labels || 'Data'} table`, onkeydown: (event) => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        const scroller = event.currentTarget;
        const maximum = scroller.scrollWidth - scroller.clientWidth;
        if (maximum <= 0) return;
        event.preventDefault();
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        const step = Math.max(48, Math.floor(scroller.clientWidth * 0.35));
        scroller.scrollLeft = Math.max(0, Math.min(maximum, scroller.scrollLeft + direction * step));
      }},
    h('table', {}, h('thead', {}, head), h('tbody', {}, ...body)));
  }
}

function fieldControl(field, value) {
  if (field.type === 'relationship') {
    const relationship = new RelationshipSelect({
      ...(field.relationship || {}), name: field.name, label: field.label,
      required: field.required, value,
    });
    return {node: relationship.node, input: relationship.hidden,
      getValue: () => relationship.getValue(), dispose: () => relationship.dispose()};
  }
  if (field.type === 'select') {
    const select = h('select', {id: `field-${field.name}`, name: field.name, required: field.required || null});
    if (field.placeholder) select.append(h('option', {value: '', text: field.placeholder}));
    (field.options || []).forEach((option) => select.append(h('option', {
      value: option.value, text: option.label, selected: String(option.value) === String(value) || null,
    })));
    select.value = value ?? '';
    return {node: select, input: select};
  }
  if (field.type === 'textarea') {
    const textarea = h('textarea', {
    id: `field-${field.name}`, name: field.name, rows: field.rows || 4,
    required: field.required || null, placeholder: field.placeholder || null, text: value ?? '',
    });
    return {node: textarea, input: textarea};
  }
  const input = h('input', {
    id: `field-${field.name}`, name: field.name, type: field.type || 'text',
    value: field.type === 'checkbox' ? null : (value ?? ''), checked: field.type === 'checkbox' && value,
    required: field.required || null, autocomplete: field.autocomplete || null,
    placeholder: field.placeholder || null, min: field.min ?? null, max: field.max ?? null,
  });
  return {node: input, input};
}

export class FormView {
  constructor({fields, value = {}, submitLabel = 'Save', onSubmit}) {
    this.fields = fields; this.value = value; this.submitLabel = submitLabel; this.onSubmit = onSubmit;
  }
  render() {
    const message = h('div', {class: 'form-message', role: 'alert'});
    const inputs = {};
    const fields = this.fields.map((field) => {
      const control = fieldControl(field, this.value[field.name]);
      const input = control.input;
      inputs[field.name] = control;
      return h('label', {class: field.type === 'checkbox' ? 'check-field' : 'field'},
        field.type === 'checkbox' ? control.node : h('span', {text: field.label}),
        field.type === 'checkbox' ? h('span', {text: field.label}) : control.node,
        field.help ? h('small', {text: field.help}) : null);
    });
    const button = h('button', {class: 'button primary', type: 'submit'}, icon('check'), this.submitLabel);
    const form = h('form', {onsubmit: async (event) => {
      event.preventDefault(); button.disabled = true; message.textContent = '';
      const data = {};
      Object.entries(inputs).forEach(([name, control]) => {
        data[name] = control.getValue ? control.getValue()
          : control.input.type === 'checkbox' ? control.input.checked : control.input.value;
      });
      try {
        await this.onSubmit(data, Object.fromEntries(Object.entries(inputs).map(([name, control]) => [name, control.input])));
      } catch (error) { message.textContent = error.message; button.disabled = false; }
    }}, ...fields, message, h('div', {class: 'form-actions'}, button));
    form.dispose = () => Object.values(inputs).forEach((control) => control.dispose?.());
    return form;
  }
}

export function badge(text, tone = 'neutral') { return h('span', {class: `badge ${tone}`, text}); }

export function pageHeader(eyebrow, title, copy, actions = []) {
  return h('header', {class: 'page-header'}, h('div', {}, h('div', {class: 'eyebrow', text: eyebrow}), h('h1', {text: title, tabindex: '-1'}), h('p', {text: copy})), h('div', {class: 'page-actions'}, ...actions));
}

export function stopRow(event) { event.stopPropagation(); }
