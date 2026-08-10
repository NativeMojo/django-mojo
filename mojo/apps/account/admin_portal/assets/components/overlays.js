const STACK = [];
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
  if (event.key === 'Escape') { event.preventDefault(); entry.close(); return; }
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

function openOverlay({kind = 'modal', title, subtitle = '', content, danger = false, wide = false, onClose = () => {}, returnFocus = null}) {
  const layer = document.getElementById('portal-layer');
  if (!layer) throw new Error('Admin overlay layer is unavailable');
  const previous = returnFocus || document.activeElement;
  const headingId = `overlay-heading-${crypto.randomUUID()}`;
  const heading = element('h2', {id: headingId, tabindex: '-1', text: title});
  const panel = element('section', {
    class: `${kind} ${danger ? 'danger-modal' : ''} ${wide ? 'wide' : ''}`,
    role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': headingId, tabindex: '-1',
  }, element('header', {}, element('div', {}, heading, subtitle ? element('p', {text: subtitle}) : null),
  element('button', {class: 'icon-button', type: 'button', 'aria-label': 'Close'}, '×')),
  element('div', {class: `${kind}-body`}, content));
  const scrim = element('div', {class: `scrim ${kind}-scrim`}, panel);
  const entry = {panel, heading, previous, scrim, closed: false};
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
        document.body.classList.remove('locked');
      }
      const next = activeEntry();
      if (next) next.panel.removeAttribute('aria-hidden');
      if (restore && previous?.isConnected) previous.focus?.({preventScroll: true});
    }
  };
  panel.querySelector('button[aria-label="Close"]').addEventListener('click', () => entry.close());
  scrim.addEventListener('click', (event) => { if (event.target === scrim && activeEntry() === entry) entry.close(); });
  const current = activeEntry();
  if (current) current.panel.setAttribute('aria-hidden', 'true');
  STACK.push(entry); layer.append(scrim);
  document.body.classList.add('locked');
  if (STACK.length === 1) document.addEventListener('keydown', keydown);
  requestAnimationFrame(() => {
    if (kind === 'inspector') heading.focus({preventScroll: true});
    else (panel.querySelector('[autofocus],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),button:not([disabled])') || heading).focus({preventScroll: true});
  });
  return entry;
}

export function openModal(options) { return openOverlay(options).close; }

export function openInspector(options) {
  const entry = openOverlay({...options, kind: 'inspector'});
  return {close: entry.close, panel: entry.panel, heading: entry.heading};
}

export function closeAllOverlays() {
  [...STACK].reverse().forEach((entry) => entry.close({restore: false}));
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
