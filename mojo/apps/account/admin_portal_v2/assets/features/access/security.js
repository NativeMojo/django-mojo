// Access ▸ Security activity — authentication evidence, where the people are.
//
// This tab does NOT re-implement Activity. It is the same recent-rows preview
// Home renders, on the two sources that answer "who has been reaching this
// installation, and what happened to their credentials":
//
//   * sign-ins — v1's People lane owned this read (`/api/account/logins`,
//     gated on `view_logins`) and had no page-level view of it: the evidence
//     existed only inside one user's record. It lives here now, whole
//     installation, newest first, and the per-user timeline stays where it was.
// A source this caller cannot read contributes no panel — an empty heading
// would read as "nothing has happened".

import {apiEnvelope, h, icon} from '../../core.js';
import {loadInto} from '../../components/actions.js';
import {capabilities} from './shared.js';

const PREVIEW_SIZE = 8;

function agoText(value) {
  const when = Date.parse(value);
  if (!Number.isFinite(when)) return '';
  const minutes = Math.max(0, Math.round((Date.now() - when) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? 'hr' : 'hrs'} ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

/** The `user` field is an id on some graphs and an object on others. */
function whoSignedIn(row) {
  const user = row.user;
  if (user && typeof user === 'object') {
    return user.display_name || user.username || (user.id != null ? `User ${user.id}` : '');
  }
  return user != null && user !== '' ? `User ${user}` : '';
}

function eventRow(text, detail) {
  return h('div', {class: 'access-event-row'},
    icon('activity'),
    h('span', {class: 'access-event-text', text}),
    h('span', {class: 'access-event-when', text: detail}));
}

function panel(title, copy, link, bodyNode) {
  return h('section', {class: 'panel'},
    h('div', {class: 'panel-head'},
      h('div', {}, h('h2', {text: title}), h('p', {text: copy})),
      link),
    bodyNode);
}

function signinsPanel() {
  const body = h('div', {class: 'panel-body'});
  const load = () => loadInto(body, async (current) => {
    const rows = (await apiEnvelope(
      `/api/account/logins?size=${PREVIEW_SIZE}&sort=-created`)).items;
    if (!current()) return;
    if (!rows.length) {
      body.replaceChildren(h('p', {text: 'No sign-ins have been recorded yet.'}));
      return;
    }
    body.replaceChildren(...rows.map((row) => eventRow(
      [row.source || 'Sign-in', row.ip_address || 'Unknown IP',
        [row.city, row.region, row.country_code].filter(Boolean).join(', '),
        row.is_new_country ? 'new country' : row.is_new_region ? 'new region' : '',
      ].filter(Boolean).join(' · '),
      [whoSignedIn(row), agoText(row.created)].filter(Boolean).join(' · '))));
  }, {message: 'Loading sign-ins…', retry: load});
  const node = panel('Recent sign-ins',
    'Who authenticated, from where, and whether the place was new.', null, body);
  load();
  return node;
}

export function securityTabVisible(ctx) {
  return capabilities(ctx).view_logins === true;
}

export function securityTab(ctx) {
  const caps = capabilities(ctx);
  return h('div', {class: 'access-tab'},
    caps.view_logins === true ? signinsPanel() : null);
}
