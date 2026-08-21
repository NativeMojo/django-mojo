// Plain language for one settings row.
//
// The old page had a single formatter that collapsed every value into one of
// four storage words. Those words say that a value exists; they never say what
// the platform does because of it. Everything here answers the reader's actual
// question instead: what is happening right now, in a sentence they could say
// out loud.
//
// The app owns two facts a browser cannot derive from a value: what an integer
// counts (`unit`) and what absence means (`unset_meaning`). Both arrive on the
// descriptor. Nothing here invents either.

import {h} from '../../core.js';
import {routeHref} from '../../components/routes.js';

const METHOD_NAMES = {
  password: 'Password', sms: 'SMS', passkey: 'Passkey', magic: 'Magic link',
  google: 'Google', apple: 'Apple', github: 'GitHub',
};

const SELF_SERVICE = {
  ALLOW_EMAIL_CHANGE: 'change their own email address',
  ALLOW_PHONE_CHANGE: 'change their own phone number',
  ALLOW_USERNAME_CHANGE: 'change their own username',
  ALLOW_SELF_DEACTIVATION: 'deactivate their own account',
};

// Topic panels own more than one descriptor each, so their rows drill in by
// topic name rather than by key.
const TOPIC_FOR_KEY = {AUTH_CONFIG: 'auth', EDGE_EXPECTED_TOPOLOGY: 'topology'};

function plural(count, word) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

function methodList(names) {
  return (names || []).map((name) => METHOD_NAMES[name] || name).join(', ');
}

export function hostOf(value) {
  try { return new URL(String(value)).host; } catch (_) { return String(value); }
}

export function agoText(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  if (Number.isNaN(then.valueOf())) return '';
  const seconds = Math.max(0, Math.round((Date.now() - then.valueOf()) / 1000));
  if (seconds < 90) return 'checked just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `checked ${plural(minutes, 'minute')} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `checked ${plural(hours, 'hour')} ago`;
  return `checked ${plural(Math.round(hours / 24), 'day')} ago`;
}

// A verification is a point-in-time fact. After this long it stops driving
// tone in either direction — stale success must not reassure and stale
// failure must not alarm; both degrade to a dated "last checked" note.
export const VERIFY_TONE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export function verifyIsCurrent(entry) {
  if (!entry || !entry.at) return false;
  const then = new Date(entry.at);
  if (Number.isNaN(then.valueOf())) return false;
  return Date.now() - then.valueOf() <= VERIFY_TONE_MAX_AGE_MS;
}

// Every entry receives a value that is present and meaningful — the guards in
// sentence() have already handled absent, ambiguous, and unreadable.
const SENTENCE = {
  DNSMAN_CERT_RENEW_DAYS: (value) => `${plural(Number(value), 'day')} before expiry`,

  EDGE_FRAMEWORK_VERSION: (value) => (String(value) === 'hold'
    ? 'Held on the last version the fleet converged on'
    : `Every deploy installs ${value}`),

  EDGE_EXPECTED_TOPOLOGY: (value) => {
    const nodes = (value.nodes || []).length;
    const pools = (value.pools || []).length;
    if (!nodes && !pools) return 'No fleet expected yet';
    return [nodes ? plural(nodes, 'node') : null,
      pools ? plural(pools, 'pool') : null].filter(Boolean).join(' · ');
  },

  EMAIL_DELIVERY_POSTURE: (value) => {
    const missing = Number(value.missing_template_count) || 0;
    const sender = value.default_sender_conflict ? 'more than one default sender'
      : value.default_sender_configured ? 'sender verified' : 'no default sender';
    const templates = value.templates_installed ? 'templates ready'
      : `${plural(missing, 'template')} missing`;
    return `SES · ${sender} · ${templates}`;
  },

  SECURE_POSTURE: (value) => {
    const cookies = value.SESSION_COOKIE_SECURE && value.CSRF_COOKIE_SECURE ? 'on'
      : value.SESSION_COOKIE_SECURE || value.CSRF_COOKIE_SECURE ? 'partly on' : 'off';
    return [`HTTPS redirect ${value.SECURE_SSL_REDIRECT ? 'on' : 'off'}`,
      `secure cookies ${cookies}`,
      `HSTS ${value.SECURE_HSTS_SECONDS ? 'on' : 'off'}`].join(' · ');
  },

  AUTH_CONFIG: (value) => {
    const methods = methodList(value.login?.methods);
    const open = value.registration?.enabled !== false
      ? 'registration open' : 'registration closed';
    return [methods || 'No sign-in method', open].join(' · ');
  },

  BASE_URL: (value) => String(value),
  WEBAPP_BASE_URL: (value) => String(value),
};

Object.keys(SELF_SERVICE).forEach((key) => {
  SENTENCE[key] = (value) => `Users ${value ? 'can' : 'cannot'} ${SELF_SERVICE[key]}`;
});

function generic(row, value) {
  if (row.sensitivity === 'configured_only') return 'Set';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (Array.isArray(value)) return value.join(' · ');
  if (row.value_type === 'integer' || typeof value === 'number') {
    return row.unit ? `${value} ${row.unit}` : String(value);
  }
  if (value && typeof value === 'object') return 'Set';
  return String(value);
}

function isUnset(value) {
  if (value == null || value === '') return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === 'object') {
    if ('configured' in value && Object.keys(value).length === 1) return !value.configured;
    return Object.keys(value).length === 0;
  }
  return false;
}

/**
 * The one sentence a row shows.
 *
 * The three guards run BEFORE any per-key formatter, and that order is the
 * contract: a conflicting, unreadable, or absent value has no meaning for a
 * formatter to describe, and letting one run first is exactly how a page ends
 * up confidently narrating a value the server refused to resolve.
 */
export function sentence(row) {
  if (row.duplicate_override) return 'Conflicting values saved — clear one';
  if (row.source === 'invalid') return 'Could not be read';
  const value = row.effective_value;
  if (isUnset(value)) {
    return row.unset_meaning ? `Not set — ${row.unset_meaning}` : 'Not set';
  }
  const formatter = SENTENCE[row.key];
  if (formatter) {
    try { return formatter(value); } catch (_) { return generic(row, value); }
  }
  return generic(row, value);
}

// An origin is read character by character, so it keeps the monospace face the
// rest of the portal gives addresses. A sentence never does.
export function isMono(row) {
  return (row.key === 'BASE_URL' || row.key === 'WEBAPP_BASE_URL' ||
    row.key === 'MOJO_INSTALLATION_UUID' || row.key === 'MOJO_INSTALLATION_SLUG') &&
    !row.duplicate_override && row.source !== 'invalid' && !isUnset(row.effective_value);
}

// Provenance belongs in the drill-in, not on the list — with one exception. A
// value nobody chose reads differently from the same value someone set, so the
// list keeps that single word.
export function defaultSuffix(row) {
  if (row.source !== 'default') return null;
  return h('span', {class: 'settings-note', text: ' · default'});
}

export function toneFor(row) {
  if (row.duplicate_override || row.source === 'invalid') return 'danger';
  return 'muted';
}

// The routes this portal actually serves. `owner_route` is v1's route name, and
// v1 has screens v2 has not built — System Setup above all. A "managed by" link
// naming one of those must open the current Admin, not a v2 hash that resolves
// to Home and silently loses the reader.
const V2_ROUTES = new Set([
  'home', 'apps', 'infrastructure', 'domains', 'access',
  'settings', 'settings-sms', 'settings-email',
]);

function ownerHref(row, ctx) {
  if (!row.owner_route) return null;
  const [route, query = ''] = row.owner_route.split('?', 2);
  if (V2_ROUTES.has(route)) {
    return {href: routeHref(route, Object.fromEntries(new URLSearchParams(query))),
      external: false};
  }
  return {href: `${ctx?.admin_path || '/admin/'}#/${row.owner_route}`, external: true};
}

/**
 * At most one thing to do per row, and a pointer when there is nothing.
 *
 * `href` present means a link; `href` null means muted text the operator
 * cannot act on here. Rows that are genuinely immutable get neither, because
 * "managed by" is information and identity is not going to change.
 */
export function actionFor(row, state = {}, ctx = null) {
  const detail = (route) => routeHref('settings', {...state, focus: route});
  if (row.duplicate_override && row.can_clear) {
    return {label: 'Clear conflicts', href: detail(row.key), tone: 'danger'};
  }
  if (row.can_write) {
    return {label: row.source === 'database' ? 'Edit' : 'Set', href: detail(row.key)};
  }
  if (row.can_owner_edit && TOPIC_FOR_KEY[row.key]) {
    return {label: 'Edit', href: detail(TOPIC_FOR_KEY[row.key])};
  }
  if (row.change_behavior === 'immutable') return null;
  const owner = ownerHref(row, ctx);
  if (row.writable === 'owner' && owner) {
    return {label: `managed by ${row.owner} →`, href: owner.href,
      external: owner.external, muted: true};
  }
  if (row.writable === 'none' && row.change_behavior === 'deploy') {
    return {label: 'managed by deployment', muted: true};
  }
  return {label: `managed by ${row.owner}`, muted: true};
}

// Where a row's own detail page lives. Topic rows collapse several descriptors,
// so they route to the topic rather than to any one key.
export function detailHref(row, state = {}) {
  return routeHref('settings', {...state, focus: TOPIC_FOR_KEY[row.key] || row.key});
}
