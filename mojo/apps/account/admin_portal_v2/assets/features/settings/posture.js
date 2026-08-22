// Settings ▸ Security & operations ▸ Security posture — the read-only
// inspector v1 kept behind the Dashboard's Incidents row
// (dashboard/inspectors.js openSecurityInspector).
//
// It belongs beside the security settings it reports on: the catalog already
// carries the SECURE_POSTURE descriptor as a configured value, and this is the
// live evidence of what that configuration is actually doing right now — which
// controls are on, whether cron is beating, and whether monitoring proved a
// delivery. Same endpoint, same permission tier, same words.
//
// Rules carried over from v1, none of them relaxed:
//   1. Security evidence is its own permission tier and is never part of any
//      other payload — it is read here, on demand, and only by a caller the
//      platform-security tier accepts.
//   2. An envelope with no data is UNAVAILABLE. Printing "No heartbeat
//      recorded" from an empty payload would report absent evidence as a
//      finding.
//   3. A read that fails renders as a failure with a Retry, never as a clean
//      posture.

import {api, formatDate, h} from '../../core.js';
import {backPill} from '../../app.js';
import {loadInto} from '../../components/actions.js';
import {statusRow} from '../../components/rows.js';

const SECURITY_PATH = '/api/account/admin/platform?sections=security';

/** The catalog section this row joins, and the route state that opens it. */
export const POSTURE_SECTION = 'Security & operations';
export const POSTURE_FOCUS = 'posture';
export const POSTURE_CORPUS = 'security posture cron heartbeat monitoring '
  + 'delivery probe secure https redirect hsts session cookie csrf open '
  + 'incidents disabled controls';

/** v1's gate: the platform-security tier, reported by the bootstrap payload. */
export function postureAvailable(ctx) {
  return ctx?.features?.platform?.capabilities?.security === true;
}

// The row says what is on the other side. It deliberately carries no tone and
// no summary value: the posture is unknown until the drill-in reads it, and a
// row that guessed "healthy" would be the one lie this whole view exists to
// avoid.
export function postureRow(href) {
  const node = statusRow({
    tone: 'muted', name: 'Security posture',
    value: 'Cron heartbeat, monitoring delivery proof, and which posture '
      + 'controls are on right now',
    action: {label: 'Review', href},
  });
  node.setAttribute('data-setting-key', 'security_posture');
  return node;
}

// The same chrome settings panels wear — a copy rather than an import, because
// panels.js keeps it private and one drill-in is not worth widening its API.
function panel(title, subtitle, ...body) {
  return h('div', {class: 'page row-page settings-page settings-panel'},
    backPill('Settings', 'settings'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Settings'}),
        h('h1', {text: title, tabindex: '-1'}),
        subtitle ? h('p', {text: subtitle}) : null)),
    ...body.filter(Boolean));
}

// v1's plainReason: the collector's own word for why a section carries no
// evidence, with the underscores taken out.
function plainReason(section) {
  const reason = section?.reason || section?.status || '';
  return String(reason).replaceAll('_', ' ');
}

// v1's factList: a fact with no value is absent rather than blank — an empty
// row reads as evidence of nothing.
function factList(rows) {
  const entries = (rows || []).filter((row) =>
    Array.isArray(row) && row[1] != null && row[1] !== '');
  if (!entries.length) return null;
  return h('dl', {class: 'settings-details'}, ...entries.map(([label, value]) =>
    h('div', {},
      h('dt', {text: label}),
      h('dd', {text: String(value)}))));
}

function technicalDetails(data) {
  return h('details', {class: 'settings-technical disclosure'},
    h('summary', {text: 'Technical details'}),
    h('pre', {class: 'evidence-json', text: JSON.stringify(data ?? {}, null, 2)}));
}

function controlList(posture) {
  const controls = posture?.controls || {};
  const names = Object.keys(controls);
  if (!names.length) {
    return h('p', {class: 'settings-note',
      text: 'No secure-posture evidence was returned.'});
  }
  return h('ul', {class: 'settings-posture-list'}, ...names.map((name) => h('li', {},
    h('span', {class: `status-dot ${controls[name] ? 'success' : 'warning'}`}),
    h('span', {text: name.replaceAll('_', ' ')}),
    h('span', {class: 'settings-note', text: controls[name] ? 'enabled' : 'disabled'}))));
}

function postureView(section) {
  const data = section?.data || {};
  if (!Object.keys(data).length) {
    return [
      h('p', {class: 'settings-lead', text: 'Security posture unavailable'}),
      h('p', {class: 'settings-note', text: plainReason(section) || 'no evidence'}),
    ];
  }
  const beat = data.cron_heartbeat || {};
  const delivery = data.monitoring_delivery || {};
  const disabled = data.secure_posture?.disabled || [];
  // The one deviation from v1's wording, and it is in the honest direction:
  // v1 formatted a missing delivery timestamp through formatDate, which prints
  // "Never" — an assertion about deliveries, made from a field the envelope
  // simply did not carry. A part that is absent is left out instead.
  const deliveryText = [delivery.status || 'unknown',
    delivery.observed_at ? formatDate(delivery.observed_at) : null]
    .filter(Boolean).join(' · ');
  return [
    section?.observed_at
      ? h('p', {class: 'settings-note', text: `Observed ${formatDate(section.observed_at)}`})
      : null,
    factList([
      ['Open incidents', data.open_incidents?.count],
      ['Cron heartbeat', beat.present
        ? `${beat.state || 'unknown'} · ${beat.age_seconds == null ? 'age unknown' : `${beat.age_seconds}s ago`}`
        : 'No heartbeat recorded'],
      ['Monitoring delivery', delivery.present
        ? deliveryText : 'No delivery proof recorded'],
      ['Disabled controls', disabled.length
        ? disabled.map((value) => value.replaceAll('_', ' ')).join(', ') : 'None'],
    ]),
    h('div', {class: 'settings-posture'},
      h('h2', {class: 'settings-posture-head', text: 'Secure posture'}),
      controlList(data.secure_posture)),
    technicalDetails(data),
  ].filter(Boolean);
}

/**
 * The drill-in. Read-only: nothing here writes, and every control it reports
 * on is changed in Django production settings and deployed, exactly as the
 * catalog's own SECURE_POSTURE row says.
 */
export function posturePanel(ctx, signal = null) {
  const slot = h('div', {class: 'settings-posture-slot'});
  const root = panel('Security posture',
    'What this installation\'s security controls are doing right now, read '
    + 'from the platform collector when you opened this page.',
    slot);
  if (!postureAvailable(ctx)) {
    // v1's sentence, unchanged. A refusal is an answer; a blank panel is not.
    slot.replaceChildren(h('p', {class: 'settings-lead',
      text: 'Your role cannot read platform security evidence.'}));
    return root;
  }
  function load() {
    return loadInto(slot, async (current) => {
      const report = await api(SECURITY_PATH, {signal});
      if (current()) slot.replaceChildren(...postureView(report?.sections?.security));
    }, {message: 'Loading security posture…', retry: () => load()});
  }
  load();
  return root;
}
