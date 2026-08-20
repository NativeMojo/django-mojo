// Shared responsiveness affordances.
//
// A click that does real work must change the screen before the work finishes.
// Three rules shape everything below, and each of them is load-bearing:
//
//  * Nothing paints under 150ms. A pending state that appears and vanishes in
//    one frame reads as a glitch, not as progress.
//  * Anything that did paint is held for 250ms, so it cannot strobe.
//  * The affordance never goes on a node the handler is about to destroy.
//    Much of this portal re-renders through `replaceChildren`, and a pending
//    state parked inside the replaced subtree is invisible from the first
//    frame — see docs/django_developer/account/admin_portal/responsiveness.md.
//
// `aria-disabled` is used rather than `disabled`: the browser blurs a focused
// element the moment it becomes disabled, focus falls to <body>, and Tab
// restarts from the top of the document. The re-entry guard already stops the
// second submit, so nothing is lost by keeping the element focusable.

import {h} from '../core.js';
import {openBusy} from './overlays.js';
import {errorState, loadingState} from './views.js';

// Delay before a pending state paints, and the floor it is held for once it
// has. Both are in the docs as the portal-wide contract; changing one here
// changes every swept surface.
export const PENDING_DELAY = 150;
export const PENDING_HOLD = 250;

// One in-flight task per key. The key defaults to the paint target, so two
// different buttons are two different guards — a superseded *render* is the
// generation token's job (see loadInto), not this one's.
//
// Target and key come apart whenever one control paints for several distinct
// actions. actionMenu is the case: every item in a record's menu paints on the
// shared ••• trigger, so keying on the target would make one menu item's click
// silently return a different item's in-flight promise and never run. Those
// callers pass the clicked item as `key` and keep the trigger as the target.
const INFLIGHT = new Map();
const GENERATION = new WeakMap();

// The announcement region lives on <body>, deliberately outside any subtree a
// handler may replace. The refresh control that starts a repaint is destroyed
// by that repaint; an announcement parked inside it would die unread.
//
// role="status" — not aria-busy. `aria-busy="true"` marks a region unstable
// and suppresses announcements while it is set; it is not itself announced.
let announcer = null;

export function announce(message) {
  if (!announcer || !announcer.isConnected) {
    announcer = h('div', {class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true'});
    document.body.append(announcer);
  }
  // Re-setting the same string is not re-announced; clearing first is.
  announcer.textContent = '';
  if (message) announcer.textContent = message;
}

function spinner() {
  return h('span', {class: 'pending-spinner', 'aria-hidden': 'true'});
}

function spins(target) {
  // Icon-only controls spin in place; a labelled control just dims. Swapping
  // an icon for a word resizes the row underneath the pointer.
  return target.classList.contains('icon-button') || target.classList.contains('row-refresh');
}

// The visual half of a pending state. Nothing here happens until show() is
// called by the delay timer, so a fast action paints nothing at all.
function pendingPaint(target, pendingLabel, announceLabel) {
  const held = document.activeElement === target;
  let shownAt = 0;
  let original = null;
  return {
    get shown() { return shownAt > 0; },
    show() {
      if (shownAt || !target.isConnected) return;
      shownAt = Date.now();
      target.setAttribute('aria-disabled', 'true');
      target.classList.add('is-pending');
      if (pendingLabel) {
        original = [...target.childNodes];
        target.replaceChildren(spinner(), document.createTextNode(pendingLabel));
        announce(pendingLabel);
      } else {
        if (spins(target)) target.classList.add('is-pending-icon');
        announce(announceLabel || target.getAttribute('aria-label')
          || target.textContent.trim() || 'Working…');
      }
    },
    restore() {
      if (!shownAt) return;
      // Held for at least PENDING_HOLD, but never on the promise's own path:
      // `await reload()` must not become 250ms slower because a spinner blinked.
      const remaining = Math.max(0, PENDING_HOLD - (Date.now() - shownAt));
      shownAt = 0;
      setTimeout(() => {
        if (!target.isConnected) return;
        target.removeAttribute('aria-disabled');
        target.classList.remove('is-pending', 'is-pending-icon');
        if (original) target.replaceChildren(...original);
        original = null;
        // The element may have been re-rendered around; if it still owns the
        // interaction but lost the focus ring, put it back.
        if (held && document.activeElement !== target) target.focus?.({preventScroll: true});
      }, remaining);
    },
  };
}

async function execute(target, task, options) {
  const {busy = null, pendingLabel = '', announceLabel = '', onError = null,
    restoreOnSuccess = true, signal = null} = options;
  const paint = target instanceof Element
    ? pendingPaint(target, pendingLabel, announceLabel) : null;
  const timer = paint ? setTimeout(() => paint.show(), PENDING_DELAY) : null;
  // A busy scrim is token-keyed, so nesting is already safe. clearBusy() is
  // never called here: the shared client calls it on a 440, and this wrapper
  // must tolerate its own scrim having been destroyed underneath it — which
  // busy.close() already does.
  const scrim = busy ? openBusy(typeof busy === 'string' ? {title: busy} : busy) : null;
  let keep = false;
  try {
    const result = await task();
    keep = !restoreOnSuccess;
    return result;
  } catch (error) {
    // Swallowed only when OUR signal is the one that aborted. Matching on the
    // error name alone would eat a genuine failure from somebody else's fetch.
    if (signal?.aborted && error?.name === 'AbortError') return undefined;
    // 440: the shared client already prompted for fresh auth and already
    // retried. Holding the pending state here would disable the control
    // forever — restore it and render nothing.
    if (error?.code === 'fresh_auth_required') return undefined;
    if (typeof onError === 'function') { onError(error); return undefined; }
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
    scrim?.close();
    if (paint && !keep) paint.restore();
  }
}

/**
 * Run `task` with the portal's pending affordance attached to `target`.
 *
 * `target` may be null for a headless action — the guard, the busy scrim and
 * the error handling still apply, and no DOM write is attempted.
 *
 * Options: {key, busy, pendingLabel, announceLabel, onError,
 *           restoreOnSuccess = true, signal}
 * `key` identifies the action for the re-entry guard; it defaults to `target`
 * and is only passed separately when one control paints for several actions.
 * `pendingLabel` swaps the control's own label; `announceLabel` is what a
 * screen reader hears when the control keeps its label (a tab, an icon).
 */
export function runAction(target, task, options = {}) {
  // A headless call gets its own symbol so it is never blocked by an
  // unrelated headless call, and takes the identical code path.
  const key = options.key || target || Symbol('headless action');
  const running = INFLIGHT.get(key);
  // Registered synchronously — before the first await — so a second click in
  // the same turn gets the in-flight promise back instead of a second task.
  if (running) return running;
  const promise = execute(target, task, options).finally(() => { INFLIGHT.delete(key); });
  INFLIGHT.set(key, promise);
  return promise;
}

/**
 * The one clipboard affordance. A non-secure context rejects writeText, which
 * is silent everywhere it is called bare today.
 */
export function copyButton(text, {label = 'Copy', copiedLabel = 'Copied',
  className = 'button ghost compact'} = {}) {
  const button = h('button', {class: className, type: 'button'}, label);
  const setLabel = (value) => button.replaceChildren(document.createTextNode(value));
  button.addEventListener('click', () => runAction(button, async () => {
    if (!navigator.clipboard?.writeText) {
      throw new Error('Copying needs a secure (https) connection — select the text and copy it by hand.');
    }
    await navigator.clipboard.writeText(typeof text === 'function' ? text() : String(text ?? ''));
    setLabel(copiedLabel);
    announce(copiedLabel);
    setTimeout(() => { if (button.isConnected) setLabel(label); }, 1600);
  }, {
    onError: (error) => {
      setLabel('Copy failed');
      announce(error?.message || 'Copy failed');
      setTimeout(() => { if (button.isConnected) setLabel(label); }, 2400);
    },
  }));
  return button;
}

/**
 * First paint of a panel, tab or section: a loading state before the awaits,
 * an in-panel failure with a retry instead of stale content.
 *
 * `loader` is called with a `current()` predicate so a loader that paints its
 * own success content can drop a render a newer click has already superseded.
 */
export function loadInto(target, loader, {message = 'Loading…', retry = null} = {}) {
  if (!(target instanceof Element)) return Promise.resolve(loader?.(() => true));
  const mine = (GENERATION.get(target) || 0) + 1;
  GENERATION.set(target, mine);
  const current = () => GENERATION.get(target) === mine;
  // Same 150ms rule as the pending state: a synchronous or cached branch
  // never flashes a skeleton.
  const timer = setTimeout(() => {
    if (current() && target.isConnected) target.replaceChildren(loadingState(message));
  }, PENDING_DELAY);
  const again = retry || (() => loadInto(target, loader, {message, retry}));
  return Promise.resolve()
    .then(() => loader(current))
    .catch((error) => {
      if (!current()) return undefined;
      if (error?.name === 'AbortError') return undefined;
      // 440 is the shared client's business; it already prompted and retried.
      if (error?.code === 'fresh_auth_required') return undefined;
      target.replaceChildren(errorState(error, again));
      return undefined;
    })
    .finally(() => clearTimeout(timer));
}
