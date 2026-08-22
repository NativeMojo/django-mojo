import {formatDate, h, icon} from '../core.js';
import {runAction} from './actions.js';

export function skeletonState(message = 'Loading…', rows = 5) {
  return h('div', {class: 'loading skeleton-state state-view', role: 'status', 'aria-label': message},
    h('span', {class: 'sr-only', text: message}),
    h('div', {class: 'skeleton skeleton-heading', 'aria-hidden': 'true'}),
    ...Array.from({length: rows}, (_, index) => h('div', {
      class: `skeleton skeleton-row skeleton-row-${(index % 3) + 1}`, 'aria-hidden': 'true',
    })));
}
export function loadingState(message = 'Loading…') { return skeletonState(message); }
export function emptyState(title = 'Nothing here yet', copy = '') { return h('div', {class: 'empty state-view'}, h('strong', {text: title}), copy ? h('p', {text: copy}) : null); }
export function errorState(error, retry = null) {
  return h('div', {class: 'error-state state-view', role: 'alert'}, icon('alert'), h('strong', {text: 'This view could not load'}), h('p', {text: error?.message || String(error || 'Unknown error')}), retry ? h('button', {class: 'button compact', onclick: retry}, 'Retry') : null);
}

export function permissionDeniedState(copy = 'Your current role cannot read this source.') {
  return h('div', {class: 'state-view permission-denied', role: 'status'}, icon('lock'),
    h('strong', {text: 'Permission denied'}), h('p', {text: copy}));
}

export function degradedState(title, copy, action = null) {
  return h('div', {class: 'state-view degraded-state', role: 'status'}, icon('alert'),
    h('strong', {text: title}), h('p', {text: copy}), action);
}

// `onChange` may be sync or async; either way the clicked tab carries the
// pending state. It is deliberately NOT held across a repaint: every caller
// rebuilds its own nav, so a fresh tab bar re-derives the state rather than
// inheriting a stale one from a node that no longer exists. The tab keeps its
// label and its caller-owned `active` class — only aria-disabled, .is-pending
// and the announcement are ours.
export function sectionTabs({items, active, onChange, label = 'Sections'}) {
  return h('nav', {class: 'tabs', 'aria-label': label}, ...items.map((item) => h('button', {
    type: 'button', class: item.id === active ? 'active' : '', 'aria-current': item.id === active ? 'page' : null,
    text: item.label,
    onclick: (event) => runAction(event.currentTarget, () => onChange(item.id),
      {announceLabel: `Loading ${item.label}…`}),
  })));
}

export function timelineView(items = [], {titlePath = 'title', detailPath = 'detail', datePath = 'created'} = {}) {
  if (!items.length) return emptyState('No activity', 'Timeline entries will appear here.');
  return h('ol', {class: 'timeline-view'}, ...items.map((item) => h('li', {}, h('span', {class: 'timeline-dot'}),
    h('div', {}, h('strong', {text: item[titlePath] || 'Activity'}), item[detailPath] ? h('p', {text: item[detailPath]}) : null,
      h('time', {text: formatDate(item[datePath])})))));
}

export function relatedRecords(items = [], {label = (item) => item.name || item.title || `#${item.id}`, detail = () => '', onSelect = null} = {}) {
  if (!items.length) return emptyState('No related records');
  return h('div', {class: 'related-records'}, ...items.map((item) => h(onSelect ? 'button' : 'div', {
    class: 'related-record', type: onSelect ? 'button' : null, onclick: onSelect ? () => onSelect(item) : null,
  }, h('strong', {text: label(item)}), detail(item) ? h('span', {text: detail(item)}) : null, onSelect ? icon('chevron') : null)));
}
