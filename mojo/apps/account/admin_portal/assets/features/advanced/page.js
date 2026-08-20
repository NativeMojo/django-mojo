import {
  api, apiOnce, badge, FormView, formatDate, h, icon, listData, openModal,
  pageHeader, statusTone, TableView,
} from '../../core.js';
import {announce, loadInto, runAction} from '../../components/actions.js';
import {activityHref, decodeRouteState, returnLocation, routeHref} from '../../components/routes.js';
import {errorState} from '../../components/views.js';

const DNS_TYPES = ['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'SRV', 'CAA', 'NS'];
const VHOST_SHAPES = [
  ['api', 'API host', 'Proxy the entire hostname to one declared upstream.', 'route'],
  ['site', 'Static site', 'Serve a static site or single-page app.', 'globe'],
  ['site_api', 'Site + API', 'Serve a site and proxy selected path prefixes.', 'deploy'],
  ['redirect', 'Redirect', 'Permanently redirect this hostname to another host.', 'route'],
];

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
const ROUTE_REPAIR_KEY = 'mojo-admin-route-repair-v1';

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

const partialRoutes = readRouteRepairs();

function writeRouteRepairs() {
  const rows = [...partialRoutes.entries()].flatMap(([vhost, plans]) => plans.map((plan) => ({
    vhost, path_prefix: plan.path_prefix, upstream: plan.upstream, upstream_name: plan.upstream_name,
  })));
  try {
    if (rows.length) localStorage.setItem(ROUTE_REPAIR_KEY, JSON.stringify(rows));
    else localStorage.removeItem(ROUTE_REPAIR_KEY);
  } catch (_) { /* authoritative reconciliation still works without browser storage */ }
}

function rememberRoute(desired, upstreamName = '') {
  const vhost = Number(desired.vhost);
  const plans = partialRoutes.get(vhost) || [];
  const plan = {path_prefix: desired.path_prefix, upstream: Number(desired.upstream), upstream_name: upstreamName || `#${desired.upstream}`};
  partialRoutes.set(vhost, [...plans.filter((row) => row.path_prefix !== plan.path_prefix), plan]);
  writeRouteRepairs();
}

function forgetRoute(desired) {
  const vhost = Number(desired.vhost);
  const remaining = (partialRoutes.get(vhost) || []).filter((row) => row.path_prefix !== desired.path_prefix);
  if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
  writeRouteRepairs();
}

function queryParam(name) {
  const query = location.hash.split('?')[1] || '';
  return new URLSearchParams(query).get(name);
}

// A provider mutation that fails has no panel of its own left to fail into —
// the table it belonged to is being rebuilt. Say so where the operator is
// looking, and to assistive technology, rather than losing it to the console.
function mutationFailed(title, error) {
  const detail = error?.message || 'That change was not applied.';
  announce(detail);
  openModal({title, content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: detail}))});
}

function statusBadge(value) { return badge(String(value || 'unknown').replaceAll('_', ' '), statusTone(value)); }

function selectOptions(rows, label, blank = 'Choose one') {
  return [{value: '', label: blank}, ...rows.map((row) => ({value: row.id, label: label(row)}))];
}

function confirmAction({title, copy, confirmLabel = 'Continue', danger = false}) {
  return new Promise((resolve) => {
    const cancel = h('button', {class: 'button ghost', onclick: () => { close(); resolve(false); }}, 'Cancel');
    const confirm = h('button', {class: `button ${danger ? 'danger' : 'primary'}`, onclick: () => { close(); resolve(true); }}, confirmLabel);
    const close = openModal({title, danger, content: h('div', {}, h('p', {class: 'modal-copy', text: copy}), h('div', {class: 'form-actions'}, cancel, confirm))});
  });
}

async function providerMutation(key, mutate, reconcile, classify) {
  const result = await networkMutations.run(key, {mutate, reconcile, classify});
  if (result.state === 'applied') return result;
  if (result.refreshRequired) throw new Error('The provider result could not be confirmed. Use Refresh before another change.');
  throw result.mutationError || new Error('The requested change was not applied.');
}

function tablePanel(title, copy) {
  return h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: title}), h('p', {text: copy}))));
}

function groupFields(ctx, required = true) {
  const options = (ctx.groups || []).map((group) => ({value: group.id, label: group.name}));
  return {name: 'group', label: 'Group', type: 'select', required, placeholder: required ? 'Choose a group' : 'Platform scope', options};
}

async function loadDomains() { return listData(await api('/api/dnsman/domain?graph=default&size=200')); }
async function loadCredentials() { return listData(await api('/api/dnsman/credential?graph=default&size=200')); }
async function loadCertificates() { return listData(await api('/api/dnsman/certificate?graph=default&size=200')); }
async function loadUpstreams() { return listData(await api('/api/edge/upstream?graph=default&size=200')); }
async function loadVhosts() { return listData(await api('/api/edge/vhost?graph=default&size=200')); }
async function loadRoutes() { return listData(await api('/api/edge/route?graph=default&size=200')); }

function routeIds(row) {
  return {vhost: Number(row.vhost?.id || row.vhost), upstream: Number(row.upstream?.id || row.upstream)};
}

function routeState(rows, desired) {
  const samePath = rows.find((row) => routeIds(row).vhost === Number(desired.vhost) && row.path_prefix === desired.path_prefix);
  if (!samePath) return {state: 'missing'};
  if (routeIds(samePath).upstream === Number(desired.upstream)) return {state: 'applied', row: samePath};
  return {state: 'mismatch', row: samePath};
}

async function ensureRoute(desired, mutate) {
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

async function ensureGroupChoices(ctx) {
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

function editDomain(domain, reload) {
  const form = new FormView({fields: [
    {name: 'auto_renew', label: 'Automatic renewal', type: 'checkbox'},
    {name: 'privacy', label: 'WHOIS privacy', type: 'checkbox'},
  ], value: domain, submitLabel: 'Save domain', onSubmit: async (values) => {
    await apiOnce(`/api/dnsman/domain/${domain.id}`, {method: 'POST', body: JSON.stringify(values)});
    close(); await reload();
  }});
  const close = openModal({title: domain.name, subtitle: 'Registrar settings for this managed domain.', content: form.render()});
}

async function registerExisting(ctx, reload) {
  const credentials = await loadCredentials();
  const form = new FormView({fields: [groupFields(ctx),
    {name: 'domain', label: 'Domain name', required: true, placeholder: 'example.com'},
    {name: 'credential', label: 'Verified credential', type: 'select', required: true, placeholder: 'Choose a credential', options: credentials.filter((row) => row.verified && row.is_active).map((row) => ({value: row.id, label: `${row.name} · ${row.provider}`}))},
  ], submitLabel: 'Register existing domain', onSubmit: async (values) => {
    const before = await loadDomains();
    await providerMutation(`domain:register:${values.domain.toLowerCase()}`,
      () => apiOnce('/api/dnsman/registrar/register-existing', {method: 'POST', body: JSON.stringify(values)}),
      loadDomains,
      (observed) => observed.some((row) => row.name === values.domain.toLowerCase()) ? 'applied' : (observed.length === before.length ? 'not-applied' : 'unconfirmed'));
    close(); await reload();
  }});
  const close = openModal({title: 'Register an existing domain', subtitle: 'The provider credential proves control before anything is stored.', content: form.render()});
}

async function adoptDomain(ctx, reload) {
  const discovery = await api('/api/dnsman/registrar/discover?untracked=true');
  const rows = discovery.domains || discovery.results || listData(discovery);
  const form = new FormView({fields: [
    {name: 'domain', label: 'Existing Route 53 domain or zone', type: 'select', required: true, placeholder: 'Choose an existing domain', options: rows.map((row) => ({value: row.name || row.domain, label: `${row.name || row.domain}${row.hosted ? ' · hosted zone' : ''}`}))},
    groupFields(ctx, false),
    {name: 'create_zone', label: 'Create a missing hosted zone', type: 'checkbox', help: 'Existing zones are always adopted in place.'},
  ], submitLabel: 'Adopt domain', onSubmit: async (values) => {
    const payload = {domain: values.domain, create_zone: values.create_zone};
    if (values.group) payload.group = values.group;
    await providerMutation(`domain:adopt:${values.domain}`,
      () => apiOnce('/api/dnsman/registrar/adopt', {method: 'POST', body: JSON.stringify(payload)}),
      loadDomains, (observed) => observed.some((row) => row.name === values.domain) ? 'applied' : 'unconfirmed');
    close(); await reload();
  }});
  const close = openModal({title: 'Adopt from Route 53', subtitle: 'Discovery is read-only. Adoption preserves the existing zone and records.', content: form.render(), wide: true});
}

async function purchaseDomain(ctx, reload) {
  let quote = null; let token = null;
  const body = h('div', {class: 'wizard'});
  const scrub = () => {
    token = null;
    if (quote) quote.token = null;
    quote = null;
    body.querySelectorAll('input,textarea').forEach((input) => { input.value = ''; });
  };
  const close = openModal({title: 'Buy a domain', subtitle: 'Search, quote, typed confirmation, then one non-retried purchase attempt.', content: body, wide: true, onClose: scrub});
  function renderSearch() {
    const name = h('input', {type: 'text', placeholder: 'example.com', autocomplete: 'off'});
    const group = h('select', {}, h('option', {value: '', text: 'Choose a group'}), ...(ctx.groups || []).map((row) => h('option', {value: row.id, text: row.name})));
    const message = h('div', {class: 'form-message', role: 'alert'});
    const search = h('button', {class: 'button primary', onclick: () => runAction(search, async () => {
      message.textContent = '';
      const row = await api('/api/dnsman/registrar/search', {method: 'POST', body: JSON.stringify({domain: name.value})});
      renderResult(row, group.value);
    }, {
      // renderResult replaces the whole wizard body, this button included, so
      // there is nothing to restore on the path that succeeds.
      pendingLabel: 'Checking…', restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    })}, icon('search'), 'Check availability');
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote'), badge('3 Confirm')),
      h('label', {class: 'field'}, h('span', {text: 'Group'}), group), h('label', {class: 'field'}, h('span', {text: 'Domain'}), name), message, search);
  }
  function renderResult(row, groupId) {
    const available = row.available === true; const price = row.price ?? '—';
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote', 'success'), badge('3 Confirm')),
      h('div', {class: 'result-card'}, h('div', {}, h('strong', {text: row.name}), h('span', {text: row.reason || (available ? 'Available to register' : 'Not available')})), statusBadge(available ? 'active' : 'failed'), h('strong', {text: `${price} ${row.currency || 'USD'}`})),
      h('div', {class: 'form-actions'}, h('button', {class: 'button ghost', onclick: renderSearch}, 'Search again'),
        available && groupId ? h('button', {class: 'button primary', onclick: (event) => runAction(event.currentTarget, async () => {
          quote = await apiOnce('/api/dnsman/registrar/quote', {method: 'POST', body: JSON.stringify({group: groupId, domain: row.name, years: 1})});
          token = quote.token; quote.token = null; renderConfirm(groupId);
        }, {
          pendingLabel: 'Getting a quote…', restoreOnSuccess: false,
          onError: (error) => { body.append(h('div', {class: 'form-message', text: error.message})); },
        })}, 'Get live quote') : null));
  }
  function renderConfirm(groupId) {
    const typedDomain = h('input', {autocomplete: 'off', placeholder: quote.name});
    const typedPrice = h('input', {autocomplete: 'off', inputmode: 'decimal', placeholder: String(quote.price)});
    const message = h('div', {class: 'form-message', role: 'alert'});
    // This spends money on a single non-retried attempt. The scrim is the
    // point: nothing else on the page is clickable — least of all this button
    // a second time — while the registrar is deciding.
    //
    // And the quote is single-use, so the control latches off the moment the
    // spend starts and never comes back. `runAction` restores an errored
    // control, which on its own would leave "Register domain" live directly
    // under the message saying the quote was spent; the server refuses that
    // retry (a select_for_update CAS on status plus the confirm token), so the
    // operator would just collect a second, more confusing refusal. The latch
    // is its own flag rather than a bare `disabled = true` because validate()
    // re-derives `disabled` on every keystroke and would undo it.
    let spent = false;
    const buy = h('button', {class: 'button danger', disabled: true, onclick: () => runAction(buy, async () => {
      // Synchronous, before the first await: the guard and the latch agree.
      spent = true; buy.disabled = true;
      let purchaseToken = token; token = null;
      try {
        await apiOnce('/api/dnsman/registrar/purchase', {method: 'POST', body: JSON.stringify({group: groupId, purchase: quote.purchase, confirm_token: purchaseToken, confirm_domain: typedDomain.value, confirm_price: typedPrice.value})});
        close(); await reload();
      } finally { purchaseToken = null; }
    }, {
      busy: {title: `Registering ${quote.name}…`, detail: 'This is a single attempt — it is not retried.'},
      restoreOnSuccess: false,
      onError: (error) => {
        if (quote) quote.token = null;
        message.textContent = `${error.message} The quote was spent; take a new quote before trying again.`;
      },
    })}, 'Register domain');
    const validate = () => { buy.disabled = spent || typedDomain.value.trim().toLowerCase() !== quote.name || typedPrice.value.trim() !== String(quote.price); };
    typedDomain.addEventListener('input', validate); typedPrice.addEventListener('input', validate);
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote', 'success'), badge('3 Confirm', 'warning')),
      h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: `This spends ${quote.price} ${quote.currency} to register ${quote.name}. The quote expires ${formatDate(quote.expires)}.`})),
      h('label', {class: 'field'}, h('span', {text: `Type ${quote.name}`}), typedDomain),
      h('label', {class: 'field'}, h('span', {text: `Type ${quote.price}`}), typedPrice), message, h('div', {class: 'form-actions'}, buy));
  }
  renderSearch();
}

async function loadDomainCertificates(domainId) {
  return listData(await api(`/api/dnsman/certificate?domain=${encodeURIComponent(domainId)}&graph=default&size=200`));
}

async function loadRetireEligibility(domainId) {
  const payload = await api(`/api/dnsman/certificate/retire-eligibility?domain=${encodeURIComponent(domainId)}`);
  return payload.eligibility || {};
}

function certificateExpiry(row) {
  return row.days_remaining == null ? formatDate(row.not_after) : `${row.days_remaining} days`;
}

async function retireCertificate(row, covering, addressCount, reload) {
  const confirmed = await confirmAction({
    title: `Retire ${row.common_name}?`,
    copy: `${covering?.common_name || 'The replacement certificate'} takes over ${addressCount} addresses now served by ${row.common_name}, then the retired certificate is removed from the inventory.`,
    confirmLabel: 'Retire certificate', danger: true});
  if (!confirmed) return;
  // The confirm was the human's part; the retirement moves live traffic onto
  // the covering certificate and rebuilds the row the button sat in.
  await runAction(null, async () => {
    await apiOnce('/api/dnsman/certificate/retire', {method: 'POST', body: JSON.stringify({certificate: row.id})});
    await reload();
  }, {
    key: `certificate-retire:${row.id}`,
    busy: {title: `Retiring ${row.common_name}…`, detail: 'Moving its addresses onto the covering certificate.'},
    onError: (error) => mutationFailed('The certificate was not retired', error),
  });
}

function domainCertificatesPanel(ctx, domain) {
  const panel = h('section', {class: 'panel'});
  // The heading is painted synchronously and the reads land underneath it, so
  // the loading state goes in `body` — replacing the panel would take the
  // heading and the Request certificate button with it.
  const body = h('div', {});
  async function render() {
    panel.replaceChildren(h('div', {class: 'panel-heading'}, h('div', {},
      h('h2', {text: 'Certificates'}),
      h('p', {text: 'TLS coverage for this domain. Private key material is never shown.'})),
      ctx.capabilities.manage_network ? h('button', {class: 'button compact', onclick: () => certificateRequest([domain], render)}, icon('certificate'), 'Request certificate') : null),
    body);
    await loadInto(body, async (current) => {
      const [rows, eligibility, vhosts] = await Promise.all([
        loadDomainCertificates(domain.id),
        loadRetireEligibility(domain.id).catch(() => ({})),
        loadVhosts().catch(() => []),
      ]);
      if (!current()) return;
      body.replaceChildren();
      const wildcardName = `*.${domain.name}`;
      const headline = rows.find((row) => row.status === 'active' && [row.common_name, ...(row.sans || [])].includes(wildcardName)) || null;
      const addressCount = (row) => vhosts.filter((vhost) => Number(vhost.certificate?.id || vhost.certificate) === Number(row.id)).length;
      const retireButton = (row) => {
        const coveringId = eligibility[String(row.id)];
        if (!coveringId || !ctx.capabilities.manage_network) return null;
        const covering = rows.find((item) => Number(item.id) === Number(coveringId));
        return h('button', {class: 'button compact', onclick: (event) => {
          event.stopPropagation();
          return retireCertificate(row, covering, addressCount(row), render);
        }}, 'Retire — use wildcard');
      };
      if (headline) {
        body.append(h('div', {class: 'result-card cert-headline'},
          h('div', {},
            h('strong', {text: [headline.common_name, ...(headline.sans || []).filter((name) => name !== headline.common_name)].join(', ')}),
            h('span', {text: 'Covers every app on this domain — current and future.'})),
          statusBadge(headline.status),
          h('strong', {text: headline.renew_after ? `Renews ${formatDate(headline.renew_after)}` : `Expires ${certificateExpiry(headline)}`})));
      }
      const others = rows.filter((row) => row !== headline);
      body.append(new TableView({rows: others, empty: headline ? 'No other certificates on this domain.' : 'No certificates have been requested for this domain.', columns: [
        {label: 'Certificate', render: (row) => h('div', {}, h('strong', {text: row.common_name}), h('small', {text: (row.sans || []).join(', ')}))},
        {label: 'Status', render: (row) => statusBadge(row.status)},
        {label: 'Expires', render: (row) => certificateExpiry(row)},
        {label: '', render: (row) => h('div', {class: 'form-actions'},
          retireButton(row),
          ctx.capabilities.manage_network && row.status === 'failed' ? h('button', {class: 'icon-button danger-text', 'aria-label': `Remove failed certificate attempt for ${row.common_name}`, onclick: (event) => { event.stopPropagation(); return removeFailedCertificate(row, render); }}, icon('trash')) : null)},
      ]}).render());
    }, {message: 'Loading certificates…', retry: render});
  }
  render();
  return panel;
}

// One domain is a page of its own (?domain=<id>), never a drawer over the
// list. The list and the detail are the same route, so a row click is a plain
// navigation and the browser Back button behaves the way an operator expects.
async function domainsPage(ctx) {
  const root = h('div', {class: 'page'});
  // Legacy ?inspector=<id> deep links predate the page and must still land on
  // the domain. replaceState fires no hashchange, so this render continues.
  const entry = decodeRouteState().state;
  if (!entry.domain && /^\d+$/.test(String(entry.inspector || ''))) {
    history.replaceState({}, '', routeHref('domains', {domain: entry.inspector}));
  }
  function renderDetail(domain) {
    root.replaceChildren(
      h('p', {class: 'back-link'}, h('a', {href: routeHref('domains')}, '← All domains')),
      pageHeader('Network & hosting', domain.name, 'Registrar settings, TLS coverage, and the live provider zone for this domain.', [
        ctx.capabilities.manage_network ? h('button', {class: 'button ghost', onclick: () => editDomain(domain, render)}, 'Edit registrar settings') : null,
      ]),
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Provider'}), h('dd', {text: domain.provider})),
        h('div', {}, h('dt', {text: 'Status'}), h('dd', {text: domain.status})),
        h('div', {}, h('dt', {text: 'Scope'}), h('dd', {text: domain.group?.name || 'Platform'})),
        h('div', {}, h('dt', {text: 'Expires'}), h('dd', {text: formatDate(domain.expires)}))),
      domainCertificatesPanel(ctx, domain),
      h('div', {class: 'activity-links'},
        h('a', {class: 'related-record', href: routeHref('dns', {domain: domain.id, return: returnLocation()})}, h('strong', {text: 'Open DNS records'}), icon('chevron')),
        h('a', {class: 'related-record', href: activityHref('logs', {type: 'model', id: domain.id, model: 'Domain'}, {return: returnLocation()})}, h('strong', {text: 'Related logs'}), icon('chevron'))));
  }
  function renderList(rows, failure) {
    root.replaceChildren(pageHeader('Network & hosting', 'Domains', 'Register, adopt, and manage the names this installation controls.', [
      ctx.capabilities.manage_network ? h('button', {class: 'button ghost', onclick: () => registerExisting(ctx, render)}, icon('plus'), 'Register existing') : null,
      ctx.user.is_superuser ? h('button', {class: 'button ghost', onclick: () => adoptDomain(ctx, render)}, icon('globe'), 'Adopt') : null,
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => purchaseDomain(ctx, render)}, icon('plus'), 'Buy domain') : null,
    ]));
    const panel = tablePanel('Managed domains', 'Open a domain to change registrar settings; use DNS Records for the live provider zone.'); root.append(panel);
    if (failure) { panel.append(errorState(failure, render)); return; }
    panel.append(new TableView({rows, empty: 'No managed domains yet.',
      onSelect: (row) => { location.hash = routeHref('domains', {domain: row.id}); }, columns: [
        {label: 'Domain', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.provider}))},
        {label: 'Status', render: (row) => statusBadge(row.status)},
        {label: 'Scope', render: (row) => row.group?.name || 'Platform'},
        {label: 'Expires', render: (row) => formatDate(row.expires)},
        {label: 'Zone', render: (row) => h('a', {class: 'button compact', href: routeHref('dns', {domain: row.id}), onclick: (event) => event.stopPropagation()}, icon('dns'), 'Records')},
      ]}).render());
  }
  // The domain read IS this page: until it lands there is nothing to look at,
  // and on a re-render the previous list would otherwise sit there reading as
  // current. The loader paints the result itself, so loadInto only owns the
  // wait — the read's own failure still becomes the list's in-panel error.
  async function render() {
    await loadInto(root, async () => {
      let rows = []; let failure = null;
      try { rows = await loadDomains(); } catch (error) { failure = error; }
      const wanted = queryParam('domain');
      // An id the caller cannot see (or that no longer exists) is not an error —
      // it is simply the list.
      const selected = wanted ? rows.find((row) => String(row.id) === String(wanted)) : null;
      if (selected) renderDetail(selected); else renderList(rows, failure);
    }, {message: 'Loading domains…', retry: render});
  }
  await render(); return root;
}

function credentialDialog(ctx, existing, reload) {
  const fields = [groupFields(ctx),
    ...(!existing ? [{name: 'provider', label: 'Provider', type: 'select', required: true, options: [{value: 'godaddy', label: 'GoDaddy'}], placeholder: 'Choose a provider'}] : []),
    {name: 'name', label: 'Credential name', required: true},
    {name: 'api_key', label: 'API key', type: 'password', required: true, autocomplete: 'new-password'},
    {name: 'api_secret', label: 'API secret', type: 'password', required: true, autocomplete: 'new-password'},
  ];
  const groupId = existing?.group?.id || existing?.group || '';
  const value = {group: groupId, provider: existing?.provider || 'godaddy', name: existing?.name || ''};
  let secretValues = null; let secretPayload = null;
  const form = new FormView({fields, value, submitLabel: existing ? 'Rotate and verify' : 'Link and verify', onSubmit: async (values, inputs) => {
    const payload = {...values, provider: existing?.provider || values.provider};
    secretValues = values; secretPayload = payload;
    if (existing) payload.credential = existing.id;
    const baseline = existing?.modified;
    try {
      await providerMutation(`credential:${existing?.id || `${values.group}:${values.name}`}`,
        () => apiOnce('/api/dnsman/credential/link', {method: 'POST', body: JSON.stringify(payload)}),
        loadCredentials,
        (observed) => {
          const row = existing ? observed.find((item) => item.id === existing.id) : observed.find((item) => item.name === values.name && item.provider === payload.provider);
          if (row?.verified && (!existing || row.modified !== baseline)) return 'applied';
          return row ? 'unconfirmed' : 'not-applied';
        });
      close(); await reload();
    } finally {
      payload.api_key = ''; payload.api_secret = ''; values.api_key = ''; values.api_secret = '';
      if (inputs.api_key) inputs.api_key.value = ''; if (inputs.api_secret) inputs.api_secret.value = '';
      secretValues = null; secretPayload = null;
    }
  }});
  const content = form.render();
  const scrub = () => {
    content.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
    if (secretValues) { secretValues.api_key = ''; secretValues.api_secret = ''; }
    if (secretPayload) { secretPayload.api_key = ''; secretPayload.api_secret = ''; }
    secretValues = null; secretPayload = null;
  };
  const close = openModal({title: existing ? `Rotate ${existing.name}` : 'Link DNS credential', subtitle: 'Provider secrets are submitted once, verified, encrypted, and never shown again.', content, danger: Boolean(existing), onClose: scrub});
}

async function credentialsPage(ctx) {
  const root = h('div', {class: 'page'});
  async function render() {
    root.replaceChildren(pageHeader('Network & hosting', 'Credentials', 'Verified provider access without exposing stored secrets.', [
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => credentialDialog(ctx, null, render)}, icon('key'), 'Link credential') : null,
    ]));
    const panel = tablePanel('DNS provider credentials', 'Only masked suffixes and verification metadata are returned by the API.'); root.append(panel);
    // Into a body node, not the panel: the panel carries the heading this
    // loading state would otherwise replace.
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadCredentials();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No DNS credentials are linked.', onSelect: ctx.capabilities.manage_network ? (row) => credentialDialog(ctx, row, render) : null, columns: [
        {label: 'Credential', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.provider}))},
        {label: 'Scope', render: (row) => row.group?.name || 'Platform'},
        {label: 'Verified', render: (row) => statusBadge(row.verified ? 'verified' : 'failed')},
        {label: 'Key', render: (row) => h('span', {class: 'mono', text: row.api_key_masked || 'Hidden'})},
        {label: 'Domains', render: (row) => String(row.domain_count || 0)},
      ]}).render());
    }, {message: 'Loading credentials…', retry: render});
  }
  await render(); return root;
}

function canonicalRecordName(domain, value) {
  const apex = String(domain.name || '').trim().toLowerCase().replace(/\.+$/, '');
  const name = String(value || '').trim().toLowerCase().replace(/\.+$/, '');
  if (!name || name === '@' || name === apex) return apex;
  if (name.endsWith(`.${apex}`)) return name;
  return `${name}.${apex}`;
}
function recordIdentity(domain, record) { return `${String(record.type).toUpperCase()}|${canonicalRecordName(domain, record.name)}`; }
function normalizedValues(values) { return [...new Set((values || []).map((value) => String(value).trim()).filter(Boolean))].sort(); }
function sameValues(a, b) { return JSON.stringify(normalizedValues(a)) === JSON.stringify(normalizedValues(b)); }
function sameRecordSet(a, b) { return sameValues(a?.record_values, b?.record_values) && Number(a?.ttl) === Number(b?.ttl); }

function recordEditor(domain, record, records, refresh) {
  const original = record ? {...record, record_values: [...(record.record_values || [])]} : null;
  const type = h('select', {disabled: Boolean(record)}, ...DNS_TYPES.map((value) => h('option', {value, text: value, selected: value === (record?.type || 'A')})));
  type.value = record?.type || 'A';
  const name = h('input', {value: record?.name || '', disabled: Boolean(record), placeholder: '@ or www'});
  const ttl = h('input', {type: 'number', min: '30', max: '86400', value: record?.ttl || 300});
  const values = h('textarea', {rows: '7', text: (record?.record_values || []).join('\n'), placeholder: 'One complete record value per line'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  // responsiveness-exempt: the first await here is confirmAction — a human
  // deciding whether to replace a record set. Policy is that nothing paints
  // until they have answered; the write that follows carries the scrim.
  const save = h('button', {class: 'button primary', onclick: async () => {
    const target = {type: type.value, name: name.value.trim(), ttl: Number(ttl.value), record_values: normalizedValues(values.value.split('\n'))};
    if (!target.name || !target.record_values.length) { message.textContent = 'Name and at least one value are required.'; return; }
    const removed = normalizedValues(original?.record_values || []).filter((value) => !target.record_values.includes(value));
    if (removed.length) {
      const confirmed = await confirmAction({title: 'Replace the complete record set?', copy: `Saving replaces every value for ${target.type} ${target.name}. These values will be removed: ${removed.join(', ')}`, confirmLabel: 'Replace record set', danger: true});
      if (!confirmed) return;
    }
    message.textContent = '';
    // A provider write that lands unconfirmed latches the whole record set;
    // nothing else on the page may be clicked while it is in flight.
    await runAction(save, async () => {
      const before = await refresh(false);
      const current = before.find((row) => recordIdentity(domain, row) === recordIdentity(domain, target));
      if (!original && current) {
        close(); recordEditor(domain, current, before, refresh); return;
      }
      if (original) {
        if (!current || !sameRecordSet(current, original)) throw new Error('The provider record set changed, including its TTL or values. Review the refreshed set and confirm again.');
      }
      const key = `dns:${domain.id}:${recordIdentity(domain, target)}`;
      await providerMutation(key,
        () => apiOnce('/api/dnsman/dns', {method: 'POST', body: JSON.stringify({domain: domain.id, ...target})}),
        () => refresh(false),
        (observed) => {
          const applied = observed.find((row) => recordIdentity(domain, row) === recordIdentity(domain, target));
          return applied && sameRecordSet(applied, target) ? 'applied' : 'unconfirmed';
        });
      close(); await refresh(false);
    }, {
      busy: {title: 'Saving the record set…', detail: 'Writing to the provider, then reading the zone back.'},
      restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    });
  }}, icon('check'), 'Save record set');
  const close = openModal({title: record ? `Edit ${record.type} record` : 'Add DNS record', subtitle: 'The provider stores one complete set for each type and name.', wide: true, content: h('div', {},
    h('div', {class: 'field-grid'}, h('label', {class: 'field'}, h('span', {text: 'Type'}), type), h('label', {class: 'field'}, h('span', {text: 'Name'}), name), h('label', {class: 'field'}, h('span', {text: 'TTL'}), ttl)),
    h('label', {class: 'field'}, h('span', {text: 'Complete values'}), values, h('small', {text: 'MX, SRV, and CAA values retain their provider wire format; one value per line.'})), message, h('div', {class: 'form-actions'}, save))});
}

async function deleteRecord(domain, record, refresh) {
  const confirmed = await confirmAction({title: `Delete ${record.type} record?`, copy: `Delete the complete ${record.type} set at ${record.name}? This request is attempted once and then reconciled from the provider.`, confirmLabel: 'Delete record set', danger: true});
  if (!confirmed) return;
  const key = `dns:${domain.id}:${recordIdentity(domain, record)}`;
  await runAction(null, async () => {
    await providerMutation(key,
      () => apiOnce('/api/dnsman/dns/delete', {method: 'POST', body: JSON.stringify({domain: domain.id, type: record.type, name: record.name, record_values: record.record_values})}),
      () => refresh(false),
      (observed) => observed.some((row) => recordIdentity(domain, row) === recordIdentity(domain, record)) ? 'unconfirmed' : 'applied');
    await refresh(false);
  }, {
    key: `delete-${key}`,
    busy: {title: `Deleting ${record.type} ${record.name}…`, detail: 'Writing to the provider, then reading the zone back.'},
    onError: (error) => mutationFailed('The record set was not deleted', error),
  });
}

async function dnsPage(ctx) {
  const root = h('div', {class: 'page'}); let domains = []; let active = null; let records = [];
  async function fetchRecords(explicit = false) {
    if (!active) return [];
    const payload = await api(`/api/dnsman/dns?domain=${encodeURIComponent(active.id)}`);
    records = payload.records || [];
    if (explicit) networkMutations.clearPrefix(`dns:${active.id}:`);
    render(); return records;
  }
  function render() {
    const picker = h('select', {'aria-label': 'Managed domain'}, h('option', {value: '', text: 'Choose a domain'}), ...domains.map((row) => h('option', {value: row.id, text: row.name, selected: row.id === active?.id})));
    picker.value = active?.id || '';
    // A <select> gets aria-disabled and the announcement, never a label swap:
    // rewriting the chosen option under the pointer is its own bug.
    picker.addEventListener('change', () => runAction(picker, async () => {
      active = domains.find((row) => String(row.id) === picker.value) || null; records = [];
      if (active) await fetchRecords(false); else render();
    }, {announceLabel: 'Loading the zone…'}));
    root.replaceChildren(pageHeader('Network & hosting', 'DNS records', 'Edit the live provider zone as complete record sets; no database mirror can drift.', [
      h('label', {class: 'toolbar-select'}, icon('globe'), picker),
      active ? h('button', {class: 'button ghost', onclick: (event) => runAction(event.currentTarget,
        () => fetchRecords(true), {announceLabel: 'Refreshing the zone…'})}, icon('refresh'), 'Refresh') : null,
      active && ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => recordEditor(active, null, records, fetchRecords)}, icon('plus'), 'Add record') : null,
    ]));
    if (!active) { root.append(h('section', {class: 'panel empty'}, h('p', {text: 'Choose a managed domain to load its authoritative provider zone.'}))); return; }
    const latched = [...networkMutations.refreshRequired].some((key) => key.startsWith(`dns:${active.id}:`));
    if (latched) root.append(h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Refresh required'}), h('p', {text: 'A provider result could not be confirmed. No further write to that record set will run until Refresh succeeds.'}))));
    const panel = tablePanel(active.name, `${records.length} live provider record sets`); root.append(panel);
    panel.append(new TableView({rows: records, empty: 'This provider zone has no records.', onSelect: ctx.capabilities.manage_network ? (row) => recordEditor(active, row, records, fetchRecords) : null, columns: [
      {label: 'Type', render: (row) => badge(row.type, 'neutral')},
      {label: 'Name', render: (row) => h('span', {class: 'mono', text: row.name})},
      {label: 'Complete values', render: (row) => h('div', {class: 'record-values'}, ...(row.record_values || []).map((value) => h('code', {text: value})))},
      {label: 'TTL', render: (row) => String(row.ttl)},
      {label: '', render: (row) => ctx.capabilities.manage_network ? h('button', {class: 'icon-button danger-text', 'aria-label': `Delete ${row.type} ${row.name}`, onclick: (event) => { event.stopPropagation(); return deleteRecord(active, row, fetchRecords); }}, icon('trash')) : null},
    ]}).render());
  }
  // Nothing can be shown until the domain list lands, and a failed read used to
  // leave a bare sentence with no way to try again.
  async function load() {
    await loadInto(root, async () => {
      domains = (await loadDomains()).filter((row) => row.status === 'active');
      active = domains.find((row) => String(row.id) === String(queryParam('domain'))) || domains[0] || null;
      if (active) await fetchRecords(false); else render();
    }, {message: 'Loading domains…', retry: load});
  }
  await load();
  return root;
}

function certificateRequest(domains, reload) {
  const form = new FormView({fields: [
    {name: 'domain', label: 'Managed domain', type: 'select', required: true, placeholder: 'Choose a domain', options: domains.map((row) => ({value: row.id, label: row.name}))},
    {name: 'names', label: 'Certificate names', type: 'textarea', rows: 4, help: 'One hostname per line. Leave empty for the domain default.'},
  ], submitLabel: 'Request certificate', onSubmit: async (values) => {
    const payload = {domain: values.domain}; const names = normalizedValues(values.names.split('\n'));
    if (names.length) payload.names = names;
    await apiOnce('/api/dnsman/certificate/request', {method: 'POST', body: JSON.stringify(payload)});
    close(); await reload();
  }});
  const close = openModal({title: 'Request certificate', subtitle: 'Issuance runs asynchronously; this portal polls status metadata only.', content: form.render()});
}

async function removeFailedCertificate(row, reload) {
  const confirmed = await confirmAction({title: `Remove failed attempt for ${row.common_name}?`, copy: 'This removes the obsolete certificate attempt from the inventory. The failure remains in the job and application logs.', confirmLabel: 'Remove failed attempt', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    await apiOnce('/api/dnsman/certificate/remove-failed', {method: 'POST', body: JSON.stringify({certificate: row.id})});
    await reload();
  }, {
    key: `certificate-remove-failed:${row.id}`,
    busy: {title: `Removing the failed attempt for ${row.common_name}…`},
    onError: (error) => mutationFailed('The failed attempt was not removed', error),
  });
}

async function certificatesPage(ctx) {
  const root = h('div', {class: 'page'}); let pollTicks = 0; let pollTimer = null;
  async function render() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const domains = await loadDomains(); const rows = await loadCertificates();
    root.replaceChildren(pageHeader('Network & hosting', 'Certificates', 'Issue and monitor TLS certificates without exposing private key material.', [
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => certificateRequest(domains.filter((row) => row.status === 'active'), render)}, icon('certificate'), 'Request certificate') : null,
    ]));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {},
      h('h2', {text: 'TLS certificates'}),
      h('p', {}, 'The whole inventory; only lifecycle, names, issuer, and expiry metadata are shown. ',
        h('a', {href: routeHref('domains'), text: 'Manage certificates per-domain from the domain page.'})))));
    root.append(panel);
    panel.append(new TableView({rows, empty: 'No certificates have been requested.', columns: [
      {label: 'Certificate', render: (row) => h('div', {}, h('strong', {text: row.common_name}), h('small', {text: (row.sans || []).join(', ')}))},
      {label: 'Status', render: (row) => statusBadge(row.status)},
      {label: 'Domain', render: (row) => row.domain?.name || '—'},
      {label: 'Issuer', render: (row) => row.issuer || 'Pending'},
      {label: 'Expires', render: (row) => row.days_remaining == null ? formatDate(row.not_after) : `${row.days_remaining} days`},
      {label: '', render: (row) => ctx.capabilities.manage_network && row.status === 'failed' ? h('button', {class: 'icon-button danger-text', 'aria-label': `Remove failed certificate attempt for ${row.common_name}`, onclick: (event) => { event.stopPropagation(); return removeFailedCertificate(row, render); }}, icon('trash')) : null},
    ]}).render());
    if (rows.some((row) => ['pending', 'issuing'].includes(row.status)) && pollTicks < 36 && location.hash.startsWith('#/certificates')) {
      pollTicks += 1; pollTimer = setTimeout(() => render().catch(() => {}), 10000);
    }
  }
  // Two reads before anything paints, and a failure that used to be a bare
  // sentence. The poll keeps calling render() directly — a background tick
  // must never blank the table the operator is reading.
  async function load() {
    await loadInto(root, () => render(), {message: 'Loading certificates…', retry: load});
  }
  await load();
  return root;
}

function upstreamDialog(ctx, reload) {
  const form = new FormView({fields: [groupFields(ctx, false),
    {name: 'name', label: 'Upstream name', required: true},
    {name: 'kind', label: 'Kind', type: 'select', required: true, options: [{value: 'http', label: 'HTTP host + port'}, {value: 'unix', label: 'Unix socket'}]},
    {name: 'host', label: 'HTTP host', placeholder: '127.0.0.1'},
    {name: 'port', label: 'HTTP port', type: 'number', min: 1, max: 65535},
    {name: 'socket_path', label: 'Unix socket path', placeholder: '/run/mojo/app.sock'},
  ], value: {kind: 'http'}, submitLabel: 'Declare upstream', onSubmit: async (values) => {
    const payload = {name: values.name, kind: values.kind}; if (values.group) payload.group = values.group;
    if (values.kind === 'http') { payload.host = values.host; payload.port = Number(values.port); }
    else payload.socket_path = values.socket_path;
    await apiOnce('/api/edge/upstream/declare', {method: 'POST', body: JSON.stringify(payload)});
    close(); await reload();
  }});
  const close = openModal({title: 'Declare upstream', subtitle: 'This allowlist row is the only kind of destination a Vhost may select.', content: form.render()});
}

async function retireUpstream(row, reload) {
  const confirmed = await confirmAction({title: `Retire ${row.name}?`, copy: 'Existing Vhosts keep their historical reference but stop serving until repaired.', confirmLabel: 'Retire upstream', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    await apiOnce('/api/edge/upstream/retire', {method: 'POST', body: JSON.stringify({upstream: row.id})}); await reload();
  }, {
    key: `upstream-retire:${row.id}`,
    busy: {title: `Retiring ${row.name}…`, detail: 'Vhosts pointing at it stop serving until they are repaired.'},
    onError: (error) => mutationFailed('The upstream was not retired', error),
  });
}

async function upstreamsPage(ctx) {
  const root = h('div', {class: 'page'});
  async function render() {
    root.replaceChildren(pageHeader('Network & hosting', 'Upstreams', 'Platform-declared destinations Vhosts may safely proxy to.', [
      ctx.user.is_superuser ? h('button', {class: 'button primary', onclick: () => upstreamDialog(ctx, render)}, icon('plus'), 'Declare upstream') : null,
    ]));
    const panel = tablePanel('Declared destinations', 'Retirement preserves history; destinations are never silently repointed.'); root.append(panel);
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadUpstreams();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No upstream destinations are declared.', columns: [
        {label: 'Upstream', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.kind}))},
        {label: 'Target', render: (row) => h('code', {text: row.kind === 'unix' ? row.socket_path : `${row.host}:${row.port}`})},
        {label: 'Scope', render: (row) => row.group?.name || 'Shared'},
        {label: 'Status', render: (row) => statusBadge(row.is_enabled ? 'active' : 'inactive')},
        {label: '', render: (row) => ctx.user.is_superuser && row.is_enabled ? h('button', {class: 'button compact', onclick: () => retireUpstream(row, render)}, 'Retire') : null},
      ]}).render());
    }, {message: 'Loading upstreams…', retry: render});
  }
  await render(); return root;
}

function parseRoutes(value, upstreams) {
  return String(value || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const [path, id] = line.split('|').map((part) => part.trim());
    const upstream = upstreams.find((row) => String(row.id) === id);
    if (!path?.startsWith('/') || path === '/' || !upstream) throw new Error(`Invalid route “${line}”. Use /path | upstream-id.`);
    return {path_prefix: path, upstream: upstream.id, upstream_name: upstream.name};
  });
}

async function createVhostWizard(ctx, reload) {
  const [domains, certificates, upstreams] = await Promise.all([loadDomains(), loadCertificates(), loadUpstreams()]);
  const body = h('div', {class: 'wizard'}); let kind = null;
  const close = openModal({title: 'Create Vhost', subtitle: 'Choose one known-good serving shape, then supply only its fields.', content: body, wide: true});
  function shapes() {
    body.replaceChildren(h('div', {class: 'shape-grid'}, ...VHOST_SHAPES.map(([value, label, copy, iconName]) => h('button', {class: 'shape-card', onclick: () => { kind = value; details(); }}, icon(iconName), h('div', {}, h('strong', {text: label}), h('p', {text: copy}))))));
  }
  function details() {
    const domain = h('select', {}, h('option', {value: '', text: 'Choose active domain'}), ...domains.filter((row) => row.status === 'active').map((row) => h('option', {value: row.id, text: row.name})));
    const certificate = h('select', {}, h('option', {value: '', text: 'Choose active certificate'}), ...certificates.filter((row) => row.status === 'active').map((row) => h('option', {value: row.id, text: row.common_name})));
    const upstream = h('select', {}, h('option', {value: '', text: 'Choose upstream'}), ...upstreams.filter((row) => row.is_enabled).map((row) => h('option', {value: row.id, text: `${row.name} (#${row.id})`})));
    const label = h('input', {placeholder: 'Blank for apex, * for wildcard'}); const pool = h('input', {value: 'default'});
    const bodySize = h('input', {type: 'number', min: '1', max: '4096', value: '50'}); const enabled = h('input', {type: 'checkbox', checked: true});
    const spa = h('input', {type: 'checkbox'}); const serveStatic = h('input', {type: 'checkbox'}); const redirect = h('input', {placeholder: 'www.example.com'});
    const routes = h('textarea', {rows: '4', placeholder: '/api | 12\n/ws | 14'}); const message = h('div', {class: 'form-message', role: 'alert'});
    // Creating a Vhost publishes a new desired generation and then walks its
    // routes one at a time; a second click mid-walk would create a second
    // Vhost. Scrim plus the guard, not an inline state.
    const create = h('button', {class: 'button primary', onclick: () => runAction(create, async () => {
      message.textContent = '';
      {
        const payload = {domain: Number(domain.value), kind, label: label.value.trim(), certificate: Number(certificate.value), pool: pool.value.trim(), body_size_mb: Number(bodySize.value), is_enabled: enabled.checked, spa: spa.checked, serve_static: serveStatic.checked, quiet_paths: []};
        if (!payload.domain || !payload.certificate || !payload.pool) throw new Error('Domain, certificate, and pool are required.');
        if (kind === 'api') { payload.upstream = Number(upstream.value); if (!payload.upstream) throw new Error('An API host requires an upstream.'); }
        else payload.upstream = null;
        if (kind === 'redirect') { payload.redirect_to = redirect.value.trim(); if (!payload.redirect_to) throw new Error('A redirect target is required.'); }
        else payload.redirect_to = null;
        const routeRows = kind === 'site_api' ? parseRoutes(routes.value, upstreams) : [];
        const vhost = await apiOnce('/api/edge/vhost', {method: 'POST', body: JSON.stringify(payload)});
        const results = [];
        routeRows.forEach((row) => rememberRoute({vhost: vhost.id, path_prefix: row.path_prefix, upstream: row.upstream}, row.upstream_name));
        for (const row of routeRows) {
          try {
            // Sequential on purpose: a partial state names exactly which rows landed.
            const desired = {vhost: vhost.id, path_prefix: row.path_prefix, upstream: row.upstream};
            await ensureRoute(desired, () => apiOnce('/api/edge/route', {method: 'POST', body: JSON.stringify(desired)}));
            forgetRoute(desired);
            results.push({...row, ok: true});
          } catch (error) { results.push({...row, ok: false, error: error.message}); }
        }
        const failed = results.filter((row) => !row.ok);
        if (failed.length) { partialRoutes.set(vhost.id, failed); writeRouteRepairs(); }
        await reload();
        body.replaceChildren(h('div', {class: `result-state ${failed.length ? 'warning' : 'success'}`}, icon(failed.length ? 'alert' : 'check'), h('h3', {text: failed.length ? 'Vhost created; routes need repair' : 'Vhost created'}), h('p', {text: failed.length ? `${failed.length} route(s) did not land. Open Routes to retry only those rows.` : 'The desired generation was published and the fleet can converge.'}), h('div', {class: 'form-actions'}, failed.length ? h('a', {class: 'button primary', href: '#/routes', onclick: close}, 'Open Routes') : h('button', {class: 'button primary', onclick: close}, 'Done'))));
      }
    }, {
      busy: {title: 'Creating the Vhost…', detail: 'Publishing the desired generation, then adding its routes one at a time.'},
      restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    })}, 'Create Vhost');
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Shape', 'success'), badge('2 Details', 'warning'), badge('3 Converge')),
      h('button', {class: 'button ghost compact', onclick: shapes}, '← Change shape'),
      h('div', {class: 'field-grid'}, h('label', {class: 'field'}, h('span', {text: 'Domain'}), domain), h('label', {class: 'field'}, h('span', {text: 'Certificate'}), certificate), h('label', {class: 'field'}, h('span', {text: 'Label'}), label), h('label', {class: 'field'}, h('span', {text: 'Pool'}), pool), h('label', {class: 'field'}, h('span', {text: 'Body size MB'}), bodySize)),
      kind === 'api' ? h('label', {class: 'field'}, h('span', {text: 'Upstream'}), upstream) : null,
      kind === 'redirect' ? h('label', {class: 'field'}, h('span', {text: 'Redirect target host'}), redirect) : null,
      kind === 'site_api' ? h('label', {class: 'field'}, h('span', {text: 'Routes (path | upstream-id)'}), routes, h('small', {text: upstreams.map((row) => `#${row.id} ${row.name}`).join(' · ')})) : null,
      ['site', 'site_api'].includes(kind) ? h('label', {class: 'check-field'}, spa, h('span', {text: 'Single-page app fallback'})) : null,
      ['api', 'site_api'].includes(kind) ? h('label', {class: 'check-field'}, serveStatic, h('span', {text: 'Serve /static/ locally'})) : null,
      h('label', {class: 'check-field'}, enabled, h('span', {text: 'Enable immediately'})), message, h('div', {class: 'form-actions'}, create));
  }
  shapes();
}

async function vhostDetail(row, reload) {
  const detail = await api(`/api/edge/vhost/${row.id}?graph=default`);
  const form = new FormView({fields: [
    {name: 'pool', label: 'Fleet pool', required: true},
    {name: 'body_size_mb', label: 'Maximum body size MB', type: 'number', min: 1, max: 4096},
    {name: 'spa', label: 'Single-page app fallback', type: 'checkbox'},
    {name: 'serve_static', label: 'Serve /static/ locally', type: 'checkbox'},
    {name: 'is_enabled', label: 'Vhost enabled', type: 'checkbox'},
  ], value: detail, submitLabel: 'Save and publish', onSubmit: async (values) => {
    values.body_size_mb = Number(values.body_size_mb);
    await apiOnce(`/api/edge/vhost/${detail.id}`, {method: 'POST', body: JSON.stringify(values)});
    close(); await reload();
  }});
  const close = openModal({title: detail.server_name, subtitle: `${detail.kind.replaceAll('_', ' ')} · shape is immutable; delete and recreate to change it.`, content: form.render()});
}

async function hostingProof(ctx) {
  if (!ctx.capabilities.setup) return null;
  try {
    const report = await api('/api/account/admin/setup/readiness?section=hosting_vhosts'); const section = report.sections?.[0];
    return h('section', {class: 'convergence-card'}, icon('activity'), h('div', {}, h('span', {text: 'Hosting convergence'}), h('strong', {text: section?.checks?.[0]?.explanation || 'Readiness has no hosting evidence yet.'})), statusBadge(section?.status || 'pending'));
  } catch (_) { return null; }
}

function edgeHttpPosture(ctx) {
  const edge = ctx.edge || {};
  const known = edge.available === true && typeof edge.http_enabled === 'boolean';
  const http = known ? (edge.http_enabled ? 'enabled' : 'disabled') : 'unknown';
  const issuance = edge.available === true && edge.dnsman_issuance ? edge.dnsman_issuance : 'unknown';
  return h('section', {class: 'convergence-card'}, icon('certificate'), h('div', {},
    h('span', {text: 'Certificate serving posture'}),
    h('strong', {text: `Public HTTP vhosts on this node: ${http}`}),
    h('small', {class: 'muted', text: `DNSMAN issuance: ${issuance}`})),
  statusBadge(known ? 'healthy' : 'pending'));
}

async function vhostsPage(ctx) {
  const root = h('div', {class: 'page'});
  async function render() {
    root.replaceChildren(pageHeader('Network & hosting', 'Vhosts', 'Structured serving shapes that publish one desired generation to the fleet.', [
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => createVhostWizard(ctx, render)}, icon('plus'), 'Create Vhost') : null,
    ]));
    root.append(edgeHttpPosture(ctx));
    const proof = await hostingProof(ctx); if (proof) root.append(proof);
    const panel = tablePanel('Serving configuration', 'API, Site, Site + API, and Redirect are the only supported shapes.'); root.append(panel);
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadVhosts();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No Vhosts are configured.', onSelect: ctx.capabilities.manage_network ? (row) => vhostDetail(row, render) : null, columns: [
        {label: 'Hostname', render: (row) => h('div', {}, h('strong', {text: row.server_name}), h('small', {text: row.domain?.name || ''}))},
        {label: 'Shape', render: (row) => badge(row.kind.replaceAll('_', ' '))},
        {label: 'Pool', render: (row) => h('span', {class: 'mono', text: row.pool})},
        {label: 'Status', render: (row) => statusBadge(row.is_enabled ? 'active' : 'inactive')},
        {label: 'Routes', render: (row) => h('a', {class: 'button compact', href: `#/routes?vhost=${row.id}`, onclick: (event) => event.stopPropagation()}, icon('route'), 'Open')},
      ]}).render());
    }, {message: 'Loading Vhosts…', retry: render});
  }
  await render(); return root;
}

function routeDialog(vhosts, upstreams, reload) {
  const form = new FormView({fields: [
    {name: 'vhost', label: 'Site + API Vhost', type: 'select', required: true, placeholder: 'Choose a Vhost', options: vhosts.filter((row) => row.kind === 'site_api').map((row) => ({value: row.id, label: row.server_name}))},
    {name: 'path_prefix', label: 'Path prefix', required: true, placeholder: '/api'},
    {name: 'upstream', label: 'Upstream', type: 'select', required: true, placeholder: 'Choose an upstream', options: upstreams.filter((row) => row.is_enabled).map((row) => ({value: row.id, label: row.name}))},
  ], submitLabel: 'Create route', onSubmit: async (values) => {
    const desired = {vhost: Number(values.vhost), path_prefix: values.path_prefix, upstream: Number(values.upstream)};
    const upstreamName = upstreams.find((row) => row.id === desired.upstream)?.name;
    rememberRoute(desired, upstreamName);
    await ensureRoute(desired, () => apiOnce('/api/edge/route', {method: 'POST', body: JSON.stringify(desired)}));
    forgetRoute(desired); close(); await reload();
  }});
  const close = openModal({title: 'Create route', subtitle: 'The path selects a platform-declared destination; arbitrary proxy targets are impossible.', content: form.render()});
}

async function repairRoutes(vhost, reload) {
  const pending = partialRoutes.get(vhost) || []; const remaining = [];
  for (const row of pending) {
    try {
      const desired = {vhost, path_prefix: row.path_prefix, upstream: row.upstream};
      await ensureRoute(desired, () => apiOnce('/api/edge/route', {method: 'POST', body: JSON.stringify(desired)}));
      forgetRoute(desired);
    } catch (error) { remaining.push({...row, error: error.message}); }
  }
  if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
  writeRouteRepairs();
  await reload();
}

async function deleteRoute(row, reload) {
  const confirmed = await confirmAction({title: `Delete ${row.path_prefix}?`, copy: 'Requests below this prefix will fall back to the static site.', confirmLabel: 'Delete route', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    let mutationError = null;
    try { await apiOnce(`/api/edge/route/${row.id}`, {method: 'DELETE'}); } catch (error) { mutationError = error; }
    const remaining = await loadRoutes();
    if (remaining.some((item) => item.id === row.id)) throw new Error(mutationError ? `Route deletion was not confirmed: ${mutationError.message}` : 'Route deletion was not visible in authoritative state.');
    await reload();
  }, {
    key: `route-delete:${row.id}`,
    busy: {title: `Deleting ${row.path_prefix}…`, detail: 'Deleting, then reading authoritative state back.'},
    onError: (error) => mutationFailed('The route was not deleted', error),
  });
}

async function routesPage(ctx) {
  const root = h('div', {class: 'page'});
  async function render() {
    const [vhosts, upstreams, rows] = await Promise.all([loadVhosts(), loadUpstreams(), loadRoutes()]);
    [...partialRoutes.entries()].forEach(([vhost, plans]) => {
      const remaining = plans.flatMap((plan) => {
        const state = routeState(rows, {vhost, ...plan}).state;
        if (state === 'applied') return [];
        return [{...plan, error: state === 'mismatch' ? `${plan.path_prefix} points to a different authoritative upstream; review it before repair.` : plan.error}];
      });
      if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
    });
    writeRouteRepairs();
    const filter = queryParam('vhost'); const filtered = filter ? rows.filter((row) => String(row.vhost?.id || row.vhost) === filter) : rows;
    root.replaceChildren(pageHeader('Network & hosting', 'Routes', 'Path-prefix proxy rules for Site + API Vhosts, created and repaired sequentially.', [
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => routeDialog(vhosts, upstreams, render)}, icon('plus'), 'Create route') : null,
    ]));
    [...partialRoutes.entries()].forEach(([vhost, pending]) => {
      const detail = pending.map((row) => row.error).filter(Boolean).join(' · ');
      // The callout this button lives in is rebuilt by render(), and the repair
      // walks one route at a time — a second click mid-walk would double-write.
      root.append(h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: `${pending.length} route(s) need repair`}), h('p', {text: detail || 'The Vhost and earlier routes remain in place. Retry only the missing rows.'})), h('button', {class: 'button compact', onclick: () => runAction(null, () => repairRoutes(vhost, render), {
        key: `route-repair:${vhost}`,
        busy: {title: 'Repairing routes…', detail: 'Retrying only the rows that did not land, one at a time.'},
        onError: (error) => mutationFailed('The routes were not repaired', error),
      })}, 'Repair routes')));
    });
    const panel = tablePanel('Proxied paths', filter ? 'Filtered to one Vhost. Clear the filter from the sidebar Routes link.' : 'Longest matching prefix wins.'); root.append(panel);
    panel.append(new TableView({rows: filtered, empty: 'No proxied routes are configured.', columns: [
      {label: 'Vhost', render: (row) => row.vhost?.server_name || `Vhost ${row.vhost?.id || row.vhost}`},
      {label: 'Path', render: (row) => h('code', {text: row.path_prefix})},
      {label: 'Upstream', render: (row) => row.upstream?.name || `#${row.upstream?.id || row.upstream}`},
      {label: 'Modified', render: (row) => formatDate(row.modified)},
      {label: '', render: (row) => ctx.capabilities.manage_network ? h('button', {class: 'icon-button danger-text', 'aria-label': `Delete route ${row.path_prefix}`, onclick: () => deleteRoute(row, render)}, icon('trash')) : null},
    ]}).render());
  }
  // Three reads before the page can paint anything, and a failure that used to
  // be a bare sentence with no way back.
  async function load() {
    await loadInto(root, () => render(), {message: 'Loading routes…', retry: load});
  }
  await load();
  return root;
}

export async function networkPage(ctx, route) {
  await ensureGroupChoices(ctx);
  if (route === 'domains') return domainsPage(ctx);
  if (route === 'credentials') return credentialsPage(ctx);
  if (route === 'dns') return dnsPage(ctx);
  if (route === 'certificates') return certificatesPage(ctx);
  if (route === 'upstreams') return upstreamsPage(ctx);
  if (route === 'vhosts') return vhostsPage(ctx);
  return routesPage(ctx);
}
