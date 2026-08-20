import {h} from '../core.js';
import {runAction} from './actions.js';

// tone -> the shared .status-dot modifier. A calm page spends colour only on
// states that need words; 'muted' is the plain grey dot.
const DOT_TONE = {ok: 'success', warn: 'warning', danger: 'danger', muted: ''};

function dotClass(tone) {
  return `status-dot ${DOT_TONE[tone] ?? ''}`.trim();
}

function shortTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

export function rowLink(label, href) {
  return h('a', {class: 'row-link', href}, label);
}

// `sub` is one plain sentence about the section as a whole — what the rows
// under it have in common. Optional and third, so the ~19 two-argument call
// sites across the portal keep working untouched.
export function rowSection(label, rowNodes, {sub = ''} = {}) {
  const rows = (rowNodes || []).flat().filter(Boolean);
  if (!rows.length) return null;
  return h('section', {class: 'row-section'},
    label ? h('h2', {class: 'row-section-label', text: label}) : null,
    sub ? h('p', {class: 'row-section-sub', text: sub}) : null,
    h('div', {class: 'row-list'}, ...rows));
}

export function statusRow({tone = 'muted', name = '', value = '', valueNode = null,
  detail = '', detailTone = '', detailNode = null, action = null, mono = false} = {}) {
  // Held as a node rather than re-found later: `detailNode` is itself an
  // anchor on several rows (a "Details" drill-in beside a cross-page action),
  // so querySelector('.row-link') would bind the row to the wrong destination.
  const actionLink = action ? rowLink(action.label, action.href) : null;
  const right = [
    detailNode || (detail
      ? h('span', {class: `row-detail ${detailTone}`.trim(), text: detail})
      : null),
    actionLink,
  ].filter(Boolean);
  const row = h('div', {class: 'status-row', 'data-tone': tone},
    h('span', {class: dotClass(tone)}),
    h('span', {class: 'row-name', text: name}),
    valueNode
      ? h('span', {class: `row-value ${mono ? 'mono' : ''}`.trim()}, valueNode)
      : h('span', {class: `row-value ${mono ? 'mono' : ''}`.trim(), text: value}),
    h('span', {class: 'row-right'}, ...right));
  if (actionLink) makeRowNavigable(row, actionLink);
  return row;
}

// The whole row opens the thing it describes. Clicking a row's name and having
// nothing happen — while a small link at the far right is the only live target
// — is not how a list behaves anywhere else.
//
// The click is delegated to the row's own .row-link rather than duplicating its
// destination, so a row wired by wireAction() (href '#' plus a JS handler that
// preventDefaults) behaves identically to one carrying a real route href. One
// definition of what the row does, in the link that was already there.
function makeRowNavigable(row, link) {
  if (!link) return;
  row.classList.add('is-navigable');
  row.addEventListener('click', (event) => {
    // A click that landed on a control belongs to that control.
    if (event.target.closest('a,button,input,select,textarea,[role="menuitem"]')) return;
    // Checked here, not at construction: a caller that drives its own
    // navigation (settings' linkRow sets role="link" and moves the hash
    // itself) decorates the row AFTER statusRow has returned it. Two
    // navigations for one click agree today only by coincidence.
    if (row.getAttribute('role') === 'link') return;
    // Selecting text in a row is reading, not navigating.
    if (window.getSelection?.().toString()) return;
    const href = link.getAttribute('href');
    if (href && href !== '#') { link.click(); return; }
    // A caller-wired link carries href="#" and a handler that preventDefaults.
    // Clicking it as-is would ALSO take the anchor's default jump — and in a
    // hash-routed app an empty hash resets the route, so a row click would
    // navigate away from the page it is on. Dropping the attribute for the
    // duration of the dispatch runs the handlers with no default to take; it
    // is synchronous, so nothing renders in between.
    link.removeAttribute('href');
    try {
      link.click();
    } finally {
      // Restore only what was there. Writing '#' onto an anchor that had no
      // href would make it tabbable and turn a direct click into a hash reset.
      if (href != null) link.setAttribute('href', href);
    }
  });
}

// `actions` are the one or two things to do about what the headline just
// said, put where the operator is already looking. They are plain nodes the
// caller has already wired: nothing here may add role="status", aria-live, or
// a loading/error state, because onRefresh replaces this whole block and a
// live region announced from inside it would be destroyed before it spoke.
export function statusHeadline({tone = 'muted', message = '', sub = '',
  observedAt = '', onRefresh = null, actions = []} = {}) {
  const acts = (actions || []).flat().filter(Boolean);
  // One timestamp for the whole page: a per-row time invites the reader to
  // compare freshness between rows that were all read in the same pass.
  const asof = [
    observedAt ? h('span', {text: `as of ${shortTime(observedAt)}`}) : null,
    onRefresh ? h('button', {
      class: 'row-refresh', type: 'button', title: 'Refresh',
      'aria-label': 'Refresh',
      // onRefresh nearly always repaints the container this button lives in,
      // so a pending state pinned here would be destroyed before it painted.
      // runAction parks the announcement on a document-level live region that
      // survives the repaint, and null-checks this node before restoring it.
      onclick: (event) => runAction(event.currentTarget, () => onRefresh(),
        {announceLabel: 'Refreshing…'}),
    }, '↻') : null,
  ].filter(Boolean);
  return h('section', {class: 'status-headline-block'},
    h('div', {class: 'status-headline'},
      h('span', {class: dotClass(tone)}),
      h('strong', {text: message}),
      asof.length ? h('span', {class: 'row-asof'}, ...asof) : null),
    sub ? h('p', {class: 'status-sub', text: sub}) : null,
    acts.length ? h('div', {class: 'status-headline-actions'}, ...acts) : null);
}
