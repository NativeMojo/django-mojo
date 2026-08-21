// The Assistant panel: a third grid child of #app, not a navigation lane.
//
// app.js render() replaces only the `content` node, so anything parked beside
// `main` survives every route change for free. It is deliberately NOT built on
// components/overlays.js: render() calls closeAllOverlays() on every
// navigation, so an overlay-based panel would be destroyed by the first click
// in the sidebar.
//
// Two modes, and the difference is not cosmetic:
//   docked (> 1100px)  role="complementary", NOT focus-trapped. A docked panel
//                      is a peer region; trapping focus in it would strand a
//                      keyboard user away from the page they are working on.
//   sheet  (<= 1100px) role="dialog" aria-modal, focus-trapped, Escape closes.
//                      A full-width sheet with no trap strands them behind it.
//
// aria-live is "off" on the aside and on the thread because #app is
// aria-live="polite" (index.html) and would otherwise narrate every streamed
// token. Turn boundaries are announced once each through the shared
// announce() helper instead.

import {h, icon} from '../core.js';
import {announce, runAction} from '../components/actions.js';

import {mountConversation} from './conversation.js';
import {mountSetup} from './setup.js';

const OPEN_KEY = 'mojo-admin-assistant-open';
const DOCKED_QUERY = '(min-width: 1101px)';
const FOCUSABLE = 'button:not([disabled]),input:not([disabled]),select:not([disabled]),'
  + 'textarea:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';

function storedOpen() {
  // A single boolean, never conversation content or ids: browser persistence
  // is not a place operator data goes.
  try { return sessionStorage.getItem(OPEN_KEY) === 'true'; } catch (_) { return false; }
}

function storeOpen(value) {
  try { sessionStorage.setItem(OPEN_KEY, value ? 'true' : 'false'); } catch (_) { /* private mode */ }
}

export function install({ctx, app}) {
  const capabilities = ctx.features?.assistant?.capabilities || {};
  const media = window.matchMedia(DOCKED_QUERY);
  let disposeBody = null;
  let open = false;

  const body = h('div', {class: 'assistant-body', 'aria-live': 'off'});
  const status = h('span', {class: 'assistant-status', role: 'status', 'aria-live': 'polite'});
  let controls = {};
  const newButton = h('button', {
    class: 'icon-button', type: 'button', 'aria-label': 'New conversation',
    onclick: (event) => runAction(event.currentTarget, () => controls.onNew?.(),
      {announceLabel: 'Starting a new conversation'}),
  }, icon('plus'));
  const historyButton = h('button', {
    class: 'icon-button', type: 'button', 'aria-label': 'Conversation history',
    onclick: (event) => runAction(event.currentTarget, () => controls.onHistory?.(),
      {announceLabel: 'Loading conversations'}),
  }, icon('activity'));
  const setupButton = h('button', {
    class: 'icon-button', type: 'button', 'aria-label': 'Assistant setup',
    onclick: (event) => runAction(event.currentTarget, () => showSetup(),
      {announceLabel: 'Opening Assistant setup'}),
  }, icon('settings'));
  const closeButton = h('button', {
    class: 'icon-button', type: 'button', 'aria-label': 'Close Assistant',
    onclick: () => setOpen(false),
  }, icon('close'));
  const head = h('div', {class: 'assistant-head'},
    icon('assistant'), h('strong', {text: 'Assistant'}), status,
    h('div', {class: 'assistant-head-actions'}, newButton, historyButton,
      capabilities.setup ? setupButton : null, closeButton));
  const aside = h('aside', {
    id: 'assistant-panel', class: 'assistant-panel', 'aria-live': 'off', hidden: true,
  }, head, body);

  const launcher = h('button', {
    class: 'icon-button', type: 'button', 'aria-label': 'Assistant',
    'aria-expanded': 'false', 'aria-controls': 'assistant-panel',
    onclick: () => setOpen(!open),
  }, icon('assistant'));

  function trapFocus(event) {
    if (!open || media.matches) return;
    if (event.key === 'Escape') { event.stopPropagation(); setOpen(false); return; }
    if (event.key !== 'Tab') return;
    const nodes = [...aside.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function applyMode() {
    if (media.matches) {
      aside.setAttribute('role', 'complementary');
      aside.setAttribute('aria-label', 'Assistant');
      aside.removeAttribute('aria-modal');
    } else {
      aside.setAttribute('role', 'dialog');
      aside.setAttribute('aria-label', 'Assistant');
      aside.setAttribute('aria-modal', 'true');
    }
  }

  function setOpen(next, {focus = true} = {}) {
    open = Boolean(next);
    storeOpen(open);
    aside.hidden = !open;
    app.classList.toggle('assistant-open', open && media.matches);
    launcher.setAttribute('aria-expanded', String(open));
    applyMode();
    if (!open) {
      session?.close?.();
      launcher.focus?.({preventScroll: true});
      return;
    }
    if (!disposeBody) disposeBody = mountBody();
    if (focus && !media.matches) {
      const target = aside.querySelector(FOCUSABLE);
      target?.focus?.({preventScroll: true});
    }
    announce('Assistant panel opened');
  }

  // Assigned by mountBody(); the setup view replaces it in place.
  let session = null;

  function showSetup() {
    if (!capabilities.setup) return Promise.resolve();
    if (typeof disposeBody === 'function') disposeBody();
    session = null;
    const view = mountSetup({ctx, panel: api, onBack: () => { disposeBody = mountBody(); }});
    disposeBody = () => view.dispose();
    return Promise.resolve();
  }

  function mountBody() {
    const mounted = mountConversation({ctx, panel: api});
    session = mounted;
    return () => mounted.dispose();
  }

  // Both, deliberately. matchMedia is the right signal, but a missed change
  // event would strand the sheet with a docked panel's role and no focus trap,
  // and `docked` is read fresh on every call so the resize listener is a
  // no-op whenever nothing actually changed.
  let docked = media.matches;
  const onModeChange = () => {
    if (media.matches === docked) return;
    docked = media.matches;
    app.classList.toggle('assistant-open', open && docked);
    applyMode();
  };
  media.addEventListener('change', onModeChange);
  window.addEventListener('resize', onModeChange);
  aside.addEventListener('keydown', trapFocus);

  function teardown() {
    media.removeEventListener('change', onModeChange);
    window.removeEventListener('resize', onModeChange);
    aside.removeEventListener('keydown', trapFocus);
    if (typeof disposeBody === 'function') disposeBody();
    disposeBody = null;
    session = null;
    app.classList.remove('assistant-open');
    aside.remove();
    launcher.remove();
  }

  const api = {
    aside,
    body,
    status,
    capabilities,
    ctx,
    setControls(next) { controls = next || {}; },
    showSetup: () => showSetup(),
    open: () => setOpen(true),
    close: () => setOpen(false),
    // Authority loss removes the panel outright rather than leaving a composer
    // whose every message the server will refuse.
    revoke: () => teardown(),
    dispose: teardown,
  };
  const topbarActions = app.querySelector('.topbar-actions');
  // Ahead of the theme toggle: the Assistant is the leftmost action so the
  // sign-out control stays the last thing in the row.
  topbarActions?.prepend(launcher);
  app.append(aside);
  applyMode();
  // Mounted only after `api` exists: the body is handed this object.
  if (storedOpen()) setOpen(true, {focus: false});

  return api;
}
