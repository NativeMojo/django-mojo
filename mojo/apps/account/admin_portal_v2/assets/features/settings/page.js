// Settings reads like a status page: one row per thing, one plain sentence
// each, one level down for anything you can change.
//
// This is v1's Settings catalog, ported whole — the same search, the same
// category chips, the same grouped rows, the same drill-in panels and the same
// per-row write gating (`can_write`, `can_owner_edit`, `can_clear`, all decided
// by the server from catalog_write / settings_owner_display /
// settings_owner_edit). Nothing here invents a setting or a permission.
//
// Two things are v2's:
//
//   1. Text messages and Email are full sub-pages here (`settings-sms`,
//      `settings-email`), because that is where the TEST tools live. v1's
//      settings page carried a second, mojo-only SMS editor that writes the
//      SAME system PhoneConfig row the messaging page writes — so the catalog
//      row now links to the one page that owns it instead of embedding a
//      duplicate editor. When the caller cannot reach that page, v1's embedded
//      editor is still what they get: no reader loses a control.
//   2. A caller who can reach an integration page but not the settings catalog
//      still gets a Settings destination — the integrations they can open, and
//      nothing pretending to be the rest.

import {api, apiOnce, h, icon, pageHeader} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {confirmAction} from '../../components/overlays.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {errorState, skeletonState} from '../../components/views.js';
import {actionFor, agoText, defaultSuffix, detailHref, hostOf, isMono,
  sentence, toneFor, verifyIsCurrent} from './language.js';
import {authPanel, geoipPanel, settingPanel, smsPanel, topologyPanel} from './panels.js';
import {POSTURE_CORPUS, POSTURE_FOCUS, POSTURE_SECTION, postureAvailable,
  posturePanel, postureRow} from './posture.js';

const INTEGRATIONS = 'Integrations';
// The six descriptors the GeoIP row speaks for. They keep their individual
// rows only when the reader cannot see the provider status that collapses them.
const GEOIP_KEYS = [
  'GEOIP_PRIMARY_PROVIDER', 'GEOIP_FALLBACK_PROVIDER', 'GEOIP_ADDITIONAL_PROVIDERS',
  'GEOIP_MOJO_PROVIDER_URL', 'GEOIP_MOJO_SYNC_ENABLED', 'GEOIP_API_KEY_MOJO',
];
const EMAIL_KEYS = ['EMAIL_DELIVERY_POSTURE', 'INCIDENT_EMAIL_FROM'];
const POSTURE_KEY = 'EMAIL_DELIVERY_POSTURE';

export const SMS_ROUTE = 'settings-sms';
export const EMAIL_ROUTE = 'settings-email';

/** The messaging sub-pages exist only when their bootstrap block does. */
export function smsAvailable(ctx) {
  return ctx?.features?.sms?.enabled === true;
}

export function emailAvailable(ctx) {
  return ctx?.features?.email?.enabled === true;
}

export function catalogAvailable(ctx) {
  return ctx?.features?.settings?.enabled === true;
}

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
    // A pointer at a screen v2 has not built says so, rather than dropping the
    // operator into different chrome unannounced.
    return {detailNode: h('span', {class: 'settings-owner-link'},
      h('a', {class: 'row-link muted', href: action.href, text: action.label}),
      action.external
        ? h('span', {class: 'settings-note', text: ' opens the current Admin'})
        : null)};
  }
  return {detail: action.label};
}

// `override` replaces the action language.js derived, and only that: a row
// whose editor moved to a sub-page still reads exactly as v1 wrote it.
function catalogRow(row, state, override = null, ctx = null) {
  const action = override || actionFor(row, state, ctx);
  const node = statusRow({
    tone: toneFor(row), name: row.label, mono: isMono(row),
    valueNode: [document.createTextNode(sentence(row)), defaultSuffix(row)],
    ...rightFor(action),
  });
  node.setAttribute('data-setting-key', row.key);
  return linkRow(node, override ? override.href : detailHref(row, state));
}

function verifyDetail(entry) {
  if (!entry) return {};
  if (!verifyIsCurrent(entry)) {
    const ago = agoText(entry.at);
    return ago ? {detail: `last ${ago}`} : {};
  }
  if (entry.ok) return {detail: agoText(entry.at)};
  return {detail: entry.message || 'Last check failed', detailTone: 'danger'};
}

function geoipRow(setup, state) {
  const geo = setup.geoip || {};
  const verify = (setup.verify_state || {}).geoip;
  const current = verifyIsCurrent(verify);
  const order = [geo.GEOIP_PRIMARY_PROVIDER, geo.GEOIP_FALLBACK_PROVIDER].filter(Boolean);
  const value = [
    order.length > 1 ? `${order[0]}, then ${order[1]}` : (order[0] || 'no provider chosen'),
    geo.GEOIP_MOJO_PROVIDER_URL ? hostOf(geo.GEOIP_MOJO_PROVIDER_URL) : null,
    geo.GEOIP_API_KEY_MOJO_CONFIGURED ? 'key set' : 'no key',
  ].filter(Boolean).join(' · ');
  const failing = Boolean(verify && !verify.ok && current);
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

// v1's embedded SMS editor, kept for the caller who cannot open the sub-page.
function smsRow(setup, state) {
  const sms = setup.sms || {};
  const verify = (setup.verify_state || {}).sms;
  const current = verifyIsCurrent(verify);
  const value = sms.configured
    ? [hostOf(sms.remote_url), sms.api_key_configured ? 'key set' : 'no key',
      sms.test_mode ? 'test mode' : null].filter(Boolean).join(' · ')
    : 'No SMS provider set up';
  const failing = Boolean(verify && !verify.ok && current);
  const href = routeHref('settings', {...state, focus: 'sms'});
  const node = statusRow({
    tone: failing ? 'danger' : 'muted', name: 'Text messages', value,
    ...verifyDetail(verify),
    action: {label: failing ? 'Fix' : sms.configured ? 'Edit' : 'Set up', href},
  });
  node.setAttribute('data-setting-key', 'sms');
  return linkRow(node, href);
}

// The same sentence v1 wrote, pointing at the page that owns the control.
function smsLinkRow(setup) {
  const sms = setup?.sms || null;
  const verify = (setup?.verify_state || {}).sms;
  const failing = Boolean(verify && !verify.ok && verifyIsCurrent(verify));
  const value = sms
    ? (sms.configured
      ? [hostOf(sms.remote_url), sms.api_key_configured ? 'key set' : 'no key',
        sms.test_mode ? 'test mode' : null].filter(Boolean).join(' · ')
      : 'No SMS provider set up')
    : 'Provider, connection test, and a test send';
  const href = `#/${SMS_ROUTE}`;
  const node = statusRow({
    tone: failing ? 'danger' : 'muted', name: 'Text messages', value,
    ...verifyDetail(verify),
    action: {label: failing ? 'Fix' : 'Open', href},
  });
  node.setAttribute('data-setting-key', 'sms');
  return linkRow(node, href);
}

// No posture descriptor to speak for: the row is the destination itself, so it
// says what is on the other side rather than pretending to carry a value.
function emailLinkRow() {
  const href = `#/${EMAIL_ROUTE}`;
  const node = statusRow({
    tone: 'muted', name: 'Email',
    value: 'Sender domains, the default sender, and a test send',
    action: {label: 'Open', href},
  });
  node.setAttribute('data-setting-key', 'email');
  return linkRow(node, href);
}

// Search still matches what an operator types, including the exact keys the
// list no longer prints. A collapsed row answers for everything it speaks for.
function corpusOf(row) {
  return `${row.label} ${row.description} ${row.key}`.toLowerCase();
}

function buildGroups(report, state, ctx) {
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
  }
  const smsCorpus = 'sms text message remote mojo provider phonehub twilio '
    + 'aws sns test connection send test message';
  if (smsAvailable(ctx)) {
    integrations.push({corpus: smsCorpus, build: () => smsLinkRow(setup)});
  } else if (setup) {
    integrations.push({corpus: smsCorpus, build: () => smsRow(setup, state)});
  }
  const emailOpen = emailAvailable(ctx)
    ? {label: 'Open', href: `#/${EMAIL_ROUTE}`} : null;
  lifted.forEach((key) => integrations.push({
    corpus: corpusOf(byKey[key]),
    build: () => catalogRow(byKey[key], state,
      emailOpen && key === POSTURE_KEY ? emailOpen : null, ctx),
  }));
  if (emailOpen && !lifted.has(POSTURE_KEY)) {
    integrations.push({
      corpus: 'email ses sender domain mailbox default sender send test email',
      build: () => emailLinkRow(),
    });
  }
  const groups = [{label: INTEGRATIONS, items: integrations}];
  (report.sections || []).forEach((section) => {
    const items = entries
      .filter((row) => row.section === section && !collapsed.has(row.key) &&
        !lifted.has(row.key))
      .map((row) => ({corpus: corpusOf(row),
        build: () => catalogRow(row, state, null, ctx)}));
    groups.push({label: section, items});
  });
  // The live security evidence joins the section whose settings it reports on.
  // If this installation's catalog has no such section, the row still needs a
  // home rather than being dropped — it gets its own group, last.
  if (postureAvailable(ctx)) {
    const href = routeHref('settings', {...state, focus: POSTURE_FOCUS});
    const item = {corpus: POSTURE_CORPUS,
      build: () => linkRow(postureRow(href), href)};
    const security = groups.find((group) => group.label === POSTURE_SECTION);
    if (security) security.items.push(item);
    else groups.push({label: POSTURE_SECTION, items: [item]});
  }
  return groups.filter((group) => group.items.length);
}

function panelFor(focus, report, actions, ctx) {
  // Read-only, and not a catalog descriptor: the posture drill-in reads the
  // platform collector itself, so it answers before any key lookup.
  if (focus === POSTURE_FOCUS) {
    return postureAvailable(ctx) ? posturePanel(ctx) : null;
  }
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
  // Own-key check: a focus like "constructor" must fall back to the list,
  // not resolve a prototype member into an empty panel.
  return Object.hasOwn(byKey, focus) ? settingPanel(byKey[focus], actions) : null;
}

// A caller who holds a messaging block but not the settings block still has a
// Settings destination. It offers exactly what they can open — no catalog read
// is attempted, because the server would refuse it.
function integrationsOnlyPage(ctx) {
  const rows = [
    smsAvailable(ctx) ? smsLinkRow(null) : null,
    emailAvailable(ctx) ? emailLinkRow() : null,
  ].filter(Boolean);
  // The posture evidence rides its own permission tier, not the catalog's, so
  // a caller who holds it keeps it even here — losing it with the catalog
  // would be the drill-in going missing for exactly the reader it is for.
  const postureHref = routeHref('settings', {focus: POSTURE_FOCUS});
  return h('div', {class: 'page row-page settings-page'},
    pageHeader('Configuration', 'Settings',
      'The integrations you can open on this installation.'),
    rowSection(INTEGRATIONS, rows),
    postureAvailable(ctx)
      ? rowSection(POSTURE_SECTION,
        [linkRow(postureRow(postureHref), postureHref)])
      : null,
    h('p', {class: 'settings-note',
      text: 'The full setting catalog needs the settings permission on this '
        + 'installation.'}));
}

export async function settingsPage(ctx, route, signal) {
  if (!catalogAvailable(ctx)) {
    // Its own tier, its own drill-in: the posture panel opens for this caller
    // too, without a catalog read the server would refuse.
    const focus = decodeRouteState().state.focus || '';
    if (focus === POSTURE_FOCUS && postureAvailable(ctx)) return posturePanel(ctx, signal);
    return integrationsOnlyPage(ctx);
  }
  const root = h('div', {class: 'page row-page settings-page'}, skeletonState('Loading Settings', 7));
  let statusText = '';
  async function load() {
    root.replaceChildren(skeletonState('Loading Settings', 7));
    try {
      // Every drill-in re-enters through here, so a panel always opens against
      // a freshly read expected_revision rather than one the list cached.
      const report = await api('/api/account/admin/settings', {signal});
      // Every save here invalidates the whole list and reloads it, and the
      // control that started it lives in a panel `load()` replaces — so the
      // affordance is the scrim, never a node. The guard key is the thing being
      // written, so two different settings are two different actions.
      const mutate = (payload) => runAction(null, async () => {
        await apiOnce('/api/account/admin/settings', {method: 'POST', body: JSON.stringify(payload)});
        statusText = `${payload.key} saved.`;
        await load();
      }, {key: `settings-mutate:${payload.action || 'set'}:${payload.key}`,
        busy: {title: 'Saving setting…', detail: 'The database override is being applied.'}});
      // Keyed on the write, not on the field names it writes: there are only
      // two owner payload shapes (`auth`, `edge_topology`), so keying on
      // Object.keys() made every auth save collide with every other auth save,
      // and a correction typed while the first was in flight silently returned
      // the first's promise and never ran.
      const owner = (payload) => runAction(null, async () => {
        await apiOnce('/api/account/admin/advanced/settings', {method: 'POST', body: JSON.stringify(payload)});
        statusText = 'Configuration saved.';
        await load();
      }, {key: `settings:owner:${JSON.stringify(payload)}`,
        busy: {title: 'Saving configuration…', detail: 'The typed owner is validating this change.'}});
      const configureProviders = (topic, providers) => runAction(null, async () => {
        await apiOnce('/api/account/admin/settings', {method: 'POST',
          body: JSON.stringify({action: 'configure_providers', topic, providers})});
        statusText = topic === 'geoip'
          ? 'GeoIP saved. Config-sync will roll the fleet restart.'
          : 'Text messaging saved.';
        await load();
      }, {key: `settings-providers:${topic}`,
        busy: {title: 'Saving…', detail: 'Writing the encrypted configuration for this integration.'}});
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
        const detail = panelFor(focus, report, actions, ctx);
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
      const groups = buildGroups(report, state, ctx);
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
        pageHeader('Configuration', 'Settings',
          'How this installation is configured. Every row says its current '
          + 'value and where it is managed.'),
        ...(report.setup_incomplete ? [h('div', {class: 'callout'}, icon('alert'),
          h('div', {}, h('strong', {text: 'Installation setup is incomplete'}),
            h('p', {text: 'Finish the required installation choices before tuning ongoing configuration.'})),
          h('a', {class: 'button compact', href: `${ctx.admin_path || '/admin/'}#/setup`},
            'Continue Setup'))] : []),
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
