// Settings reads like a status page: one row per thing, one plain sentence
// each, one level down for anything you can change.
//
// The card grid this replaced showed a key, a provenance badge, and one word
// meaning "a value exists" — three facts about storage and no answer to "what
// is this platform doing?". Everything technical still exists; it moved into
// the row's own panel, which is also where every editor now lives.

import {api, apiOnce, h, icon, pageHeader} from '../../core.js';
import {confirmAction, openBusy} from '../../components/overlays.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {errorState, skeletonState} from '../../components/views.js';
import {actionFor, agoText, defaultSuffix, detailHref, hostOf, isMono,
  sentence, toneFor} from './language.js';
import {authPanel, geoipPanel, settingPanel, smsPanel, topologyPanel} from './panels.js';

const INTEGRATIONS = 'Integrations';
// The six descriptors the GeoIP row speaks for. They keep their individual
// rows only when the reader cannot see the provider status that collapses them.
const GEOIP_KEYS = [
  'GEOIP_PRIMARY_PROVIDER', 'GEOIP_FALLBACK_PROVIDER', 'GEOIP_ADDITIONAL_PROVIDERS',
  'GEOIP_MOJO_PROVIDER_URL', 'GEOIP_MOJO_SYNC_ENABLED', 'GEOIP_API_KEY_MOJO',
];
const EMAIL_KEYS = ['EMAIL_DELIVERY_POSTURE', 'INCIDENT_EMAIL_FROM'];

// A row is a link. Clicks on its own controls are not.
function linkRow(node, href) {
  if (!href) return node;
  node.classList.add('is-linked');
  node.setAttribute('role', 'link');
  node.setAttribute('tabindex', '0');
  const go = () => { location.hash = href; };
  node.addEventListener('click', (event) => {
    if (event.target.closest('a, button, input, select, textarea, summary')) return;
    go();
  });
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); go(); }
  });
  return node;
}

function rightFor(action) {
  if (!action) return {};
  if (action.href && !action.muted) {
    return {action: {label: action.label, href: action.href}};
  }
  if (action.href) {
    return {detailNode: h('a', {class: 'row-link muted', href: action.href, text: action.label})};
  }
  return {detail: action.label};
}

function catalogRow(row, state) {
  const action = actionFor(row, state);
  const node = statusRow({
    tone: toneFor(row), name: row.label, mono: isMono(row),
    valueNode: [document.createTextNode(sentence(row)), defaultSuffix(row)],
    ...rightFor(action),
  });
  node.setAttribute('data-setting-key', row.key);
  return linkRow(node, detailHref(row, state));
}

function verifyDetail(entry) {
  if (!entry) return {};
  if (entry.ok) return {detail: agoText(entry.at)};
  return {detail: entry.message || 'Last check failed', detailTone: 'danger'};
}

function geoipRow(setup, state) {
  const geo = setup.geoip || {};
  const verify = (setup.verify_state || {}).geoip;
  const order = [geo.GEOIP_PRIMARY_PROVIDER, geo.GEOIP_FALLBACK_PROVIDER].filter(Boolean);
  const value = [
    order.length > 1 ? `${order[0]}, then ${order[1]}` : (order[0] || 'no provider chosen'),
    geo.GEOIP_MOJO_PROVIDER_URL ? hostOf(geo.GEOIP_MOJO_PROVIDER_URL) : null,
    geo.GEOIP_API_KEY_MOJO_CONFIGURED ? 'key set' : 'no key',
  ].filter(Boolean).join(' · ');
  const failing = Boolean(verify && !verify.ok);
  const href = routeHref('settings', {...state, focus: 'geoip'});
  const node = statusRow({
    tone: failing ? 'danger' : setup.pending_restart ? 'warn' : 'muted',
    name: 'GeoIP', value,
    ...(setup.pending_restart && !failing
      ? {detail: 'waiting for the config-sync restart', detailTone: 'warning'}
      : verifyDetail(verify)),
    action: {label: failing ? 'Fix' : 'Edit', href},
  });
  node.setAttribute('data-setting-key', 'geoip');
  return linkRow(node, href);
}

function smsRow(setup, state) {
  const sms = setup.sms || {};
  const verify = (setup.verify_state || {}).sms;
  const value = sms.configured
    ? [hostOf(sms.remote_url), sms.api_key_configured ? 'key set' : 'no key',
      sms.test_mode ? 'test mode' : null].filter(Boolean).join(' · ')
    : 'No SMS provider set up';
  const failing = Boolean(verify && !verify.ok);
  const href = routeHref('settings', {...state, focus: 'sms'});
  const node = statusRow({
    tone: failing ? 'danger' : 'muted', name: 'Text messages', value,
    ...verifyDetail(verify),
    action: {label: failing ? 'Fix' : sms.configured ? 'Edit' : 'Set up', href},
  });
  node.setAttribute('data-setting-key', 'sms');
  return linkRow(node, href);
}

// Search still matches what an operator types, including the exact keys the
// list no longer prints. A collapsed row answers for everything it speaks for.
function corpusOf(row) {
  return `${row.label} ${row.description} ${row.key}`.toLowerCase();
}

function buildGroups(report, state) {
  const entries = report.entries || [];
  const setup = report.provider_setup;
  const byKey = Object.fromEntries(entries.map((row) => [row.key, row]));
  const collapsed = setup ? new Set(GEOIP_KEYS) : new Set();
  const lifted = new Set(EMAIL_KEYS.filter((key) => byKey[key]));
  const integrations = [];
  if (setup) {
    integrations.push({
      corpus: `geoip ip intelligence provider ${GEOIP_KEYS.map(
        (key) => `${key} ${byKey[key] ? byKey[key].label : ''}`).join(' ')}`.toLowerCase(),
      build: () => geoipRow(setup, state),
    });
    integrations.push({
      corpus: 'sms text message remote mojo provider phonehub',
      build: () => smsRow(setup, state),
    });
  }
  lifted.forEach((key) => integrations.push({
    corpus: corpusOf(byKey[key]), build: () => catalogRow(byKey[key], state),
  }));
  const groups = [{label: INTEGRATIONS, items: integrations}];
  (report.sections || []).forEach((section) => {
    const items = entries
      .filter((row) => row.section === section && !collapsed.has(row.key) &&
        !lifted.has(row.key))
      .map((row) => ({corpus: corpusOf(row), build: () => catalogRow(row, state)}));
    groups.push({label: section, items});
  });
  return groups.filter((group) => group.items.length);
}

function panelFor(focus, report, actions) {
  if (focus === 'geoip') return report.provider_setup ? geoipPanel(report, actions) : null;
  if (focus === 'sms') return report.provider_setup ? smsPanel(report, actions) : null;
  const byKey = Object.fromEntries((report.entries || []).map((row) => [row.key, row]));
  if (focus === 'auth') {
    return byKey.AUTH_CONFIG ? authPanel(byKey.AUTH_CONFIG, actions) : null;
  }
  if (focus === 'topology') {
    return byKey.EDGE_EXPECTED_TOPOLOGY
      ? topologyPanel(byKey.EDGE_EXPECTED_TOPOLOGY, actions) : null;
  }
  return byKey[focus] ? settingPanel(byKey[focus], actions) : null;
}

export async function settingsPage(ctx, route, signal) {
  const root = h('div', {class: 'page row-page settings-page'}, skeletonState('Loading Settings', 7));
  let statusText = '';
  async function load() {
    root.replaceChildren(skeletonState('Loading Settings', 7));
    try {
      // Every drill-in re-enters through here, so a panel always opens against
      // a freshly read expected_revision rather than one the list cached.
      const report = await api('/api/account/admin/settings', {signal});
      const mutate = async (payload) => {
        const busy = openBusy({title: 'Saving setting…', detail: 'The database override is being applied.'});
        try { await apiOnce('/api/account/admin/settings', {method: 'POST', body: JSON.stringify(payload)}); statusText = `${payload.key} saved.`; await load(); }
        finally { busy.close(); }
      };
      const owner = async (payload) => {
        const busy = openBusy({title: 'Saving configuration…', detail: 'The typed owner is validating this change.'});
        try { await apiOnce('/api/account/admin/advanced/settings', {method: 'POST', body: JSON.stringify(payload)}); statusText = 'Configuration saved.'; await load(); }
        finally { busy.close(); }
      };
      const configureProviders = async (topic, providers) => {
        const busy = openBusy({title: 'Saving…', detail: 'Writing the encrypted configuration for this integration.'});
        try {
          await apiOnce('/api/account/admin/settings', {method: 'POST',
            body: JSON.stringify({action: 'configure_providers', topic, providers})});
          statusText = topic === 'geoip'
            ? 'GeoIP saved. Config-sync will roll the fleet restart.'
            : 'Text messaging saved.';
          await load();
        } finally { busy.close(); }
      };
      const testProviders = async (topic, providers) => apiOnce('/api/account/admin/settings', {
        method: 'POST', body: JSON.stringify({action: 'test_providers', topic, providers})});
      const clear = async (row, conflicts) => {
        const answer = await confirmAction({
          title: conflicts ? 'Clear conflicting values?' : 'Reset to the default?',
          copy: conflicts
            ? `This removes every conflicting global ${row.key} row and reveals the deployment or default value.`
            : `This removes the global ${row.key} override and reveals the deployment or default value.`,
          confirmLabel: conflicts ? 'Clear conflicts' : 'Reset', danger: true});
        if (answer.confirmed) await mutate({action: 'clear', key: row.key});
      };
      const actions = {mutate, owner, clear, configureProviders, testProviders};

      const decoded = decodeRouteState();
      const focus = decoded.state.focus || '';
      if (focus) {
        const detail = panelFor(focus, report, actions);
        // An unknown or unavailable focus is not an error page: the list is a
        // correct answer to "show me settings".
        if (detail) { root.replaceChildren(detail); return; }
      }

      const state = {};
      if (decoded.state.search) state.search = decoded.state.search;
      if (decoded.state.category) state.category = decoded.state.category;
      const search = h('input', {type: 'search', value: state.search || '',
        placeholder: 'Search settings', 'aria-label': 'Search settings'});
      const chips = h('div', {class: 'settings-categories', 'aria-label': 'Settings categories'});
      const list = h('div', {class: 'settings-list'});
      const empty = h('p', {class: 'settings-empty', text: 'No settings match this search.'});
      const groups = buildGroups(report, state);
      const categories = ['All', ...groups.map((group) => group.label)];
      let active = categories.includes(state.category) ? state.category : 'All';
      const render = () => {
        const term = search.value.trim().toLowerCase();
        state.search = term || undefined;
        state.category = active === 'All' ? undefined : active;
        const sections = groups
          .filter((group) => active === 'All' || group.label === active)
          .map((group) => rowSection(group.label, group.items
            .filter((item) => !term || item.corpus.includes(term))
            .map((item) => item.build())))
          .filter(Boolean);
        list.replaceChildren(...(sections.length ? sections : [empty]));
      };
      const renderChips = () => chips.replaceChildren(...categories.map((name) => h('button', {
        type: 'button', class: name === active ? 'active' : '',
        'aria-pressed': name === active ? 'true' : 'false',
        onclick: () => { active = name; renderChips(); render(); }}, name)));
      search.addEventListener('input', render); renderChips(); render();
      root.replaceChildren(
        pageHeader('Configuration', 'Settings', 'How this platform is configured.'),
        ...(report.setup_incomplete ? [h('div', {class: 'callout'}, icon('alert'),
          h('div', {}, h('strong', {text: 'Installation setup is incomplete'}),
            h('p', {text: 'Finish the required installation choices before tuning ongoing configuration.'})),
          h('a', {class: 'button compact', href: routeHref('setup')}, 'Continue Setup'))] : []),
        ...(statusText ? [h('div', {class: 'settings-success', role: 'status', text: statusText})] : []),
        h('div', {class: 'settings-toolbar'}, h('label', {class: 'search'}, icon('search'), search), chips),
        list);
    } catch (error) {
      if (error?.name !== 'AbortError' && error?.code !== 'fresh_auth_required') {
        root.replaceChildren(errorState(error, load));
      }
    }
  }
  await load(); return root;
}
