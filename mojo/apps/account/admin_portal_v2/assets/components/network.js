// The control-plane reads and write-safety machinery shared by the two
// destinations that touch the network model.
//
// v1 kept all of it in one 1100-line `advanced/page.js`. v2 splits that file
// across two destinations — Domains (names, DNS, certificates, credentials) and
// Apps ▸ Serving (vhosts, routes, upstreams) — so the parts BOTH need live
// here, in the shared component layer, rather than one feature reaching into
// the other's directory.
//
// Nothing here is new behaviour. The mutation coordinator, the reconcile-then-
// classify contract, the route repair plan in localStorage and every refusal
// sentence are v1's, ported verbatim.
import {api, apiOnce, badge, h, icon, listData, openModal, statusTone} from '../core.js';
import {announce} from './actions.js';
import {confirmAction as overlayConfirm} from './overlays.js';

export const DNS_TYPES = ['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'SRV', 'CAA', 'NS'];

export const VHOST_SHAPES = [
  ['api', 'API host', 'Proxy the entire hostname to one declared upstream.', 'route'],
  ['site', 'Static site', 'Serve a static site or single-page app.', 'globe'],
  ['site_api', 'Site + API', 'Serve a site and proxy selected path prefixes.', 'deploy'],
  ['redirect', 'Redirect', 'Permanently redirect this hostname to another host.', 'route'],
];

// ---------------------------------------------------------------------------
// mutation coordination
// ---------------------------------------------------------------------------

/**
 * A provider write is never trusted on its own answer: it is followed by an
 * authoritative read, and the pair is classified as applied / not-applied /
 * unconfirmed. An unconfirmed result LATCHES that key — no further write to it
 * runs until an explicit Refresh clears the latch.
 */
export class MutationCoordinator {
  constructor() { this.inFlight = new Map(); this.refreshRequired = new Set(); }
  isLatched(key) { return this.refreshRequired.has(String(key)); }
  clearPrefix(prefix) { [...this.refreshRequired].forEach((key) => { if (key.startsWith(String(prefix))) this.refreshRequired.delete(key); }); }
  run(key, {mutate, reconcile, classify}) {
    key = String(key);
    if (this.inFlight.has(key)) return this.inFlight.get(key);
    if (this.isLatched(key)) return Promise.resolve({state: 'refresh-required', refreshRequired: true, attempted: false});
    const promise = this._run(key, mutate, reconcile, classify).finally(() => this.inFlight.delete(key));
    this.inFlight.set(key, promise); return promise;
  }
  async _run(key, mutate, reconcile, classify) {
    let response; let mutationError; let observed; let reconcileError;
    try { response = await mutate(); } catch (error) { mutationError = error; }
    try { observed = await reconcile(); } catch (error) { reconcileError = error; }
    if (reconcileError || observed == null) {
      this.refreshRequired.add(key);
      return {state: 'unconfirmed', refreshRequired: true, mutationError, reconcileError, response, observed, attempted: true};
    }
    let state = 'unconfirmed';
    try { state = classify(observed, response, mutationError); } catch (_) { state = 'unconfirmed'; }
    if (!['applied', 'not-applied'].includes(state)) state = 'unconfirmed';
    if (state === 'unconfirmed') this.refreshRequired.add(key);
    return {state, refreshRequired: state === 'unconfirmed', mutationError, response, observed, attempted: true};
  }
}

export const networkMutations = new MutationCoordinator();

export async function providerMutation(key, mutate, reconcile, classify) {
  const result = await networkMutations.run(key, {mutate, reconcile, classify});
  if (result.state === 'applied') return result;
  if (result.refreshRequired) throw new Error('The provider result could not be confirmed. Use Refresh before another change.');
  throw result.mutationError || new Error('The requested change was not applied.');
}

// A provider mutation that fails has no panel of its own left to fail into —
// the table it belonged to is being rebuilt. Say so where the operator is
// looking, and to assistive technology, rather than losing it to the console.
export function mutationFailed(title, error) {
  const detail = error?.message || 'That change was not applied.';
  announce(detail);
  openModal({title, content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: detail}))});
}

/**
 * v1's boolean confirm, on top of v2's shared reason-capable dialog.
 *
 * Every network confirm is a yes/no with no audited reason, so the caller gets
 * the boolean it had in v1 instead of unwrapping `{confirmed, reason}` at
 * fifteen call sites.
 */
export function confirmNetwork({title, copy, confirmLabel = 'Continue', danger = false}) {
  return overlayConfirm({title, copy, confirmLabel, danger}).then((answer) => answer.confirmed === true);
}

// ---------------------------------------------------------------------------
// reads
// ---------------------------------------------------------------------------

export async function loadDomains() { return listData(await api('/api/dnsman/domain?graph=default&size=200')); }
export async function loadCredentials() { return listData(await api('/api/dnsman/credential?graph=default&size=200')); }
export async function loadCertificates() { return listData(await api('/api/dnsman/certificate?graph=default&size=200')); }
export async function loadUpstreams() { return listData(await api('/api/edge/upstream?graph=default&size=200')); }
export async function loadVhosts() { return listData(await api('/api/edge/vhost?graph=default&size=200')); }
export async function loadRoutes() { return listData(await api('/api/edge/route?graph=default&size=200')); }

export async function loadDomainCertificates(domainId) {
  return listData(await api(`/api/dnsman/certificate?domain=${encodeURIComponent(domainId)}&graph=default&size=200`));
}

export async function loadRetireEligibility(domainId) {
  const payload = await api(`/api/dnsman/certificate/retire-eligibility?domain=${encodeURIComponent(domainId)}`);
  return payload.eligibility || {};
}

// ---------------------------------------------------------------------------
// small shared render helpers
// ---------------------------------------------------------------------------

export function statusBadge(value) { return badge(String(value || 'unknown').replaceAll('_', ' '), statusTone(value)); }

export function tablePanel(title, copy) {
  return h('section', {class: 'panel'}, h('div', {class: 'panel-head'}, h('div', {}, h('h2', {text: title}), h('p', {text: copy}))));
}

export function selectOptions(rows, label, blank = 'Choose one') {
  return [{value: '', label: blank}, ...rows.map((row) => ({value: row.id, label: label(row)}))];
}

export function groupFields(ctx, required = true) {
  const options = (ctx.groups || []).map((group) => ({value: group.id, label: group.name}));
  return {name: 'group', label: 'Group', type: 'select', required, placeholder: required ? 'Choose a group' : 'Platform scope', options};
}

export async function ensureGroupChoices(ctx) {
  if (ctx.networkGroupsLoaded) return;
  ctx.networkGroupsLoaded = true;
  if (!ctx.capabilities.manage_network) return;
  try {
    const choices = listData(await api('/api/dnsman/credential/group-choice?size=50'));
    if (choices.length) ctx.groups = choices;
  } catch (_) {
    // A tenant-scoped DNS grant cannot use the global choice endpoint. Its
    // bootstrap memberships remain the only groups offered to the browser.
  }
}

// ---------------------------------------------------------------------------
// record-set identity
// ---------------------------------------------------------------------------

export function canonicalRecordName(domain, value) {
  const apex = String(domain.name || '').trim().toLowerCase().replace(/\.+$/, '');
  const name = String(value || '').trim().toLowerCase().replace(/\.+$/, '');
  if (!name || name === '@' || name === apex) return apex;
  if (name.endsWith(`.${apex}`)) return name;
  return `${name}.${apex}`;
}
export function recordIdentity(domain, record) { return `${String(record.type).toUpperCase()}|${canonicalRecordName(domain, record.name)}`; }
export function normalizedValues(values) { return [...new Set((values || []).map((value) => String(value).trim()).filter(Boolean))].sort(); }
export function sameValues(a, b) { return JSON.stringify(normalizedValues(a)) === JSON.stringify(normalizedValues(b)); }
export function sameRecordSet(a, b) { return sameValues(a?.record_values, b?.record_values) && Number(a?.ttl) === Number(b?.ttl); }

// ---------------------------------------------------------------------------
// routes: authoritative reconciliation + the local repair plan
// ---------------------------------------------------------------------------

const ROUTE_REPAIR_KEY = 'mojo-admin-route-repair-v2';

function readRouteRepairs() {
  const plans = new Map();
  try {
    const parsed = JSON.parse(localStorage.getItem(ROUTE_REPAIR_KEY) || '[]');
    if (!Array.isArray(parsed)) throw new Error('invalid repair plan');
    parsed.slice(0, 100).forEach((plan) => {
      const vhost = Number(plan.vhost); const upstream = Number(plan.upstream);
      const path = String(plan.path_prefix || '');
      if (Number.isInteger(vhost) && vhost > 0 && Number.isInteger(upstream) && upstream > 0 && path.length <= 255 && path.startsWith('/') && path !== '/') {
        const rows = plans.get(vhost) || []; rows.push({path_prefix: path, upstream, upstream_name: String(plan.upstream_name || `#${upstream}`).slice(0, 128)}); plans.set(vhost, rows);
      }
    });
  } catch (_) { try { localStorage.removeItem(ROUTE_REPAIR_KEY); } catch (_) { /* storage is optional */ } }
  return plans;
}

export const partialRoutes = readRouteRepairs();

export function writeRouteRepairs() {
  const rows = [...partialRoutes.entries()].flatMap(([vhost, plans]) => plans.map((plan) => ({
    vhost, path_prefix: plan.path_prefix, upstream: plan.upstream, upstream_name: plan.upstream_name,
  })));
  try {
    if (rows.length) localStorage.setItem(ROUTE_REPAIR_KEY, JSON.stringify(rows));
    else localStorage.removeItem(ROUTE_REPAIR_KEY);
  } catch (_) { /* authoritative reconciliation still works without browser storage */ }
}

export function rememberRoute(desired, upstreamName = '') {
  const vhost = Number(desired.vhost);
  const plans = partialRoutes.get(vhost) || [];
  const plan = {path_prefix: desired.path_prefix, upstream: Number(desired.upstream), upstream_name: upstreamName || `#${desired.upstream}`};
  partialRoutes.set(vhost, [...plans.filter((row) => row.path_prefix !== plan.path_prefix), plan]);
  writeRouteRepairs();
}

export function forgetRoute(desired) {
  const vhost = Number(desired.vhost);
  const remaining = (partialRoutes.get(vhost) || []).filter((row) => row.path_prefix !== desired.path_prefix);
  if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
  writeRouteRepairs();
}

export function routeIds(row) {
  return {vhost: Number(row.vhost?.id || row.vhost), upstream: Number(row.upstream?.id || row.upstream)};
}

export function routeState(rows, desired) {
  const samePath = rows.find((row) => routeIds(row).vhost === Number(desired.vhost) && row.path_prefix === desired.path_prefix);
  if (!samePath) return {state: 'missing'};
  if (routeIds(samePath).upstream === Number(desired.upstream)) return {state: 'applied', row: samePath};
  return {state: 'mismatch', row: samePath};
}

export async function ensureRoute(desired, mutate) {
  const before = routeState(await loadRoutes(), desired);
  if (before.state === 'applied') return before.row;
  if (before.state === 'mismatch') throw new Error(`${desired.path_prefix} already points to a different upstream. Review it before retrying.`);
  let mutationError = null;
  try { await mutate(); } catch (error) { mutationError = error; }
  const after = routeState(await loadRoutes(), desired);
  if (after.state === 'applied') return after.row;
  if (after.state === 'mismatch') throw new Error(`${desired.path_prefix} landed with a different upstream. No retry was attempted.`);
  throw new Error(mutationError ? `The route result was not confirmed: ${mutationError.message}` : 'The route response succeeded but authoritative state is still missing.');
}

export function parseRoutes(value, upstreams) {
  return String(value || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const [path, id] = line.split('|').map((part) => part.trim());
    const upstream = upstreams.find((row) => String(row.id) === id);
    if (!path?.startsWith('/') || path === '/' || !upstream) throw new Error(`Invalid route “${line}”. Use /path | upstream-id.`);
    return {path_prefix: path, upstream: upstream.id, upstream_name: upstream.name};
  });
}

// The one write helper every caller shares — kept here so `apiOnce` is imported
// once and the "single attempt, never retried" rule reads the same everywhere.
export function postOnce(path, body) {
  return apiOnce(path, {method: 'POST', body: JSON.stringify(body)});
}
