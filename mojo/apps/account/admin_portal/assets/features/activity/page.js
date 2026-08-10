import {api, apiEnvelope, badge, formatDate, h, pageHeader, statusTone, TableView} from '../../core.js';
import {openInspector} from '../../components/overlays.js';
import {emptyState, errorState, loadingState, sectionTabs} from '../../components/views.js';

const PAGE_SIZES = [10, 25, 50];
const QUERY_KEYS = new Set([
  'tab', 'search', 'start', 'size', 'sort', 'status', 'category', 'level', 'kind',
  'date_from', 'date_to', 'subject_type', 'subject_id',
  'subject_model',
]);
const SENSITIVE_KEY = /(password|secret|token|authorization|cookie|credential|api[_-]?key|private[_-]?key)/i;
const SUBJECT_TYPES = new Set(['incident', 'user', 'group', 'model']);

const MODELS = {
  incidents: {
    label: 'Incidents', endpoint: '/api/incident/incident', capability: 'view_security',
    date: 'created', sorts: ['-created', 'created', '-priority', 'priority', '-id'],
    filters: [{name: 'status', label: 'Status'}, {name: 'category', label: 'Category'}],
    columns: [
      {label: 'Incident', render: (row) => h('div', {}, h('strong', {text: row.title || `Incident ${row.id}`}), h('small', {text: row.category || 'uncategorized'}))},
      {label: 'Status', render: (row) => badge(row.status || 'unknown', statusTone(row.status))},
      {label: 'Priority', render: (row) => String(row.priority ?? '—')},
      {label: 'Created', render: (row) => formatDate(row.created)},
    ],
  },
  events: {
    label: 'Events', endpoint: '/api/incident/event', capability: 'view_security',
    date: 'created', sorts: ['-created', 'created', '-level', 'level', '-id'],
    filters: [{name: 'level', label: 'Level'}, {name: 'category', label: 'Category'}],
    columns: [
      {label: 'Event', render: (row) => h('div', {}, h('strong', {text: row.title || row.category || `Event ${row.id}`}), h('small', {text: row.scope || 'global'}))},
      {label: 'Level', render: (row) => badge(String(row.level ?? '—'), Number(row.level) > 7 ? 'danger' : Number(row.level) > 3 ? 'warning' : 'neutral')},
      {label: 'Source', render: (row) => row.source_ip || row.hostname || '—'},
      {label: 'Created', render: (row) => formatDate(row.created)},
    ],
  },
  logs: {
    label: 'Logs', endpoint: '/api/logs', capability: 'view_logs',
    date: 'created', sorts: ['-created', 'created', '-level', 'level', '-id'],
    filters: [{name: 'level', label: 'Level'}, {name: 'kind', label: 'Kind'}],
    columns: [
      {label: 'Log', render: (row) => h('div', {}, h('strong', {text: row.kind || row.method || `Log ${row.id}`}), h('small', {text: row.path || row.model_name || ''}))},
      {label: 'Level', render: (row) => badge(row.level || 'info', statusTone(row.level))},
      {label: 'Source', render: (row) => row.ip || row.username || '—'},
      {label: 'Created', render: (row) => formatDate(row.created)},
    ],
  },
  tickets: {
    label: 'Tickets', endpoint: '/api/incident/ticket', capability: 'view_security',
    date: 'modified', sorts: ['-modified', 'modified', '-created', 'created', '-priority', 'priority', '-id'],
    filters: [{name: 'status', label: 'Status'}, {name: 'category', label: 'Category'}],
    columns: [
      {label: 'Ticket', render: (row) => h('div', {}, h('strong', {text: row.title || `Ticket ${row.id}`}), h('small', {text: row.activity_group_label || row.category || 'uncategorized'}))},
      {label: 'Status', render: (row) => badge(row.status || 'unknown', statusTone(row.status))},
      {label: 'Assignee', render: (row) => row.activity_assignee_label || 'Unassigned'},
      {label: 'Modified', render: (row) => formatDate(row.modified)},
    ],
  },
};

function hashParams() {
  return new URLSearchParams(location.hash.split('?')[1] || '');
}

function normalizedState() {
  const params = hashParams();
  const unknown = [...params.keys()].filter((key) => !QUERY_KEYS.has(key));
  const tab = MODELS[params.get('tab')] ? params.get('tab') : 'incidents';
  const size = PAGE_SIZES.includes(Number(params.get('size'))) ? Number(params.get('size')) : 25;
  const model = MODELS[tab];
  const sort = model.sorts.includes(params.get('sort')) ? params.get('sort') : model.sorts[0];
  const subjectType = params.get('subject_type') || '';
  const subjectId = params.get('subject_id') || '';
  const subjectModel = params.get('subject_model') || '';
  let unsupported = unknown.length ? `Unsupported Activity parameter: ${unknown[0]}` : '';
  if ((subjectType || subjectId) && (!SUBJECT_TYPES.has(subjectType) || !/^\d+$/.test(subjectId))) {
    unsupported = 'Subject links require a supported subject_type and numeric subject_id.';
  }
  if (subjectType === 'model' && !/^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(subjectModel)) unsupported = 'Model subject links require a registered subject_model name.';
  if (subjectModel && subjectType !== 'model') unsupported = 'subject_model is valid only with subject_type=model.';
  return {
    tab, size, sort, unsupported, search: params.get('search') || '',
    start: Math.max(0, Number.parseInt(params.get('start') || '0', 10) || 0),
    status: params.get('status') || '', category: params.get('category') || '',
    level: params.get('level') || '', kind: params.get('kind') || '',
    date_from: params.get('date_from') || '', date_to: params.get('date_to') || '',
    subject_type: subjectType, subject_id: subjectId, subject_model: subjectModel,
  };
}

function writeState(state, {load = true} = {}) {
  const params = new URLSearchParams();
  Object.entries(state).forEach(([key, value]) => {
    if (QUERY_KEYS.has(key) && value !== '' && value != null && !(key === 'start' && Number(value) === 0)) params.set(key, value);
  });
  const target = `#/activity${params.size ? `?${params}` : ''}`;
  history.replaceState({}, '', target);
  state.unsupported = '';
  if (load) state.reload?.();
}

function subjectFilters(state, tab) {
  if (!state.subject_type) {
    if (state.subject_id || state.subject_model) throw new Error('Incomplete subject filters cannot be discarded.');
    return {};
  }
  const id = state.subject_id;
  const translations = {
    incident: {incidents: {id}, events: {incident: id}, tickets: {incident: id}, logs: {model_name: 'Incident', model_id: id}},
    user: {events: {uid: id}, tickets: {user: id}, logs: {uid: id}},
    group: {incidents: {group: id}, events: {group: id}, tickets: {group: id}, logs: {gid: id}},
  };
  let result = translations[state.subject_type]?.[tab];
  if (state.subject_type === 'model') {
    const recordModel = {incidents: 'Incident', events: 'Event', logs: 'Log', tickets: 'Ticket'}[tab];
    if (recordModel.toLowerCase() === state.subject_model.toLowerCase()) result = {id};
    else if (tab !== 'tickets') result = {model_name: state.subject_model, model_id: id};
  }
  if (!result) throw new Error(`${MODELS[tab].label} cannot represent this ${state.subject_type} subject without broadening the query.`);
  return result;
}

function listURL(state, {countOnly = false} = {}) {
  const model = MODELS[state.tab];
  const params = new URLSearchParams({graph: 'activity', start: String(countOnly ? 0 : state.start), size: String(countOnly ? 1 : state.size), sort: state.sort});
  if (state.search) params.set('search', state.search);
  for (const field of model.filters) if (state[field.name]) params.set(field.name, state[field.name]);
  if (state.date_from) params.set(`${model.date}__gte`, state.date_from);
  if (state.date_to) params.set(`${model.date}__lte`, state.date_to);
  Object.entries(subjectFilters(state, state.tab)).forEach(([key, value]) => params.set(key, value));
  return `${model.endpoint}?${params}`;
}

function activityEnvelope(envelope) {
  const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  return {
    ...envelope,
    count: number(envelope.raw?.count, envelope.count),
    start: number(envelope.raw?.start, envelope.start),
    size: number(envelope.raw?.size, envelope.size),
  };
}

function maskEvidence(value, depth = 0) {
  if (depth >= 5) return '[depth limit]';
  if (Array.isArray(value)) return value.slice(0, 40).map((item) => maskEvidence(item, depth + 1));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).slice(0, 80).map(([key, item]) => [key, SENSITIVE_KEY.test(key) ? '[redacted]' : maskEvidence(item, depth + 1)]));
  }
  if (typeof value === 'string' && value.length > 4000) return `${value.slice(0, 4000)}… [truncated]`;
  return value;
}

function parseEvidence(value) {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch (_) { return value; }
}

function evidenceBlock(label, value) {
  const safe = maskEvidence(parseEvidence(value));
  const text = typeof safe === 'string' ? safe : JSON.stringify(safe, null, 2);
  const copy = h('button', {class: 'button ghost compact', type: 'button', onclick: async (event) => {
    await navigator.clipboard?.writeText(text); event.currentTarget.textContent = 'Copied';
  }}, 'Copy evidence');
  return h('section', {class: 'activity-evidence'}, h('header', {}, h('h3', {text: label}), copy), h('pre', {text}));
}

function detailFields(row) {
  return Object.entries(row).filter(([key, value]) => !['metadata', 'payload', 'log'].includes(key) && value != null && typeof value !== 'object')
    .slice(0, 24).map(([key, value]) => h('div', {class: 'activity-field'}, h('dt', {text: key.replaceAll('_', ' ')}), h('dd', {text: key.includes('created') || key.includes('modified') ? formatDate(value) : String(value)})));
}

function knownReference(row) {
  const name = String(row.model_name || '').toLowerCase();
  const id = Number(row.model_id);
  const tab = {incident: 'incidents', event: 'events', ticket: 'tickets', log: 'logs'}[name];
  if (!tab || !Number.isInteger(id) || id < 1) return null;
  const model = encodeURIComponent(row.model_name);
  return h('a', {class: 'button ghost compact', href: `#/activity?tab=${tab}&subject_type=${name === 'incident' ? 'incident' : 'model'}&subject_id=${id}${name === 'incident' ? '' : `&subject_model=${model}`}`}, `Open ${name} ${id}`);
}

async function saveStatus(state, row, inspector, status, refresh) {
  const model = MODELS[state.tab];
  const message = inspector.panel.querySelector('.form-message');
  try {
    await api(`${model.endpoint}/${row.id}`, {method: 'PUT', body: JSON.stringify({status})});
    inspector.close(); await refresh();
  } catch (error) { message.textContent = error.message; }
}

function openRecord(state, row, ctx, refresh) {
  const content = h('div', {class: 'activity-inspector'}, h('dl', {class: 'activity-fields'}, ...detailFields(row)));
  const reference = knownReference(row); if (reference) content.append(reference);
  for (const key of ['metadata', 'payload', 'log']) if (row[key] != null && row[key] !== '') content.append(evidenceBlock(key, row[key]));
  const canManage = ctx.features.activity.capabilities.manage_security && ['incidents', 'tickets'].includes(state.tab);
  if (canManage) {
    const statuses = state.tab === 'incidents' ? ['new', 'open', 'paused', 'resolved', 'closed'] : ['new', 'open', 'paused', 'resolved', 'closed'];
    const select = h('select', {}, ...statuses.map((value) => h('option', {value, text: value, selected: row.status === value || null})));
    const message = h('div', {class: 'form-message', role: 'alert'});
    const save = h('button', {class: 'button primary compact', type: 'button'}, 'Save status');
    content.append(h('section', {class: 'activity-action'}, h('h3', {text: 'Lifecycle'}), h('p', {text: 'Uses the existing record save path so model history and audit hooks remain authoritative.'}), select, save, message));
    const inspector = openInspector({title: `${MODELS[state.tab].label.slice(0, -1)} ${row.id}`, subtitle: row.title || row.category || row.kind || '', content, wide: true});
    save.addEventListener('click', () => saveStatus(state, row, inspector, select.value, refresh));
    return;
  }
  openInspector({title: `${MODELS[state.tab].label.slice(0, -1)} ${row.id}`, subtitle: row.title || row.category || row.kind || '', content, wide: true});
}

function filtersView(state, reload) {
  const model = MODELS[state.tab];
  const search = h('input', {type: 'search', value: state.search, placeholder: `Search ${model.label.toLowerCase()}`, 'aria-label': `Search ${model.label}`});
  let debounce = null;
  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => { state.search = search.value.trim(); state.start = 0; writeState(state); }, 300); });
  const controls = [h('label', {class: 'field'}, h('span', {text: 'Search'}), search)];
  model.filters.forEach(({name, label}) => {
    const input = h('input', {value: state[name], placeholder: `Any ${label.toLowerCase()}`});
    input.addEventListener('change', () => { state[name] = input.value.trim(); state.start = 0; writeState(state); });
    controls.push(h('label', {class: 'field'}, h('span', {text: label}), input));
  });
  const sort = h('select', {}, ...model.sorts.map((value) => h('option', {value, text: value, selected: state.sort === value || null})));
  sort.addEventListener('change', () => { state.sort = sort.value; state.start = 0; writeState(state); });
  controls.push(h('label', {class: 'field'}, h('span', {text: 'Sort'}), sort));
  const dateFrom = h('input', {type: 'date', value: state.date_from}); const dateTo = h('input', {type: 'date', value: state.date_to});
  for (const [input, key] of [[dateFrom, 'date_from'], [dateTo, 'date_to']]) input.addEventListener('change', () => { state[key] = input.value; state.start = 0; writeState(state); });
  controls.push(h('label', {class: 'field'}, h('span', {text: 'From'}), dateFrom), h('label', {class: 'field'}, h('span', {text: 'To'}), dateTo));
  const node = h('section', {class: 'activity-filters'}, ...controls);
  node.dispose = () => clearTimeout(debounce);
  state.reload = reload;
  return node;
}

function pagination(state, envelope) {
  const page = Math.floor(state.start / state.size) + 1;
  const pages = Math.max(1, Math.ceil(envelope.count / state.size));
  const previous = h('button', {class: 'button ghost compact', disabled: state.start === 0 || null, onclick: () => { state.start = Math.max(0, state.start - state.size); writeState(state); }}, 'Previous');
  const next = h('button', {class: 'button ghost compact', disabled: state.start + state.size >= envelope.count || null, onclick: () => { state.start += state.size; writeState(state); }}, 'Next');
  const size = h('select', {'aria-label': 'Rows per page'}, ...PAGE_SIZES.map((value) => h('option', {value, text: `${value} rows`, selected: state.size === value || null})));
  size.addEventListener('change', () => { state.size = Number(size.value); state.start = 0; writeState(state); });
  return h('footer', {class: 'activity-pagination'}, h('span', {text: `${envelope.count} total · page ${page} of ${pages}`}), h('div', {}, size, previous, next));
}

function unavailableState(model, retry) {
  return h('div', {class: 'state-view activity-unavailable', role: 'status'},
    h('strong', {text: `${model.label} unavailable`}),
    h('p', {text: 'The source did not return authoritative data. No zero count is inferred.'}),
    h('button', {class: 'button compact', type: 'button', onclick: retry}, 'Retry'));
}

async function countFor(tab, ctx, signal) {
  const model = MODELS[tab];
  if (!ctx.features.activity.capabilities[model.capability]) return {label: 'Unavailable', tone: 'neutral'};
  const state = {...normalizedState(), tab, start: 0, search: '', status: '', category: '', level: '', kind: '', subject_type: '', subject_id: ''};
  try { const envelope = activityEnvelope(await apiEnvelope(listURL(state, {countOnly: true}), {signal})); return {label: String(envelope.count), tone: 'accent'}; }
  catch (_) { return {label: 'Unavailable', tone: 'warning'}; }
}

export async function activityPage(ctx, parentSignal) {
  const state = normalizedState(); let controller = null; let disposed = false; let filters = null;
  const abortFromParent = () => controller?.abort();
  parentSignal?.addEventListener('abort', abortFromParent, {once: true});
  if (!hashParams().has('tab') && !ctx.features.activity.capabilities.view_security && ctx.features.activity.capabilities.view_logs) {
    state.tab = 'logs'; state.sort = MODELS.logs.sorts[0];
  }
  const body = h('section', {class: 'panel activity-stream'}, loadingState('Loading activity…'));
  const summary = h('section', {class: 'activity-summary'}, ...Object.values(MODELS).map((model) => h('article', {class: 'kpi'}, h('span', {text: model.label}), h('strong', {text: '…'}))));
  let tabs;
  tabs = sectionTabs({items: Object.entries(MODELS).map(([id, model]) => ({id, label: model.label})), active: state.tab, label: 'Activity types', onChange: (tab) => {
    state.tab = tab; state.start = 0; state.sort = MODELS[tab].sorts[0]; state.search = '';
    state.status = ''; state.category = ''; state.level = ''; state.kind = '';
    [...tabs.querySelectorAll('button')].forEach((button, index) => {
      const active = Object.keys(MODELS)[index] === tab;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    writeState(state);
  }});
  const page = h('div', {class: 'page'}, pageHeader('Operations', 'Activity', 'Search and inspect bounded system evidence without bypassing source permissions.'), summary, tabs, body);

  const refresh = async () => {
    if (disposed) return;
    controller?.abort(); const activeController = new AbortController(); controller = activeController;
    if (parentSignal?.aborted) activeController.abort();
    if (state.unsupported) { body.replaceChildren(emptyState('Unsupported Activity link', state.unsupported)); return; }
    const model = MODELS[state.tab];
    if (!ctx.features.activity.capabilities[model.capability]) { body.replaceChildren(emptyState('Access denied', `${model.label} require ${model.capability}.`)); return; }
    body.replaceChildren(loadingState(`Loading ${model.label.toLowerCase()}…`));
    let envelope;
    try {
      let url;
      try { url = listURL(state); } catch (error) { body.replaceChildren(emptyState('Unsupported subject filter', error.message)); return; }
      try { envelope = activityEnvelope(await apiEnvelope(url, {signal: activeController.signal})); }
      catch (_) { if (!activeController.signal.aborted && controller === activeController) body.replaceChildren(unavailableState(model, refresh)); return; }
      if (activeController.signal.aborted || controller !== activeController || disposed) return;
      if (envelope.count > 0 && state.start >= envelope.count) {
        state.start = Math.floor((envelope.count - 1) / state.size) * state.size; writeState(state); return;
      }
      if (envelope.count === 0 && state.start !== 0) { state.start = 0; writeState(state); return; }
      filters?.dispose?.(); filters = filtersView(state, refresh);
      const table = new TableView({columns: model.columns, rows: envelope.items, empty: `No ${model.label.toLowerCase()} match this query.`, onSelect: (row) => openRecord(state, row, ctx, refresh)}).render();
      body.replaceChildren(filters, table, pagination(state, envelope));
    } catch (error) { if (!activeController.signal.aborted && controller === activeController) body.replaceChildren(errorState(error, refresh)); }
  };
  state.reload = refresh;
  Promise.all(Object.keys(MODELS).map((tab) => countFor(tab, ctx, parentSignal))).then((counts) => {
    if (disposed || parentSignal?.aborted) return;
    [...summary.children].forEach((card, index) => { card.querySelector('strong').textContent = counts[index].label; card.dataset.tone = counts[index].tone; });
  });
  await refresh();
  page.dispose = () => { disposed = true; controller?.abort(); filters?.dispose?.(); parentSignal?.removeEventListener('abort', abortFromParent); };
  return page;
}
