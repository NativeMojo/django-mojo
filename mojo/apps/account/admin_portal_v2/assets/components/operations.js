// The global operation store.
//
// Anything that takes longer than a click lives here, not in a toast that can
// be missed and not in one page's private state. Feature pages that poll an
// endpoint reporting a running operation upsert it; the banner under the topbar
// renders whatever the store holds, on every page.
//
// This module does NOT poll. It has no idea which endpoints exist — a store
// that invented operations would show operations that are not running, and the
// portal's one rule is that a screen never claims more than an endpoint said.
//
// An operation is: {id, title, phase, startedAt, href}
//   id        stable string, unique per operation (the provider's operation id)
//   title     what is happening, in words ("Adding a database reader")
//   phase     what the provider last reported ("RDS reports: creating")
//   startedAt epoch ms; elapsed is derived at render, never stored
//   href      where to go to watch it (a route hash), optional

import {h} from '../core.js';

const operations = new Map();
const listeners = new Set();

function announce() {
  for (const listener of [...listeners]) {
    try {
      listener(list());
    } catch (_) {
      // A broken subscriber must not stop the others from being told.
    }
  }
}

/** Every known operation, oldest first. */
export function list() {
  return [...operations.values()].sort(
    (left, right) => (left.startedAt || 0) - (right.startedAt || 0));
}

/**
 * Subscribe to store changes. Returns an unsubscribe function — call it from
 * the page's dispose(), or the banner outlives the page that fed it.
 */
export function subscribe(listener) {
  if (typeof listener !== 'function') throw new Error('operations.subscribe needs a function');
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Add or replace one operation. Only the fields a caller actually knows are
 * kept: a missing phase stays missing rather than becoming a guess.
 */
export function upsert(operation) {
  const id = operation && operation.id != null ? String(operation.id) : '';
  if (!id) throw new Error('an operation needs a stable id');
  const startedAt = Number(operation.startedAt);
  operations.set(id, {
    id,
    title: operation.title ? String(operation.title) : 'Operation running',
    phase: operation.phase ? String(operation.phase) : '',
    startedAt: Number.isFinite(startedAt) ? startedAt : Date.now(),
    href: typeof operation.href === 'string' ? operation.href : '',
  });
  announce();
}

/** Forget one operation — it finished, failed, or was cancelled. */
export function remove(id) {
  if (operations.delete(String(id))) announce();
}

/** Forget everything. Used when the portal reloads its context. */
export function clear() {
  if (!operations.size) return;
  operations.clear();
  announce();
}

function elapsedMinutes(startedAt) {
  return Math.max(0, Math.round((Date.now() - startedAt) / 60000));
}

function bannerRow(rows) {
  const lead = rows[0];
  const count = `${rows.length} operation${rows.length === 1 ? '' : 's'} running`;
  const detail = [lead.title, lead.phase, `${elapsedMinutes(lead.startedAt)} min`]
    .filter(Boolean).join(' · ');
  return h('div', {class: 'op-banner', role: 'status', 'aria-live': 'polite'},
    h('span', {class: 'spin', 'aria-hidden': 'true'}),
    h('span', {text: `${count} — `}),
    h('span', {class: 'op-detail', text: detail}),
    lead.href ? h('a', {class: 'op-view', href: lead.href}, 'View') : null);
}

/**
 * Render the banner into `container` and keep it in step with the store.
 * Returns a dispose function that stops listening.
 *
 * The container is emptied when nothing is running, so `.op-banner-host:empty`
 * takes no vertical space and the page below does not jump.
 */
export function renderOpBanner(container) {
  const paint = (rows) => {
    if (!rows.length) {
      container.replaceChildren();
      return;
    }
    container.replaceChildren(bannerRow(rows));
  };
  paint(list());
  return subscribe(paint);
}
