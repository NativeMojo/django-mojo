import {h} from '../core.js';

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

export function rowSection(label, rowNodes) {
  const rows = (rowNodes || []).flat().filter(Boolean);
  if (!rows.length) return null;
  return h('section', {class: 'row-section'},
    label ? h('h2', {class: 'row-section-label', text: label}) : null,
    h('div', {class: 'row-list'}, ...rows));
}

export function statusRow({tone = 'muted', name = '', value = '', valueNode = null,
  detail = '', detailTone = '', detailNode = null, action = null, mono = false} = {}) {
  const right = [
    detailNode || (detail
      ? h('span', {class: `row-detail ${detailTone}`.trim(), text: detail})
      : null),
    action ? rowLink(action.label, action.href) : null,
  ].filter(Boolean);
  return h('div', {class: 'status-row', 'data-tone': tone},
    h('span', {class: dotClass(tone)}),
    h('span', {class: 'row-name', text: name}),
    valueNode
      ? h('span', {class: `row-value ${mono ? 'mono' : ''}`.trim()}, valueNode)
      : h('span', {class: `row-value ${mono ? 'mono' : ''}`.trim(), text: value}),
    h('span', {class: 'row-right'}, ...right));
}

export function statusHeadline({tone = 'muted', message = '', sub = '',
  observedAt = '', onRefresh = null} = {}) {
  // One timestamp for the whole page: a per-row time invites the reader to
  // compare freshness between rows that were all read in the same pass.
  const asof = [
    observedAt ? h('span', {text: `as of ${shortTime(observedAt)}`}) : null,
    onRefresh ? h('button', {
      class: 'row-refresh', type: 'button', title: 'Refresh',
      'aria-label': 'Refresh', onclick: onRefresh,
    }, '↻') : null,
  ].filter(Boolean);
  return h('section', {class: 'status-headline-block'},
    h('div', {class: 'status-headline'},
      h('span', {class: dotClass(tone)}),
      h('strong', {text: message}),
      asof.length ? h('span', {class: 'row-asof'}, ...asof) : null),
    sub ? h('p', {class: 'status-sub', text: sub}) : null);
}
