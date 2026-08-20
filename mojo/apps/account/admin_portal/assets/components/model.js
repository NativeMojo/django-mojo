import {badge, h, icon, statusTone} from '../core.js';
import {runAction} from './actions.js';
import {confirmAction} from './overlays.js';

export function allows(capability, context = {}) {
  if (capability == null) return true;
  return typeof capability === 'function' ? Boolean(capability(context)) : Boolean(capability);
}

export function actionMenu({actions = [], context = {}, label = 'Record actions'}) {
  const available = actions.filter((action) => allows(action.capability, context));
  if (!available.length) return null;
  const menu = h('div', {class: 'action-menu-list', role: 'menu', hidden: true}, ...available.map((action) => h('button', {
    type: 'button', role: 'menuitem', class: action.danger ? 'danger-text' : '', text: action.label,
    // The menu is hidden before the action runs, so a pending state on this
    // item would paint inside a hidden container and never be seen. It goes on
    // the ••• trigger, which is the nearest node that survives the action.
    onclick: (event) => {
      event.stopPropagation(); menu.hidden = true; button.setAttribute('aria-expanded', 'false');
      runAction(button, () => action.run?.(context), {announceLabel: `${action.label}…`});
    },
  })));
  const button = h('button', {class: 'icon-button', type: 'button', 'aria-label': label, 'aria-haspopup': 'menu', 'aria-expanded': 'false'}, '•••');
  button.addEventListener('click', (event) => {
    event.stopPropagation(); menu.hidden = !menu.hidden;
    button.setAttribute('aria-expanded', String(!menu.hidden));
    if (!menu.hidden) menu.querySelector('button')?.focus();
  });
  menu.addEventListener('keydown', (event) => {
    const items = [...menu.querySelectorAll('button')]; const index = items.indexOf(document.activeElement);
    if (event.key === 'Escape') { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); button.focus(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
    if (event.key === 'ArrowUp') { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
  });
  return h('div', {class: 'action-menu'}, button, menu);
}

export function lifecycleControl({active, label = 'record', capability = true, onDisable, onReactivate}) {
  if (!allows(capability, {active})) return null;
  const nextActive = !active;
  const button = h('button', {class: `lifecycle-switch ${active ? 'active' : ''}`, type: 'button', role: 'switch', 'aria-checked': String(active),
    'aria-label': `${active ? 'Disable' : 'Reactivate'} ${label}`}, h('span', {class: 'lifecycle-switch-knob'}), h('span', {text: active ? 'Active' : 'Inactive'}));
  // responsiveness-exempt: the await here is confirmAction — a human answering
  // a dialog. Policy is that no affordance paints until they have answered; the
  // work that follows is wrapped in runAction below.
  button.addEventListener('click', async () => {
    const callback = nextActive ? onReactivate : onDisable;
    if (typeof callback !== 'function') return;
    // No affordance while the dialog is open: this await is for a human, and a
    // cancelled confirm must leave the switch exactly as the operator found it.
    const result = await confirmAction({
      title: `${nextActive ? 'Reactivate' : 'Disable'} ${label}?`,
      copy: nextActive ? 'Access is restored through the supplied lifecycle action.' : 'Access is revoked through the supplied lifecycle action.',
      confirmLabel: nextActive ? 'Reactivate' : 'Disable', danger: !nextActive,
      requireReason: true,
    });
    if (!result.confirmed) return;
    // The pending state starts only once the human has answered.
    await runAction(button, () => callback({reason: result.reason}),
      {announceLabel: `${nextActive ? 'Reactivating' : 'Disabling'} ${label}…`});
  });
  return button;
}

export function modelHeader({iconName = 'settings', avatar = '', primary, secondary = '', status = '', warning = '', lifecycle = null, actions = [], context = {}}) {
  const mark = avatar ? h('span', {class: 'model-avatar', text: String(avatar).slice(0, 3).toUpperCase()}) : h('span', {class: 'model-icon'}, icon(iconName));
  return h('header', {class: `model-header ${warning ? 'warning' : ''}`}, mark,
    h('div', {class: 'model-identity'}, h('strong', {text: primary}), secondary ? h('span', {text: secondary}) : null,
      warning ? h('p', {class: 'model-warning', text: warning}) : null),
    status ? badge(status, statusTone(status)) : null,
    lifecycle ? lifecycleControl(lifecycle) : null,
    actionMenu({actions, context}));
}
