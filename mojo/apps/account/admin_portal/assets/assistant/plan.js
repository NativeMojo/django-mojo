// The plan tracker.
//
// It is NOT a block. `progress` is in the server's accepted block-type set but
// has no validator, no system-prompt documentation and no producer: the live
// tracker arrives as the assistant_plan / assistant_plan_update WebSocket
// events, and this module renders those.

import {h} from '../core.js';

const STATUSES = {pending: '○', in_progress: '◐', done: '✓', skipped: '⊘'};

function step(entry) {
  const status = STATUSES[entry?.status] ? entry.status : 'pending';
  return h('li', {class: `is-${status}`, 'data-step': String(entry?.id ?? '')},
    h('span', {'aria-hidden': 'true', text: STATUSES[status]}),
    h('div', {},
      h('span', {text: String(entry?.description ?? '')}),
      entry?.summary ? h('small', {text: String(entry.summary)}) : null));
}

export function planTracker(plan) {
  if (!plan || typeof plan !== 'object') return null;
  const steps = Array.isArray(plan.steps) ? plan.steps.slice(0, 40) : [];
  if (!steps.length) return null;
  const done = steps.filter((entry) => entry?.status === 'done').length;
  const node = h('div', {class: 'assistant-plan', 'data-plan': String(plan.plan_id || '')},
    h('header', {}, h('strong', {text: String(plan.title || 'Plan')}),
      h('span', {class: 'assistant-plan-count', text: `${done} of ${steps.length} complete`})),
    h('ol', {}, ...steps.map(step)));
  return node;
}

export function applyPlanUpdate(node, update) {
  if (!node || !update) return false;
  if (String(node.dataset.plan || '') !== String(update.plan_id || '')) return false;
  const row = node.querySelector(`li[data-step="${CSS.escape(String(update.step_id ?? ''))}"]`);
  if (!row) return false;
  const status = STATUSES[update.status] ? update.status : 'pending';
  row.className = `is-${status}`;
  row.firstChild.textContent = STATUSES[status];
  const body = row.lastElementChild;
  const summary = body.querySelector('small');
  if (update.summary) {
    if (summary) summary.textContent = String(update.summary);
    else body.append(h('small', {text: String(update.summary)}));
  } else if (summary) summary.remove();
  const rows = [...node.querySelectorAll('li')];
  const done = rows.filter((entry) => entry.classList.contains('is-done')).length;
  const counter = node.querySelector('.assistant-plan-count');
  if (counter) counter.textContent = `${done} of ${rows.length} complete`;
  return true;
}
