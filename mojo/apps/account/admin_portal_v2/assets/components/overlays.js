const STACK = [];
const BUSY = new Map();
let busyNode = null;
const FOCUSABLE = 'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value ?? '';
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : String(value));
  });
  children.flat().filter((child) => child != null).forEach((child) => node.append(child instanceof Node ? child : document.createTextNode(String(child))));
  return node;
}

function activeEntry() { return STACK[STACK.length - 1]; }

function keydown(event) {
  const entry = activeEntry();
  if (!entry) return;
  if (event.key === 'Escape') {
    if (entry.dismissible) { event.preventDefault(); entry.close(); }
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...entry.panel.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null);
  if (!focusable.length) { event.preventDefault(); entry.heading.focus(); return; }
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === entry.panel)) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
}

// One overlay shape, centered: the right-side inspector drawer is retired.
// A record is a page of its own now (Access, Apps, Domains all do it), and
// everything that is not a record — a form, a confirm, a read-only peek at
// evidence — is this modal. `wide` is the peek size; the header stays put and
// the body scrolls, so a long record never pushes its own title off-screen.
function openOverlay({title, subtitle = '', content, danger = false, wide = false, dismissible = true, onClose = () => {}, returnFocus = null}) {
  const layer = document.getElementById('portal-layer');
  if (!layer) throw new Error('Admin overlay layer is unavailable');
  const previous = returnFocus || document.activeElement;
  const headingId = `overlay-heading-${crypto.randomUUID()}`;
  const heading = element('h2', {id: headingId, tabindex: '-1', text: title});
  const panel = element('section', {
    class: `modal ${danger ? 'danger-modal' : ''} ${wide ? 'wide' : ''}`,
    role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': headingId, tabindex: '-1',
  }, element('header', {}, element('div', {}, heading, subtitle ? element('p', {text: subtitle}) : null),
  dismissible ? element('button', {class: 'icon-button', type: 'button', 'aria-label': 'Close'}, '×') : null),
  element('div', {class: 'modal-body'}, content));
  const scrim = element('div', {class: 'scrim modal-scrim'}, panel);
  const entry = {panel, heading, previous, scrim, dismissible, closed: false};
  entry.close = ({restore = true} = {}) => {
    if (entry.closed) return;
    entry.closed = true;
    const index = STACK.indexOf(entry);
    if (index >= 0) STACK.splice(index, 1);
    try { content?.dispose?.(); onClose(); }
    finally {
      scrim.remove();
      if (!STACK.length) {
        document.removeEventListener('keydown', keydown);
        if (!BUSY.size) document.body.classList.remove('locked');
      }
      const next = activeEntry();
      if (next) next.panel.removeAttribute('aria-hidden');
      if (restore && previous?.isConnected) previous.focus?.({preventScroll: true});
    }
  };
  panel.querySelector('button[aria-label="Close"]')?.addEventListener('click', () => entry.close());
  scrim.addEventListener('click', (event) => {
    if (dismissible && event.target === scrim && activeEntry() === entry) entry.close();
  });
  const current = activeEntry();
  if (current) current.panel.setAttribute('aria-hidden', 'true');
  STACK.push(entry); layer.append(scrim);
  document.body.classList.add('locked');
  if (STACK.length === 1) document.addEventListener('keydown', keydown);
  requestAnimationFrame(() => {
    // Queried in preference order, not as one selector list: querySelector on
    // a list returns document order, and the header's × button precedes every
    // body control, so a combined query handed initial focus to Close in every
    // dismissible modal. Body controls only — a modal with nothing to fill in
    // reads its own title first.
    const body = ['input', 'select', 'textarea', 'button']
      .map((tag) => `.modal-body ${tag}:not([disabled])`).join(',');
    (panel.querySelector('[autofocus]') || panel.querySelector(body) || heading)
      .focus({preventScroll: true});
  });
  return entry;
}

/**
 * Open the one overlay this portal has. Returns its close function.
 *
 * A caller that has to close the modal from inside its own content declares
 * the binding first and calls it from a handler — handlers run long after the
 * assignment lands.
 */
export function openModal(options) { return openOverlay(options).close; }

export function closeAllOverlays() {
  [...STACK].reverse().forEach((entry) => entry.close({restore: false}));
}

function renderBusy() {
  const layer = document.getElementById('portal-layer');
  if (!layer) return;
  if (!BUSY.size) {
    busyNode?.remove(); busyNode = null;
    document.getElementById('app')?.removeAttribute('inert');
    if (!STACK.length) document.body.classList.remove('locked');
    return;
  }
  const state = [...BUSY.values()].at(-1);
  if (!busyNode) {
    busyNode = element('div', {class: 'busy-scrim'},
      element('section', {class: 'busy-panel', role: 'status', 'aria-live': 'polite', 'aria-busy': 'true'},
        element('span', {class: 'busy-spinner', 'aria-hidden': 'true'}),
        element('strong', {class: 'busy-title'}), element('p', {class: 'busy-detail'}),
        element('progress', {class: 'busy-progress', max: '100'})));
    layer.prepend(busyNode);
  }
  busyNode.querySelector('.busy-title').textContent = state.title;
  busyNode.querySelector('.busy-detail').textContent = state.detail || 'Please wait. This operation is still running.';
  const progress = busyNode.querySelector('.busy-progress');
  if (Number.isFinite(state.progress)) { progress.value = state.progress; progress.hidden = false; }
  else { progress.removeAttribute('value'); progress.hidden = false; }
  document.getElementById('app')?.setAttribute('inert', '');
  document.body.classList.add('locked');
}

export function openBusy({title = 'Working…', detail = '', progress = null} = {}) {
  const token = crypto.randomUUID();
  const state = {title, detail, progress};
  BUSY.set(token, state); renderBusy();
  let closed = false;
  return {
    token,
    update(next = {}) { if (closed || !BUSY.has(token)) return; Object.assign(state, next); renderBusy(); },
    close() { if (closed) return; closed = true; BUSY.delete(token); renderBusy(); },
  };
}

export function clearBusy() {
  BUSY.clear(); renderBusy();
}

export function confirmAction({title, copy, confirmLabel = 'Confirm', danger = false, requireReason = false, reasonLabel = 'Reason'}) {
  return new Promise((resolve) => {
    const reason = element('textarea', {rows: '3', autocomplete: 'off'});
    const message = element('div', {class: 'form-message', role: 'alert'});
    const cancel = element('button', {class: 'button ghost', type: 'button', text: 'Cancel'});
    const confirm = element('button', {class: `button ${danger ? 'danger' : 'primary'}`, type: 'button', text: confirmLabel});
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const content = element('div', {}, element('p', {class: 'modal-copy', text: copy}),
      requireReason ? element('label', {class: 'field'}, element('span', {text: reasonLabel}), reason) : null,
      message, element('div', {class: 'form-actions'}, cancel, confirm));
    const close = openModal({title, content, danger, onClose: () => { if (!settled) { settled = true; resolve({confirmed: false, reason: ''}); } }});
    cancel.addEventListener('click', () => settle({confirmed: false, reason: ''}));
    confirm.addEventListener('click', () => {
      const value = reason.value.trim();
      if (requireReason && !value) { message.textContent = 'A reason is required.'; reason.focus(); return; }
      settle({confirmed: true, reason: value});
    });
  });
}
