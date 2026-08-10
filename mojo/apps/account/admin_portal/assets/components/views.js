import {formatDate, h, icon} from '../core.js';

export function loadingState(message = 'Loading…') { return h('div', {class: 'loading state-view', role: 'status'}, h('span', {text: message})); }
export function emptyState(title = 'Nothing here yet', copy = '') { return h('div', {class: 'empty state-view'}, h('strong', {text: title}), copy ? h('p', {text: copy}) : null); }
export function errorState(error, retry = null) {
  return h('div', {class: 'error-state state-view', role: 'alert'}, icon('alert'), h('strong', {text: 'This view could not load'}), h('p', {text: error?.message || String(error || 'Unknown error')}), retry ? h('button', {class: 'button compact', onclick: retry}, 'Retry') : null);
}

export function sectionTabs({items, active, onChange, label = 'Sections'}) {
  return h('nav', {class: 'tabs', 'aria-label': label}, ...items.map((item) => h('button', {
    type: 'button', class: item.id === active ? 'active' : '', 'aria-current': item.id === active ? 'page' : null,
    text: item.label, onclick: () => onChange(item.id),
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
