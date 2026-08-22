// Access — the pieces People, Groups, Keys and Security activity all use.
//
// Everything here is v1's features/people/page.js, lifted out so the four tabs
// can share it. The behaviour is unchanged: same endpoints, same one-time
// secret dialog, same capability names. Only the CSS class names are v2's, and
// the Activity cross-links now honour v2's Activity gates rather than v1's.

import {api, h, icon} from '../../core.js';
import {copyButton} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {activityHref, returnLocation, routeHref} from '../../components/routes.js';

// The same read Settings ▸ Email and the Domains email-identity panel make.
export const EMAIL_SUMMARY_URL = '/api/aws/email/summary';

/** v1's people capability block, verbatim — users, groups, manage_*, view_logins. */
export function capabilities(ctx) { return ctx.features?.people?.capabilities || {}; }

export function post(path, body) { return api(path, {method: 'POST', body: JSON.stringify(body)}); }

export function initials(row) { return (row.display_name || row.name || row.username || '?').slice(0, 2); }

export function detailGrid(rows) {
  return h('dl', {class: 'access-detail'}, ...rows.flatMap(([label, value]) => [
    h('dt', {text: label}), h('dd', {}, value instanceof Node ? value : String(value ?? '—')),
  ]));
}

/**
 * Whether v2's Activity sub-page will actually render the named tab.
 *
 * Stated here rather than imported from features/home/activity.js: a feature
 * module reaching into another feature's file makes the two load together, and
 * this is one boolean. It mirrors that file's `activityTabVisible` exactly —
 * incidents, events and tickets all come from `view_security`; logs from
 * `view_logs`.
 *
 * v1 gated these links on the PEOPLE block's own view_events/view_incidents/
 * view_tickets keys, which is what its own Activity page used. v2's Activity
 * page reads the activity block, so a link offered from a key the destination
 * does not consult would be a link to a tab that is not there.
 */
export function activityTabVisible(ctx, tab) {
  if (ctx.features?.activity?.enabled !== true) return false;
  if (!['incidents', 'events', 'tickets', 'logs'].includes(tab)) return false;
  const capability = tab === 'logs' ? 'view_logs' : 'view_security';
  return ctx.features.activity.capabilities?.[capability] === true;
}

/** The Activity lanes that carry evidence about one user or group. */
export function activityLinks(ctx, subject) {
  const lanes = ['logs', 'events', 'incidents', 'tickets']
    .filter((tab) => activityTabVisible(ctx, tab));
  if (!lanes.length) {
    return h('p', {class: 'muted',
      text: 'No related Activity lanes are available with your current access.'});
  }
  return h('div', {class: 'activity-links'},
    lanes.map((tab) => h('a', {
      class: 'related-record', href: activityHref(tab, subject, {return: returnLocation()}),
    }, h('strong', {text: tab[0].toUpperCase() + tab.slice(1)}), icon('chevron'))));
}

export function oneTimeSecret(title, label, value, returnFocus = null) {
  let secret = String(value || '');
  const input = h('input', {class: 'secret-value', readonly: true, value: secret,
    autocomplete: 'off', 'data-one-time-secret': 'true'});
  // A function, not the string: the dialog scrubs `secret` on close, and the
  // shared button reads it at click time so a copy after that copies nothing
  // rather than re-leaking a value the dialog promised to forget.
  const copy = copyButton(() => secret, {label: 'Copy once', copiedLabel: 'Copied',
    className: 'button primary'});
  const content = h('div', {class: 'secret-reveal'},
    h('p', {text: `${label} is displayed only in this dialog. Store it now.`}), input,
    h('div', {class: 'form-actions'}, copy));
  const close = openModal({title, content, returnFocus, onClose: () => {
    secret = ''; input.value = ''; copy.remove(); content.replaceChildren();
  }});
  return close;
}

/**
 * The honest note above the People list when nothing can send mail.
 *
 * One read — the same `/api/aws/email/summary` the Domains page and Settings ▸
 * Email already make, and only its persisted verdict. An invite is still
 * created when no sender is verified; what does not happen is the email. v1
 * has no copyable invite link to offer instead (`User.send_invite` builds the
 * token URL server-side and returns nothing), so the note says plainly that
 * the message will not be delivered rather than implying a workaround that
 * does not exist.
 *
 * A caller without the email block sees nothing: they cannot read the summary,
 * and a note about a fact this portal could not check would be a guess. A read
 * that fails is silent for the same reason — "could not check" is not "cannot
 * send".
 */
export function inviteWarning(ctx) {
  if (ctx.features?.email?.enabled !== true) return null;
  const host = h('div', {});
  api(EMAIL_SUMMARY_URL).then((report) => {
    const domains = report.domains || [];
    if (domains.some((domain) => domain.can_send)) return;
    const fix = ctx.features?.advanced?.enabled === true
      ? h('a', {href: routeHref('domains')}, 'see Domains')
      : ctx.features?.email?.enabled === true
        ? h('a', {href: routeHref('settings-email')}, 'see Settings ▸ Email') : null;
    host.replaceChildren(h('div', {class: 'access-callout danger', role: 'status'},
      icon('alert'),
      h('p', {},
        'Email can’t send yet (no verified sender', fix ? ' — ' : '', fix, '). ',
        'Invites are created, but the invite email won’t be delivered until '
        + 'that clears.')));
  }).catch(() => {});
  return host;
}
