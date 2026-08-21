// Activity & logs — everything the platform recorded, in the four views v1
// already had: incidents, security events, tickets, and the audit log.
//
// This is v1's features/activity/page.js ported whole. The query machinery is
// unchanged on purpose: the same QUERY_KEYS, the same subject translations, the
// same size-bounded evidence blocks, the same inspector, the same refusal to
// infer a zero count from a source that did not answer. Anything an operator
// can reach in the current Admin they can reach here, at the same address
// shape, and a link one of them wrote still works in the other.
//
// What changed is chrome and gating:
//   * it is a SUB-PAGE of Home now, so it wears the back pill and Home's
//     eyebrow instead of a sidebar entry of its own;
//   * a tab whose capability this caller does not hold is HIDDEN rather than
//     rendered-and-refused, and a link naming a tab they cannot read is
//     corrected in the address bar the way Infrastructure corrects its tabs;
//   * a record that lives on a screen v2 has not built yet links into the
//     current Admin, and says so out loud.

import {api, apiEnvelope, badge, formatDate, h, icon, statusTone, TableView} from '../../core.js';
import {backPill} from '../../app.js';
import {copyButton, runAction} from '../../components/actions.js';
import {openInspector} from '../../components/overlays.js';
import {decodeRouteState, restoreReturnLocation, returnLocation, routeHref} from '../../components/routes.js';
import {emptyState, errorState, loadingState, sectionTabs} from '../../components/views.js';

const PAGE_SIZES = [10, 25, 50];
const SORT_LABELS = {
  '-created': 'Newest first', created: 'Oldest first',
  '-modified': 'Recently updated', modified: 'Least recently updated',
  '-priority': 'Highest priority', priority: 'Lowest priority',
  '-level': 'Highest level', level: 'Lowest level', '-id': 'Newest record',
};
const QUERY_KEYS = new Set([
  'tab', 'search', 'start', 'size', 'sort', 'status', 'category', 'level', 'kind',
  'date_from', 'date_to', 'subject_type', 'subject_id',
  'subject_model', 'inspector', 'return',
]);
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
};

// Tab order is the order of the mockup and of v1's tab bar: Incidents · Events
// · Tickets · Logs. MODELS is keyed for lookup, this names the display order.
const TAB_ORDER = ['incidents', 'events', 'tickets', 'logs'];

/** Does this caller hold the capability the named tab's source requires? */
export function activityTabVisible(ctx, tab) {
  if (ctx?.features?.activity?.enabled !== true) return false;
  const model = MODELS[tab];
  if (!model) return false;
  return ctx.features.activity.capabilities?.[model.capability] === true;
}

/** The tabs this caller may actually read, in display order. */
function visibleTabs(ctx) {
  return TAB_ORDER.filter((tab) => activityTabVisible(ctx, tab));
}

/**
 * The Activity surface opens only when the bootstrap payload carries the block
 * AND the caller can read at least one of its four sources. A page with four
 * hidden tabs is not a page.
 */
export function activityAvailable(ctx) {
  return visibleTabs(ctx).length > 0;
}

function hashParams() {
  return new URLSearchParams(decodeRouteState().state);
}

function normalizedState() {
  const params = hashParams();
  const unknown = [...decodeRouteState().unknown,
    ...[...params.keys()].filter((key) => !QUERY_KEYS.has(key))];
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
    inspector: params.get('inspector') || '', return: params.get('return') || '',
  };
}

function writeState(state, {load = true} = {}) {
  const params = new URLSearchParams();
  Object.entries(state).forEach(([key, value]) => {
    if (QUERY_KEYS.has(key) && value !== '' && value != null && !(key === 'start' && Number(value) === 0)) params.set(key, value);
  });
  const target = routeHref('activity', Object.fromEntries(params));
  history.replaceState({}, '', target);
  state.unsupported = '';
  // Returned, not fired and forgotten: every caller below is a control whose
  // affordance has to last as long as the reload it started.
  return load ? state.reload?.() : undefined;
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

// Size-bounding only: the API already serves admins the raw metadata, so these
// caps exist to keep a pathological blob from hanging the renderer — never to
// withhold evidence. Values render exactly as stored; every cap must be far
// beyond real data, and every truncation must announce itself.
function boundEvidence(value, depth = 0) {
  if (depth >= 1000) return '[depth limit]';
  if (Array.isArray(value)) {
    const bounded = value.slice(0, 500).map((item) => boundEvidence(item, depth + 1));
    if (value.length > bounded.length) bounded.push(`[+${value.length - bounded.length} more items]`);
    return bounded;
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value);
    const bounded = Object.fromEntries(entries.slice(0, 500).map(([key, item]) => [key, boundEvidence(item, depth + 1)]));
    if (entries.length > 500) bounded['[truncated]'] = `+${entries.length - 500} more keys`;
    return bounded;
  }
  if (typeof value === 'string' && value.length > 4000) return `${value.slice(0, 4000)}… [truncated]`;
  return value;
}

function parseEvidence(value) {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch (_) { return value; }
}

function evidenceBlock(label, value) {
  const parsed = parseEvidence(value);
  const display = boundEvidence(parsed);
  const text = typeof display === 'string' ? display : JSON.stringify(display, null, 2);
  // Copy hands over the stored value exactly — investigations must never
  // inherit the display bounds.
  const raw = typeof parsed === 'string' ? parsed : JSON.stringify(parsed, null, 2);
  // The shared clipboard affordance: a non-secure context rejects writeText,
  // which the hand-rolled version swallowed silently.
  const copy = copyButton(raw, {label: 'Copy evidence', copiedLabel: 'Copied'});
  return h('section', {class: 'activity-evidence'}, h('header', {}, h('h3', {text: label}), copy), h('pre', {text}));
}

function detailFields(row) {
  return Object.entries(row).filter(([key, value]) => !['metadata', 'payload', 'log'].includes(key) && value != null && typeof value !== 'object')
    .slice(0, 24).map(([key, value]) => h('div', {class: 'activity-field'}, h('dt', {text: key.replaceAll('_', ' ')}), h('dd', {text: key.includes('created') || key.includes('modified') ? formatDate(value) : String(value)})));
}

// The whole record, exactly as the source returned it. v1 offered this on the
// evidence blocks only; the inspector-level copy is the same promise applied to
// the row — nothing bounded, nothing reformatted, nothing dropped.
function copyRecordButton(row) {
  return copyButton(JSON.stringify(row, null, 2),
    {label: 'Copy as JSON', copiedLabel: 'Copied'});
}

/**
 * A record this row names, if the portal has somewhere to open it.
 *
 * Incidents, events, tickets and logs are other tabs of this very page, so
 * those links stay in v2. So does a WebApp: Apps is built, and the app's own
 * page takes `?webapp=<id>` — the link carries this view's location in
 * `?return=` so the app page can offer the way back. Users, groups and domains
 * are screens v2 has not built yet; those open in the current Admin, and the
 * button says so rather than dropping the operator into different chrome
 * unannounced.
 */
// Whether v2's Apps destination is open to this caller. Same predicate as
// features/apps/feature.js — stated here rather than imported, because Home's
// page.js already imports this file and a second import back would make the two
// modules circular for one boolean.
function appsEnabled(ctx) {
  return ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true;
}

function knownReference(ctx, row) {
  const name = String(row.model_name || '').toLowerCase();
  const rawId = String(row.model_id || '');
  if (name === 'webapp' && /^\d+$/.test(rawId) && appsEnabled(ctx)) {
    return h('div', {class: 'activity-reference'},
      h('a', {class: 'button ghost compact', href: routeHref('apps', {
        webapp: rawId, return: returnLocation(),
      })}, `Open WebApp ${rawId}`));
  }
  const destinations = {
    user: ['users', 'User'], group: ['groups', 'Group'], webapp: ['deployments', 'WebApp'],
    domain: ['domains', 'Domain'], platformdeployment: ['deployments', 'Deployment'],
  };
  if (destinations[name] && /^[A-Za-z0-9._:-]{1,80}$/.test(rawId)) {
    const [route, label] = destinations[name];
    const href = `${ctx.admin_path || '/admin/'}#/${route}?inspector=${encodeURIComponent(rawId)}`;
    return h('div', {class: 'activity-reference'},
      h('a', {class: 'button ghost compact', href}, `Open ${label} ${rawId}`),
      h('small', {text: 'opens the current Admin'}));
  }
  const id = Number(rawId);
  const tab = {incident: 'incidents', event: 'events', ticket: 'tickets', log: 'logs'}[name];
  if (!tab || !Number.isInteger(id) || id < 1) return null;
  if (!activityTabVisible(ctx, tab)) return null;
  return h('div', {class: 'activity-reference'}, h('a', {class: 'button ghost compact', href: routeHref('activity', {
    tab, subject_type: name === 'incident' ? 'incident' : 'model', subject_id: id,
    subject_model: name === 'incident' ? '' : row.model_name,
  })}, `Open ${name} ${id}`));
}

// The Save button lives in the inspector this closes on success, so the
// pending state rides the button for the wait and is never restored onto a
// panel that has gone.
function saveStatus(state, row, inspector, button, status, refresh) {
  const model = MODELS[state.tab];
  const message = inspector.panel.querySelector('.form-message');
  return runAction(button, async () => {
    message.textContent = '';
    await api(`${model.endpoint}/${row.id}`, {method: 'PUT', body: JSON.stringify({status})});
    inspector.close(); await refresh();
  }, {
    pendingLabel: 'Saving…', restoreOnSuccess: false,
    onError: (error) => { message.textContent = error.message; },
  });
}

function openRecord(state, row, ctx, refresh) {
  const content = h('div', {class: 'activity-inspector'},
    h('div', {class: 'activity-inspector-actions'}, copyRecordButton(row)),
    h('dl', {class: 'activity-fields'}, ...detailFields(row)));
  const reference = knownReference(ctx, row); if (reference) content.append(reference);
  for (const key of ['metadata', 'payload', 'log']) if (row[key] != null && row[key] !== '') content.append(evidenceBlock(key, row[key]));
  const canManage = ctx.features.activity.capabilities.manage_security && ['incidents', 'tickets'].includes(state.tab);
  if (canManage) {
    const statuses = ['new', 'open', 'paused', 'resolved', 'closed'];
    const select = h('select', {}, ...statuses.map((value) => h('option', {value, text: value, selected: row.status === value || null})));
    const message = h('div', {class: 'form-message', role: 'alert'});
    const save = h('button', {class: 'button primary compact', type: 'button'}, 'Save status');
    content.append(h('section', {class: 'activity-action'}, h('h3', {text: 'Lifecycle'}), h('p', {text: 'Uses the existing record save path so model history and audit hooks remain authoritative.'}), select, save, message));
    const inspector = openInspector({title: `${MODELS[state.tab].label.slice(0, -1)} ${row.id}`, subtitle: row.title || row.category || row.kind || '', content, wide: true});
    save.addEventListener('click', () => saveStatus(state, row, inspector, save, select.value, refresh));
    return;
  }
  openInspector({title: `${MODELS[state.tab].label.slice(0, -1)} ${row.id}`, subtitle: row.title || row.category || row.kind || '', content, wide: true});
}

function filtersView(state, reload) {
  const model = MODELS[state.tab];
  const search = h('input', {type: 'search', value: state.search, placeholder: `Search ${model.label.toLowerCase()}`, 'aria-label': `Search ${model.label}`});
  let debounce = null;
  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => { state.search = search.value.trim(); state.start = 0; writeState(state); }, 300); });
  const controls = [];
  model.filters.forEach(({name, label}) => {
    const input = h('input', {value: state[name], placeholder: `Any ${label.toLowerCase()}`});
    input.addEventListener('change', () => { state[name] = input.value.trim(); state.start = 0; writeState(state); });
    controls.push(h('label', {class: 'field'}, h('span', {text: label}), input));
  });
  const sort = h('select', {}, ...model.sorts.map((value) => h('option', {value, text: SORT_LABELS[value] || value, selected: state.sort === value || null})));
  sort.addEventListener('change', () => { state.sort = sort.value; state.start = 0; writeState(state); });
  controls.push(h('label', {class: 'field'}, h('span', {text: 'Sort'}), sort));
  const dateFrom = h('input', {type: 'date', value: state.date_from}); const dateTo = h('input', {type: 'date', value: state.date_to});
  for (const [input, key] of [[dateFrom, 'date_from'], [dateTo, 'date_to']]) input.addEventListener('change', () => { state[key] = input.value; state.start = 0; writeState(state); });
  controls.push(h('label', {class: 'field'}, h('span', {text: 'From'}), dateFrom), h('label', {class: 'field'}, h('span', {text: 'To'}), dateTo));
  const advancedKeys = [...model.filters.map(({name}) => name), 'date_from', 'date_to'];
  const activeAdvanced = advancedKeys.filter((key) => state[key]).length + (state.sort === model.sorts[0] ? 0 : 1);
  const hasQuery = Boolean(state.search || activeAdvanced);
  if (state.filters_open == null) state.filters_open = activeAdvanced > 0;
  const panelId = `activity-filter-panel-${state.tab}`;
  const panel = h('div', {class: 'activity-filter-panel', id: panelId, hidden: !state.filters_open || null}, ...controls);
  const toggle = h('button', {
    class: 'button ghost compact activity-filter-toggle', type: 'button',
    'aria-controls': panelId, 'aria-expanded': state.filters_open ? 'true' : 'false',
  }, 'Filters', activeAdvanced ? h('span', {class: 'activity-filter-count', text: activeAdvanced}) : null);
  toggle.addEventListener('click', () => {
    state.filters_open = !state.filters_open;
    panel.hidden = !state.filters_open;
    toggle.setAttribute('aria-expanded', String(state.filters_open));
  });
  const clear = hasQuery ? h('button', {class: 'button ghost compact activity-filter-clear', type: 'button', onclick: () => {
    clearTimeout(debounce); state.search = ''; state.start = 0; state.sort = model.sorts[0];
    for (const key of advancedKeys) state[key] = '';
    state.filters_open = false; writeState(state);
  }}, 'Clear') : null;
  const toolbar = h('div', {class: 'activity-toolbar'}, h('label', {class: 'search activity-search'}, icon('search'), search), toggle, clear);
  const node = h('section', {class: 'activity-query'}, toolbar, panel);
  node.dispose = () => clearTimeout(debounce);
  state.reload = reload;
  return node;
}

// A subject filter is a claim about what you are looking at, so it is said in
// words above the table with the one control that drops it. v1 carried it only
// in the address bar, where a narrowed table looked like an empty one.
function subjectChip(state) {
  if (!state.subject_type) return null;
  const label = state.subject_type === 'model'
    ? `${state.subject_model} ${state.subject_id}`
    : `${state.subject_type} ${state.subject_id}`;
  return h('div', {class: 'activity-subject', role: 'status'},
    h('span', {class: 'badge accent', text: 'Filtered'}),
    h('span', {text: `Showing only what names ${label}.`}),
    h('button', {class: 'button ghost compact', type: 'button', onclick: () => {
      state.subject_type = ''; state.subject_id = ''; state.subject_model = '';
      state.start = 0; writeState(state);
    }}, 'Show everything'));
}

function pagination(state, envelope) {
  const page = Math.floor(state.start / state.size) + 1;
  const pages = Math.max(1, Math.ceil(envelope.count / state.size));
  // † Every one of these controls lives inside the stream body that refresh()
  // replaces with a loading state on its very first synchronous line, so the
  // affordance is that repaint, never a pending state pinned here. What the
  // wrapper adds is the guard: one page turn per click, and the promise is
  // awaited rather than dropped. They share a key because they all mean
  // "re-run this query" — a second one landing mid-flight would fight the first.
  const turn = (mutate) => runAction(null, () => { mutate(); return writeState(state); },
    {key: 'activity-query'});
  const previous = h('button', {class: 'button ghost compact', disabled: state.start === 0 || null, onclick: () => turn(() => { state.start = Math.max(0, state.start - state.size); })}, 'Previous');
  const next = h('button', {class: 'button ghost compact', disabled: state.start + state.size >= envelope.count || null, onclick: () => turn(() => { state.start += state.size; })}, 'Next');
  const size = h('select', {'aria-label': 'Rows per page'}, ...PAGE_SIZES.map((value) => h('option', {value, text: `${value} rows`, selected: state.size === value || null})));
  size.addEventListener('change', () => turn(() => { state.size = Number(size.value); state.start = 0; }));
  return h('footer', {class: 'activity-pagination'},
    h('span', {text: `${envelope.count} total · page ${page} of ${pages} · any row opens the full record`}),
    h('div', {}, size, previous, next));
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
  const tabs = visibleTabs(ctx);
  const state = normalizedState(); let controller = null; let disposed = false; let filters = null; let linkedInspectorOpened = false;
  const abortFromParent = () => controller?.abort();
  parentSignal?.addEventListener('abort', abortFromParent, {once: true});
  // v1's default-tab rule, generalised to the tabs this caller can see: an
  // address that names no tab, or names one they cannot read, lands on the
  // first tab they can — and the address bar is corrected to say so, so the URL
  // and the screen never disagree about which view this is.
  const requested = hashParams().get('tab') || '';
  if (!tabs.includes(state.tab)) {
    state.tab = tabs[0];
    state.sort = MODELS[state.tab].sorts[0];
    state.start = 0;
    if (requested) writeState(state, {load: false});
  }
  const body = h('section', {class: 'panel activity-stream'}, loadingState('Loading activity…'));
  const summary = h('section', {class: 'activity-summary'}, ...tabs.map((tab) => h('article', {class: 'activity-count'}, h('span', {text: MODELS[tab].label}), h('strong', {text: '…'}))));
  let tabBar;
  tabBar = sectionTabs({items: tabs.map((id) => ({id, label: MODELS[id].label})), active: state.tab, label: 'Activity types', onChange: (tab) => {
    state.tab = tab; state.start = 0; state.sort = MODELS[tab].sorts[0]; state.search = '';
    state.status = ''; state.category = ''; state.level = ''; state.kind = '';
    [...tabBar.querySelectorAll('button')].forEach((button, index) => {
      const active = tabs[index] === tab;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    // Returned so sectionTabs' pending state on the clicked tab lasts as long
    // as the reload it started. The tab bar itself is built once and lives
    // outside the body refresh() repaints, so the tab survives its own action.
    return writeState(state);
  }});
  const returnHref = restoreReturnLocation(state.return);
  const page = h('div', {class: 'page activity-page'},
    backPill('Home', 'home'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Home · Activity'}),
        h('h1', {text: 'Activity & logs', tabindex: '-1'}),
        h('p', {text: 'Everything the platform recorded — incidents, security '
          + 'events, tickets, and the audit log. Search and inspect bounded '
          + 'system evidence without bypassing source permissions.'})),
      h('div', {class: 'page-actions'},
        returnHref ? h('a', {class: 'button ghost', href: returnHref}, 'Return to record') : null)),
    summary, tabBar, body);

  const refresh = async () => {
    if (disposed) return;
    controller?.abort(); const activeController = new AbortController(); controller = activeController;
    if (parentSignal?.aborted) activeController.abort();
    if (state.unsupported) { body.replaceChildren(emptyState('Unsupported Activity link', state.unsupported)); return; }
    const model = MODELS[state.tab];
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
      // replaceChildren stringifies a null child, so the chip is filtered out
      // rather than passed through — an unfiltered query must add nothing.
      body.replaceChildren(...[filters, subjectChip(state), table,
        pagination(state, envelope)].filter(Boolean));
      const linked = state.inspector && envelope.items.find((row) => String(row.id) === String(state.inspector));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; openRecord(state, linked, ctx, refresh); }
    } catch (error) { if (!activeController.signal.aborted && controller === activeController) body.replaceChildren(errorState(error, refresh)); }
  };
  state.reload = refresh;
  Promise.all(tabs.map((tab) => countFor(tab, ctx, parentSignal))).then((counts) => {
    if (disposed || parentSignal?.aborted) return;
    [...summary.children].forEach((card, index) => { card.querySelector('strong').textContent = counts[index].label; card.dataset.tone = counts[index].tone; });
  });
  await refresh();
  page.dispose = () => { disposed = true; controller?.abort(); filters?.dispose?.(); parentSignal?.removeEventListener('abort', abortFromParent); };
  return page;
}
